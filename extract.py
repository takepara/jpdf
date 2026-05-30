import json
import fitz # PyMuPDF
import argparse


def _line_rep_span(line):
    spans = line.get("spans", [])
    if not spans:
        return None
    return next((s for s in spans if (s.get("text", "") or "").strip()), spans[0])


def _paragraph_groups_from_lines(lines):
    def is_bullet_marker(text):
        stripped = (text or "").strip()
        if not stripped:
            return False
        return stripped in {"•", "-", "*", "・"}

    def ends_sentence(text):
        stripped = (text or "").strip()
        if not stripped:
            return False
        return stripped.endswith((".", "!", "?", ":", ";", "。", "！", "？"))

    groups = []
    current = []
    current_min_x0 = None

    for line in lines:
        spans = line.get("spans", [])
        if not spans:
            continue

        line_text = "".join(span.get("text", "") for span in spans)
        if not line_text.strip():
            if current:
                groups.append(current)
                current = []
                current_min_x0 = None
            continue

        lx0, _, _, _ = line.get("bbox", (0.0, 0.0, 0.0, 0.0))

        if current:
            prev_line = current[-1]
            prev_text = "".join(span.get("text", "") for span in prev_line.get("spans", []))
            prev_x0, _, _, _ = prev_line.get("bbox", (lx0, 0.0, 0.0, 0.0))

            indent_jump_from_prev = (lx0 - prev_x0) >= 20
            indent_jump_from_group = current_min_x0 is not None and (lx0 - current_min_x0) >= 20
            should_split_for_indent = (
                (indent_jump_from_prev or indent_jump_from_group)
                and ends_sentence(prev_text)
                and not is_bullet_marker(prev_text)
                and not is_bullet_marker(line_text)
            )

            if should_split_for_indent:
                groups.append(current)
                current = []
                current_min_x0 = None

        current.append(line)
        current_min_x0 = lx0 if current_min_x0 is None else min(current_min_x0, lx0)

    if current:
        groups.append(current)

    return groups

def extract_pdf_text(pdf_path, json_path):
    doc = fitz.open(pdf_path)
    extracted_data = []

    for page_num in range(len(doc)):
        page = doc[page_num]
        blocks = page.get_text("dict")["blocks"]
        reading_order = 0
        
        for block in blocks:
            if block.get("type") != 0: # Skip image/non-text blocks
                continue
            
            lines = block.get("lines", [])
            if not lines:
                continue

            for group in _paragraph_groups_from_lines(lines):
                text_parts = []
                x0 = float("inf")
                y0 = float("inf")
                x1 = float("-inf")
                y1 = float("-inf")
                rep_span = None

                for line in group:
                    spans = line.get("spans", [])
                    line_text = "".join(span.get("text", "") for span in spans)
                    if line_text.strip():
                        text_parts.append(line_text)

                    lx0, ly0, lx1, ly1 = line.get("bbox", block["bbox"])
                    x0 = min(x0, lx0)
                    y0 = min(y0, ly0)
                    x1 = max(x1, lx1)
                    y1 = max(y1, ly1)

                    if rep_span is None:
                        rep_span = _line_rep_span(line)

                full_text = "\n".join(text_parts)
                if not full_text.strip():
                    continue

                if rep_span is not None:
                    font_name = rep_span.get("font", "helv")
                    font_size = rep_span.get("size", 10.0)
                    color_int = rep_span.get("color", 0)
                    color_rgb = fitz.sRGB_to_pdf(color_int)
                else:
                    font_name = "helv"
                    font_size = 10.0
                    color_rgb = (0.0, 0.0, 0.0)

                bbox = [x0, y0, x1, y1]
                extracted_data.append({
                    "page": page_num,
                    "block_idx": reading_order,
                    "bbox": bbox,
                    "text": full_text,
                    "size": font_size,
                    "color": color_rgb,
                    "font": font_name,
                    "x0": x0,
                    "y0": y0,
                    "x1": x1,
                    "y1": y1,
                    "width": x1 - x0,
                    "height": y1 - y0,
                    "line_count": len(text_parts),
                    "reading_order": reading_order
                })
                reading_order += 1
            
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(extracted_data, f, ensure_ascii=False, indent=2)
        
    print(f"Extracted {len(extracted_data)} text blocks to {json_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract text blocks from a PDF into JSON.")
    parser.add_argument("pdf_path", help="Path to source PDF")
    parser.add_argument("json_path", help="Path to output JSON")
    args = parser.parse_args()

    extract_pdf_text(args.pdf_path, args.json_path)
