"""Tests for the resume upload API.

The LLM is always mocked here — these tests never make a real call to
Ollama or any other provider, so they run fast and offline.
"""
from unittest.mock import patch

from app.config import get_settings
from app.schemas.candidate import CandidateProfileSchema

FAKE_PROFILE = CandidateProfileSchema(
    name="John Doe",
    email="john.doe@example.com",
    skills=["Python", "FastAPI"],
    target_roles=["Backend Engineer"],
)


def _mock_build_profile(*args, **kwargs):
    return FAKE_PROFILE


def test_upload_pdf_success(client, dummy_pdf_path):
    with patch("app.agents.resume_agent.ResumeAgent.build_profile", _mock_build_profile):
        with open(dummy_pdf_path, "rb") as f:
            response = client.post(
                "/api/resume/upload",
                files={"file": ("resume.pdf", f, "application/pdf")},
            )

    assert response.status_code == 200
    body = response.json()
    assert body["candidate"]["name"] == "John Doe"
    assert "Python" in body["candidate"]["skills"]
    assert body["candidate_id"] > 0


def test_upload_docx_success(client, dummy_docx_path):
    with patch("app.agents.resume_agent.ResumeAgent.build_profile", _mock_build_profile):
        with open(dummy_docx_path, "rb") as f:
            response = client.post(
                "/api/resume/upload",
                files={
                    "file": (
                        "resume.docx",
                        f,
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    )
                },
            )

    assert response.status_code == 200
    assert response.json()["candidate"]["email"] == "john.doe@example.com"


def test_upload_unsupported_extension_rejected(client, tmp_path):
    fake_file = tmp_path / "resume.txt"
    fake_file.write_text("just text")

    with open(fake_file, "rb") as f:
        response = client.post(
            "/api/resume/upload",
            files={"file": ("resume.txt", f, "text/plain")},
        )

    assert response.status_code == 415


def test_upload_empty_file_rejected(client, tmp_path):
    empty_file = tmp_path / "empty.pdf"
    empty_file.write_bytes(b"")

    with open(empty_file, "rb") as f:
        response = client.post(
            "/api/resume/upload",
            files={"file": ("empty.pdf", f, "application/pdf")},
        )

    assert response.status_code == 400


def test_upload_oversized_file_rejected(client, monkeypatch, dummy_pdf_path):
    monkeypatch.setenv("MAX_FILE_SIZE_MB", "0")
    get_settings.cache_clear()

    with open(dummy_pdf_path, "rb") as f:
        response = client.post(
            "/api/resume/upload",
            files={"file": ("resume.pdf", f, "application/pdf")},
        )

    assert response.status_code == 400
    get_settings.cache_clear()


def test_upload_pdf_with_no_extractable_text_rejected(client, empty_pdf_path):
    with open(empty_pdf_path, "rb") as f:
        response = client.post(
            "/api/resume/upload",
            files={"file": ("empty.pdf", f, "application/pdf")},
        )

    assert response.status_code == 400
