import sys
import os
from auto_translate import translate_blocks

def get_env_or_default(key, default=None, cast_func=None):
    val = os.environ.get(key)
    if val is not None and cast_func is not None:
        try:
            return cast_func(val)
        except Exception:
            return default
    return val if val is not None else default

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
        "max_workers": get_env_or_default("LMSTUDIO_MAX_WORKERS", 1, int),
        "llm_translate_mode": get_env_or_default("LLM_TRANSLATE_MODE", "page"),
        "llm_page_max_chars": get_env_or_default("LLM_PAGE_MAX_CHARS", 70000, int),
        "llm_page_retries": get_env_or_default("LLM_PAGE_RETRIES", 1, int),
        "llm": {
            "base_url": get_env_or_default("LMSTUDIO_BASE_URL", "http://127.0.0.1:1234/v1"),
            "model": get_env_or_default("LMSTUDIO_MODEL", "translategemma-4b-it"),
            "timeout": get_env_or_default("LMSTUDIO_TIMEOUT", 60, int),
            "max_tokens": get_env_or_default("LMSTUDIO_MAX_TOKENS", 4096, int),
            "temperature": get_env_or_default("LMSTUDIO_TEMPERATURE", 0.2, float),
        },
    }

    print(f"Translating {input_json} → {output_json} ...")
    translate_blocks(input_json, output_json, llm_options)
    print("Done.")
