from extract import _paragraph_groups_from_lines
from generate import normalize_render_text


def _line(text, x0=10, y0=10, x1=100, y1=20):
    return {
        "bbox": [x0, y0, x1, y1],
        "spans": [{"text": text}],
    }


def test_paragraph_groups_split_on_indent_after_sentence_end():
    lines = [
        _line("First sentence."),
        _line("Indented next paragraph", x0=40, y0=22, x1=200, y1=30),
    ]
    groups = _paragraph_groups_from_lines(lines)
    assert len(groups) == 2


def test_paragraph_groups_keep_bullet_followup_together():
    lines = [
        _line("•"),
        _line("Indented list item", x0=40, y0=22, x1=200, y1=30),
    ]
    groups = _paragraph_groups_from_lines(lines)
    assert len(groups) == 1


def test_normalize_render_text_for_list_mode_splits_items():
    text = "1. one 2. two"
    normalized = normalize_render_text(text, segment_kind="list")
    assert normalized == "1. one\n2. two"


def test_normalize_render_text_for_paragraph_removes_cjk_space():
    text = "日本 語"
    normalized = normalize_render_text(text, segment_kind="paragraph")
    assert normalized == "日本語"
