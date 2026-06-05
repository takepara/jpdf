import sys
import os
from jpdf import translate_blocks

def get_env_or_default(key, default=None, cast_func=None):
    val = os.environ.get(key)
    if val is not None and cast_func is not None:
        try:
            return cast_func(val)
        except Exception:
            return default
    return val if val is not None else default


def get_env_or_legacy(new_key, legacy_key, default=None, cast_func=None):
    if os.environ.get(new_key) is not None:
        return get_env_or_default(new_key, default, cast_func)
    if os.environ.get(legacy_key) is not None:
        return get_env_or_default(legacy_key, default, cast_func)
    return default

from dotenv import load_dotenv
load_dotenv()

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python translate_json_only.py <input_json> <output_json>")
        sys.exit(1)

    input_json = sys.argv[1]
    output_json = sys.argv[2]

    llm_options = {
        "engine": "llm",
        "llm_max_workers": get_env_or_legacy("LLM_MAX_WORKERS", "LMSTUDIO_MAX_WORKERS", 1, int),
        "max_workers": get_env_or_legacy("LLM_MAX_WORKERS", "LMSTUDIO_MAX_WORKERS", 1, int),
        "llm_translate_mode": get_env_or_default("LLM_TRANSLATE_MODE", "page"),
        "llm_page_max_chars": get_env_or_default("LLM_PAGE_MAX_CHARS", 70000, int),
        "llm_page_retries": get_env_or_default("LLM_PAGE_RETRIES", 1, int),
        "llm": {
            "base_url": get_env_or_legacy("LLM_BASE_URL", "LMSTUDIO_BASE_URL", "http://127.0.0.1:1234/v1"),
            "model": get_env_or_legacy("LLM_MODEL", "LMSTUDIO_MODEL", "translategemma-4b-it"),
            "timeout": get_env_or_legacy("LLM_TIMEOUT", "LMSTUDIO_TIMEOUT", 60, int),
            "max_tokens": get_env_or_legacy("LLM_MAX_TOKENS", "LMSTUDIO_MAX_TOKENS", 4096, int),
            "temperature": get_env_or_legacy("LLM_TEMPERATURE", "LMSTUDIO_TEMPERATURE", 0.2, float),
            "repetition_penalty": get_env_or_legacy("LLM_REPETITION_PENALTY", "LMSTUDIO_REPETITION_PENALTY", 1.0, float),
        },
    }

    print(f"Translating {input_json} → {output_json} ...")
    translate_blocks(input_json, output_json, llm_options)
    print("Done.")
