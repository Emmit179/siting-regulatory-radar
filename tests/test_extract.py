import fitz

from countywatch.config import Settings
from countywatch.extract import extract_document


def test_extract_html(monkeypatch, tmp_path):
    monkeypatch.setenv("COUNTYWATCH_ROOT", str(tmp_path))
    settings = Settings.load(tmp_path / ".env")
    result = extract_document(
        b"<html><head><title>Agenda</title></head><body><nav>Menu</nav><h1>Commissioners Court</h1><p>Discuss solar moratorium.</p></body></html>",
        "text/html", "https://county.gov/agenda", settings,
    )
    assert "Discuss solar moratorium" in result.text
    assert "Menu" not in result.text


def test_extract_text_pdf(monkeypatch, tmp_path):
    monkeypatch.setenv("COUNTYWATCH_ROOT", str(tmp_path))
    monkeypatch.setenv("COUNTYWATCH_OCR", "false")
    settings = Settings.load(tmp_path / ".env")
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Commissioners Court agenda: utility-scale solar moratorium")
    data = doc.tobytes()
    doc.close()
    result = extract_document(data, "application/pdf", "https://county.gov/a.pdf", settings)
    assert "utility-scale solar moratorium" in result.text
    assert result.page_count == 1


def test_docx_archive_entry_limit(monkeypatch, tmp_path):
    import io
    import zipfile

    import pytest

    monkeypatch.setenv("COUNTYWATCH_ROOT", str(tmp_path))
    settings = Settings.load(tmp_path / ".env")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index in range(5001):
            archive.writestr(f"word/empty-{index}.xml", "")
    with pytest.raises(ValueError, match="safe expansion limits"):
        extract_document(
            buffer.getvalue(),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "https://county.gov/packet.docx",
            settings,
        )
