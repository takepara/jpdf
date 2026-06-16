import sys
import os
from jpdf import translate_blocks

CLI_REQUIRED_ARG_COUNT = 3
CLI_INPUT_JSON_INDEX = 1
CLI_OUTPUT_JSON_INDEX = 2
CLI_ERROR_EXIT_CODE = 1

# 単体翻訳スクリプトの既定値。環境変数未設定時の安全なフォールバックとして使う。
DEFAULT_LLM_WORKERS = 1
DEFAULT_LLM_TRANSLATE_MODE = "page"
DEFAULT_LLM_PAGE_MAX_CHARS = 70000
DEFAULT_LLM_PAGE_RETRIES = 1
DEFAULT_LLM_BASE_URL = "http://127.0.0.1:1234/v1"
DEFAULT_LLM_MODEL = "translategemma-4b-it"
DEFAULT_LLM_TIMEOUT = 60
DEFAULT_LLM_MAX_TOKENS = 4096
DEFAULT_LLM_TEMPERATURE = 0.2
DEFAULT_LLM_REPETITION_PENALTY = 1.0

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
    if len(sys.argv) < CLI_REQUIRED_ARG_COUNT:
        print("Usage: python translate_json_only.py <input_json> <output_json>")
        sys.exit(CLI_ERROR_EXIT_CODE)

    input_json = sys.argv[CLI_INPUT_JSON_INDEX]
    output_json = sys.argv[CLI_OUTPUT_JSON_INDEX]

    llm_options = {
        "engine": "llm",
        "llm_max_workers": get_env_or_legacy("LLM_MAX_WORKERS", "LMSTUDIO_MAX_WORKERS", DEFAULT_LLM_WORKERS, int),
        "max_workers": get_env_or_legacy("LLM_MAX_WORKERS", "LMSTUDIO_MAX_WORKERS", DEFAULT_LLM_WORKERS, int),
        "llm_translate_mode": get_env_or_default("LLM_TRANSLATE_MODE", DEFAULT_LLM_TRANSLATE_MODE),
        "llm_page_max_chars": get_env_or_default("LLM_PAGE_MAX_CHARS", DEFAULT_LLM_PAGE_MAX_CHARS, int),
        "llm_page_retries": get_env_or_default("LLM_PAGE_RETRIES", DEFAULT_LLM_PAGE_RETRIES, int),
        "llm": {
            "base_url": get_env_or_legacy("LLM_BASE_URL", "LMSTUDIO_BASE_URL", DEFAULT_LLM_BASE_URL),
            "model": get_env_or_legacy("LLM_MODEL", "LMSTUDIO_MODEL", DEFAULT_LLM_MODEL),
            "timeout": get_env_or_legacy("LLM_TIMEOUT", "LMSTUDIO_TIMEOUT", DEFAULT_LLM_TIMEOUT, int),
            "max_tokens": get_env_or_legacy("LLM_MAX_TOKENS", "LMSTUDIO_MAX_TOKENS", DEFAULT_LLM_MAX_TOKENS, int),
            "temperature": get_env_or_legacy("LLM_TEMPERATURE", "LMSTUDIO_TEMPERATURE", DEFAULT_LLM_TEMPERATURE, float),
            "repetition_penalty": get_env_or_legacy("LLM_REPETITION_PENALTY", "LMSTUDIO_REPETITION_PENALTY", DEFAULT_LLM_REPETITION_PENALTY, float),
        },
    }

    print(f"Translating {input_json} → {output_json} ...")
    translate_blocks(input_json, output_json, llm_options)
    print("Done.")
