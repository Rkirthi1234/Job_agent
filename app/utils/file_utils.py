"""Small filesystem helpers with no business logic of their own."""
import os
import uuid

ALLOWED_EXTENSIONS = {".pdf", ".docx"}


def get_extension(filename: str) -> str:
    """Return the lowercase extension of a filename, including the dot."""
    return os.path.splitext(filename)[1].lower()


def is_allowed_extension(filename: str) -> bool:
    return get_extension(filename) in ALLOWED_EXTENSIONS


def generate_safe_filename(original_filename: str) -> str:
    """Build a random, collision-safe filename that keeps the original extension.

    The original filename is never trusted directly — it may contain path
    separators or other unsafe characters — so we discard it entirely and
    keep only the extension. The original name is still stored separately
    as metadata (see CandidateProfile.original_filename).
    """
    extension = get_extension(original_filename)
    return f"{uuid.uuid4().hex}{extension}"
