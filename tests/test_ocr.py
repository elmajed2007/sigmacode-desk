from conftest import FIXTURES
from sigmadesk import config
from sigmadesk.ocr import arabic, loader
from sigmadesk.ocr.engine import OcrEngine, OcrSettings


def test_scanned_pdf_ocr_reads_arabic():
    eng = OcrEngine(config.assets_dir() / "ocr")
    pages = loader.extract(FIXTURES / "scan2.pdf", "pdf", lambda: eng, OcrSettings(), force_ocr=True)
    assert pages and pages[0]["method"] == "ocr"
    text = " ".join(p["text"] for p in pages)
    assert arabic.arabic_letter_count(text) > 50
    assert pages[0]["confidence"] > 0.7


def test_word_document_is_read_without_ocr(tmp_path):
    import docx
    d = docx.Document()
    d.add_paragraph("سياسة الجودة في المؤسسة تهدف إلى تحسين الخدمات المقدمة للمستفيدين بشكل مستمر.")
    p = tmp_path / "policy.docx"
    d.save(p)
    pages = loader.extract(p, "docx", lambda: None, OcrSettings())
    assert pages[0]["method"] == "text" and "الجودة" in pages[0]["text"]


def test_text_file_decoding(tmp_path):
    p = tmp_path / "a.txt"
    p.write_bytes("مرحبا بالعالم".encode("cp1256"))
    assert "مرحبا" in loader.read_text_file(p)


def test_kind_of_rejects_unknown():
    import pytest
    with pytest.raises(loader.LoadError):
        loader.kind_of("x.exe")
