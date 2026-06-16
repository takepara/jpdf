import json
import fitz  # PyMuPDF: PDF の描画・編集ライブラリ
import os
import re
import argparse
import math

# 引用記号のみのテキスト行を除外するためのセット。
# 日本語の「」や『』などの引用記号のみで構成される行は本文として扱わない。
QUOTE_ONLY_TOKENS = {"\"", "'", "“", "”", "‘", "’", "「", "」", "『", "』"}

# 段落・箇条書きの描画矩形に追加する縦余白の係数と上下限。
PARAGRAPH_VERTICAL_PADDING_RATIO = 0.16
PARAGRAPH_VERTICAL_PADDING_MIN = 3.0
PARAGRAPH_VERTICAL_PADDING_MAX = 14.0
LIST_VERTICAL_PADDING_RATIO = 0.10
LIST_VERTICAL_PADDING_MIN = 2.0
LIST_VERTICAL_PADDING_MAX = 8.0

# 右隣ブロック判定や段組み分離に使うレイアウト閾値。
DEFAULT_VERTICAL_OVERLAP_RATIO = 0.3
DECORATIVE_MAX_TEXT_LENGTH = 12
DECORATIVE_PAGE_AREA_RATIO = 0.20
DECORATIVE_TALL_TEXT_LENGTH = 8
DECORATIVE_PAGE_HEIGHT_RATIO = 0.45
MIN_COLUMN_GAP_FROM_SELF = 20.0
RIGHT_NEIGHBOR_PADDING = 10.0
MIN_MIRRORED_RIGHT_MARGIN = 24.0
PARAGRAPH_WIDTH_EXPANSION_RATIO = 1.35
PARAGRAPH_WIDTH_EXPANSION_ABSOLUTE = 120.0
MIN_ADJUSTED_RECT_WIDTH = 20.0
MIN_RENDER_RECT_HEIGHT = 4.0

# フォントサイズ探索の下限と探索精度。
MIN_FONT_SIZE = 3.0
FONT_SIZE_HEADROOM_RATIO = 1.05
FONT_SIZE_BINARY_SEARCH_STEPS = 12
FONT_SIZE_BINARY_SEARCH_DELTA = 0.05

# PyMuPDF の整列指定値と保存最適化設定。
TEXT_ALIGN_LEFT = 0
TEXT_ALIGN_JUSTIFY = 3
REDACTION_KEEP_IMAGES = 0
REDACTION_KEEP_GRAPHICS = 0
PDF_SAVE_GARBAGE_LEVEL = 3
SINGLE_COLUMN_JUSTIFY_WIDTH_RATIO = 0.45

# 本文 paragraph を描画時に揃えるための判定・クラスタリング閾値。
BODY_PARAGRAPH_MIN_LINE_COUNT = 2
BODY_PARAGRAPH_MIN_TEXT_LENGTH = 40
BODY_PARAGRAPH_MIN_WIDTH_RATIO = 0.18
BODY_PARAGRAPH_MAX_SIZE_RATIO = 1.35
COLUMN_X_TOLERANCE = 36.0
COLUMN_WIDTH_RATIO_TOLERANCE = 0.35
PARAGRAPH_TARGET_PERCENTILE = 0.25


def normalize_list_render_text(text):
    """箇条書きのテキストを正規化する。

    箇条書きマーカー（•, ・, ●, 番号など）を保持しつつ、
    マーカー間の空白を整理して「1項目 = 1行」の形式に整える。
    """
    text = re.sub(r"\s*\n+\s*", " ", text).strip()
    text = re.sub(r"\s{2,}", " ", text)

    # 箇条書きマーカー（記号または番号）を正規表現で定義。
    bullet_chars = r"[•・●◦▪■□◆◇▶▸▹▻]"
    numbered_markers = r"(?:\([1-9]\d?\)|(?<!\d)[1-9]\d?[.)](?!\d)|[①-⑳])"
    marker_pattern = rf"(?:{bullet_chars}|{numbered_markers})"

    # マーカーの前後に空白を挿入し、分割しやすい状態にする。
    text = re.sub(rf"\s*({marker_pattern})\s*", r" \1 ", text)
    # マーカーの手前で分割（マーカーは残す）。
    parts = re.split(rf"\s+(?={marker_pattern}\s+)", text)

    normalized_lines = []
    for part in parts:
        item = part.strip()
        if not item:
            continue

        # マーカーと本文が離れている場合は、1行にまとめる。
        item = re.sub(rf"^({marker_pattern})\s*", r"\1 ", item)
        item = re.sub(r"\s{2,}", " ", item)
        normalized_lines.append(item)

    if normalized_lines:
        return "\n".join(normalized_lines)
    return text

def normalize_paragraph_render_text(text):
    text = re.sub(r"\s*\n+\s*", " ", text).strip()
    text = re.sub(r"\s{2,}", " ", text)

    cjk = r"一-龥ぁ-ゔァ-ヴー々〆〤"
    # 日本語の前後にある空白を削除（可読性を損なうため）。
    text = re.sub(rf"(?<=[{cjk}])\s+(?=[{cjk}])", "", text)
    # 日本語と数字の間の空白も整理。
    text = re.sub(rf"(?<=[{cjk}])\s+(?=[0-9A-Za-z%])", "", text)
    # 数字と日本語の間の空白も整理。
    text = re.sub(rf"(?<=[0-9A-Za-z%])\s+(?=[{cjk}])", "", text)
    # 数字と % の間の空白も削除。
    text = re.sub(r"(?<=\d)\s+(?=%)", "", text)
    return text

def normalize_render_text(text, segment_kind=None):
    """テキストを正規化する（segment_kind に応じて処理を切り替える）。

    segment_kind が "list" の場合は箇条書き用、"paragraph" の場合は
    段落用の正規化を適用する。それ以外の場合は標準的な処理を行う。
    """
    lines = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        # 引用記号のみの行は除外する。
        if stripped in QUOTE_ONLY_TOKENS:
            continue
        lines.append(stripped)

    if not lines:
        lines = [text.strip()]

    normalized = "\n".join(lines)
    normalized = re.sub(r"\s{2,}", " ", normalized)

    # 日本語文字範囲。
    cjk = r"一-龥ぁ-ゔァ-ヴー々〆〤"
    normalized = re.sub(rf"(?<=[{cjk}])\s+(?=[{cjk}])", "", normalized)
    # 日本語と数字の間の空白も整理。
    normalized = re.sub(rf"(?<=[{cjk}])\s+(?=[0-9A-Za-z%])", "", normalized)
    # 数字と日本語の間の空白も整理。
    normalized = re.sub(rf"(?<=[0-9A-Za-z%])\s+(?=[{cjk}])", "", normalized)
    # 数字と % の間の空白も削除。
    normalized = re.sub(r"(?<=\d)\s+(?=%)", "", normalized)

    if segment_kind == "list":
        normalized = normalize_list_render_text(normalized)
    elif segment_kind == "paragraph":
        normalized = normalize_paragraph_render_text(normalized)

    # 末尾の引用記号を削除。
    normalized = re.sub(r"[\s\u00A0]*[“”‘’「」『』]+$", "", normalized)

    return normalized


def has_meaningful_vertical_overlap(rect, other, ratio=DEFAULT_VERTICAL_OVERLAP_RATIO):
    """2つの矩形が垂直方向に意味のある重複を持っているか判定する。

    重複率が ratio（デフォルト0.3）以上であれば、同じ行の高さ范围内にあるとみなす。
    段落の右隣判定に使用する。
    """
    overlap = min(rect.y1, other.y1) - max(rect.y0, other.y0)
    if overlap <= 0:
        return False
    return overlap >= min(rect.height, other.height) * ratio


def is_decorative_overlay_block(block, rect, page_rect):
    """装飾的なオーバーレイ要素（背景文字など）を判定する。

    大きな表示用文字は多くの行と重なるが、段落の右隣として扱うべきではない。
    文字数が少なく面積が大きい場合、または高さがページ全体の45%以上の場合に
    装飾要素とみなす。
    """
    text_len = len(normalize_render_text(block.get("text", ""), segment_kind=block.get("segment_kind", "")).strip())
    area = rect.width * rect.height
    page_area = max(1.0, page_rect.width * page_rect.height)
    if text_len <= DECORATIVE_MAX_TEXT_LENGTH and area >= page_area * DECORATIVE_PAGE_AREA_RATIO:
        return True
    if text_len <= DECORATIVE_TALL_TEXT_LENGTH and rect.height >= page_rect.height * DECORATIVE_PAGE_HEIGHT_RATIO:
        return True
    return False


def adjust_paragraph_rect(rect, page_rect, peer_rects, peer_blocks, self_index):
    """段落の矩形を調整する（右隣がある場合は幅を狭める）。

    段落ボックスの右側に隣接する要素があれば、その位置まで幅を狭める。
    装飾要素や垂直方向の重複がない場合は元の矩形を維持する。
    これにより、2段組レイアウトでも正しくテキストが配置される。
    """
    # 左余白の計算。
    left_margin = max(0.0, rect.x0 - page_rect.x0)
    # 右余白は左余白に合わせる（行が広がりすぎないように）。
    mirrored_right_margin = max(MIN_MIRRORED_RIGHT_MARGIN, left_margin)
    right_neighbor_boundary = page_rect.x1 - mirrored_right_margin
    has_right_neighbor = False

    for idx, peer in enumerate(peer_rects):
        if idx == self_index:
            continue
        # 左端が近い場合は無視。
        if peer.x0 <= rect.x0 + MIN_COLUMN_GAP_FROM_SELF:
            continue
        # 装飾要素の場合は無視。
        if is_decorative_overlay_block(peer_blocks[idx], peer, page_rect):
            continue
        # 垂直方向の重複がない場合は無視。
        if not has_meaningful_vertical_overlap(rect, peer):
            continue
        has_right_neighbor = True
        right_neighbor_boundary = min(right_neighbor_boundary, peer.x0 - RIGHT_NEIGHBOR_PADDING)

    desired_right = min(
        page_rect.x1 - mirrored_right_margin,
        rect.x0 + max(rect.width * PARAGRAPH_WIDTH_EXPANSION_RATIO, rect.width + PARAGRAPH_WIDTH_EXPANSION_ABSOLUTE),
    )

    if has_right_neighbor:
        safe_right = min(desired_right, right_neighbor_boundary)
    else:
        safe_right = desired_right

    # 元の抽出ボックスが列をまたいでいる場合は、幅を狭めて列を分離する。
    if safe_right <= rect.x0 + MIN_ADJUSTED_RECT_WIDTH:
        return rect, has_right_neighbor

    adjusted = fitz.Rect(rect.x0, rect.y0, safe_right, rect.y1)
    return adjusted, has_right_neighbor


def expand_text_rect_for_readability(rect, page_rect, segment_kind, font_size):
    """読みやすさを考慮してテキストの描画矩形を拡張する。

    段落とリストについては、フォントサイズに応じた縦方向の余白を追加する。
    これにより、同じフォントサイズでも行間が広く見えるようになる。
    拡張後の矩形はフォントサイズ探索と実際の描画の両方に使用される。
    """
    if segment_kind == "paragraph":
        pad = max(
            PARAGRAPH_VERTICAL_PADDING_MIN,
            min(PARAGRAPH_VERTICAL_PADDING_MAX, float(font_size) * PARAGRAPH_VERTICAL_PADDING_RATIO),
        )
    elif segment_kind == "list":
        pad = max(
            LIST_VERTICAL_PADDING_MIN,
            min(LIST_VERTICAL_PADDING_MAX, float(font_size) * LIST_VERTICAL_PADDING_RATIO),
        )
    else:
        return rect

    top = max(page_rect.y0, rect.y0 - pad)
    bottom = min(page_rect.y1, rect.y1 + pad)
    if bottom <= top + MIN_RENDER_RECT_HEIGHT:
        return rect
    return fitz.Rect(rect.x0, top, rect.x1, bottom)


def is_body_paragraph_block(block, page_rect):
    """本文としてサイズ統一対象にする paragraph かを判定する。"""
    if block.get("segment_kind") != "paragraph":
        return False

    line_count = max(1, int(block.get("line_count", 1)))
    if line_count < BODY_PARAGRAPH_MIN_LINE_COUNT:
        return False

    normalized_text = normalize_render_text(block.get("text", ""), segment_kind="paragraph")
    if len(normalized_text) < BODY_PARAGRAPH_MIN_TEXT_LENGTH:
        return False

    page_width = max(1.0, page_rect.width)
    block_width = max(0.0, float(block.get("width", block["bbox"][2] - block["bbox"][0])))
    if block_width < page_width * BODY_PARAGRAPH_MIN_WIDTH_RATIO:
        return False

    size = max(MIN_FONT_SIZE, float(block.get("size", MIN_FONT_SIZE)))
    if size > DEFAULT_FONT_SIZE_FALLBACK(page_rect):
        return False

    return True


def DEFAULT_FONT_SIZE_FALLBACK(page_rect):
    """本文候補から除外する極端に大きい文字サイズの上限を返す。"""
    return max(18.0, page_rect.height * 0.03)


def group_body_paragraphs_by_column(page_blocks, page_rect):
    """本文 paragraph を列ごとにグループ化し、block index の配列を返す。"""
    candidate_indexes = [
        index for index, block in enumerate(page_blocks) if is_body_paragraph_block(block, page_rect)
    ]
    if not candidate_indexes:
        return []

    groups = []
    for index in sorted(candidate_indexes, key=lambda idx: (page_blocks[idx].get("x0", page_blocks[idx]["bbox"][0]), idx)):
        block = page_blocks[index]
        block_x0 = float(block.get("x0", block["bbox"][0]))
        block_width = max(1.0, float(block.get("width", block["bbox"][2] - block["bbox"][0])))
        assigned = False

        for group in groups:
            anchor = page_blocks[group[0]]
            anchor_x0 = float(anchor.get("x0", anchor["bbox"][0]))
            anchor_width = max(1.0, float(anchor.get("width", anchor["bbox"][2] - anchor["bbox"][0])))
            width_ratio_gap = abs(block_width - anchor_width) / max(block_width, anchor_width)
            if abs(block_x0 - anchor_x0) <= COLUMN_X_TOLERANCE and width_ratio_gap <= COLUMN_WIDTH_RATIO_TOLERANCE:
                group.append(index)
                assigned = True
                break

        if not assigned:
            groups.append([index])

    return groups


def percentile_value(values, percentile):
    """昇順値列から指定パーセンタイルの値を返す。"""
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = max(0.0, min(1.0, percentile)) * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def compute_column_target_font_sizes(page_blocks, column_groups):
    """列ごとの本文基準フォントサイズを block index -> size で返す。"""
    target_sizes = {}
    for group in column_groups:
        sizes = [float(page_blocks[index].get("size", MIN_FONT_SIZE)) for index in group]
        target_size = percentile_value(sizes, PARAGRAPH_TARGET_PERCENTILE)
        if target_size is None:
            continue
        for index in group:
            target_sizes[index] = target_size
    return target_sizes

def generate_translated_pdf(original_pdf, json_path, output_pdf):
    """日本語PDFを生成するメイン処理。

    1. フォントの選択（macOSの場合はHiragino Sans GB.ttcを優先）
    2. JSONからページごとにブロックをグループ化
    3. 各ページのテキストボックスを赤文字で消去（背景は保持）
    4. 日本語テキストを元の位置に挿入（フォントサイズを最適化）
    5. PDFを圧縮して保存

    README と整合性を取りつつ、レイアウトをできるだけ維持する。
    """
    # macOSの標準フォント（Hiragino Sans GB.ttc）を使用。
    font_path = "/System/Library/Fonts/Hiragino Sans GB.ttc"
    if not os.path.exists(font_path):
        # フォントがない場合はシステムCJKフォントにフォールバック。
        font_name = "cjk"
        print("Hiragino font not found on system. Falling back to built-in CJK font.")
    else:
        font_name = "hira"
        print(f"Using system Japanese font: {font_path}")
        
    doc = fitz.open(original_pdf)
    
    with open(json_path, "r", encoding="utf-8") as f:
        translated_blocks = json.load(f)
        
    # ページ番号ごとにブロックをグループ化。
    blocks_by_page = {}
    for block in translated_blocks:
        page_num = block["page"]
        if page_num not in blocks_by_page:
            blocks_by_page[page_num] = []
        blocks_by_page[page_num].append(block)

    # バイナリサーチで bounding box に収まる最適なフォントサイズを見つける。
    def find_best_font_size(rect, text, original_fs, page_width, page_height, align=0):
        if not font_path:
            f_file = None
            f_name = "cjk"
        else:
            f_file = font_path
            f_name = "hira"

        # 3pt から元のフォントサイズまで全範囲を許容し、必ずテキストが収まるようにする。
        low = MIN_FONT_SIZE
        high = original_fs * FONT_SIZE_HEADROOM_RATIO
        best_fs = MIN_FONT_SIZE

        if original_fs <= MIN_FONT_SIZE:
            return original_fs

        # 仮のドキュメント/ページを作成してテキスト挿入をシミュレート。
        scratch_doc = fitz.open()
        scratch_page = scratch_doc.new_page(width=page_width, height=page_height)

        # 固定回数の二分探索で、収まる最大フォントサイズを安定して求める。
        for _ in range(FONT_SIZE_BINARY_SEARCH_STEPS):
            mid = (low + high) / 2
            rc = scratch_page.insert_textbox(rect, text, fontfile=f_file, fontname=f_name, fontsize=mid, align=align)
            if rc >= 0:
                best_fs = mid
                low = mid + FONT_SIZE_BINARY_SEARCH_DELTA
            else:
                high = mid - FONT_SIZE_BINARY_SEARCH_DELTA

        scratch_doc.close()
        return best_fs

    print("Generating translated PDF...", flush=True)

    for page_num in sorted(blocks_by_page.keys()):
        if page_num >= len(doc):
            continue

        page = doc[page_num]
        page_rect = page.rect
        page_width = page_rect.width
        page_height = page_rect.height

        page_blocks = blocks_by_page[page_num]
        page_rects = [fitz.Rect(block["bbox"]) for block in page_blocks]
        paragraph_column_groups = group_body_paragraphs_by_column(page_blocks, page_rect)
        paragraph_target_sizes = compute_column_target_font_sizes(page_blocks, paragraph_column_groups)

        # Step 1: ページ内の全ブロックに赤文字注釈を追加。
        # fill=False を使用して背景を透明にし、
        # 元のデザイン、色、線、画像を保持する。
        for block in page_blocks:
            bbox = fitz.Rect(block["bbox"])
            page.add_redact_annot(bbox, fill=False)

        # Step 2: 赤文字注釈を適用（英語テキストを消去、図形/画像は保持）。
        # 互換性のために整数値のフラグを使用。
        page.apply_redactions(images=REDACTION_KEEP_IMAGES, graphics=REDACTION_KEEP_GRAPHICS)

        # Step 3: 日本語テキストを元の位置に挿入。
        for block_index, block in enumerate(page_blocks):
            bbox = fitz.Rect(block["bbox"])
            align = TEXT_ALIGN_LEFT
            segment_kind = block.get("segment_kind")
            translated_text = normalize_render_text(block["text"], segment_kind=segment_kind)
            if segment_kind == "paragraph":
                bbox, has_right_neighbor = adjust_paragraph_rect(bbox, page_rect, page_rects, page_blocks, block_index)
                # 広い単一列段落のみ全角揃えを適用。
                if not has_right_neighbor and bbox.width >= page_width * SINGLE_COLUMN_JUSTIFY_WIDTH_RATIO:
                    align = TEXT_ALIGN_JUSTIFY
            original_fs = block["size"]
            target_fs = min(float(original_fs), float(paragraph_target_sizes.get(block_index, original_fs)))
            color = tuple(block["color"])
            # 読みやすさ用の描画矩形を計算。
            render_bbox = expand_text_rect_for_readability(bbox, page_rect, segment_kind, target_fs)

            # 最適なフォントサイズを見つける。
            best_fs = find_best_font_size(render_bbox, translated_text, target_fs, page_width, page_height, align=align)

            # システムフォントまたはCJKフォールバックでテキストを挿入。
            if font_path:
                page.insert_textbox(render_bbox, translated_text, fontfile=font_path, fontname="hira", fontsize=best_fs, color=color, align=align)
            else:
                page.insert_textbox(render_bbox, translated_text, fontname="cjk", fontsize=best_fs, color=color, align=align)

        print(f"  Processed page {page_num + 1}/{len(doc)}", flush=True)

    # Step 4: 最適化・圧縮してPDFを保存。
    doc.save(output_pdf, garbage=PDF_SAVE_GARBAGE_LEVEL, deflate=True)
    doc.close()
    print(f"Successfully generated layout-preserved translated PDF: {output_pdf}", flush=True)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate a translated PDF from source PDF and translated JSON blocks."
    )
    parser.add_argument("original_pdf", help="Path to source PDF")
    parser.add_argument("json_path", help="Path to translated JSON")
    parser.add_argument("output_pdf", help="Path to output translated PDF")
    args = parser.parse_args()

    generate_translated_pdf(args.original_pdf, args.json_path, args.output_pdf)
