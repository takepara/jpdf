import json
import pytest
import jpdf


def test_run_pipeline_raises_if_source_missing(tmp_path):
    missing = tmp_path / "missing.pdf"
    with pytest.raises(FileNotFoundError):
        jpdf.run_pipeline(str(missing), None, translate_options={"engine": "google"})


def test_run_pipeline_removes_intermediate_files_when_not_debug(tmp_path, monkeypatch):
    source_pdf = tmp_path / "sample.pdf"
    source_pdf.write_bytes(b"%PDF-1.4\n")

    def fake_extract(_source, extracted_json):
        payload = [
            {
                "page": 0,
                "block_idx": 0,
                "bbox": [0, 0, 10, 10],
                "text": "Hello",
                "original_text": "Hello",
                "size": 12,
                "font": "A",
            }
        ]
        with open(extracted_json, "w", encoding="utf-8") as f:
            json.dump(payload, f)

    def fake_translate(extracted_json, translated_json, llm_options, debug_json_path=None):
        with open(extracted_json, "r", encoding="utf-8") as f:
            data = json.load(f)
        translated = []
        for item in data:
            copied = item.copy()
            copied["original_text"] = item["text"]
            copied["text"] = "こんにちは"
            translated.append(copied)
        with open(translated_json, "w", encoding="utf-8") as f:
            json.dump(translated, f)
        if debug_json_path:
            with open(debug_json_path, "w", encoding="utf-8") as f:
                json.dump({"segment_debug": []}, f)

    def fake_generate(_source, translated_json, output_pdf):
        assert (tmp_path / "sample_translated_text.json") == tmp_path / translated_json.split("/")[-1]
        with open(translated_json, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        assert loaded
        with open(output_pdf, "wb") as f:
            f.write(b"%PDF-1.4\n")

    import extract
    import generate

    monkeypatch.setattr(extract, "extract_pdf_text", fake_extract)
    monkeypatch.setattr(jpdf, "translate_blocks", fake_translate)
    monkeypatch.setattr(generate, "generate_translated_pdf", fake_generate)

    jpdf.run_pipeline(
        str(source_pdf),
        None,
        translate_options={"engine": "google", "keep_debug_files": False},
    )

    assert not (tmp_path / "sample_extracted_text.json").exists()
    assert not (tmp_path / "sample_translated_text.json").exists()
    assert not (tmp_path / "sample_segment_debug.json").exists()
    assert (tmp_path / "sample_G.pdf").exists()


def test_run_pipeline_keeps_intermediate_files_in_debug_mode(tmp_path, monkeypatch):
    source_pdf = tmp_path / "sample.pdf"
    source_pdf.write_bytes(b"%PDF-1.4\n")

    def fake_extract(_source, extracted_json):
        with open(extracted_json, "w", encoding="utf-8") as f:
            json.dump([], f)

    def fake_translate(_extracted_json, translated_json, llm_options, debug_json_path=None):
        with open(translated_json, "w", encoding="utf-8") as f:
            json.dump([], f)
        if debug_json_path:
            with open(debug_json_path, "w", encoding="utf-8") as f:
                json.dump({}, f)

    def fake_generate(_source, _translated_json, output_pdf):
        with open(output_pdf, "wb") as f:
            f.write(b"%PDF-1.4\n")

    import extract
    import generate

    monkeypatch.setattr(extract, "extract_pdf_text", fake_extract)
    monkeypatch.setattr(jpdf, "translate_blocks", fake_translate)
    monkeypatch.setattr(generate, "generate_translated_pdf", fake_generate)

    jpdf.run_pipeline(
        str(source_pdf),
        None,
        translate_options={"engine": "google", "keep_debug_files": True},
    )

    assert (tmp_path / "sample_extracted_text.json").exists()
    assert (tmp_path / "sample_translated_text.json").exists()
    assert (tmp_path / "sample_segment_debug.json").exists()
