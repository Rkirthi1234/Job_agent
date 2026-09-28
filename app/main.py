"""FastAPI application entry point.

Run with:
    uvicorn app.main:app --reload
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes.applications import router as applications_router
from app.api.routes.candidates import router as candidates_router
from app.api.routes.job_search import router as job_search_router
from app.api.routes.jobs import router as jobs_router
from app.api.routes.matching import router as matching_router
from app.api.routes.resume import router as resume_router
from app.config import get_settings
from app.models.database import init_db

logging.basicConfig(level=logging.INFO)

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create database tables once, when the app starts."""
    init_db()
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)

app.include_router(resume_router)
app.include_router(candidates_router)
app.include_router(jobs_router)
app.include_router(job_search_router)
app.include_router(matching_router)
app.include_router(applications_router)


@app.get("/health")
def health() -> dict:
    """Simple liveness check."""
    return {"status": "healthy"}
