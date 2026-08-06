from __future__ import annotations

from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import require_admin
from app.models.entities import UserAccount
from app.rag_ingestion.schema import AccessClass
from app.rag_ingestion.service import KnowledgeIngestionService


router = APIRouter(prefix="/api/admin/knowledge", tags=["knowledge-ingestion"])


def _service(request: Request, db: Session) -> KnowledgeIngestionService:
    return KnowledgeIngestionService(db, request.app.state.settings)


@router.post("/files", status_code=202)
async def upload_knowledge_file(
    request: Request,
    user: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    file: UploadFile = File(...),
    cloud_vision_allowed: bool = Query(False, alias="cloudVisionAllowed"),
):
    try:
        result = _service(request, db).submit_file(
            filename=file.filename or "uploaded-file",
            data=await file.read(),
            mime_type=file.content_type or "application/octet-stream",
            actor=user.username,
            access_class=AccessClass.ADMIN_PRIVATE,
            cloud_vision_allowed=cloud_vision_allowed,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return JSONResponse(status_code=202, content=jsonable_encoder(_submission_payload(result)))


@router.post("/file", status_code=202)
async def upload_knowledge_file_legacy(
    request: Request,
    user: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    file: UploadFile = File(...),
    cloud_vision_allowed: bool = Query(False, alias="cloudVisionAllowed"),
):
    response = await upload_knowledge_file(
        request=request,
        user=user,
        db=db,
        file=file,
        cloud_vision_allowed=cloud_vision_allowed,
    )
    payload = response.body
    import json

    body = json.loads(payload)
    body.update({"source": file.filename or "uploaded-file", "chunks": 0})
    return JSONResponse(status_code=202, content=body)


@router.get("/documents")
def list_knowledge_documents(
    request: Request,
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    return _service(request, db).list_documents()


@router.get("/documents/{document_id}")
def get_knowledge_document(
    document_id: str,
    request: Request,
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    try:
        return _service(request, db).get_document(document_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/documents/{document_id}/pages")
def list_knowledge_pages(
    document_id: str,
    request: Request,
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    try:
        return _service(request, db).list_pages(document_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/jobs/{job_id}")
def get_knowledge_job(
    job_id: str,
    request: Request,
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    try:
        return _service(request, db).get_job(job_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/jobs/{job_id}/retry", status_code=202)
def retry_knowledge_job(
    job_id: str,
    request: Request,
    user: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    cloud_vision_allowed: bool | None = Query(None, alias="cloudVisionAllowed"),
):
    try:
        result = _service(request, db).retry(
            job_id,
            actor=user.username,
            cloud_vision_allowed=cloud_vision_allowed,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return JSONResponse(status_code=202, content=jsonable_encoder(_submission_payload(result)))


def _submission_payload(result) -> dict:
    payload = asdict(result)
    return {
        "documentId": payload["document_id"],
        "versionId": payload["version_id"],
        "jobId": payload["job_id"],
        "status": payload["status"],
        "created": payload["created"],
        "cloudVisionAllowed": payload["cloud_vision_allowed"],
    }
