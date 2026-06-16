from extract import _paragraph_groups_from_lines
from generate import (
    compute_column_target_font_sizes,
    group_body_paragraphs_by_column,
    is_body_paragraph_block,
    normalize_render_text,
)


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


def test_is_body_paragraph_block_requires_multi_line_body_like_text():
    page_rect = type("Rect", (), {"width": 600.0, "height": 800.0})()
    block = {
        "segment_kind": "paragraph",
        "line_count": 3,
        "text": "This is a sufficiently long paragraph body text for classification.",
        "width": 220.0,
        "size": 11.0,
        "bbox": [10.0, 10.0, 230.0, 60.0],
    }
    assert is_body_paragraph_block(block, page_rect) is True


def test_is_body_paragraph_block_excludes_short_single_line_text():
    page_rect = type("Rect", (), {"width": 600.0, "height": 800.0})()
    block = {
        "segment_kind": "paragraph",
        "line_count": 1,
        "text": "Short note",
        "width": 120.0,
        "size": 11.0,
        "bbox": [10.0, 10.0, 130.0, 24.0],
    }
    assert is_body_paragraph_block(block, page_rect) is False


def test_group_body_paragraphs_by_column_groups_similar_x0_blocks():
    page_rect = type("Rect", (), {"width": 600.0, "height": 800.0})()
    page_blocks = [
        {"segment_kind": "paragraph", "line_count": 3, "text": "A" * 60, "width": 220.0, "size": 11.0, "x0": 40.0, "bbox": [40.0, 10.0, 260.0, 60.0]},
        {"segment_kind": "paragraph", "line_count": 3, "text": "B" * 60, "width": 225.0, "size": 10.0, "x0": 44.0, "bbox": [44.0, 70.0, 269.0, 120.0]},
        {"segment_kind": "paragraph", "line_count": 3, "text": "C" * 60, "width": 210.0, "size": 9.0, "x0": 320.0, "bbox": [320.0, 10.0, 530.0, 60.0]},
    ]
    groups = group_body_paragraphs_by_column(page_blocks, page_rect)
    assert groups == [[0, 1], [2]]


def test_compute_column_target_font_sizes_uses_lower_percentile_per_group():
    page_blocks = [
        {"size": 12.0},
        {"size": 10.0},
        {"size": 9.0},
    ]
    target_sizes = compute_column_target_font_sizes(page_blocks, [[0, 1], [2]])
    assert round(target_sizes[0], 2) == 10.5
    assert round(target_sizes[1], 2) == 10.5
    assert round(target_sizes[2], 2) == 9.0
