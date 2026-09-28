"""A fake job source for testing the discovery architecture.

This is NOT a real job-site integration and never will scrape or call
any real job website. It exists purely so JobDiscoveryService has a
working source to exercise end-to-end — the same shape a future
NaukriJobSource or LinkedInJobSource would implement.
"""
from app.integrations.job_sources.base import BaseJobSource
from app.schemas.job_search import JobSearchRequest


class MockJobSource(BaseJobSource):
    """Returns a small, fixed set of sample jobs, filtered in-memory."""

    name = "mock"

    _SAMPLE_JOBS: list[dict] = [
        {
            "title": "AI Engineer",
            "company": "Example Technologies",
            "location": "Bengaluru",
            "description": "Build and deploy LLM-powered applications using Python and FastAPI.",
            "url": "https://example.com/jobs/1",
            "remote": False,
        },
        {
            "title": "Generative AI Engineer",
            "company": "Innotech Labs",
            "location": "Bengaluru",
            "description": "Work on RAG pipelines, prompt engineering, and fine-tuning for GenAI products.",
            "url": "https://example.com/jobs/2",
            "remote": False,
        },
        {
            "title": "Machine Learning Engineer",
            "company": "DataWorks Inc",
            "location": "Hyderabad",
            "description": "Design and train ML models for recommendation systems at scale.",
            "url": "https://example.com/jobs/3",
            "remote": False,
        },
        {
            "title": "Backend Engineer",
            "company": "Example Technologies",
            "location": "Remote",
            "description": "Build reliable backend services in Python and PostgreSQL.",
            "url": "https://example.com/jobs/4",
            "remote": True,
        },
        {
            "title": "Data Scientist",
            "company": "Innotech Labs",
            "location": "Bengaluru",
            "description": "Analyze data and build predictive models using Python and SQL.",
            "url": "https://example.com/jobs/5",
            "remote": False,
        },
    ]

    def search_jobs(self, search_request: JobSearchRequest) -> list[dict]:
        """Filter the fixed sample set by keywords/location/remote, then apply limit."""
        jobs = list(self._SAMPLE_JOBS)

        if search_request.keywords:
            keyword = search_request.keywords.lower()
            jobs = [
                job
                for job in jobs
                if keyword in job["title"].lower() or keyword in job["description"].lower()
            ]

        if search_request.location:
            location = search_request.location.lower()
            jobs = [job for job in jobs if location in job["location"].lower()]

        if search_request.remote:
            jobs = [job for job in jobs if job.get("remote")]

        return jobs[: search_request.limit]
