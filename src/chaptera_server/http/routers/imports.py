"""Book import inspection; no database mutation in preview."""
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from ...book_import import ImportFormatError, MAX_UPLOAD_BYTES, preview_book_import
from ..dependencies import require_member

router = APIRouter(prefix="/v1/imports", dependencies=[Depends(require_member)])


@router.post("/preview")
async def inspect_upload(file: UploadFile = File(...)):
    try:
        content = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="File exceeds 12 MiB limit")
        return preview_book_import(file.filename or "", content)
    except ImportFormatError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        await file.close()
