"""Converts source-specific raw job dicts into the app's common NormalizedJob shape.

Different job sites return different field names for the same concept
(e.g. "title" vs "jobTitle" vs "job_title"). This is the one place that
knows about those variants, so JobDiscoveryService and everything
downstream of it only ever deals with one consistent shape. This is
intentionally kept separate from the LLM-based Job Intelligence Agent
(Phase 2) — normalization here is plain field-mapping, not AI extraction.
"""
import re
from html import unescape

from app.schemas.job_search import NormalizedJob

# Each tuple lists the field-name variants (across current and
# anticipated future sources) that map to one canonical field. Add a
# new source's field name here rather than writing a new normalizer.
_TITLE_KEYS = ("title", "job_title", "jobTitle")
_COMPANY_KEYS = ("company", "company_name", "employer")
_LOCATION_KEYS = ("location", "job_location")
# Jooble's raw jobs only ever include "snippet" (a short excerpt), never
# a "description"/"job_description" key -- it's still mapped into the
# same NormalizedJob.description field, but callers should not assume
# it's a complete job description just because the field is populated.
_DESCRIPTION_KEYS = ("description", "job_description", "snippet")
_URL_KEYS = ("url", "job_url", "link")

# Jooble (and potentially other sources) return description/snippet
# text with HTML markup (e.g. "&nbsp;", "<b>...</b>") and literal
# "\r\n" line endings mixed in. These are compiled once at import time
# and reused by _clean_description below.
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_INLINE_WHITESPACE_RE = re.compile(r"[ \t]+")
_EXCESS_BLANK_LINES_RE = re.compile(r"\n{3,}")


def _clean_description(text: str | None) -> str | None:
    """Strip HTML markup/entities and normalize whitespace in a raw
    source description/snippet, so NormalizedJob.description is always
    plain, human-readable text regardless of which source produced it.

    Returns None if there is nothing left after cleaning (e.g. the raw
    value was only markup/whitespace), rather than an empty string.
    """
    if text is None:
        return None

    # Unescape entities first ("&nbsp;", "&amp;", ...) so any tags they
    # were hiding get exposed to the tag stripper below.
    cleaned = unescape(text)
    cleaned = _HTML_TAG_RE.sub(" ", cleaned)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _INLINE_WHITESPACE_RE.sub(" ", cleaned)
    # Trim each line individually so stray leading/trailing spaces left
    # by removed tags don't survive, then collapse runs of 3+ blank
    # lines (common in Jooble snippets) down to a single blank line.
    cleaned = "\n".join(line.strip() for line in cleaned.split("\n"))
    cleaned = _EXCESS_BLANK_LINES_RE.sub("\n\n", cleaned)
    cleaned = cleaned.strip()
    return cleaned or None


class JobNormalizer:
    """Stateless converter from raw source job dicts to NormalizedJob."""

    @staticmethod
    def _first_present(raw_job: dict, keys: tuple[str, ...]) -> str | None:
        for key in keys:
            value = raw_job.get(key)
            if value is not None:
                return value
        return None

    def normalize(self, raw_job: dict, source: str) -> NormalizedJob:
        """Convert one raw job dict from `source` into a NormalizedJob."""
        raw_description = self._first_present(raw_job, _DESCRIPTION_KEYS)
        return NormalizedJob(
            title=self._first_present(raw_job, _TITLE_KEYS),
            company=self._first_present(raw_job, _COMPANY_KEYS),
            location=self._first_present(raw_job, _LOCATION_KEYS),
            description=_clean_description(raw_description),
            url=self._first_present(raw_job, _URL_KEYS),
            source=source,
        )

    def normalize_many(self, raw_jobs: list[dict], source: str) -> list[NormalizedJob]:
        return [self.normalize(raw_job, source) for raw_job in raw_jobs]
