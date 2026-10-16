"""Small closed-team HTTP boundary; queue worker is a separate process."""
from __future__ import annotations

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .auth import authenticate
from .config import Settings
from .storage import IdempotencyConflict, JobSpec, QueueStore


class SubmitChapter(BaseModel):
    chapter_id: str = Field(min_length=1, max_length=255)
    source_html: str = Field(min_length=1, max_length=2_000_000)
    source_language: str = Field(default="auto", max_length=64)
    target_language: str = Field(default="Russian", min_length=1, max_length=64)
    glossary: dict[str, str] = Field(default_factory=dict)
    context: str = Field(default="", max_length=100_000)
    chapter_path: str = Field(default="", max_length=1024)
    max_attempts: int = Field(default=3, ge=1, le=10)


def create_app(settings: Settings, store: QueueStore | None = None) -> FastAPI:
    app = FastAPI(title="Chaptera Team Translation API", version="0.1.0",
                  docs_url=None, redoc_url=None, openapi_url=None)
    repo = store or QueueStore(settings.db_path)

    def require_member(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> str:
        return authenticate(x_api_key, settings)

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/jobs", status_code=201)
    def submit(body: SubmitChapter, actor: str = Depends(require_member),
               idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        try:
            return repo.create_job(actor, JobSpec(**body.model_dump()), idempotency_key=idempotency_key)
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/v1/jobs")
    def list_jobs(limit: int = 50, actor: str = Depends(require_member)):
        return {"jobs": repo.list_jobs(limit=limit)}

    @app.get("/v1/jobs/{job_id}")
    def get_job(job_id: str, actor: str = Depends(require_member)):
        job = repo.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return job

    @app.get("/v1/jobs/{job_id}/events")
    def get_events(job_id: str, actor: str = Depends(require_member)):
        if repo.get_job(job_id, include_result=False) is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return {"events": repo.events(job_id)}

    @app.post("/v1/jobs/{job_id}/cancel")
    def cancel(job_id: str, actor: str = Depends(require_member)):
        job = repo.request_cancel(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return job

    return app


def create_default_app() -> FastAPI:
    return create_app(Settings.from_env())
