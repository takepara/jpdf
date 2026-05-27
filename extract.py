import json
import fitz # PyMuPDF
import argparse

def extract_pdf_text(pdf_path, json_path):
    doc = fitz.open(pdf_path)
    extracted_data = []

    for page_num in range(len(doc)):
        page = doc[page_num]
        blocks = page.get_text("dict")["blocks"]
        reading_order = 0
        
        for block_idx, block in enumerate(blocks):
            if block.get("type") != 0: # Skip image/non-text blocks
                continue
            
            lines = block.get("lines", [])
            if not lines:
                continue
                
            # Collect text and analyze properties
            spans_info = []
            text_parts = []
            
            for line in lines:
                line_text = ""
                for span in line.get("spans", []):
                    text = span.get("text", "")
                    line_text += text
                    spans_info.append(span)
                # Keep line parts intact
                if line_text:
                    text_parts.append(line_text)
            
            # Join lines in a block with a space (standard paragraph reflow)
            # but preserve tabs if present
            full_text = "\n".join(text_parts)
            
            # Strip outer whitespace and check if empty
            if not full_text.strip():
                continue
                
            # Find representative font properties from the spans
            if spans_info:
                # Use the first span with non-empty text as representative
                rep_span = next((s for s in spans_info if s.get("text", "").strip()), spans_info[0])
                font_name = rep_span.get("font", "helv")
                font_size = rep_span.get("size", 10.0)
                color_int = rep_span.get("color", 0)
                # Convert color integer to PDF RGB float tuple
                color_rgb = fitz.sRGB_to_pdf(color_int)
            else:
                font_name = "helv"
                font_size = 10.0
                color_rgb = (0.0, 0.0, 0.0)

            x0, y0, x1, y1 = block["bbox"]
                
            extracted_data.append({
                "page": page_num,
                "block_idx": block_idx,
                "bbox": block["bbox"],
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
