from jpdf import normalize_for_translation
from jpdf import parse_target_pages
from jpdf import should_merge_blocks


def _block(text, page=0, x0=10, y0=10, x1=100, y1=20, size=12, font="A"):
    return {
        "text": text,
        "page": page,
        "bbox": [x0, y0, x1, y1],
        "x0": x0,
        "y0": y0,
        "x1": x1,
        "y1": y1,
        "size": size,
        "font": font,
    }


def test_parse_target_pages_handles_ranges_and_invalid_tokens():
    assert parse_target_pages("0,2-4, 8-7, x, -1") == [0, 2, 3, 4, 7, 8]


def test_parse_target_pages_returns_none_for_empty_or_invalid():
    assert parse_target_pages("") is None
    assert parse_target_pages("abc, -") is None


def test_normalize_for_translation_preserves_paragraph_break_and_restores_hyphenation():
    raw = "impor-\ntant line\r\nsecond\n\nthird"
    normalized = normalize_for_translation(raw, preserve_paragraph_break=True)
    assert normalized == "important line second\n\nthird"


def test_should_merge_blocks_merges_mid_sentence_paragraphs():
    prev_block = _block("this sentence continues", y1=20)
    next_block = _block("And next line", y0=24, y1=32)
    merged, reason = should_merge_blocks(prev_block, next_block, "paragraph", "paragraph")
    assert merged is True
    assert reason == "mid_sentence"


def test_should_merge_blocks_rejects_cross_page():
    prev_block = _block("line one", page=0)
    next_block = _block("line two", page=1)
    merged, reason = should_merge_blocks(prev_block, next_block, "paragraph", "paragraph")
    assert merged is False
    assert reason == "cross_page"
