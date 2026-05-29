import argparse
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

BLOCK_MARKER_TEMPLATE = "__PDFTRANSLATE_BLOCK_{index}__"
LMSTUDIO_DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"
GOOGLE_TRANSLATE_DEFAULT_URL = "https://translate.googleapis.com/translate_a/single"


def load_dotenv_file(env_path):
    if not env_path.exists() or not env_path.is_file():
        return 0

    loaded = 0
    with open(env_path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue

            if line.startswith("export "):
                line = line[len("export ") :].strip()

            if "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()

            if not key:
                continue

            if len(value) >= 2 and (
                (value[0] == '"' and value[-1] == '"')
                or (value[0] == "'" and value[-1] == "'")
            ):
                value = value[1:-1]

            if key not in os.environ:
                os.environ[key] = value
                loaded += 1

    return loaded


def load_dotenv_candidates(source_pdf_path=None, explicit_env_file=None):
    candidates = []
    if explicit_env_file:
        candidates.append(Path(explicit_env_file).expanduser())

    cwd_env = Path.cwd() / ".env"
    if cwd_env not in candidates:
        candidates.append(cwd_env)

    script_env = Path(__file__).resolve().parent / ".env"
    if script_env not in candidates:
        candidates.append(script_env)

    if source_pdf_path:
        source_env = Path(source_pdf_path).resolve().parent / ".env"
        if source_env not in candidates:
            candidates.append(source_env)

    loaded_total = 0
    loaded_files = []
    for path in candidates:
        loaded = load_dotenv_file(path)
        if loaded > 0:
            loaded_total += loaded
            loaded_files.append(str(path))

    return loaded_total, loaded_files


def normalize_text(text):
    return re.sub(r"\s+", " ", text).strip()


def is_page_number_or_code(text):
    stripped = (text or "").strip()
    if re.match(r"^\d+$", stripped):
        return True
    if not stripped:
        return True
    if len(stripped) <= 1:
        return True
    return False


def is_short_text(text):
    return len(normalize_text(text)) <= 80


def restore_hyphenation(text):
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
    return (
        abs(prev_block.get("size", 0) - next_block.get("size", 0)) <= 1.5
        and prev_block.get("font") == next_block.get("font")
    )


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


def build_translate_prompt(text, keep_markers=False):
    marker_rule = "Keep all marker lines like __PDFTRANSLATE_BLOCK_0__ unchanged and on their own lines." if keep_markers else ""
    return (
        "Translate the following English text into natural Japanese. "
        "Keep numbers, units, URLs, product names, model numbers, and proper nouns unchanged. "
        "Output only the Japanese translation text. "
        f"{marker_rule}\n\n"
        f"SOURCE:\n{text}"
    )


def lmstudio_complete(base_url, model, prompt, temperature=0.2, timeout=120, max_tokens=4096):
    url = f"{base_url.rstrip('/')}/completions"
    payload = {
        "model": model,
        "prompt": prompt,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read().decode("utf-8")

    parsed = json.loads(body)
    choices = parsed.get("choices", [])
    if not choices:
        return None

    return (choices[0].get("text", "") or "").strip()


def google_translate_text(text, timeout=30):
    params = {
        "client": "gtx",
        "sl": "auto",
        "tl": "ja",
        "dt": "t",
        "q": text,
    }
    query = urllib.parse.urlencode(params)
    url = f"{GOOGLE_TRANSLATE_DEFAULT_URL}?{query}"

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read().decode("utf-8")

    parsed = json.loads(body)
    translated_parts = parsed[0] if isinstance(parsed, list) and parsed else []
    if not translated_parts:
        return None

    pieces = []
    for part in translated_parts:
        if isinstance(part, list) and part and part[0]:
            pieces.append(part[0])
    translated = "".join(pieces).strip()
    return translated or None


def translate_with_lmstudio(llm_options, text, keep_markers=False):
    cleaned = text.strip()
    if not cleaned:
        return cleaned

    prompt = build_translate_prompt(cleaned, keep_markers=keep_markers)
    translated = None
    for _ in range(3):
        try:
            translated = lmstudio_complete(
                base_url=llm_options["base_url"],
                model=llm_options["model"],
                prompt=prompt,
                temperature=llm_options["temperature"],
                timeout=llm_options["timeout"],
                max_tokens=llm_options["max_tokens"],
            )
            if translated:
                break
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, Exception):
            time.sleep(0.5)

    return translated


def translate_with_google(text, timeout=30):
    cleaned = text.strip()
    if not cleaned:
        return cleaned

    translated = None
    for _ in range(3):
        try:
            translated = google_translate_text(cleaned, timeout=timeout)
            if translated:
                break
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, Exception):
            time.sleep(0.5)
    return translated


def translate_text(translate_options, text, keep_markers=False):
    engine = translate_options.get("engine", "google")
    if engine == "llm":
        return translate_with_lmstudio(translate_options["llm"], text, keep_markers=keep_markers)
    return translate_with_google(text, timeout=translate_options.get("google_timeout", 30))


def translate_single_block(block, translate_options):
    text = block["text"]
    if is_page_number_or_code(text):
        translated_text = text
    else:
        normalized = normalize_for_translation(text, preserve_paragraph_break=False)
        translated_text = translate_text(translate_options, normalized, keep_markers=False)
        if not translated_text:
            translated_text = text

    block_copy = block.copy()
    block_copy["text"] = translated_text
    block_copy["original_text"] = text
    return block_copy


def split_japanese_chunks(text):
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

    if idx < len(chunks):
        assigned[-1].extend(chunks[idx:])

    results = [" ".join(parts).strip() for parts in assigned]
    if any(not text for text in results):
        return None
    return results


def translate_paragraph_segment(segment, translate_options):
    blocks = segment["blocks"]
    if len(blocks) == 1:
        translated_block = translate_single_block(blocks[0], translate_options)
        return [translated_block], "single_paragraph"

    joined = "\n\n".join(normalize_for_translation(block["text"], preserve_paragraph_break=True) for block in blocks)
    translated = translate_text(translate_options, joined, keep_markers=False)
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


def translate_structured_segment(segment, translate_options):
    if translate_options.get("engine", "google") != "llm":
        return None, "structured_requires_llm"

    blocks = segment["blocks"]
    payload = create_segment_payload(blocks)
    translated_payload = translate_text(translate_options, payload, keep_markers=True)
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


def translate_segment(segment, index, llm_options):
    blocks = segment["blocks"]
    kind = segment["kind"]

    if len(blocks) == 1 and kind != "paragraph":
        translated_block = translate_single_block(blocks[0], llm_options)
        return index, [translated_block], {
            "kind": kind,
            "strategy": "single_block",
            "result": "single_block_direct",
            "block_refs": [[blocks[0]["page"], blocks[0]["block_idx"]]],
            "merge_reasons": segment.get("merge_reasons", []),
            "source_preview": normalize_text(blocks[0]["text"])[:180],
        }

    strategy = "structured"
    translated_blocks = None
    result_note = ""

    if kind == "paragraph":
        strategy = "paragraph_reflow"
        translated_blocks, result_note = translate_paragraph_segment(segment, llm_options)
    else:
        translated_blocks, result_note = translate_structured_segment(segment, llm_options)

    if translated_blocks is None:
        strategy = "block_fallback"
        translated_blocks = [translate_single_block(block, llm_options) for block in blocks]
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


def translate_blocks(input_json, output_json, llm_options, debug_json_path=None):
    with open(input_json, "r", encoding="utf-8") as f:
        data = json.load(f)

    translation_segments = build_translation_segments(data)
    engine = llm_options.get("engine", "google")
    engine_label = "LM Studio" if engine == "llm" else "Google Translate"
    print(f"Starting {engine_label} translation of {len(data)} blocks in {len(translation_segments)} segments...", flush=True)

    translated_count = 0
    segment_slots = [None] * len(translation_segments)
    segment_debug = [None] * len(translation_segments)

    max_workers = max(1, int(llm_options.get("max_workers", 1)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(translate_segment, segment, idx, llm_options): idx
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

    results_by_key = {(block["page"], block["block_idx"]): block for block in translated_blocks}
    results = []
    for original_block in data:
        block_copy = results_by_key[(original_block["page"], original_block["block_idx"])]
        results.append(block_copy)
        if block_copy["text"] != block_copy["original_text"] and not is_page_number_or_code(block_copy["original_text"]):
            translated_count += 1

    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    if debug_json_path:
        debug_payload = {
            "segment_debug": segment_debug,
            "engine": engine,
            "workers": max_workers,
        }
        if engine == "llm":
            llm_cfg = llm_options["llm"]
            debug_payload["llm"] = {
                "provider": "lmstudio",
                "model": llm_cfg["model"],
                "base_url": llm_cfg["base_url"],
                "temperature": llm_cfg["temperature"],
                "max_tokens": llm_cfg["max_tokens"],
                "max_workers": max_workers,
            }
        with open(debug_json_path, "w", encoding="utf-8") as f:
            json.dump(debug_payload, f, ensure_ascii=False, indent=2)

    print(f"Completed translation! Saved to {output_json}. Total translated: {translated_count}", flush=True)


def build_default_output_pdf(source_pdf, engine="google"):
    source_path = Path(source_pdf)
    suffix = "_LLM" if engine == "llm" else "_G"
    return str(source_path.with_name(f"{source_path.stem}{suffix}.pdf"))


def run_pipeline(source_pdf, output_pdf, translate_options):
    from extract import extract_pdf_text
    from generate import generate_translated_pdf

    source_path = Path(source_pdf)
    if not source_path.exists():
        raise FileNotFoundError(f"Source PDF not found: {source_pdf}")

    engine = translate_options.get("engine", "google")
    output_pdf = output_pdf or build_default_output_pdf(source_pdf, engine=engine)
    extracted_json = str(source_path.with_name(f"{source_path.stem}_extracted_text.json"))
    translated_json = str(source_path.with_name(f"{source_path.stem}_translated_text.json"))
    segment_debug_json = str(source_path.with_name(f"{source_path.stem}_segment_debug.json"))

    print(f"[1/4] Extracting text blocks from: {source_pdf}", flush=True)
    extract_pdf_text(str(source_path), extracted_json)

    engine_label = "LM Studio" if engine == "llm" else "Google Translate"
    print(f"[2/4] Translating blocks to Japanese with {engine_label}", flush=True)
    translate_blocks(
        extracted_json,
        translated_json,
        llm_options=translate_options,
        debug_json_path=segment_debug_json,
    )

    print(f"[3/4] Generating translated PDF: {output_pdf}", flush=True)
    generate_translated_pdf(str(source_path), translated_json, output_pdf)

    print("[4/4] Done.", flush=True)


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
    parser.add_argument(
        "--engine",
        choices=["google", "llm"],
        default=os.environ.get("TRANSLATION_ENGINE", "google"),
        help="Translation engine: google (default) or llm",
    )
    parser.add_argument(
        "--lmstudio-base-url",
        default=os.environ.get("LMSTUDIO_BASE_URL", LMSTUDIO_DEFAULT_BASE_URL),
        help="LM Studio OpenAI-compatible base URL",
    )
    parser.add_argument(
        "--lmstudio-model",
        default=os.environ.get("LMSTUDIO_MODEL", "translategemma-4b-it"),
        help="LM Studio model name",
    )
    parser.add_argument(
        "--lmstudio-timeout",
        type=int,
        default=int(os.environ.get("LMSTUDIO_TIMEOUT", "120")),
        help="LM Studio HTTP timeout seconds",
    )
    parser.add_argument(
        "--lmstudio-max-tokens",
        type=int,
        default=int(os.environ.get("LMSTUDIO_MAX_TOKENS", "4096")),
        help="LM Studio max_tokens per call",
    )
    parser.add_argument(
        "--lmstudio-temperature",
        type=float,
        default=float(os.environ.get("LMSTUDIO_TEMPERATURE", "0.2")),
        help="LM Studio sampling temperature",
    )
    parser.add_argument(
        "--lmstudio-max-workers",
        type=int,
        default=int(os.environ.get("LMSTUDIO_MAX_WORKERS", "8")),
        help="Concurrent translation workers",
    )
    parser.add_argument(
        "--google-timeout",
        type=int,
        default=int(os.environ.get("GOOGLE_TIMEOUT", "30")),
        help="Google Translate HTTP timeout seconds",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="Optional .env file path. By default, .env in cwd/script/source-pdf dir are auto-loaded.",
    )
    args = parser.parse_args()

    loaded_count, loaded_files = load_dotenv_candidates(
        source_pdf_path=args.source_pdf,
        explicit_env_file=args.env_file,
    )
    if loaded_count > 0:
        print(f"Loaded {loaded_count} env vars from: {', '.join(loaded_files)}", flush=True)

    translate_options = {
        "engine": args.engine,
        "google_timeout": args.google_timeout,
        "max_workers": args.lmstudio_max_workers,
        "llm": {
            "base_url": args.lmstudio_base_url,
            "model": args.lmstudio_model,
            "timeout": args.lmstudio_timeout,
            "max_tokens": args.lmstudio_max_tokens,
            "temperature": args.lmstudio_temperature,
        },
    }

    run_pipeline(args.source_pdf, args.output_pdf, translate_options=translate_options)
