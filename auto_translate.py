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

    if engine != "llm":
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

            previous = os.environ.get(key)
            os.environ[key] = value
            if previous != value:
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


def parse_target_pages(value):
    raw = str(value or "").strip()
    if not raw:
        return None

    pages = set()
    for token in raw.split(","):
        part = token.strip()
        if not part:
            continue
        if "-" in part:
            left, right = part.split("-", 1)
            try:
                start = int(left.strip())
                end = int(right.strip())
            except ValueError:
                continue
            if end < start:
                start, end = end, start
            for page in range(start, end + 1):
                if page >= 0:
                    pages.add(page)
            continue
        try:
            page = int(part)
        except ValueError:
            continue
        if page >= 0:
            pages.add(page)

    if not pages:
        return None
    return sorted(pages)


def normalize_text(text):
    return re.sub(r"\s+", " ", text).strip()


def has_unwanted_english_fragment(text):
    cleaned = normalize_text(text or "")
    if not cleaned:
        return False
    if re.fullmatch(r"(?:https?://\S+|www\.\S+|\S+@\S+)", cleaned):
        return False
    if re.search(r"\b[A-Za-z]{4,}\b", cleaned):
        return True
    return False


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
    marker_rule = "__PDFTRANSLATE_BLOCK_0__ のようなマーカー行は変更せず、必ず単独行のまま保持してください。" if keep_markers else ""
    return (
        "以下の英語テキストを、正確で自然な日本語に翻訳してください。 "
        "これは厳密な翻訳であり、リライトではありません。意味、因果関係、確率・断定の強さ、事実のニュアンスを厳密に保持してください。 "
        "すべての文を完全に翻訳し、要約・省略・追加・弱化を行ってはいけません。 "
        "出力に英語の文や節を残してはいけません。また英語文を別の英語表現に言い換えることも禁止です。 "
        "そのまま残してよいラテン文字は、URL・メールアドレス・型番・国際的に固定された製品名/ブランド名のみです。 "
        "見出し、図表ラベル、短い断片、箇条書きも例外なく日本語にしてください。 "
        "組織名・人名は一般的な日本語表記を優先し、ない場合はカタカナ化し、必要なら原文を1回だけ括弧で補足してください。 "
        "英語フレーズが残っていたらエラーです。最終出力前に必ず日本語へ直してください。 "
        "内部手順: まず全体を翻訳し、その後に最終文面を自己検査してください。 "
        "自己検査では、A-Z/a-z の連続語を確認し、許可例外（URL・メール・型番・国際固定の製品名/ブランド名）以外は必ず日本語へ再翻訳してください。 "
        "特に長い英語句（3語以上）が1つでも残っていたら不合格として再翻訳してください。 "
        "出力は最終的な日本語訳テキストのみを返してください。 "
        f"{marker_rule}\n\n"
        f"原文:\n{text}"
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
        "あなたはビジネス文書の英日翻訳者です。 "
        "各 item について、意味と全情報を保持した厳密な日本語訳を t に作成してください。 "
        "要約・省略・追加・解釈変更は禁止です。因果関係、確率表現、断定/否定を厳密に保持してください。 "
        "英語を英語のまま言い換えることは禁止です。原文の英語文は必ず日本語へ翻訳してください。 "
        "t に英語の文や節を残してはいけません。例外として、URL・メールアドレス・型番・国際的に固定された製品名/ブランド名のみそのまま可。 "
        "見出し、表のラベル、図中の短い文字列、箇条書きも必ず日本語化してください。 "
        "組織名・人名は一般的な日本語表記を優先し、ない場合はカタカナ化し、必要なら原文を1回だけ括弧で補足してください。 "
        "最終チェック: すべての t が日本語として完結し、英語文断片が残っていないことを確認してください。 "
        "各 item ごとに自己検査を実施し、A-Z/a-z の連続語を検出したら、許可例外以外は必ず日本語に直してください。 "
        "特に3語以上の英語句が t に残っていたら、その item は不合格として再翻訳してください。 "
        "id ごとに完全翻訳を保証し、1件でも不合格 item がある状態で出力してはいけません。 "
        "必ず次の形式の正しい JSON のみを返してください: "
        '{"items":[{"id":"<same id>","t":"<translated text>"}]} . '
        "説明文、Markdownフェンス、余計なキーは出力してはいけません。 "
        "id は入力と完全一致させ、入力1件につき出力1件を返してください。\n\n"
        f"入力JSON:\n{payload_json}"
    )


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


def structured_items_response_format():
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "translation_items",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["items"],
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["id", "t"],
                            "properties": {
                                "id": {"type": "string"},
                                "t": {"type": "string"},
                            },
                        },
                    }
                },
            },
        },
    }


def _coerce_message_content_to_text(content):
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts = []
        for part in content:
            if not isinstance(part, dict):
                continue
            for key in ("text", "content", "value"):
                value = part.get(key)
                if isinstance(value, str) and value:
                    parts.append(value)
                    break
        return "".join(parts)

    if isinstance(content, dict):
        for key in ("text", "content", "value"):
            value = content.get(key)
            if isinstance(value, str) and value:
                return value

    return ""


def _extract_choice_text(first_choice):
    if not isinstance(first_choice, dict):
        return "", "none"

    message = first_choice.get("message") if isinstance(first_choice, dict) else None
    if isinstance(message, dict):
        content_text = _coerce_message_content_to_text(message.get("content"))
        if content_text.strip():
            return content_text, "message.content"

        # Qwen系などで本文が reasoning_content 側に入る場合にフォールバック。
        for key in ("output_text", "text", "reasoning_content"):
            value = message.get(key)
            if isinstance(value, str) and value.strip():
                return value, f"message.{key}"

    for key in ("text", "output_text", "reasoning_content"):
        value = first_choice.get(key)
        if isinstance(value, str) and value.strip():
            return value, f"choice.{key}"

    return "", "none"


def lmstudio_complete(
    base_url,
    model,
    prompt,
    temperature=0.2,
    timeout=120,
    max_tokens=4096,
    log_label="",
    response_format=None,
    allow_response_format_fallback=True,
):
    call_seq = next_llm_call_seq()
    label = f" {log_label}" if log_label else ""
    started_at = time.time()
    prompt_chars = len(prompt)
    log_info(f"[LLM call {call_seq}{label}] start prompt_chars={prompt_chars}")

    url = f"{base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "source_lang_code": "en",
                        "target_lang_code": "ja",
                        "text": prompt,
                        "image": None,
                    }
                ],
            }
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "thinking": {"type": "disabled"},
        "enable_thinking": False,
    }
    if response_format is not None:
        payload["response_format"] = response_format
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

        if response_format is not None and allow_response_format_fallback and status in (400, 404, 422):
            log_warn(
                f"[LLM call {call_seq}{label}] response_format rejected by server "
                f"(HTTP {status}); retrying without response_format"
            )
            return lmstudio_complete(
                base_url=base_url,
                model=model,
                prompt=prompt,
                temperature=temperature,
                timeout=timeout,
                max_tokens=max_tokens,
                log_label=log_label,
                response_format=None,
                allow_response_format_fallback=False,
            )

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

    first_choice = choices[0] if isinstance(choices[0], dict) else {}
    extracted_text, extracted_field = _extract_choice_text(first_choice)
    text = extracted_text.strip()
    elapsed = time.time() - started_at
    log_info(
        f"[LLM call {call_seq}{label}] done in {elapsed:.1f}s "
        f"response_chars={len(text)} source_field={extracted_field}"
    )
    record_llm_metrics(success=True, elapsed=elapsed, prompt_chars=prompt_chars, completion_chars=len(text), usage=usage)
    return text


def strip_json_fences(text):
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\\s*```$", "", cleaned)
    return cleaned.strip()


def extract_first_json_object(text):
    raw = text or ""
    start = raw.find("{")
    if start < 0:
        return None

    depth = 0
    in_string = False
    escaped = False
    for idx in range(start, len(raw)):
        ch = raw[idx]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            depth += 1
            continue
        if ch == "}":
            depth -= 1
            if depth == 0:
                return raw[start : idx + 1]

    return None


def normalize_structured_items(parsed):
    if not isinstance(parsed, dict):
        return None
    items = parsed.get("items")
    if not isinstance(items, list):
        return None

    normalized_items = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = item.get("id")
        text = item.get("t")
        if not isinstance(item_id, str) or not isinstance(text, str):
            continue
        item_id = item_id.strip()
        text = text.strip()
        if not item_id or not text:
            continue
        normalized_items.append({"id": item_id, "t": text})

    return {"items": normalized_items}


def parse_structured_translation_response(response_text):
    cleaned = strip_json_fences(response_text)
    if not cleaned:
        return None

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        first_obj = extract_first_json_object(cleaned)
        if not first_obj:
            return None
        try:
            parsed = json.loads(first_obj)
        except json.JSONDecodeError:
            return None

    return normalize_structured_items(parsed)


def build_json_repair_prompt(raw_response_text, expected_items_count):
    cleaned = (raw_response_text or "").strip()
    return (
        "Convert the following model output into strict valid JSON only. "
        "Return exactly one JSON object with shape: "
        '{"items":[{"id":"<same id>","t":"<translated text>"}]}. '
        "Do not add explanations, markdown, or extra keys. "
        f"Expected number of items: {expected_items_count}.\n\n"
        "MODEL_OUTPUT:\n"
        f"{cleaned}"
    )


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
                "kind": classify_block_kind(block),
            }
        )
    return items


def build_structured_request_payload(items):
    payload_items = [{"id": item.get("id", ""), "t": item.get("t", "")} for item in items]
    input_chars = sum(len(item["t"]) for item in payload_items)
    payload_json = json.dumps({"items": payload_items}, ensure_ascii=False, separators=(",", ":"))
    prompt = build_structured_translate_prompt(payload_json)
    prompt_chars = len(prompt)
    return {
        "input_chars": input_chars,
        "payload_json": payload_json,
        "payload_chars": len(payload_json),
        "prompt": prompt,
        "prompt_chars": prompt_chars,
        "estimated_prompt_tokens": _estimate_tokens_from_chars(prompt_chars),
    }


def translate_structured_items_once(items, llm_options, batch_label="", request_meta=None, allow_text_repair=True):
    request_payload = build_structured_request_payload(items)
    input_chars = request_payload["input_chars"]
    prompt = request_payload["prompt"]
    effective_meta = {
        "mode": "document_batch",
        "strategy": "structured_json",
        "kind": "mixed",
        "items": len(items),
        "chars": input_chars,
    }
    if isinstance(request_meta, dict):
        effective_meta.update({key: value for key, value in request_meta.items() if value is not None})
    request_label = format_request_log_label(effective_meta)
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
                response_format=structured_items_response_format(),
            )
            if response_text:
                break
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, Exception):
            time.sleep(0.5)

    if not response_text:
        return None, {"reason": "empty_response", "input_chars": input_chars, "items": len(items)}

    parsed = parse_structured_translation_response(response_text)
    if not parsed:
        repair_prompt = build_json_repair_prompt(response_text, expected_items_count=len(items))
        repaired_response = None
        try:
            repaired_response = lmstudio_complete(
                base_url=llm_options["base_url"],
                model=llm_options["model"],
                prompt=repair_prompt,
                temperature=min(0.1, float(llm_options.get("temperature", 0.2))),
                timeout=llm_options["timeout"],
                max_tokens=llm_options["max_tokens"],
                log_label=f"{combined_label} repair_json",
                response_format=structured_items_response_format(),
            )
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, Exception):
            repaired_response = None

        parsed = parse_structured_translation_response(repaired_response)
        if not parsed:
            return None, {
                "reason": "invalid_json_response",
                "input_chars": input_chars,
                "items": len(items),
                "response_preview": (response_text or "")[:400],
            }

    source_items_by_index = list(items)
    out_items = parsed.get("items", [])
    translated_by_id = {}
    output_texts_in_order = []
    for out_item in out_items:
        if not isinstance(out_item, dict):
            continue
        item_id = out_item.get("id")
        translated_text = (out_item.get("t") or "").strip()
        if not translated_text:
            continue
        output_texts_in_order.append(translated_text)
        if not item_id:
            continue
        if item_id in translated_by_id:
            log_warn(f"duplicate_id_detected (continuing): item_id={item_id}")
            continue
        translated_by_id[item_id] = translated_text

    expected_ids = {item["id"] for item in items}
    actual_ids = set(translated_by_id.keys())
    had_id_mismatch = expected_ids != actual_ids
    missing_count = 0
    extra_count = 0
    if had_id_mismatch:
        missing_ids = list(expected_ids - actual_ids)
        extra_ids = list(actual_ids - expected_ids)
        missing_count = len(missing_ids)
        extra_count = len(extra_ids)
        log_warn(
            "id_mismatch_detected (continuing): "
            f"missing={missing_count} extra={extra_count}"
        )

        # Fallback 1: remap by position when model returned same count but wrong IDs.
        if len(output_texts_in_order) == len(source_items_by_index):
            remapped = {}
            for idx, src_item in enumerate(source_items_by_index):
                remapped[src_item["id"]] = output_texts_in_order[idx]
            translated_by_id = remapped
            actual_ids = set(translated_by_id.keys())

    if allow_text_repair:
        suspect_items = [item for item in items if has_unwanted_english_fragment(translated_by_id.get(item["id"], ""))]
        if suspect_items:
            repaired_by_id = dict(translated_by_id)
            repaired_count = 0
            for suspect_rank, item in enumerate(suspect_items, start=1):
                suspect_map, suspect_meta = translate_structured_items_once(
                    [item],
                    llm_options,
                    batch_label=f"{combined_label} quality_repair={suspect_rank}",
                    request_meta={
                        "mode": "quality_repair",
                        "strategy": "structured_json",
                        "kind": item.get("kind", "unknown"),
                        "items": 1,
                        "chars": len(item.get("t", "")),
                        "page": effective_meta.get("page"),
                        "block_idx": effective_meta.get("block_idx"),
                    },
                    allow_text_repair=False,
                )
                suspect_text = ""
                if isinstance(suspect_map, dict):
                    suspect_text = (suspect_map.get(item["id"]) or "").strip()
                if suspect_text:
                    repaired_by_id[item["id"]] = suspect_text
                    repaired_count += 1

            if repaired_count > 0:
                translated_by_id = repaired_by_id
                actual_ids = set(translated_by_id.keys())

    return translated_by_id, {
        "reason": "ok_with_warnings" if had_id_mismatch else "ok",
        "input_chars": input_chars,
        "items": len(items),
        "response_chars": len(response_text),
        "id_mismatch_warning": had_id_mismatch,
        "id_mismatch_missing": missing_count,
        "id_mismatch_extra": extra_count,
    }


def build_structured_translation_items_by_page(data):
    ordered_blocks = sorted(data, key=lambda block: (block["page"], block.get("reading_order", block["block_idx"])))
    page_map = {}
    for block in ordered_blocks:
        text = block.get("text", "")
        if is_page_number_or_code(text):
            continue
        normalized = normalize_for_translation(text, preserve_paragraph_break=True)
        if not normalized:
            continue

        page = block["page"]
        page_items = page_map.setdefault(page, [])
        page_items.append(
            {
                "id": f"p{block['page']}_b{block['block_idx']}",
                "t": normalized,
                "kind": classify_block_kind(block),
            }
        )

    return [(page, page_map[page]) for page in sorted(page_map.keys())]


def collect_page_request_stats(data, llm_options, max_input_chars=70000):
    page_items = build_structured_translation_items_by_page(data)
    max_tokens = int(llm_options.get("max_tokens", 4096))

    stats = []
    for page, items in page_items:
        request_payload = build_structured_request_payload(items)
        prompt_tokens = request_payload["estimated_prompt_tokens"]
        estimated_total_tokens = prompt_tokens + max_tokens
        stats.append(
            {
                "page": page,
                "items": len(items),
                "input_chars": request_payload["input_chars"],
                "payload_chars": request_payload["payload_chars"],
                "prompt_chars": request_payload["prompt_chars"],
                "estimated_prompt_tokens": prompt_tokens,
                "max_output_tokens": max_tokens,
                "estimated_total_tokens": estimated_total_tokens,
                "over_input_char_limit": request_payload["input_chars"] > max_input_chars,
                "max_input_chars": max_input_chars,
            }
        )

    return stats


def build_page_failure_message(page_info, llm_options):
    info = page_info if isinstance(page_info, dict) else {}
    reason = info.get("reason", "unknown")
    page = info.get("page")
    max_input_chars = int(info.get("max_input_chars", 0) or 0)
    configured_max_tokens = int(llm_options.get("max_tokens", 0) or 0)

    lines = ["Page-wise LLM translation failed and fallback is disabled."]
    lines.append(f"reason={reason}")
    if page is not None:
        lines.append(f"page={page}")

    if reason == "page_too_large":
        lines.append(f"input_chars={info.get('input_chars')} max_input_chars={max_input_chars}")
        lines.append("resolution:")
        lines.append(f"- Lower per-page payload size: set LLM_PAGE_MAX_CHARS below {max_input_chars} and retry.")
        lines.append("- Enable page-internal split implementation (split one page into multiple API calls).")
        if configured_max_tokens > 0:
            lines.append(f"- Reduce LMSTUDIO_MAX_TOKENS from {configured_max_tokens} to 1024 or 768.")
    elif reason == "page_batch_failed":
        meta = info.get("meta") if isinstance(info.get("meta"), dict) else {}
        lines.append(f"api_meta_reason={meta.get('reason', 'unknown')}")
        if "input_chars" in meta:
            lines.append(f"input_chars={meta.get('input_chars')}")
        if "items" in meta:
            lines.append(f"items={meta.get('items')}")
        lines.append("resolution:")
        lines.append("- Keep page mode and retry with smaller payload: LLM_PAGE_MAX_CHARS=12000 (or 8000).")
        if configured_max_tokens > 0:
            lines.append(f"- Reduce LMSTUDIO_MAX_TOKENS from {configured_max_tokens} to 1024 or 768.")
        lines.append("- Confirm LM Studio server model/context settings for API endpoint /v1/chat/completions.")
    else:
        lines.append("resolution:")
        lines.append("- Retry with LLM_PAGE_MAX_CHARS=12000 and LMSTUDIO_MAX_TOKENS=1024.")
        lines.append("- Check LM Studio server logs for request rejection details.")

    return "\n".join(lines)


def translate_blocks_llm_page_mode(
    data,
    llm_options,
    max_input_chars=70000,
    max_workers=1,
    page_retry_count=1,
):
    page_items = build_structured_translation_items_by_page(data)
    if not page_items:
        return None, {"used": False, "reason": "no_items"}

    translated_by_id = {}
    page_summaries = []
    max_output_tokens = int(llm_options.get("max_tokens", 4096))

    prepared_pages = []
    for page, items in page_items:
        request_payload = build_structured_request_payload(items)
        input_chars = request_payload["input_chars"]
        log_info(
            "[LLM page] "
            f"page={page} items={len(items)} "
            f"input_chars={input_chars} payload_chars={request_payload['payload_chars']} "
            f"prompt_chars={request_payload['prompt_chars']} est_prompt_tokens={request_payload['estimated_prompt_tokens']} "
            f"max_output_tokens={max_output_tokens} est_total_tokens={request_payload['estimated_prompt_tokens'] + max_output_tokens}"
        )
        if input_chars > max_input_chars:
            return None, {
                "used": False,
                "reason": "page_too_large",
                "page": page,
                "input_chars": input_chars,
                "max_input_chars": max_input_chars,
                "items": len(items),
            }
        prepared_pages.append(
            {
                "page": page,
                "items": items,
                "request_payload": request_payload,
                "input_chars": input_chars,
            }
        )

    max_workers = max(1, int(max_workers))
    if max_workers > 1:
        log_info(f"[LLM page] parallel mode enabled workers={max_workers}")

    def process_page(prepared):
        page = prepared["page"]
        items = prepared["items"]
        input_chars = prepared["input_chars"]

        max_attempts = max(1, int(page_retry_count) + 1)
        max_split_level = 2  # full page -> half -> quarter

        def split_items_half(chunk_items):
            if len(chunk_items) <= 1:
                return [chunk_items]
            mid = max(1, len(chunk_items) // 2)
            left = chunk_items[:mid]
            right = chunk_items[mid:]
            if not right:
                return [left]
            return [left, right]

        def source_fallback(chunk_items, existing_map=None):
            mapped = dict(existing_map or {})
            source_fallback_count = 0
            for item in chunk_items:
                item_id = item["id"]
                if (mapped.get(item_id) or "").strip():
                    continue
                mapped[item_id] = item["t"]
                source_fallback_count += 1
            return mapped, {
                "calls": 0,
                "retries": 0,
                "response_chars": 0,
                "source_fallback_blocks": source_fallback_count,
                "source_fallback_ids": [item["id"] for item in chunk_items if not (existing_map or {}).get(item["id"])],
                "max_split_level": 0,
                "reason": "source_fallback_after_split",
            }

        def translate_missing_item(item, missing_rank):
            translated_map, meta = translate_structured_items_once(
                [item],
                llm_options,
                batch_label=(
                    f"page={page} missing_item={missing_rank} chars={len(item.get('t', ''))} split=single"
                ),
                request_meta={
                    "mode": "page_item_fallback",
                    "strategy": "structured_json",
                    "kind": item.get("kind", "unknown"),
                    "page": page,
                    "items": 1,
                    "chars": len(item.get("t", "")),
                    "split_level": "single",
                },
            )
            if isinstance(translated_map, dict):
                return translated_map.get(item["id"], ""), meta
            return "", meta

        def merge_stats(base_stats, child_stats):
            base_stats["calls"] += int(child_stats.get("calls", 0))
            base_stats["retries"] += int(child_stats.get("retries", 0))
            base_stats["source_fallback_blocks"] += int(child_stats.get("source_fallback_blocks", 0))
            base_stats["source_fallback_ids"].extend(child_stats.get("source_fallback_ids", []))
            base_stats["response_chars"] += int(child_stats.get("response_chars", 0))
            base_stats["max_split_level"] = max(
                int(base_stats.get("max_split_level", 0)),
                int(child_stats.get("max_split_level", 0)),
            )

        def translate_chunk_adaptive(chunk_items, split_level):
            last_meta = {"reason": "unknown"}
            retry_used = 0
            translated_chunk = None
            response_chars_sum = 0

            for attempt in range(1, max_attempts + 1):
                translated_chunk, meta = translate_structured_items_once(
                    chunk_items,
                    llm_options,
                    batch_label=(
                        f"page={page} items={len(chunk_items)} chars={sum(len(item.get('t', '')) for item in chunk_items)} "
                        f"split={split_level}"
                    ),
                    request_meta={
                        "mode": "page_batch",
                        "strategy": "structured_json",
                        "kind": "mixed",
                        "page": page,
                        "items": len(chunk_items),
                        "chars": sum(len(item.get("t", "")) for item in chunk_items),
                        "split_level": split_level,
                    },
                )

                meta = meta if isinstance(meta, dict) else {"reason": "unknown"}
                last_meta = meta
                response_chars_sum += int(meta.get("response_chars", 0))

                if translated_chunk is not None:
                    break

                if attempt < max_attempts:
                    retry_used += 1
                    reason = meta.get("reason", "unknown")
                    log_warn(
                        f"[LLM page] retry page={page} split={split_level} "
                        f"attempt={attempt}/{max_attempts} reason={reason}"
                    )

            if translated_chunk is not None:
                missing_items = [item for item in chunk_items if not (translated_chunk.get(item["id"]) or "").strip()]
                if missing_items:
                    repaired = dict(translated_chunk)
                    missing_fallback_count = 0
                    for missing_rank, item in enumerate(missing_items, start=1):
                        repaired_text, _ = translate_missing_item(item, missing_rank)
                        if repaired_text.strip():
                            repaired[item["id"]] = repaired_text.strip()
                        missing_fallback_count += 1

                    translated_chunk = repaired
                    if all((repaired.get(item["id"]) or "").strip() for item in chunk_items):
                        return repaired, {
                            "calls": 1 + missing_fallback_count,
                            "retries": retry_used,
                            "source_fallback_blocks": 0,
                            "source_fallback_ids": [],
                            "response_chars": response_chars_sum,
                            "max_split_level": split_level,
                            "reason": "ok_with_item_fallback" if missing_fallback_count > 0 else "ok",
                        }

                if all((translated_chunk.get(item["id"]) or "").strip() for item in chunk_items):
                    return translated_chunk, {
                        "calls": 1,
                        "retries": retry_used,
                        "source_fallback_blocks": 0,
                        "source_fallback_ids": [],
                        "response_chars": response_chars_sum,
                        "max_split_level": split_level,
                        "reason": "ok",
                    }

                # If some items are still missing after per-item fallback, continue splitting.
                translated_chunk = {
                    item["id"]: (translated_chunk.get(item["id"]) or "")
                    for item in chunk_items
                    if (translated_chunk.get(item["id"]) or "").strip()
                }

            if split_level < max_split_level and len(chunk_items) > 1:
                log_warn(
                    f"[LLM page] split page={page} level={split_level}->{split_level + 1} "
                    f"items={len(chunk_items)} reason={last_meta.get('reason', 'unknown')}"
                )
                merged = {}
                stats = {
                    "calls": 0,
                    "retries": retry_used,
                    "source_fallback_blocks": 0,
                    "source_fallback_ids": [],
                    "response_chars": response_chars_sum,
                    "max_split_level": split_level,
                    "reason": "split",
                }
                for part in split_items_half(chunk_items):
                    part_result, part_stats = translate_chunk_adaptive(part, split_level + 1)
                    merged.update(part_result)
                    merge_stats(stats, part_stats)
                stats["calls"] += 1
                stats["reason"] = "ok_with_split"
                return merged, stats

            log_warn(
                f"[LLM page] source_fallback page={page} split={split_level} items={len(chunk_items)}"
            )
            fallback_result, fallback_stats = source_fallback(chunk_items, translated_chunk)
            fallback_stats["calls"] += 1
            fallback_stats["retries"] += retry_used
            fallback_stats["response_chars"] += response_chars_sum
            fallback_stats["max_split_level"] = split_level
            return fallback_result, fallback_stats

        translated_chunk, adaptive_stats = translate_chunk_adaptive(items, split_level=0)
        meta = {
            "reason": "ok_with_warnings" if adaptive_stats.get("source_fallback_blocks", 0) > 0 else "ok",
            "attempts": int(adaptive_stats.get("retries", 0)) + 1,
            "retry_count": int(adaptive_stats.get("retries", 0)),
            "source_fallback_blocks": int(adaptive_stats.get("source_fallback_blocks", 0)),
            "source_fallback_ids": adaptive_stats.get("source_fallback_ids", []),
            "response_chars": int(adaptive_stats.get("response_chars", 0)),
            "max_split_level": int(adaptive_stats.get("max_split_level", 0)),
            "api_calls": int(adaptive_stats.get("calls", 0)),
        }
        return page, translated_chunk, meta

    if max_workers == 1:
        for prepared in prepared_pages:
            page, translated_chunk, meta = process_page(prepared)
            if translated_chunk is None:
                return None, {
                    "used": False,
                    "reason": "page_batch_failed",
                    "page": page,
                    "meta": meta,
                    "pages_done": len(page_summaries),
                    "pages_total": len(prepared_pages),
                }

            translated_by_id.update(translated_chunk)
            request_payload = prepared["request_payload"]
            page_summaries.append(
                {
                    "page": page,
                    "items": len(prepared["items"]),
                    "input_chars": prepared["input_chars"],
                    "payload_chars": request_payload["payload_chars"],
                    "prompt_chars": request_payload["prompt_chars"],
                    "estimated_prompt_tokens": request_payload["estimated_prompt_tokens"],
                    "max_output_tokens": max_output_tokens,
                    "estimated_total_tokens": request_payload["estimated_prompt_tokens"] + max_output_tokens,
                    "response_chars": int(meta.get("response_chars", 0)) if isinstance(meta, dict) else 0,
                    "retry_attempts": int(meta.get("attempts", 1)) if isinstance(meta, dict) else 1,
                    "retry_count": int(meta.get("retry_count", 0)) if isinstance(meta, dict) else 0,
                    "source_fallback_blocks": int(meta.get("source_fallback_blocks", 0)) if isinstance(meta, dict) else 0,
                    "max_split_level": int(meta.get("max_split_level", 0)) if isinstance(meta, dict) else 0,
                    "api_calls": int(meta.get("api_calls", 0)) if isinstance(meta, dict) else 0,
                }
            )
    else:
        prepared_by_page = {item["page"]: item for item in prepared_pages}
        success_map = {}
        failure_map = {}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(process_page, prepared): prepared["page"] for prepared in prepared_pages}
            for future in as_completed(futures):
                page = futures[future]
                try:
                    _, translated_chunk, meta = future.result()
                except Exception as exc:
                    translated_chunk, meta = None, {"reason": f"error:{exc}"}

                if translated_chunk is None:
                    failure_map[page] = meta
                else:
                    success_map[page] = (translated_chunk, meta)

        if failure_map:
            first_failed_page = sorted(failure_map.keys())[0]
            return None, {
                "used": False,
                "reason": "page_batch_failed",
                "page": first_failed_page,
                "meta": failure_map[first_failed_page],
                "pages_done": len(success_map),
                "pages_total": len(prepared_pages),
            }

        for page in sorted(success_map.keys()):
            translated_chunk, meta = success_map[page]
            prepared = prepared_by_page[page]
            request_payload = prepared["request_payload"]
            translated_by_id.update(translated_chunk)
            page_summaries.append(
                {
                    "page": page,
                    "items": len(prepared["items"]),
                    "input_chars": prepared["input_chars"],
                    "payload_chars": request_payload["payload_chars"],
                    "prompt_chars": request_payload["prompt_chars"],
                    "estimated_prompt_tokens": request_payload["estimated_prompt_tokens"],
                    "max_output_tokens": max_output_tokens,
                    "estimated_total_tokens": request_payload["estimated_prompt_tokens"] + max_output_tokens,
                    "response_chars": int(meta.get("response_chars", 0)) if isinstance(meta, dict) else 0,
                    "retry_attempts": int(meta.get("attempts", 1)) if isinstance(meta, dict) else 1,
                    "retry_count": int(meta.get("retry_count", 0)) if isinstance(meta, dict) else 0,
                    "source_fallback_blocks": int(meta.get("source_fallback_blocks", 0)) if isinstance(meta, dict) else 0,
                    "max_split_level": int(meta.get("max_split_level", 0)) if isinstance(meta, dict) else 0,
                    "api_calls": int(meta.get("api_calls", 0)) if isinstance(meta, dict) else 0,
                }
            )

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

    pages_with_retries = sum(1 for p in page_summaries if int(p.get("retry_count", 0)) > 0)
    source_fallback_blocks = sum(int(p.get("source_fallback_blocks", 0)) for p in page_summaries)

    return results, {
        "used": True,
        "reason": "ok_with_warnings" if source_fallback_blocks > 0 else "ok",
        "pages_total": len(page_items),
        "pages_translated": len(page_summaries),
        "page_summaries": page_summaries,
        "max_input_chars": max_input_chars,
        "page_retry_count": int(page_retry_count),
        "pages_with_retries": pages_with_retries,
        "source_fallback_blocks": source_fallback_blocks,
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

    target_pages = parse_target_pages(llm_options.get("llm_target_pages"))
    selected_data = data
    if target_pages:
        page_set = set(target_pages)
        selected_data = [block for block in data if int(block.get("page", -1)) in page_set]
        log_info(
            "Page filter enabled: "
            f"pages={target_pages} selected_blocks={len(selected_data)}/{len(data)}"
        )
        if not selected_data:
            raise RuntimeError(f"No blocks matched requested pages: {target_pages}")

    engine = llm_options.get("engine", "google")
    if engine == "llm":
        reset_llm_metrics()

    translation_segments = build_translation_segments(selected_data)
    engine_label = "LM Studio" if engine == "llm" else "Google Translate"
    llm_translate_mode = llm_options.get("llm_translate_mode", "page")
    llm_page_stats_only = bool(llm_options.get("llm_page_stats_only", False))

    llm_page_info = None
    llm_page_stats = None

    if engine == "llm" and llm_translate_mode == "page":
        if llm_page_stats_only:
            llm_page_stats = collect_page_request_stats(
                selected_data,
                llm_options=llm_options["llm"],
                max_input_chars=max(1000, int(llm_options.get("llm_page_max_chars", 70000))),
            )
            for stat in llm_page_stats:
                log_info(
                    "[LLM page stats] "
                    f"page={stat['page']} items={stat['items']} input_chars={stat['input_chars']} "
                    f"payload_chars={stat['payload_chars']} prompt_chars={stat['prompt_chars']} "
                    f"est_prompt_tokens={stat['estimated_prompt_tokens']} max_output_tokens={stat['max_output_tokens']} "
                    f"est_total_tokens={stat['estimated_total_tokens']} over_limit={stat['over_input_char_limit']}"
                )

            if debug_json_path:
                debug_payload = {
                    "segment_debug": [],
                    "engine": engine,
                    "workers": 1,
                    "llm_translate_mode": llm_translate_mode,
                    "llm_page_stats_only": True,
                    "llm_page_stats": llm_page_stats,
                    "llm_target_pages": target_pages,
                }
                with open(debug_json_path, "w", encoding="utf-8") as f:
                    json.dump(debug_payload, f, ensure_ascii=False, indent=2)

            return {
                "stats_only": True,
                "llm_page_stats": llm_page_stats,
            }

        log_info("Trying page-wise structured LLM translation...")
        page_results, llm_page_info = translate_blocks_llm_page_mode(
            selected_data,
            llm_options=llm_options["llm"],
            max_input_chars=max(1000, int(llm_options.get("llm_page_max_chars", 70000))),
            max_workers=max(1, int(llm_options.get("max_workers", 1))),
            page_retry_count=max(0, int(llm_options.get("llm_page_retries", 1))),
        )
        if page_results is not None:
            translated_by_key = {
                (block["page"], block["block_idx"]): block
                for block in page_results
            }
            merged_results = []
            for original_block in data:
                key = (original_block["page"], original_block["block_idx"])
                if key in translated_by_key:
                    merged_results.append(translated_by_key[key])
                else:
                    block_copy = original_block.copy()
                    original_text = original_block.get("text", "")
                    block_copy["original_text"] = original_text
                    block_copy["segment_kind"] = classify_block_kind(original_block)
                    merged_results.append(block_copy)

            translated_count = sum(
                1
                for block in merged_results
                if block.get("text") != block.get("original_text") and not is_page_number_or_code(block.get("original_text", ""))
            )
            with open(output_json, "w", encoding="utf-8") as f:
                json.dump(merged_results, f, ensure_ascii=False, indent=2)

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
                    "llm_translate_mode": llm_translate_mode,
                    "llm_page": llm_page_info,
                    "llm_target_pages": target_pages,
                }
                with open(debug_json_path, "w", encoding="utf-8") as f:
                    json.dump(debug_payload, f, ensure_ascii=False, indent=2)

            log_info(
                "Page-wise translation completed: "
                f"pages={llm_page_info.get('pages_translated', 0)}/{llm_page_info.get('pages_total', 0)}"
            )
            log_info(f"Completed translation! Saved to {output_json}. Total translated: {translated_count}")
            log_final_statistics(engine=engine, started_at=pipeline_started_at, translated_count=translated_count)
            return

        page_reason = llm_page_info.get("reason") if isinstance(llm_page_info, dict) else "unknown"
        error_message = build_page_failure_message(llm_page_info, llm_options.get("llm", {}))
        log_error(error_message)
        if debug_json_path:
            debug_payload = {
                "segment_debug": [],
                "engine": engine,
                "workers": 1,
                "llm_translate_mode": llm_translate_mode,
                "llm_page": llm_page_info,
                "llm_target_pages": target_pages,
                "failure": {
                    "stage": "page_mode",
                    "reason": page_reason,
                    "message": error_message,
                },
            }
            with open(debug_json_path, "w", encoding="utf-8") as f:
                json.dump(debug_payload, f, ensure_ascii=False, indent=2)
        raise RuntimeError(error_message)

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

    translated_blocks = []
    for translated_segment in segment_slots:
        translated_blocks.extend(translated_segment)

    results_by_key = {(block["page"], block["block_idx"]): block for block in translated_blocks}
    results = []
    for original_block in data:
        key = (original_block["page"], original_block["block_idx"])
        if key in results_by_key:
            block_copy = results_by_key[key]
        else:
            block_copy = original_block.copy()
            original_text = original_block.get("text", "")
            block_copy["original_text"] = original_text
            block_copy["segment_kind"] = classify_block_kind(original_block)
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
            "llm_translate_mode": llm_translate_mode,
            "llm_target_pages": target_pages,
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
        if llm_page_info is not None:
            debug_payload["llm_page"] = llm_page_info
        with open(debug_json_path, "w", encoding="utf-8") as f:
            json.dump(debug_payload, f, ensure_ascii=False, indent=2)

    log_info(f"Completed translation! Saved to {output_json}. Total translated: {translated_count}")
    log_final_statistics(engine=engine, started_at=pipeline_started_at, translated_count=translated_count)


def build_default_output_pdf(source_pdf, engine="google"):
    source_path = Path(source_pdf)
    if engine == "llm":
        suffix = "_LLM"
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

    engine_label = "LM Studio" if engine == "llm" else "Google Translate"
    log_info(f"[2/4] Translating blocks to Japanese with {engine_label}")
    translate_started_at = time.time()
    translate_result = translate_blocks(
        extracted_json,
        translated_json,
        llm_options=translate_options,
        debug_json_path=segment_debug_json,
    )
    translate_elapsed = time.time() - translate_started_at

    if translate_options.get("llm_page_stats_only", False):
        total_elapsed = time.time() - pipeline_started_at
        log_info("[3/4] Skipped PDF generation (stats-only mode).")
        log_info(
            "Stage time: "
            f"extract={format_elapsed(extract_elapsed)} "
            f"translate={format_elapsed(translate_elapsed)} "
            f"total={format_elapsed(total_elapsed)}"
        )
        if isinstance(translate_result, dict):
            stats = translate_result.get("llm_page_stats") or []
            first_page = stats[0] if stats else None
            if first_page:
                log_info(
                    "First page stats: "
                    f"page={first_page['page']} items={first_page['items']} input_chars={first_page['input_chars']} "
                    f"prompt_chars={first_page['prompt_chars']} est_prompt_tokens={first_page['estimated_prompt_tokens']} "
                    f"max_output_tokens={first_page['max_output_tokens']}"
                )
        return

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
    # Bootstrap parse to know source_pdf/env-file, then load .env before building
    # the main parser defaults from environment variables.
    bootstrap_parser = argparse.ArgumentParser(add_help=False)
    bootstrap_parser.add_argument("source_pdf", nargs="?")
    bootstrap_parser.add_argument("--env-file", default=None)
    bootstrap_args, _ = bootstrap_parser.parse_known_args()

    loaded_count, loaded_files = load_dotenv_candidates(
        source_pdf_path=bootstrap_args.source_pdf,
        explicit_env_file=bootstrap_args.env_file,
    )

    parser = argparse.ArgumentParser(
        description="Translate an English PDF into a Japanese PDF while preserving layout."
    )
    parser.add_argument("source_pdf", help="Path to source English PDF")
    parser.add_argument(
        "output_pdf",
        nargs="?",
        help="Path to output Japanese PDF (default by engine: _G / _LLM)",
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
        "--llm-translate-mode",
        choices=["page", "segment"],
        default=os.environ.get("LLM_TRANSLATE_MODE", "page"),
        help="LLM translation mode after optional single-call step: page (default) or segment",
    )
    parser.add_argument(
        "--llm-page-max-chars",
        type=int,
        default=int(os.environ.get("LLM_PAGE_MAX_CHARS", "70000")),
        help="Maximum total input characters per page request in page mode",
    )
    parser.add_argument(
        "--llm-page-retries",
        type=int,
        default=int(os.environ.get("LLM_PAGE_RETRIES", "1")),
        help="Retry count per page in LLM page mode when response is empty/invalid or warning-prone",
    )
    parser.add_argument(
        "--llm-target-pages",
        default=os.environ.get("LLM_TARGET_PAGES", ""),
        help="Optional target page list/range for debug runs (e.g. '0' or '0,2-3'). Pages are 0-based.",
    )
    parser.add_argument(
        "--llm-page-stats-only",
        action=argparse.BooleanOptionalAction,
        default=os.environ.get("LLM_PAGE_STATS_ONLY", "0") not in ("0", "false", "False"),
        help="Collect per-page LLM payload stats (chars/tokens estimates) without calling LM Studio API",
    )
    parser.add_argument(
        "--google-timeout",
        type=int,
        default=int(os.environ.get("GOOGLE_TIMEOUT", "30")),
        help="Google Translate HTTP timeout seconds",
    )
    parser.add_argument(
        "--env-file",
        default=bootstrap_args.env_file,
        help="Optional .env file path. By default, .env in cwd/script/source-pdf dir are auto-loaded.",
    )
    args = parser.parse_args()

    if loaded_count > 0:
        log_info(f"Loaded {loaded_count} env vars from: {', '.join(loaded_files)}")

    translate_options = {
        "engine": args.engine,
        "google_timeout": args.google_timeout,
        "max_workers": args.lmstudio_max_workers,
        "llm_translate_mode": args.llm_translate_mode,
        "llm_page_max_chars": args.llm_page_max_chars,
        "llm_page_retries": args.llm_page_retries,
        "llm_target_pages": args.llm_target_pages,
        "llm_page_stats_only": args.llm_page_stats_only,
        "llm": {
            "base_url": args.lmstudio_base_url,
            "model": args.lmstudio_model,
            "timeout": args.lmstudio_timeout,
            "max_tokens": args.lmstudio_max_tokens,
            "temperature": args.lmstudio_temperature,
        },
    }

    run_pipeline(args.source_pdf, args.output_pdf, translate_options=translate_options)
