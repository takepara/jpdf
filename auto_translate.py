import argparse
import json
import math
import os
import re
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

BLOCK_MARKER_TEMPLATE = "__PDFTRANSLATE_BLOCK_{index}__"
LMSTUDIO_DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"
GOOGLE_TRANSLATE_DEFAULT_URL = "https://translate.googleapis.com/translate_a/single"

_LLM_CALL_SEQ = 0
_LLM_CALL_SEQ_LOCK = threading.Lock()

_LLM_METRICS_LOCK = threading.Lock()
_LLM_METRICS = {
    "requests_total": 0,
    "requests_ok": 0,
    "requests_failed": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
    "successful_elapsed": 0.0,
}

ANSI_RESET = "\033[0m"
ANSI_INFO = "\033[96m"
ANSI_WARN = "\033[93m"
ANSI_ERROR = "\033[91m"


def log_info(message):
    print(f"{ANSI_INFO}[INFO]{ANSI_RESET} {message}", flush=True)


def log_warn(message):
    print(f"{ANSI_WARN}[WARN]{ANSI_RESET} {message}", flush=True)


def log_error(message):
    print(f"{ANSI_ERROR}[ERROR]{ANSI_RESET} {message}", flush=True)


def next_llm_call_seq():
    global _LLM_CALL_SEQ
    with _LLM_CALL_SEQ_LOCK:
        _LLM_CALL_SEQ += 1
        return _LLM_CALL_SEQ


def reset_llm_metrics():
    with _LLM_METRICS_LOCK:
        _LLM_METRICS["requests_total"] = 0
        _LLM_METRICS["requests_ok"] = 0
        _LLM_METRICS["requests_failed"] = 0
        _LLM_METRICS["prompt_tokens"] = 0
        _LLM_METRICS["completion_tokens"] = 0
        _LLM_METRICS["total_tokens"] = 0
        _LLM_METRICS["successful_elapsed"] = 0.0


def _estimate_tokens_from_chars(char_count):
    return max(0, int(math.ceil(max(0, char_count) / 4.0)))


def _safe_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def record_llm_metrics(success, elapsed, prompt_chars=0, completion_chars=0, usage=None):
    usage = usage if isinstance(usage, dict) else {}

    prompt_tokens = _safe_int(usage.get("prompt_tokens"))
    completion_tokens = _safe_int(usage.get("completion_tokens"))
    total_tokens = _safe_int(usage.get("total_tokens"))

    if prompt_tokens is None:
        prompt_tokens = _estimate_tokens_from_chars(prompt_chars)
    if completion_tokens is None:
        completion_tokens = _estimate_tokens_from_chars(completion_chars)
    if total_tokens is None:
        total_tokens = prompt_tokens + completion_tokens

    with _LLM_METRICS_LOCK:
        _LLM_METRICS["requests_total"] += 1
        if success:
            _LLM_METRICS["requests_ok"] += 1
            _LLM_METRICS["prompt_tokens"] += max(0, prompt_tokens)
            _LLM_METRICS["completion_tokens"] += max(0, completion_tokens)
            _LLM_METRICS["total_tokens"] += max(0, total_tokens)
            _LLM_METRICS["successful_elapsed"] += max(0.0, float(elapsed))
        else:
            _LLM_METRICS["requests_failed"] += 1


def snapshot_llm_metrics():
    with _LLM_METRICS_LOCK:
        return dict(_LLM_METRICS)


def format_elapsed(seconds):
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    remain = seconds - (minutes * 60)
    return f"{minutes}m{remain:04.1f}s"


def log_final_statistics(engine, started_at, translated_count):
    total_elapsed = time.time() - started_at
    log_info(f"Total processing time: {format_elapsed(total_elapsed)}")

    if engine not in ("llm", "hybrid"):
        return

    metrics = snapshot_llm_metrics()
    ok_elapsed = max(0.0, metrics.get("successful_elapsed", 0.0))
    completion_tokens = int(metrics.get("completion_tokens", 0))
    generation_tps = completion_tokens / ok_elapsed if ok_elapsed > 0 else 0.0
    e2e_blocks_per_sec = translated_count / max(total_elapsed, 1e-6)

    log_info(
        "LLM stats: "
        f"requests_total={metrics.get('requests_total', 0)} "
        f"requests_ok={metrics.get('requests_ok', 0)} "
        f"requests_failed={metrics.get('requests_failed', 0)}"
    )
    log_info(
        "LLM tokens: "
        f"prompt={metrics.get('prompt_tokens', 0)} "
        f"completion={metrics.get('completion_tokens', 0)} "
        f"total={metrics.get('total_tokens', 0)}"
    )
    log_info(
        "Performance: "
        f"generation={generation_tps:.2f} tok/s "
        f"e2e_blocks={e2e_blocks_per_sec:.2f} blocks/s"
    )


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
        "Do not copy any full English sentence or long English phrase from SOURCE into the output. "
        "Output only the Japanese translation text. "
        f"{marker_rule}\n\n"
        f"SOURCE:\n{text}"
    )


def build_naturalize_prompt(text):
    return (
        "Rewrite the following Japanese text to natural and fluent business Japanese. "
        "Do not change facts, numbers, units, URLs, product names, model numbers, or proper nouns. "
        "Keep the original meaning and level of detail. "
        "Output only the revised Japanese text.\n\n"
        f"SOURCE_JA:\n{text}"
    )


def build_structured_translate_prompt(payload_json):
    return (
        "You are a professional English-to-Japanese translator for business reports. "
        "Translate each item's text into natural Japanese while preserving meaning. "
        "Keep numbers, units, URLs, product names, model numbers, and proper nouns unchanged. "
        "For each item, do not copy any full English sentence or long English phrase from the input into t. "
        "Return ONLY valid JSON with this exact shape: "
        '{"items":[{"id":"<same id>","t":"<translated text>"}]} . '
        "Do not add commentary, markdown fences, or extra keys. "
        "Keep all item ids exactly as given and return one output item per input item.\n\n"
        f"INPUT_JSON:\n{payload_json}"
    )


def has_verbatim_source_overlap(source_text, output_text, min_source_chars=40, min_words=12):
    source = normalize_text(source_text)
    output = normalize_text(output_text)
    if not source or not output:
        return False

    source_lower = source.lower()
    output_lower = output.lower()

    # Direct full-source echo.
    if len(source_lower) >= min_source_chars and source_lower in output_lower:
        return True

    # Sentence-level echo for long English clauses.
    sentence_candidates = [
        normalize_text(part)
        for part in re.split(r"[\n\r.!?;:]+", source)
        if normalize_text(part)
    ]
    for candidate in sentence_candidates:
        words = re.findall(r"[A-Za-z][A-Za-z0-9'\-/]*", candidate)
        if len(words) >= min_words and candidate.lower() in output_lower:
            return True

    # Sliding window over English tokens to detect long phrase reuse.
    english_tokens = re.findall(r"[A-Za-z][A-Za-z0-9'\-/]*", source)
    if len(english_tokens) >= min_words:
        for start in range(0, len(english_tokens) - min_words + 1):
            phrase = " ".join(english_tokens[start : start + min_words]).lower()
            if phrase in output_lower:
                return True

    return False


def format_request_log_label(request_meta):
    if not request_meta:
        return ""

    ordered_keys = [
        "mode",
        "strategy",
        "kind",
        "items",
        "chars",
        "markers",
        "page",
        "block_idx",
    ]
    parts = []
    for key in ordered_keys:
        if key in request_meta and request_meta[key] is not None:
            parts.append(f"{key}={request_meta[key]}")

    for key, value in request_meta.items():
        if key in ordered_keys or value is None:
            continue
        parts.append(f"{key}={value}")

    if not parts:
        return ""
    return "req " + " ".join(parts)


def _extract_http_error_detail(body_text):
    raw = (body_text or "").strip()
    if not raw:
        return ""

    parsed = None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, dict):
        error_obj = parsed.get("error")
        if isinstance(error_obj, dict):
            message = str(error_obj.get("message") or "").strip()
            code = str(error_obj.get("code") or "").strip()
            err_type = str(error_obj.get("type") or "").strip()
            detail_parts = [part for part in [message, code, err_type] if part]
            if detail_parts:
                return " | ".join(detail_parts)
        for key in ["message", "detail", "error"]:
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    one_line = re.sub(r"\s+", " ", raw)
    return one_line[:300]


def lmstudio_complete(base_url, model, prompt, temperature=0.2, timeout=120, max_tokens=4096, log_label=""):
    call_seq = next_llm_call_seq()
    label = f" {log_label}" if log_label else ""
    started_at = time.time()
    prompt_chars = len(prompt)
    log_info(f"[LLM call {call_seq}{label}] start prompt_chars={prompt_chars}")

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

    try:
        if timeout is None or timeout <= 0:
            with urllib.request.urlopen(req) as response:
                body = response.read().decode("utf-8")
        else:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        elapsed = time.time() - started_at
        error_body = ""
        try:
            error_body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            error_body = ""

        detail = _extract_http_error_detail(error_body)
        status = getattr(exc, "code", "?")
        reason = getattr(exc, "reason", "")
        reason_text = f" {reason}" if reason else ""
        if detail:
            log_error(
                f"[LLM call {call_seq}{label}] failed after {elapsed:.1f}s: "
                f"HTTP {status}{reason_text} detail={detail}"
            )
        else:
            log_error(
                f"[LLM call {call_seq}{label}] failed after {elapsed:.1f}s: "
                f"HTTP {status}{reason_text}"
            )
        record_llm_metrics(success=False, elapsed=elapsed, prompt_chars=prompt_chars, completion_chars=0, usage=None)
        raise
    except Exception as exc:
        elapsed = time.time() - started_at
        log_error(f"[LLM call {call_seq}{label}] failed after {elapsed:.1f}s: {type(exc).__name__}: {exc}")
        record_llm_metrics(success=False, elapsed=elapsed, prompt_chars=prompt_chars, completion_chars=0, usage=None)
        raise

    parsed = json.loads(body)
    usage = parsed.get("usage") if isinstance(parsed, dict) else None
    choices = parsed.get("choices", [])
    if not choices:
        elapsed = time.time() - started_at
        log_warn(f"[LLM call {call_seq}{label}] done in {elapsed:.1f}s (empty choices)")
        record_llm_metrics(success=True, elapsed=elapsed, prompt_chars=prompt_chars, completion_chars=0, usage=usage)
        return None

    text = (choices[0].get("text", "") or "").strip()
    elapsed = time.time() - started_at
    log_info(f"[LLM call {call_seq}{label}] done in {elapsed:.1f}s response_chars={len(text)}")
    record_llm_metrics(success=True, elapsed=elapsed, prompt_chars=prompt_chars, completion_chars=len(text), usage=usage)
    return text


def strip_json_fences(text):
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\\s*```$", "", cleaned)
    return cleaned.strip()


def parse_structured_translation_response(response_text):
    cleaned = strip_json_fences(response_text)
    if not cleaned:
        return None

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None

    if not isinstance(parsed, dict):
        return None
    items = parsed.get("items")
    if not isinstance(items, list):
        return None
    return parsed


def build_structured_translation_items(data):
    ordered_blocks = sorted(data, key=lambda block: (block["page"], block.get("reading_order", block["block_idx"])))
    items = []
    for block in ordered_blocks:
        text = block.get("text", "")
        if is_page_number_or_code(text):
            continue
        normalized = normalize_for_translation(text, preserve_paragraph_break=True)
        if not normalized:
            continue
        items.append(
            {
                "id": f"p{block['page']}_b{block['block_idx']}",
                "t": normalized,
            }
        )
    return items


def translate_structured_items_once(items, llm_options, batch_label=""):
    input_chars = sum(len(item["t"]) for item in items)
    payload_json = json.dumps({"items": items}, ensure_ascii=False, separators=(",", ":"))
    prompt = build_structured_translate_prompt(payload_json)
    request_label = format_request_log_label(
        {
            "mode": "document_batch",
            "strategy": "structured_json",
            "kind": "mixed",
            "items": len(items),
            "chars": input_chars,
        }
    )
    combined_label = " ".join(part for part in [batch_label, request_label] if part)

    response_text = None
    for _ in range(3):
        try:
            response_text = lmstudio_complete(
                base_url=llm_options["base_url"],
                model=llm_options["model"],
                prompt=prompt,
                temperature=llm_options["temperature"],
                timeout=llm_options["timeout"],
                max_tokens=llm_options["max_tokens"],
                log_label=combined_label,
            )
            if response_text:
                break
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, Exception):
            time.sleep(0.5)

    if not response_text:
        return None, {"reason": "empty_response", "input_chars": input_chars, "items": len(items)}

    parsed = parse_structured_translation_response(response_text)
    if not parsed:
        return None, {"reason": "invalid_json_response", "input_chars": input_chars, "items": len(items)}

    source_by_id = {item["id"]: item["t"] for item in items}
    out_items = parsed.get("items", [])
    translated_by_id = {}
    for out_item in out_items:
        if not isinstance(out_item, dict):
            continue
        item_id = out_item.get("id")
        translated_text = (out_item.get("t") or "").strip()
        if not item_id or not translated_text:
            continue
        source_text = source_by_id.get(item_id, "")
        if source_text and has_verbatim_source_overlap(source_text, translated_text):
            return None, {
                "reason": "source_echo_detected",
                "echo_item_id": item_id,
                "input_chars": input_chars,
                "items": len(items),
            }
        if item_id in translated_by_id:
            return None, {"reason": "duplicate_id", "input_chars": input_chars, "items": len(items)}
        translated_by_id[item_id] = translated_text

    expected_ids = {item["id"] for item in items}
    actual_ids = set(translated_by_id.keys())
    if expected_ids != actual_ids:
        return None, {
            "reason": "id_mismatch",
            "missing": len(expected_ids - actual_ids),
            "extra": len(actual_ids - expected_ids),
            "input_chars": input_chars,
            "items": len(items),
        }

    return translated_by_id, {
        "reason": "ok",
        "input_chars": input_chars,
        "items": len(items),
        "response_chars": len(response_text),
    }


def translate_blocks_llm_structured_adaptive(data, llm_options, max_input_chars=180000, min_batch_items=8):
    items = build_structured_translation_items(data)
    if not items:
        return None, {"used": False, "reason": "no_items"}

    initial_chars = sum(len(item["t"]) for item in items)
    translated_by_id = {}
    split_events = 0
    failed_leaf_batches = []
    successful_batches = 0
    configured_min_batch_items = max(1, int(min_batch_items))
    index = 0

    # Bottom-up adaptive sizing: start from 1 item, increase on success,
    # and step back by one on failure to find stable throughput dynamically.
    current_batch_size = 1
    max_stable_batch_size = 1

    while index < len(items):
        remaining = len(items) - index
        target_size = min(current_batch_size, remaining)

        # Respect input size ceiling by shrinking this attempt before calling the model.
        while target_size > 1:
            trial_batch = items[index : index + target_size]
            trial_chars = sum(len(item["t"]) for item in trial_batch)
            if trial_chars <= max_input_chars:
                break
            target_size -= 1
            split_events += 1
            log_warn(f"[LLM batch] reduce by size limit target={target_size + 1}->{target_size} (max_chars={max_input_chars})")

        batch = items[index : index + target_size]
        batch_chars = sum(len(item["t"]) for item in batch)
        log_info(f"[LLM batch] processing items={len(batch)} chars={batch_chars} index={index}/{len(items)} current_target={current_batch_size}")

        translated_chunk, meta = translate_structured_items_once(
            batch,
            llm_options,
            batch_label=f"batch items={len(batch)} chars={batch_chars}",
        )
        if translated_chunk is None:
            failure_reason = meta.get("reason", "unknown")
            if failure_reason == "empty_response":
                log_warn(
                    "[LLM batch] no usable response text returned (empty_response). "
                    "Treating this as batch-too-large/unstable and retrying with smaller batches."
                )

            if len(batch) > 1:
                previous_target = current_batch_size
                current_batch_size = max(1, len(batch) - 1)
                split_events += 1
                log_warn(f"[LLM batch] reduce by failure({failure_reason}) target={previous_target}->{current_batch_size}")
                continue

            failed_leaf_batches.append({"items": len(batch), "chars": batch_chars, "reason": meta.get("reason", "unknown")})
            log_error(f"[LLM batch] failed at minimum batch size items={len(batch)} reason={meta.get('reason', 'unknown')}")
            return None, {
                "used": False,
                "reason": "batch_failure_at_min_size",
                "initial_items": len(items),
                "initial_chars": initial_chars,
                "split_events": split_events,
                "failed_leaf_batches": failed_leaf_batches,
                "configured_min_batch_items": configured_min_batch_items,
            }

        translated_by_id.update(translated_chunk)
        successful_batches += 1
        index += len(batch)
        max_stable_batch_size = max(max_stable_batch_size, len(batch))

        previous_target = current_batch_size
        current_batch_size = min(len(items), max_stable_batch_size + 1)
        log_info(
            f"[LLM batch] success items={len(batch)} successful_batches={successful_batches} split_events={split_events} next_target={current_batch_size} prev_target={previous_target}"
        )

    expected_ids = {item["id"] for item in items}
    if set(translated_by_id.keys()) != expected_ids:
        return None, {
            "used": False,
            "reason": "final_id_mismatch",
            "initial_items": len(items),
            "initial_chars": initial_chars,
            "split_events": split_events,
        }

    results = []
    for block in data:
        block_copy = block.copy()
        original_text = block.get("text", "")
        block_copy["original_text"] = original_text
        block_copy["segment_kind"] = classify_block_kind(block)

        item_id = f"p{block['page']}_b{block['block_idx']}"
        translated_text = translated_by_id.get(item_id)
        if translated_text and not is_page_number_or_code(original_text):
            block_copy["text"] = translated_text
        else:
            block_copy["text"] = original_text
        results.append(block_copy)

    return results, {
        "used": True,
        "reason": "ok",
        "initial_items": len(items),
        "initial_chars": initial_chars,
        "split_events": split_events,
        "successful_batches": successful_batches,
        "min_batch_items": 1,
        "configured_min_batch_items": configured_min_batch_items,
        "max_stable_batch_size": max_stable_batch_size,
        "max_input_chars": max_input_chars,
    }


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


def translate_with_lmstudio(llm_options, text, keep_markers=False, request_meta=None):
    cleaned = text.strip()
    if not cleaned:
        return cleaned

    prompt = build_translate_prompt(cleaned, keep_markers=keep_markers)
    effective_meta = dict(request_meta or {})
    effective_meta.setdefault("chars", len(cleaned))
    if keep_markers:
        marker_count = len(re.findall(r"__PDFTRANSLATE_BLOCK_\d+__", cleaned))
        effective_meta.setdefault("markers", marker_count)
    request_label = format_request_log_label(effective_meta)

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
                log_label=request_label,
            )
            if translated:
                if has_verbatim_source_overlap(cleaned, translated):
                    log_warn("LLM output included verbatim source English; retrying request.")
                    translated = None
                    time.sleep(0.5)
                    continue
                break
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, Exception):
            time.sleep(0.5)

    return translated


def naturalize_with_lmstudio(llm_options, text):
    cleaned = text.strip()
    if not cleaned:
        return cleaned

    prompt = build_naturalize_prompt(cleaned)
    naturalized = None
    for _ in range(3):
        try:
            naturalized = lmstudio_complete(
                base_url=llm_options["base_url"],
                model=llm_options["model"],
                prompt=prompt,
                temperature=min(0.2, llm_options["temperature"]),
                timeout=llm_options["timeout"],
                max_tokens=llm_options["max_tokens"],
            )
            if naturalized:
                break
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, Exception):
            time.sleep(0.5)
    return naturalized


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


def hybrid_segment_score(source_text, translated_text):
    score = 0
    reasons = []

    cleaned_source = normalize_text(source_text)
    cleaned_translated = normalize_text(translated_text)
    if not cleaned_translated:
        return score, reasons

    ascii_letters = len(re.findall(r"[A-Za-z]", cleaned_translated))
    ascii_ratio = ascii_letters / max(1, len(cleaned_translated))
    if ascii_ratio >= 0.15:
        score += 1
        reasons.append("high_ascii_ratio")

    if re.search(r"\b(the|and|for|with|from|into|that|this|are|is|to|of)\b", cleaned_translated, flags=re.IGNORECASE):
        score += 1
        reasons.append("english_word_residue")

    source_len = len(cleaned_source)
    translated_len = len(cleaned_translated)
    if source_len >= 40:
        length_ratio = translated_len / max(1, source_len)
        if length_ratio < 0.45 or length_ratio > 1.35:
            score += 1
            reasons.append("length_ratio_outlier")

    if translated_len >= 120 and "。" not in cleaned_translated and "、" not in cleaned_translated:
        score += 1
        reasons.append("low_japanese_punctuation")

    return score, reasons


HYBRID_EXCLUDE_PATTERNS = [
    r"references?",
    r"bibliography",
    r"appendix",
    r"sources?",
    r"disclaimer",
    r"copyright",
    r"all rights reserved",
    r"deloitte\s+llp",
    r"registered",
    r"registration\s+number",
    r"\bnc\d{3,}\b",
    r"limited liability",
    r"免責",
    r"著作権",
    r"参考文献",
    r"出典",
    r"付録",
    r"登録番号",
    r"有限責任",
]

HYBRID_HALLUCINATION_MARKERS = [
    "SOURCE_EN:",
    "SOURCE_JA:",
    "[製品名]",
    "[モデル番号]",
    "AwesomeGadget",
    "URL:",
]


def extract_numeric_tokens(text):
    return set(re.findall(r"\d+(?:[\.,]\d+)?%?", text))


def sentence_uniqueness_ratio(text):
    sentences = [s.strip() for s in re.split(r"[。！？!?]+", text) if len(s.strip()) >= 10]
    if len(sentences) < 4:
        return 1.0
    return len(set(sentences)) / len(sentences)


def is_hybrid_naturalization_candidate(segment, source_text, translated_text, min_chars):
    normalized_source = normalize_text(source_text)
    normalized_translated = normalize_text(translated_text)

    if len(normalized_translated) < min_chars:
        return False, "too_short"

    merged_lower = (normalized_source + " " + normalized_translated).lower()
    for pattern in HYBRID_EXCLUDE_PATTERNS:
        if re.search(pattern, merged_lower, flags=re.IGNORECASE):
            return False, "excluded_pattern"

    url_hits = len(re.findall(r"https?://", normalized_source + " " + normalized_translated, flags=re.IGNORECASE))
    if url_hits >= 1:
        return False, "url_heavy"

    citation_hits = len(re.findall(r"\(\d{1,3}\)", normalized_source + " " + normalized_translated))
    if citation_hits >= 4:
        return False, "citation_heavy"

    blocks = segment["blocks"]
    if blocks:
        avg_size = sum(float(block.get("size", 10.0)) for block in blocks) / len(blocks)
        if avg_size <= 8.5:
            return False, "small_font"

    return True, "candidate"


def validate_naturalized_text(base_text, candidate_text):
    normalized_base = normalize_text(base_text)
    normalized_candidate = normalize_text(candidate_text)

    if not normalized_candidate:
        return False, "empty"

    for marker in HYBRID_HALLUCINATION_MARKERS:
        if marker.lower() in normalized_candidate.lower():
            return False, "hallucination_marker"

    if re.search(r"\bSOURCE_[A-Z]+\b", normalized_candidate):
        return False, "prompt_leak"

    base_len = len(normalized_base)
    cand_len = len(normalized_candidate)
    length_ratio = cand_len / max(1, base_len)
    if length_ratio < 0.70 or length_ratio > 1.30:
        return False, "length_ratio"

    base_urls = len(re.findall(r"https?://", normalized_base, flags=re.IGNORECASE))
    cand_urls = len(re.findall(r"https?://", normalized_candidate, flags=re.IGNORECASE))
    if cand_urls > base_urls:
        return False, "url_increase"

    base_numbers = extract_numeric_tokens(normalized_base)
    if base_numbers:
        cand_numbers = extract_numeric_tokens(normalized_candidate)
        kept = sum(1 for token in base_numbers if token in cand_numbers)
        if kept / len(base_numbers) < 0.85:
            return False, "number_loss"

    uniq_ratio = sentence_uniqueness_ratio(normalized_candidate)
    if uniq_ratio < 0.75:
        return False, "repetition"

    repeated_span = re.search(r"(.{24,80}?)(?:\s*\1){2,}", normalized_candidate)
    if repeated_span:
        return False, "span_repetition"

    ascii_base = len(re.findall(r"[A-Za-z]", normalized_base)) / max(1, base_len)
    ascii_candidate = len(re.findall(r"[A-Za-z]", normalized_candidate)) / max(1, cand_len)
    if ascii_candidate > max(0.35, ascii_base + 0.15):
        return False, "ascii_increase"

    return True, "ok"


def naturalize_hybrid_segment(segment, translated_segment, translate_options):
    blocks = segment["blocks"]
    current_text = "\n\n".join(normalize_for_translation(block["text"], preserve_paragraph_break=True) for block in translated_segment)
    naturalized = naturalize_with_lmstudio(translate_options["llm"], current_text)
    if not naturalized:
        return None, "naturalize_failed"

    is_valid, validate_note = validate_naturalized_text(current_text, naturalized)
    if not is_valid:
        return None, f"naturalize_rejected:{validate_note}"

    if len(translated_segment) == 1:
        updated = translated_segment[0].copy()
        updated["text"] = naturalized
        return [updated], "naturalized_single"

    chunks = split_japanese_chunks(naturalized)
    reassigned = assign_chunks_to_blocks(chunks, blocks)
    if reassigned is None:
        return None, "naturalize_reassign_failed"

    updated = []
    for base_block, new_text in zip(translated_segment, reassigned):
        block_copy = base_block.copy()
        block_copy["text"] = new_text
        updated.append(block_copy)
    return updated, "naturalized_reassigned"


def run_hybrid_refinement(translation_segments, segment_slots, segment_debug, translate_options):
    max_segments = max(0, int(translate_options.get("hybrid_max_segments", 40)))
    min_chars = max(1, int(translate_options.get("hybrid_min_chars", 100)))
    min_score = max(1, int(translate_options.get("hybrid_min_score", 2)))

    candidates = []
    for idx, (segment, translated_segment) in enumerate(zip(translation_segments, segment_slots)):
        if segment["kind"] != "paragraph" or not translated_segment:
            continue

        source_text = "\n\n".join(block["text"] for block in segment["blocks"])
        translated_text = "\n\n".join(block["text"] for block in translated_segment)
        can_naturalize, candidate_note = is_hybrid_naturalization_candidate(
            segment,
            source_text,
            translated_text,
            min_chars,
        )
        if not can_naturalize:
            if segment_debug[idx] is not None:
                segment_debug[idx]["hybrid_refined"] = False
                segment_debug[idx]["hybrid_note"] = f"candidate_skip:{candidate_note}"
            continue

        score, reasons = hybrid_segment_score(source_text, translated_text)
        if score < min_score:
            if segment_debug[idx] is not None:
                segment_debug[idx]["hybrid_refined"] = False
                segment_debug[idx]["hybrid_note"] = "candidate_skip:low_score"
            continue

        candidates.append(
            {
                "idx": idx,
                "score": score,
                "length": len(normalize_text(translated_text)),
                "reasons": reasons,
            }
        )

    candidates.sort(key=lambda item: (item["score"], item["length"]), reverse=True)
    selected = candidates[:max_segments]

    if not selected:
        return {"selected": 0, "updated": 0, "skipped": len(candidates), "details": []}

    workers = max(1, int(translate_options.get("hybrid_workers", 2)))
    selected_map = {item["idx"]: item for item in selected}
    details = []
    updated_count = 0

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                naturalize_hybrid_segment,
                translation_segments[item["idx"]],
                segment_slots[item["idx"]],
                translate_options,
            ): item["idx"]
            for item in selected
        }

        for future in as_completed(futures):
            idx = futures[future]
            item = selected_map[idx]
            try:
                refined_segment, note = future.result()
                if refined_segment is not None:
                    segment_slots[idx] = refined_segment
                    updated_count += 1
                    if segment_debug[idx] is not None:
                        segment_debug[idx]["hybrid_refined"] = True
                        segment_debug[idx]["hybrid_note"] = note
                        segment_debug[idx]["hybrid_score"] = item["score"]
                        segment_debug[idx]["hybrid_reasons"] = item["reasons"]
                elif segment_debug[idx] is not None:
                    segment_debug[idx]["hybrid_refined"] = False
                    segment_debug[idx]["hybrid_note"] = note
                    segment_debug[idx]["hybrid_score"] = item["score"]
                    segment_debug[idx]["hybrid_reasons"] = item["reasons"]
                details.append({"idx": idx, "score": item["score"], "result": note, "reasons": item["reasons"]})
            except Exception as exc:
                details.append({"idx": idx, "score": item["score"], "result": f"error:{exc}", "reasons": item["reasons"]})

    return {
        "selected": len(selected),
        "updated": updated_count,
        "skipped": max(0, len(candidates) - len(selected)),
        "details": details,
    }


def translate_single_block(block, translate_options):
    text = block["text"]
    if is_page_number_or_code(text):
        translated_text = text
    else:
        normalized = normalize_for_translation(text, preserve_paragraph_break=False)
        translated_text = translate_with_lmstudio(
            translate_options["llm"],
            normalized,
            keep_markers=False,
            request_meta={
                "mode": "block",
                "strategy": "single_block",
                "kind": classify_block_kind(block),
                "items": 1,
                "page": block.get("page"),
                "block_idx": block.get("block_idx"),
            },
        ) if translate_options.get("engine", "google") == "llm" else translate_text(translate_options, normalized, keep_markers=False)
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
    if translate_options.get("engine", "google") == "llm":
        translated = translate_with_lmstudio(
            translate_options["llm"],
            joined,
            keep_markers=False,
            request_meta={
                "mode": "section",
                "strategy": "paragraph_join",
                "kind": "paragraph",
                "items": len(blocks),
            },
        )
    else:
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
    if translate_options.get("engine", "google") == "llm":
        translated_payload = translate_with_lmstudio(
            translate_options["llm"],
            payload,
            keep_markers=True,
            request_meta={
                "mode": "section",
                "strategy": "marker_payload",
                "kind": segment.get("kind", "unknown"),
                "items": len(blocks),
            },
        )
    else:
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


def _is_singleton_batch_candidate(segment):
    return (
        len(segment.get("blocks", [])) == 1
        and segment.get("kind") in {"toc", "heading", "list"}
    )


def _segment_chars_for_batch(segment):
    block = segment["blocks"][0]
    return len(normalize_for_translation(block.get("text", ""), preserve_paragraph_break=False))


def translate_segments_llm_adaptive(translation_segments, llm_options):
    segment_slots = [None] * len(translation_segments)
    segment_debug = [None] * len(translation_segments)

    max_batch_items = max(2, int(llm_options.get("adaptive_singleton_max_items", 12)))
    max_batch_chars = max(500, int(llm_options.get("adaptive_singleton_max_chars", 4500)))
    current_target = 1
    processed_segments = 0
    started_at = time.time()

    def log_progress():
        if processed_segments % 10 == 0 or processed_segments == len(translation_segments):
            elapsed = time.time() - started_at
            log_info(
                f"Processed {processed_segments}/{len(translation_segments)} segments... elapsed={format_elapsed(elapsed)}"
            )

    i = 0
    while i < len(translation_segments):
        segment = translation_segments[i]

        if not _is_singleton_batch_candidate(segment):
            idx, translated_segment, debug_info = translate_segment(segment, i, llm_options)
            segment_slots[idx] = translated_segment
            segment_debug[idx] = debug_info
            processed_segments += 1
            log_progress()
            i += 1
            continue

        kind = segment["kind"]
        run = []
        j = i
        while j < len(translation_segments):
            next_segment = translation_segments[j]
            if not _is_singleton_batch_candidate(next_segment) or next_segment["kind"] != kind:
                break
            run.append((j, next_segment))
            j += 1

        run_cursor = 0
        while run_cursor < len(run):
            remaining = len(run) - run_cursor
            target = min(current_target, remaining, max_batch_items)

            while target > 1:
                candidate = [run[run_cursor + k][1] for k in range(target)]
                candidate_chars = sum(_segment_chars_for_batch(seg) for seg in candidate)
                if candidate_chars <= max_batch_chars:
                    break
                target -= 1

            candidate = [run[run_cursor + k][1] for k in range(target)]
            candidate_blocks = [seg["blocks"][0] for seg in candidate]
            candidate_chars = sum(_segment_chars_for_batch(seg) for seg in candidate)
            adaptive_segment = {
                "kind": kind,
                "blocks": candidate_blocks,
                "merge_reasons": ["adaptive_singleton_batch"],
            }

            translated_blocks, result_note = translate_structured_segment(adaptive_segment, llm_options)
            if translated_blocks is None or len(translated_blocks) != len(candidate_blocks):
                if target > 1:
                    previous = current_target
                    current_target = max(1, target - 1)
                    log_warn(
                        f"[LLM adaptive] batch_failed kind={kind} items={target} chars={candidate_chars} "
                        f"target={previous}->{current_target}"
                    )
                    continue

                global_idx = run[run_cursor][0]
                fallback_block = translate_single_block(run[run_cursor][1]["blocks"][0], llm_options)
                segment_slots[global_idx] = [fallback_block]
                segment_debug[global_idx] = {
                    "kind": kind,
                    "strategy": "single_block_fallback",
                    "result": result_note,
                    "block_refs": [[fallback_block["page"], fallback_block["block_idx"]]],
                    "merge_reasons": ["adaptive_singleton_batch"],
                    "source_preview": normalize_text(fallback_block["original_text"])[:180],
                }
                current_target = 1
                run_cursor += 1
                processed_segments += 1
                log_progress()
                continue

            for offset, translated_block in enumerate(translated_blocks):
                global_idx, original_segment = run[run_cursor + offset]
                translated_block["segment_kind"] = kind
                segment_slots[global_idx] = [translated_block]
                source_block = original_segment["blocks"][0]
                segment_debug[global_idx] = {
                    "kind": kind,
                    "strategy": "adaptive_marker_batch",
                    "result": result_note,
                    "block_refs": [[source_block["page"], source_block["block_idx"]]],
                    "merge_reasons": ["adaptive_singleton_batch"],
                    "source_preview": normalize_text(source_block["text"])[:180],
                }

            previous = current_target
            current_target = min(max_batch_items, max(1, current_target + 1))
            log_info(
                f"[LLM adaptive] batch_ok kind={kind} items={len(candidate_blocks)} chars={candidate_chars} "
                f"target={previous}->{current_target}"
            )

            run_cursor += len(candidate_blocks)
            processed_segments += len(candidate_blocks)
            log_progress()

        i = j

    return segment_slots, segment_debug


def translate_blocks(input_json, output_json, llm_options, debug_json_path=None):
    pipeline_started_at = time.time()
    with open(input_json, "r", encoding="utf-8") as f:
        data = json.load(f)

    engine = llm_options.get("engine", "google")
    if engine in ("llm", "hybrid"):
        reset_llm_metrics()

    translation_segments = build_translation_segments(data)
    engine_label = "LM Studio" if engine == "llm" else "Google Translate"

    llm_single_call_info = None
    if engine == "llm" and llm_options.get("llm_single_call", True):
        log_info("Trying structured LLM translation with adaptive batch splitting...")
        single_results, llm_single_call_info = translate_blocks_llm_structured_adaptive(
            data,
            llm_options=llm_options["llm"],
            max_input_chars=max(1000, int(llm_options.get("llm_single_call_max_chars", 180000))),
            min_batch_items=max(1, int(llm_options.get("llm_single_call_min_batch_items", 8))),
        )
        if single_results is not None:
            translated_count = sum(
                1
                for block in single_results
                if block.get("text") != block.get("original_text") and not is_page_number_or_code(block.get("original_text", ""))
            )
            with open(output_json, "w", encoding="utf-8") as f:
                json.dump(single_results, f, ensure_ascii=False, indent=2)

            if debug_json_path:
                debug_payload = {
                    "segment_debug": [],
                    "engine": engine,
                    "workers": 1,
                    "llm": {
                        "provider": "lmstudio",
                        "model": llm_options["llm"]["model"],
                        "base_url": llm_options["llm"]["base_url"],
                        "temperature": llm_options["llm"]["temperature"],
                        "max_tokens": llm_options["llm"]["max_tokens"],
                        "max_workers": 1,
                    },
                    "llm_single_call": llm_single_call_info,
                }
                with open(debug_json_path, "w", encoding="utf-8") as f:
                    json.dump(debug_payload, f, ensure_ascii=False, indent=2)

            log_info(
                "Structured translation completed: "
                f"initial_items={llm_single_call_info.get('initial_items')}, "
                f"split_events={llm_single_call_info.get('split_events')}, "
                f"successful_batches={llm_single_call_info.get('successful_batches')}"
            )
            log_info(f"Completed translation! Saved to {output_json}. Total translated: {translated_count}")
            log_final_statistics(engine=engine, started_at=pipeline_started_at, translated_count=translated_count)
            return

        reason = llm_single_call_info.get("reason") if isinstance(llm_single_call_info, dict) else "unknown"
        log_warn(f"Single-call structured translation skipped/fallback: {reason}")

    log_info(f"Starting {engine_label} translation of {len(data)} blocks in {len(translation_segments)} segments...")

    translated_count = 0
    segment_slots = [None] * len(translation_segments)
    segment_debug = [None] * len(translation_segments)

    max_workers = max(1, int(llm_options.get("max_workers", 1)))
    processing_started_at = time.time()
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
                elapsed = time.time() - processing_started_at
                log_info(
                    f"Processed {processed_segments}/{len(translation_segments)} segments... "
                    f"elapsed={format_elapsed(elapsed)}"
                )

    hybrid_summary = None
    if engine == "hybrid":
        log_info("Starting hybrid naturalization pass with LM Studio...")
        hybrid_summary = run_hybrid_refinement(translation_segments, segment_slots, segment_debug, llm_options)
        log_info(
            "Hybrid naturalization completed: "
            f"selected={hybrid_summary['selected']}, updated={hybrid_summary['updated']}, skipped={hybrid_summary['skipped']}"
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
        if engine in ("llm", "hybrid"):
            llm_cfg = llm_options["llm"]
            debug_payload["llm"] = {
                "provider": "lmstudio",
                "model": llm_cfg["model"],
                "base_url": llm_cfg["base_url"],
                "temperature": llm_cfg["temperature"],
                "max_tokens": llm_cfg["max_tokens"],
                "max_workers": max_workers,
            }
        if llm_single_call_info is not None:
            debug_payload["llm_single_call"] = llm_single_call_info
        if hybrid_summary is not None:
            debug_payload["hybrid"] = hybrid_summary
        with open(debug_json_path, "w", encoding="utf-8") as f:
            json.dump(debug_payload, f, ensure_ascii=False, indent=2)

    log_info(f"Completed translation! Saved to {output_json}. Total translated: {translated_count}")
    log_final_statistics(engine=engine, started_at=pipeline_started_at, translated_count=translated_count)


def build_default_output_pdf(source_pdf, engine="google"):
    source_path = Path(source_pdf)
    if engine == "llm":
        suffix = "_LLM"
    elif engine == "hybrid":
        suffix = "_HYBRID"
    else:
        suffix = "_G"
    return str(source_path.with_name(f"{source_path.stem}{suffix}.pdf"))


def _count_json_items(json_path):
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if isinstance(payload, list):
            return len(payload)
    except Exception:
        return None
    return None


def run_pipeline(source_pdf, output_pdf, translate_options):
    from extract import extract_pdf_text
    from generate import generate_translated_pdf

    source_path = Path(source_pdf)
    if not source_path.exists():
        raise FileNotFoundError(f"Source PDF not found: {source_pdf}")

    pipeline_started_at = time.time()

    engine = translate_options.get("engine", "google")
    output_pdf = output_pdf or build_default_output_pdf(source_pdf, engine=engine)
    extracted_json = str(source_path.with_name(f"{source_path.stem}_extracted_text.json"))
    translated_json = str(source_path.with_name(f"{source_path.stem}_translated_text.json"))
    segment_debug_json = str(source_path.with_name(f"{source_path.stem}_segment_debug.json"))

    log_info(f"[1/4] Extracting text blocks from: {source_pdf}")
    extract_started_at = time.time()
    extract_pdf_text(str(source_path), extracted_json)
    extract_elapsed = time.time() - extract_started_at

    engine_label = "LM Studio" if engine == "llm" else ("Google + LM Studio Hybrid" if engine == "hybrid" else "Google Translate")
    log_info(f"[2/4] Translating blocks to Japanese with {engine_label}")
    translate_started_at = time.time()
    translate_blocks(
        extracted_json,
        translated_json,
        llm_options=translate_options,
        debug_json_path=segment_debug_json,
    )
    translate_elapsed = time.time() - translate_started_at

    log_info(f"[3/4] Generating translated PDF: {output_pdf}")
    generate_started_at = time.time()
    generate_translated_pdf(str(source_path), translated_json, output_pdf)
    generate_elapsed = time.time() - generate_started_at

    total_elapsed = time.time() - pipeline_started_at
    extracted_blocks = _count_json_items(extracted_json)
    translated_blocks = _count_json_items(translated_json)

    log_info("[4/4] Done.")
    log_info(
        "Pipeline summary: "
        f"engine={engine} "
        f"extracted_blocks={extracted_blocks if extracted_blocks is not None else 'n/a'} "
        f"translated_blocks={translated_blocks if translated_blocks is not None else 'n/a'} "
        f"output_pdf={output_pdf}"
    )
    log_info(
        "Stage time: "
        f"extract={format_elapsed(extract_elapsed)} "
        f"translate={format_elapsed(translate_elapsed)} "
        f"generate={format_elapsed(generate_elapsed)} "
        f"total={format_elapsed(total_elapsed)}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Translate an English PDF into a Japanese PDF while preserving layout."
    )
    parser.add_argument("source_pdf", help="Path to source English PDF")
    parser.add_argument(
        "output_pdf",
        nargs="?",
        help="Path to output Japanese PDF (default by engine: _G / _HYBRID / _LLM)",
    )
    parser.add_argument(
        "--engine",
        choices=["google", "llm", "hybrid"],
        default=os.environ.get("TRANSLATION_ENGINE", "google"),
        help="Translation engine: google (default), hybrid, or llm",
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
        default=int(os.environ.get("LMSTUDIO_TIMEOUT", "0")),
        help="LM Studio HTTP timeout seconds (<=0 disables forced timeout)",
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
        "--llm-single-call",
        action=argparse.BooleanOptionalAction,
        default=os.environ.get("LLM_SINGLE_CALL", "1") not in ("0", "false", "False"),
        help="Try one structured LLM API call for full-document translation before segmented fallback (llm engine only)",
    )
    parser.add_argument(
        "--llm-single-call-max-chars",
        type=int,
        default=int(os.environ.get("LLM_SINGLE_CALL_MAX_CHARS", "180000")),
        help="Maximum total input characters per structured LLM batch",
    )
    parser.add_argument(
        "--llm-single-call-min-batch-items",
        type=int,
        default=int(os.environ.get("LLM_SINGLE_CALL_MIN_BATCH_ITEMS", "8")),
        help="Minimum batch size when adaptive structured LLM batching retries with smaller batches",
    )
    parser.add_argument(
        "--google-timeout",
        type=int,
        default=int(os.environ.get("GOOGLE_TIMEOUT", "30")),
        help="Google Translate HTTP timeout seconds",
    )
    parser.add_argument(
        "--hybrid-max-segments",
        type=int,
        default=int(os.environ.get("HYBRID_MAX_SEGMENTS", "40")),
        help="Maximum number of segments to naturalize in hybrid mode",
    )
    parser.add_argument(
        "--hybrid-min-chars",
        type=int,
        default=int(os.environ.get("HYBRID_MIN_CHARS", "100")),
        help="Minimum translated characters to consider hybrid naturalization",
    )
    parser.add_argument(
        "--hybrid-min-score",
        type=int,
        default=int(os.environ.get("HYBRID_MIN_SCORE", "2")),
        help="Minimum heuristic score required for hybrid naturalization",
    )
    parser.add_argument(
        "--hybrid-workers",
        type=int,
        default=int(os.environ.get("HYBRID_WORKERS", "2")),
        help="Concurrent workers for LM Studio naturalization in hybrid mode",
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
        log_info(f"Loaded {loaded_count} env vars from: {', '.join(loaded_files)}")

    translate_options = {
        "engine": args.engine,
        "google_timeout": args.google_timeout,
        "max_workers": args.lmstudio_max_workers,
        "llm_single_call": args.llm_single_call,
        "llm_single_call_max_chars": args.llm_single_call_max_chars,
        "llm_single_call_min_batch_items": args.llm_single_call_min_batch_items,
        "hybrid_max_segments": args.hybrid_max_segments,
        "hybrid_min_chars": args.hybrid_min_chars,
        "hybrid_min_score": args.hybrid_min_score,
        "hybrid_workers": args.hybrid_workers,
        "llm": {
            "base_url": args.lmstudio_base_url,
            "model": args.lmstudio_model,
            "timeout": args.lmstudio_timeout,
            "max_tokens": args.lmstudio_max_tokens,
            "temperature": args.lmstudio_temperature,
        },
    }

    run_pipeline(args.source_pdf, args.output_pdf, translate_options=translate_options)
