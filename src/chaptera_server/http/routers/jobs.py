"""Job submission, inspection and cancellation endpoints."""
from fastapi import APIRouter, Depends, Header, HTTPException

from chaptera_translation_core.prompt_policy import GlossaryVersionConflict, scoped_glossary

from ...storage import IdempotencyConflict, JobSpec, QueueStore
from ..dependencies import get_store, require_member
from ..schemas import SubmitChapter

router = APIRouter(prefix="/v1/jobs")


@router.post("", status_code=201)
def submit(body: SubmitChapter, actor: str = Depends(require_member),
           repo: QueueStore = Depends(get_store),
           idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    try:
        scoped_glossary(body.glossary, body.glossary_versions, body.chapter_path, body.glossary_authority)
        return repo.create_job(actor, JobSpec(**body.model_dump()), idempotency_key=idempotency_key)
    except GlossaryVersionConflict as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("")
def list_jobs(limit: int = 50, actor: str = Depends(require_member),
              repo: QueueStore = Depends(get_store)):
    return {"jobs": repo.list_jobs(limit=limit)}


@router.get("/{job_id}")
def get_job(job_id: str, actor: str = Depends(require_member),
            repo: QueueStore = Depends(get_store)):
    job = repo.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.get("/{job_id}/events")
def get_events(job_id: str, actor: str = Depends(require_member),
               repo: QueueStore = Depends(get_store)):
    if repo.get_job(job_id, include_result=False) is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"events": repo.events(job_id)}


@router.post("/{job_id}/cancel")
def cancel(job_id: str, actor: str = Depends(require_member),
           repo: QueueStore = Depends(get_store)):
    job = repo.request_cancel(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job
