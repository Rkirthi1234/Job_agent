"""Extract and clean raw text from uploaded resume files (PDF or DOCX)."""
from pypdf import PdfReader

from docx import Document


class DocumentExtractionError(Exception):
    """Raised when a resume file cannot be read or has no usable text."""


def extract_pdf_text(file_path: str) -> str:
    """Extract and combine text from every page of a PDF."""
    try:
        reader = PdfReader(file_path)
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:  # pypdf raises its own exception types on corrupt files
        raise DocumentExtractionError(f"Could not open PDF file: {exc}") from exc

    text = "\n".join(pages).strip()
    if not text:
        raise DocumentExtractionError("No extractable text found in PDF.")
    return text


def extract_docx_text(file_path: str) -> str:
    """Extract paragraph and basic table text from a DOCX file."""
    try:
        document = Document(file_path)
    except Exception as exc:
        raise DocumentExtractionError(f"Could not open DOCX file: {exc}") from exc

    parts: list[str] = [p.text for p in document.paragraphs if p.text.strip()]

    for table in document.tables:
        for row in table.rows:
            row_text = " | ".join(cell.text.strip() for cell in row.cells if cell.text.strip())
            if row_text:
                parts.append(row_text)

    text = "\n".join(parts).strip()
    if not text:
        raise DocumentExtractionError("No extractable text found in DOCX.")
    return text


def extract_text(file_path: str, file_type: str) -> str:
    """Dispatch to the right extractor based on file extension."""
    if file_type == ".pdf":
        return extract_pdf_text(file_path)
    if file_type == ".docx":
        return extract_docx_text(file_path)
    raise DocumentExtractionError(f"Unsupported file type: {file_type}")


def clean_text(raw_text: str) -> str:
    """Normalize whitespace without destroying resume structure.

    Collapses repeated blank lines and trims trailing spaces from each
    line, but keeps line breaks so section headings and bullet lists
    stay readable when handed to the LLM.
    """
    lines = [line.strip() for line in raw_text.splitlines()]
    cleaned_lines: list[str] = []
    previous_blank = False

    for line in lines:
        if not line:
            if not previous_blank:
                cleaned_lines.append("")
            previous_blank = True
            continue
        cleaned_lines.append(line)
        previous_blank = False

    return "\n".join(cleaned_lines).strip()
