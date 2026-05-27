import json
import time
import re
import argparse
import math
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from deep_translator import GoogleTranslator

BLOCK_MARKER_TEMPLATE = "__PDFTRANSLATE_BLOCK_{index}__"

def normalize_text(text):
    return re.sub(r"\s+", " ", text).strip()

def is_page_number_or_code(text):
    # If the text is just a number, keep it as is
    if re.match(r'^\d+$', text.strip()):
        return True
    # If the text is empty or just whitespace
    if not text.strip():
        return True
    # If the text is a single character or short symbol, or URL
    if len(text.strip()) <= 1:
        return True
    return False

def is_short_text(text):
    return len(normalize_text(text)) <= 80

def restore_hyphenation(text):
    # example: transla-\ntion -> translation
    return re.sub(r"([A-Za-z])\-\n([A-Za-z])", r"\1\2", text)

def normalize_for_translation(text, preserve_paragraph_break=True):
    text = restore_hyphenation(text)
    text = text.replace("\r\n", "\n")

    if preserve_paragraph_break:
        marker = "__PARA_BREAK__"
        text = re.sub(r"\n\s*\n+", marker, text)
        text = re.sub(r"\n", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        text = text.replace(marker, "\n\n")
    else:
        text = re.sub(r"\n", " ", text)
        text = re.sub(r"\s+", " ", text).strip()

    return text

def looks_like_heading(block):
    text = normalize_text(block["text"])
    if not text:
        return False
    word_count = len(re.findall(r"\b\w+\b", text))
    sentence_like = bool(re.search(r"[。！？!?.,;:—-]", text)) or len(text) > 80 or word_count > 14

    if block.get("size", 0) >= 18 and not sentence_like:
        return True
    if len(text) <= 60 and text == text.upper() and any(ch.isalpha() for ch in text):
        return True
    if re.match(r"^\d+[\.)]\s+", text):
        return True
    return False

def looks_like_toc_line(block):
    text = normalize_text(block["text"])
    if not text:
        return False
    if "contents" in text.lower():
        return True
    if "\t" in block["text"]:
        return True
    if re.match(r"^(\d+|/|[A-Za-z])", text) and len(text) <= 120:
        return True
    return False

def looks_like_list_line(block):
    text = normalize_text(block["text"])
    if not text:
        return False
    return bool(re.match(r"^([\-\u2022]|\d+[\.)])\s+", text))

def classify_block_kind(block):
    if is_page_number_or_code(block["text"]):
        return "noise"
    if looks_like_toc_line(block):
        return "toc"
    if looks_like_list_line(block):
        return "list"
    if looks_like_heading(block):
        return "heading"
    return "paragraph"

def same_column(prev_block, next_block):
    return abs(prev_block.get("x0", prev_block["bbox"][0]) - next_block.get("x0", next_block["bbox"][0])) <= 20

def vertical_gap(prev_block, next_block):
    prev_y1 = prev_block.get("y1", prev_block["bbox"][3])
    next_y0 = next_block.get("y0", next_block["bbox"][1])
    return next_y0 - prev_y1

def font_close(prev_block, next_block):
    return abs(prev_block.get("size", 0) - next_block.get("size", 0)) <= 1.5 and prev_block.get("font") == next_block.get("font")

def prev_ends_mid_sentence(text):
    stripped = text.strip()
    if not stripped:
        return False
    if stripped.endswith((".", "!", "?", ":", ";", "。", "！", "？")):
        return False
    if stripped.endswith((",", "-", "—", "/")):
        return True
    if stripped[-1].islower():
        return True
    return False

def starts_like_continuation(text):
    stripped = text.strip()
    if not stripped:
        return False
    if stripped[0].islower():
        return True
    if re.match(r"^(and|or|but|to|for|with|of|the)\b", stripped, flags=re.IGNORECASE):
        return True
    return False

def should_merge_blocks(prev_block, next_block, prev_kind, next_kind):
    if is_page_number_or_code(prev_block["text"]) or is_page_number_or_code(next_block["text"]):
        return False, "noise"

    if next_block["page"] != prev_block["page"]:
        return False, "cross_page"

    if not same_column(prev_block, next_block):
        return False, "column_change"

    gap = vertical_gap(prev_block, next_block)
    if gap < -2 or gap > max(prev_block.get("size", 12) * 1.6, 24):
        return False, "large_gap"

    if prev_kind == "toc" and next_kind == "toc":
        if is_short_text(prev_block["text"]) and is_short_text(next_block["text"]):
            return True, "toc_short_lines"
        if font_close(prev_block, next_block):
            return True, "toc_font_match"
        return False, "toc_break"

    if prev_kind == "list" and next_kind == "list" and font_close(prev_block, next_block):
        return True, "list_continuation"

    if prev_kind != next_kind:
        return False, "kind_change"

    if next_kind == "heading":
        return False, "heading"

    if next_kind == "paragraph" and font_close(prev_block, next_block):
        if prev_ends_mid_sentence(prev_block["text"]):
            return True, "mid_sentence"
        if starts_like_continuation(next_block["text"]):
            return True, "continuation_start"
        if is_short_text(prev_block["text"]) and is_short_text(next_block["text"]):
            return True, "short_paragraph_lines"

    return False, "default_break"

def build_translation_segments(blocks):
    ordered_blocks = sorted(blocks, key=lambda block: (block["page"], block.get("reading_order", block["block_idx"])))
    segments = []
    current_blocks = []
    current_kind = None
    current_reasons = []

    for block in ordered_blocks:
        block_kind = classify_block_kind(block)

        if not current_blocks:
            current_blocks = [block]
            current_kind = block_kind
            continue

        merge, reason = should_merge_blocks(current_blocks[-1], block, current_kind, block_kind)
        if merge:
            current_blocks.append(block)
            current_reasons.append(reason)
        else:
            segments.append(
                {
                    "kind": current_kind,
                    "blocks": current_blocks,
                    "merge_reasons": current_reasons,
                }
            )
            current_blocks = [block]
            current_kind = block_kind
            current_reasons = []

    if current_blocks:
        segments.append(
            {
                "kind": current_kind,
                "blocks": current_blocks,
                "merge_reasons": current_reasons,
            }
        )

    return segments

def create_segment_payload(segment):
    lines = []
    for index, block in enumerate(segment):
        marker = BLOCK_MARKER_TEMPLATE.format(index=index)
        lines.append(marker)
        lines.append(normalize_for_translation(block["text"], preserve_paragraph_break=False))
    return "\n".join(lines)

def split_translated_segment(translated_text, expected_count):
    segments = {}
    current_index = None
    current_lines = []

    for raw_line in translated_text.splitlines():
        line = raw_line.strip()
        match = re.fullmatch(r"__PDFTRANSLATE_BLOCK_(\d+)__", line)
        if match:
            if current_index is not None:
                segments[current_index] = "\n".join(current_lines).strip()
            current_index = int(match.group(1))
            current_lines = []
        elif current_index is not None:
            current_lines.append(raw_line)

    if current_index is not None:
        segments[current_index] = "\n".join(current_lines).strip()

    if len(segments) != expected_count:
        return None
    if any(not segments.get(index, "").strip() for index in range(expected_count)):
        return None
    return [segments[index] for index in range(expected_count)]

def translate_text(translator, text):
    cleaned_text = text.strip()
    translated_text = None
    for attempt in range(3):
        try:
            translated_text = translator.translate(cleaned_text)
            break
        except Exception:
            time.sleep(0.5)
    return translated_text

def translate_single_block(block):
    text = block["text"]
    if is_page_number_or_code(text):
        translated_text = text
    else:
        translator = GoogleTranslator(source="en", target="ja")
        normalized = normalize_for_translation(text, preserve_paragraph_break=False)
        translated_text = translate_text(translator, normalized)
        if not translated_text:
            translated_text = text

    block_copy = block.copy()
    block_copy["text"] = translated_text
    block_copy["original_text"] = text
    return block_copy

def split_japanese_chunks(text):
    # Split by sentence-ending punctuation while preserving punctuation.
    parts = re.split(r"(?<=[。！？!?])\s+", text.strip())
    chunks = [part.strip() for part in parts if part.strip()]
    if not chunks:
        chunks = [text.strip()]
    return chunks

def estimate_block_capacity(block):
    source_len = len(normalize_text(block.get("text", "")))
    line_count = max(1, int(block.get("line_count", 1)))
    width = max(1.0, float(block.get("width", block["bbox"][2] - block["bbox"][0])))
    size = max(1.0, float(block.get("size", 10.0)))
    visual_capacity = (width / size) * line_count
    return max(8.0, source_len * 0.6 + visual_capacity)

def assign_chunks_to_blocks(chunks, blocks):
    if not chunks:
        return None

    if len(chunks) < len(blocks):
        expanded = []
        for chunk in chunks:
            # Try finer split for long sentence chunks when block count is larger.
            parts = re.split(r"(?<=[、，,])\s+", chunk)
            refined = [part.strip() for part in parts if part.strip()]
            if len(refined) >= 2:
                expanded.extend(refined)
            else:
                expanded.append(chunk)
        chunks = expanded

    if len(chunks) < len(blocks):
        return None

    capacities = [estimate_block_capacity(block) for block in blocks]
    total_capacity = sum(capacities)
    targets = [max(1.0, len(chunks) * (cap / total_capacity)) for cap in capacities]

    assigned = [[] for _ in blocks]
    idx = 0
    for block_index in range(len(blocks)):
        remaining_blocks = len(blocks) - block_index
        remaining_chunks = len(chunks) - idx
        if remaining_chunks <= 0:
            break

        min_take = 1 if remaining_chunks >= remaining_blocks else 0
        max_take = remaining_chunks - max(0, remaining_blocks - 1)
        take = int(math.floor(targets[block_index]))
        take = max(min_take, min(max_take, take))

        if block_index == len(blocks) - 1:
            take = remaining_chunks

        for _ in range(take):
            assigned[block_index].append(chunks[idx])
            idx += 1

    # Safety: distribute leftover chunks to the last block.
    if idx < len(chunks):
        assigned[-1].extend(chunks[idx:])

    results = [" ".join(parts).strip() for parts in assigned]
    if any(not text for text in results):
        return None
    return results

def translate_paragraph_segment(segment, translator):
    blocks = segment["blocks"]
    if len(blocks) == 1:
        translated_block = translate_single_block(blocks[0])
        return [translated_block], "single_paragraph"

    joined = "\n\n".join(normalize_for_translation(block["text"], preserve_paragraph_break=True) for block in blocks)
    translated = translate_text(translator, joined)
    if not translated:
        return None, "paragraph_translate_failed"

    chunks = split_japanese_chunks(translated)
    reassigned = assign_chunks_to_blocks(chunks, blocks)
    if reassigned is None:
        return None, "paragraph_reassign_failed"

    translated_blocks = []
    for block, text in zip(blocks, reassigned):
        block_copy = block.copy()
        block_copy["text"] = text
        block_copy["original_text"] = block["text"]
        translated_blocks.append(block_copy)
    return translated_blocks, "paragraph_reassigned"

def translate_structured_segment(segment, translator):
    blocks = segment["blocks"]
    payload = create_segment_payload(blocks)
    translated_payload = translate_text(translator, payload)
    if translated_payload:
        split_result = split_translated_segment(translated_payload, len(blocks))
        if split_result is not None:
            translated_blocks = []
            for block, translated_text in zip(blocks, split_result):
                block_copy = block.copy()
                block_copy["text"] = translated_text
                block_copy["original_text"] = block["text"]
                translated_blocks.append(block_copy)
            return translated_blocks, "structured_split_ok"

    return None, "structured_split_failed"

def translate_segment(segment, index, total):
    blocks = segment["blocks"]
    kind = segment["kind"]

    if len(blocks) == 1 and kind != "paragraph":
        translated_block = translate_single_block(blocks[0])
        return index, [translated_block], {
            "kind": kind,
            "strategy": "single_block",
            "result": "single_block_direct",
            "block_refs": [[blocks[0]["page"], blocks[0]["block_idx"]]],
            "merge_reasons": segment.get("merge_reasons", []),
            "source_preview": normalize_text(blocks[0]["text"])[:180],
        }

    translator = GoogleTranslator(source="en", target="ja")

    strategy = "structured"
    translated_blocks = None
    result_note = ""

    if kind == "paragraph":
        strategy = "paragraph_reflow"
        translated_blocks, result_note = translate_paragraph_segment(segment, translator)
    else:
        translated_blocks, result_note = translate_structured_segment(segment, translator)

    if translated_blocks is None:
        strategy = "block_fallback"
        translated_blocks = [translate_single_block(block) for block in blocks]
        result_note = f"{result_note}|fallback"

    for block_copy in translated_blocks:
        block_copy["segment_kind"] = kind

    return index, translated_blocks, {
        "kind": kind,
        "strategy": strategy,
        "result": result_note,
        "block_refs": [[block["page"], block["block_idx"]] for block in blocks],
        "merge_reasons": segment.get("merge_reasons", []),
        "source_preview": normalize_text(" ".join(block["text"] for block in blocks))[:180],
    }

def translate_blocks(input_json, output_json, debug_json_path=None):
    with open(input_json, "r", encoding="utf-8") as f:
        data = json.load(f)

    translation_segments = build_translation_segments(data)
    print(
        f"Starting context-aware translation of {len(data)} blocks in {len(translation_segments)} segments...",
        flush=True,
    )

    translated_count = 0

    segment_slots = [None] * len(translation_segments)
    segment_debug = [None] * len(translation_segments)
    max_workers = 8
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(translate_segment, segment, idx, len(translation_segments)): idx
            for idx, segment in enumerate(translation_segments)
        }

        for future in as_completed(futures):
            idx, translated_segment, debug_info = future.result()
            segment_slots[idx] = translated_segment
            segment_debug[idx] = debug_info

            processed_segments = sum(1 for segment in segment_slots if segment is not None)
            if processed_segments % 10 == 0 or processed_segments == len(translation_segments):
                print(
                    f"Processed {processed_segments}/{len(translation_segments)} segments...",
                    flush=True,
                )

    translated_blocks = []
    for translated_segment in segment_slots:
        translated_blocks.extend(translated_segment)

    results_by_key = {
        (block["page"], block["block_idx"]): block
        for block in translated_blocks
    }
    results = []
    for original_block in data:
        block_copy = results_by_key[(original_block["page"], original_block["block_idx"])]
        results.append(block_copy)
        if block_copy["text"] != block_copy["original_text"] and not is_page_number_or_code(block_copy["original_text"]):
            translated_count += 1

    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    if debug_json_path:
        with open(debug_json_path, "w", encoding="utf-8") as f:
            json.dump(segment_debug, f, ensure_ascii=False, indent=2)

    print(f"Completed translation! Saved to {output_json}. Total translated: {translated_count}", flush=True)

def build_default_output_pdf(source_pdf):
    source_path = Path(source_pdf)
    return str(source_path.with_name(f"{source_path.stem}_JA.pdf"))

def run_pipeline(source_pdf, output_pdf=None):
    from extract import extract_pdf_text
    from generate import generate_translated_pdf

    source_path = Path(source_pdf)
    if not source_path.exists():
        raise FileNotFoundError(f"Source PDF not found: {source_pdf}")

    output_pdf = output_pdf or build_default_output_pdf(source_pdf)
    extracted_json = str(source_path.with_name(f"{source_path.stem}_extracted_text.json"))
    translated_json = str(source_path.with_name(f"{source_path.stem}_translated_text.json"))
    segment_debug_json = str(source_path.with_name(f"{source_path.stem}_segment_debug.json"))

    print(f"[1/4] Extracting text blocks from: {source_pdf}", flush=True)
    extract_pdf_text(str(source_path), extracted_json)

    print(f"[2/4] Translating blocks to Japanese", flush=True)
    translate_blocks(extracted_json, translated_json, debug_json_path=segment_debug_json)

    print(f"[3/3] Generating translated PDF: {output_pdf}", flush=True)
    generate_translated_pdf(str(source_path), translated_json, output_pdf)

    print("Done.", flush=True)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Translate an English PDF into a Japanese PDF while preserving layout."
    )
    parser.add_argument("source_pdf", help="Path to source English PDF")
    parser.add_argument(
        "output_pdf",
        nargs="?",
        help="Path to output Japanese PDF (default: <source_stem>_JA.pdf)",
    )
    args = parser.parse_args()

    run_pipeline(args.source_pdf, args.output_pdf)
