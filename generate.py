import json
import fitz # PyMuPDF
import os
import re
import argparse

QUOTE_ONLY_TOKENS = {"\"", "'", "“", "”", "‘", "’", "「", "」", "『", "』"}

def normalize_paragraph_render_text(text):
    text = re.sub(r"\s*\n+\s*", " ", text).strip()
    text = re.sub(r"\s{2,}", " ", text)

    cjk = r"一-龥ぁ-ゔァ-ヴー々〆〤"
    # Remove spaces that only hurt Japanese paragraph wrapping.
    text = re.sub(rf"(?<=[{cjk}])\s+(?=[{cjk}])", "", text)
    text = re.sub(rf"(?<=[{cjk}])\s+(?=[0-9A-Za-z%])", "", text)
    text = re.sub(rf"(?<=[0-9A-Za-z%])\s+(?=[{cjk}])", "", text)
    text = re.sub(r"(?<=\d)\s+(?=%)", "", text)
    return text

def normalize_render_text(text, segment_kind=None):
    lines = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped in QUOTE_ONLY_TOKENS:
            continue
        lines.append(stripped)

    if not lines:
        lines = [text.strip()]

    normalized = "\n".join(lines)
    normalized = re.sub(r"\s{2,}", " ", normalized)

    cjk = r"一-龥ぁ-ゔァ-ヴー々〆〤"
    normalized = re.sub(rf"(?<=[{cjk}])\s+(?=[{cjk}])", "", normalized)
    normalized = re.sub(rf"(?<=[{cjk}])\s+(?=[0-9A-Za-z%])", "", normalized)
    normalized = re.sub(rf"(?<=[0-9A-Za-z%])\s+(?=[{cjk}])", "", normalized)
    normalized = re.sub(r"(?<=\d)\s+(?=%)", "", normalized)

    if segment_kind == "paragraph":
        normalized = normalize_paragraph_render_text(normalized)

    normalized = re.sub(r"[\s\u00A0]*[“”‘’「」『』]+$", "", normalized)

    return normalized


def has_meaningful_vertical_overlap(rect, other, ratio=0.3):
    overlap = min(rect.y1, other.y1) - max(rect.y0, other.y0)
    if overlap <= 0:
        return False
    return overlap >= min(rect.height, other.height) * ratio


def adjust_paragraph_rect(rect, page_rect, peer_rects, self_index):
    # Generic rule for any PDF: respect nearest right neighbor in the same rows.
    right_neighbor_boundary = page_rect.x1 - 24
    has_right_neighbor = False

    for idx, peer in enumerate(peer_rects):
        if idx == self_index:
            continue
        if peer.x0 <= rect.x0 + 20:
            continue
        if not has_meaningful_vertical_overlap(rect, peer):
            continue
        has_right_neighbor = True
        right_neighbor_boundary = min(right_neighbor_boundary, peer.x0 - 10)

    desired_right = min(page_rect.x1 - 24, rect.x0 + max(rect.width * 1.35, rect.width + 120))

    if has_right_neighbor:
        safe_right = min(desired_right, right_neighbor_boundary)
    else:
        safe_right = desired_right

    # If original extracted boxes overlap columns, shrink to keep columns separated.
    if safe_right <= rect.x0 + 20:
        return rect, has_right_neighbor

    adjusted = fitz.Rect(rect.x0, rect.y0, safe_right, rect.y1)
    return adjusted, has_right_neighbor

def generate_translated_pdf(original_pdf, json_path, output_pdf):
    # Path to standard macOS Hiragino Sans font
    font_path = "/System/Library/Fonts/Hiragino Sans GB.ttc"
    if not os.path.exists(font_path):
        # Fallback to universal CJK if Hiragino Sans is missing
        font_path = None
        font_name = "cjk"
        print("Hiragino font not found on system. Falling back to built-in CJK font.")
    else:
        font_name = "hira"
        print(f"Using system Japanese font: {font_path}")
        
    doc = fitz.open(original_pdf)
    
    with open(json_path, "r", encoding="utf-8") as f:
        translated_blocks = json.load(f)
        
    # Group blocks by page number
    blocks_by_page = {}
    for block in translated_blocks:
        page_num = block["page"]
        if page_num not in blocks_by_page:
            blocks_by_page[page_num] = []
        blocks_by_page[page_num].append(block)
        
    # Binary search helper to find the best font size that fits inside the bounding box
    def find_best_font_size(rect, text, original_fs, page_width, page_height, align=0):
        if not font_path:
            f_file = None
            f_name = "cjk"
        else:
            f_file = font_path
            f_name = "hira"
            
        low = 3.0
        high = original_fs
        best_fs = 3.0
        
        if original_fs <= 3.0:
            return original_fs
            
        # Create a scratch document/page to dry-run text insertion
        scratch_doc = fitz.open()
        scratch_page = scratch_doc.new_page(width=page_width, height=page_height)
        
        # 12 iterations are extremely fast and narrow down the size to 0.02 pt accuracy
        for _ in range(12):
            mid = (low + high) / 2
            rc = scratch_page.insert_textbox(rect, text, fontfile=f_file, fontname=f_name, fontsize=mid, align=align)
            if rc >= 0:
                best_fs = mid
                low = mid + 0.05
            else:
                high = mid - 0.05
                
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
        
        # Step 1: Add redaction annotations for all blocks on this page
        # Note: We use fill=False to make the background transparent,
        # which preserves original background designs, colors, lines, and images!
        for block in page_blocks:
            bbox = fitz.Rect(block["bbox"])
            page.add_redact_annot(bbox, fill=False)
            
        # Step 2: Apply redactions (removes original English text, keeps graphics/images)
        # using integer values for flags to ensure robust compatibility
        page.apply_redactions(images=0, graphics=0)
        
        # Step 3: Insert translated Japanese text at the exact same location
        for block_index, block in enumerate(page_blocks):
            bbox = fitz.Rect(block["bbox"])
            align = 0
            translated_text = normalize_render_text(block["text"], segment_kind=block.get("segment_kind"))
            if block.get("segment_kind") == "paragraph":
                bbox, has_right_neighbor = adjust_paragraph_rect(bbox, page_rect, page_rects, block_index)
                # Justify only on wide single-column paragraphs.
                if not has_right_neighbor and bbox.width >= page_width * 0.55:
                    align = 3
            original_fs = block["size"]
            color = tuple(block["color"])
            
            # Find the best fitting font size
            best_fs = find_best_font_size(bbox, translated_text, original_fs, page_width, page_height, align=align)
            
            # Insert the text using system font or cjk fallback
            if font_path:
                page.insert_textbox(bbox, translated_text, fontfile=font_path, fontname="hira", fontsize=best_fs, color=color, align=align)
            else:
                page.insert_textbox(bbox, translated_text, fontname="cjk", fontsize=best_fs, color=color, align=align)
                
        print(f"  Processed page {page_num + 1}/{len(doc)}", flush=True)
        
    # Step 4: Save optimized and compressed PDF
    doc.save(output_pdf, garbage=3, deflate=True)
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
