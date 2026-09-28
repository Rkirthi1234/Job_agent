"""Tests for document text extraction and cleaning."""
import pytest

from app.services.document_service import (
    DocumentExtractionError,
    clean_text,
    extract_docx_text,
    extract_pdf_text,
    extract_text,
)


def test_extract_pdf_text(dummy_pdf_path):
    text = extract_pdf_text(str(dummy_pdf_path))
    assert "John Doe" in text
    assert "Acme Corp" in text


def test_extract_docx_text(dummy_docx_path):
    text = extract_docx_text(str(dummy_docx_path))
    assert "John Doe" in text
    assert "University of Texas" in text


def test_extract_pdf_with_no_text_raises(empty_pdf_path):
    with pytest.raises(DocumentExtractionError):
        extract_pdf_text(str(empty_pdf_path))


def test_extract_text_dispatches_by_extension(dummy_pdf_path, dummy_docx_path):
    assert "John Doe" in extract_text(str(dummy_pdf_path), ".pdf")
    assert "John Doe" in extract_text(str(dummy_docx_path), ".docx")


def test_extract_text_rejects_unsupported_extension(tmp_path):
    fake_file = tmp_path / "resume.txt"
    fake_file.write_text("hello")
    with pytest.raises(DocumentExtractionError):
        extract_text(str(fake_file), ".txt")


def test_clean_text_collapses_blank_lines():
    raw = "Line one\n\n\n\nLine two   \n   \nLine three"
    cleaned = clean_text(raw)
    assert cleaned == "Line one\n\nLine two\n\nLine three"
