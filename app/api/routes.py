"""
API routes for TriagePilot — Commercial Insurance.

POST /api/submissions       — upload files + form data, run pipeline
GET  /api/submissions       — list user's submissions
GET  /api/submissions/{id}  — get submission result
POST /api/appetite/ingest   — upload reference docs to vector DB
"""

import json
import structlog
from fastapi import APIRouter, UploadFile, File, HTTPException, Depends, Form
from typing import Optional
from app.models.schemas import UploadedDocument, SubmissionOutput
from app.services.supabase_service import StorageService, SubmissionDB
from app.agents.pipeline import run_pipeline
from app.core.config import get_settings
from app.core.auth import get_current_user, AuthUser

log = structlog.get_logger()
router = APIRouter(prefix="/api")

storage = StorageService()
db = SubmissionDB()


# ── Upload & Process ─────────────────────────────────

@router.post("/submissions", response_model=SubmissionOutput)
async def create_submission(
    files: list[UploadFile] = File(default=[], description="PDF, image files"),
    form_data: Optional[str] = Form(default=None, description="JSON string with company details, business description, property details, incident data"),
    user: AuthUser = Depends(get_current_user),
):
    """
    Upload submission documents + form data and run the full triage pipeline.

    form_data is a JSON string with optional fields:
    {
      "company": {
        "name": "", "registration_number": "", "description": "",
        "funding_stage": "", "headcount": 0, "year_established": 0,
        "annual_revenue": 0, "entity_type": "", "naics_code": "",
        "annual_payroll": 0
      },
      "business_description": "free text",
      "property_description": "free text",
      "incidents": [
        {"date": "", "time": "", "location": "", "description": ""}
      ]
    }
    """
    settings = get_settings()

    # Parse form data JSON
    parsed_form_data = {}
    if form_data:
        try:
            parsed_form_data = json.loads(form_data)
        except json.JSONDecodeError:
            raise HTTPException(400, "form_data must be valid JSON")

    # Must have at least files or form data
    if not files and not parsed_form_data:
        raise HTTPException(400, "At least one file or form data is required")

    # Process files — deduplicate by filename before anything else
    documents = []
    file_bytes_map = {}
    seen_filenames: set[str] = set()

    for f in files:
        # Skip duplicate filenames from the same upload batch
        if f.filename in seen_filenames:
            log.warning("duplicate_filename_skipped", filename=f.filename)
            continue
        seen_filenames.add(f.filename)

        ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
        if ext not in settings.allowed_ext_list:
            raise HTTPException(400, f"File type .{ext} not allowed. Accepted: {settings.allowed_extensions}")

        content = await f.read()
        size_mb = len(content) / (1024 * 1024)
        if size_mb > settings.max_upload_size_mb:
            raise HTTPException(400, f"File {f.filename} exceeds {settings.max_upload_size_mb}MB limit")

        doc = UploadedDocument(filename=f.filename, file_type=ext, storage_path="")
        documents.append(doc)
        file_bytes_map[f.filename] = content

    # Upload to Supabase storage
    submission_id = documents[0].doc_id.split("-")[0] if documents else "form"
    for doc in documents:
        try:
            path = await storage.upload_file(
                submission_id=f"{user.id}/{submission_id}",
                filename=doc.filename,
                file_bytes=file_bytes_map[doc.filename],
                content_type=f"application/{doc.file_type}",
            )
            doc.storage_path = path
        except Exception as e:
            log.error("upload_failed", filename=doc.filename, error=str(e))
            # Storage upload failed (e.g. duplicate in Supabase) but file bytes
            # are still in memory — pipeline can proceed without storage path

    # Run the pipeline
    log.info("pipeline_starting",
        submission_id=submission_id,
        user_id=user.id,
        file_count=len(documents),
        has_form_data=bool(parsed_form_data),
    )
    output = await run_pipeline(
        documents=documents,
        files=file_bytes_map,
        form_data=parsed_form_data,
    )

    # Persist to DB
    try:
        await db.create_submission(
            submission_id=output.submission_id,
            data={
                "user_id": user.id,
                "status": output.status.value if hasattr(output.status, 'value') else output.status,
                "lob": output.line_of_business,
                "insured_name": output.company.name,
                "appetite_score": output.appetite_assessment.score,
                "appetite_status": output.appetite_assessment.status.value if hasattr(output.appetite_assessment.status, 'value') else output.appetite_assessment.status,
                "winnability": output.winnability_score,
                "priority": output.priority_score,
                "queue": output.recommended_queue,
                "referral_required": output.referral_required,
                "brief_markdown": output.risk_brief_markdown,
                "processing_time": output.processing_time_seconds,
                "errors": output.errors,
                "result_json": output.model_dump_json(),
            },
        )
    except Exception as e:
        log.error("db_persist_failed", error=str(e))

    return output


# ── Read ─────────────────────────────────────────────

@router.get("/submissions")
async def list_submissions(
    limit: int = 20,
    offset: int = 0,
    user: AuthUser = Depends(get_current_user),
):
    try:
        results = await db.list_submissions(user_id=user.id, limit=limit, offset=offset)
        return {"submissions": results, "count": len(results)}
    except Exception as e:
        raise HTTPException(500, f"Failed to list submissions: {str(e)}")


@router.get("/submissions/{submission_id}")
async def get_submission(
    submission_id: str,
    user: AuthUser = Depends(get_current_user),
):
    try:
        result = await db.get_submission(submission_id, user_id=user.id)
        if not result:
            raise HTTPException(404, "Submission not found")
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Failed to get submission: {str(e)}")


# ── Appetite Guide Ingestion ────────────────────────

@router.post("/appetite/ingest")
async def ingest_appetite_docs(
    files: list[UploadFile] = File(...),
    doc_type: str = Form("reference"),
    lob: str = Form("commercial"),
    insurer: str = Form("default"),
    user: AuthUser = Depends(get_current_user),
):
    """
    Upload policy wordings, UW guides, claims guides etc.
    LlamaParse → markdown → structure-aware chunking → Pinecone.
    """
    from app.services.parse_service import ParseService
    from app.services.chunking_service import ChunkingService
    from app.services.vector_service import VectorService

    parser = ParseService()
    chunker = ChunkingService()
    vector = VectorService()
    total_chunks = 0
    files_processed = 0

    for f in files:
        content = await f.read()
        ext = f.filename.rsplit(".", 1)[-1].lower()

        if ext != "pdf":
            continue

        markdown = await parser.pdf_to_markdown(content, f.filename)

        chunks = chunker.chunk_markdown(
            markdown=markdown,
            filename=f.filename,
            doc_type=doc_type,
            insurer=insurer,
            lob=lob,
        )

        for chunk in chunks:
            chunk.metadata["user_id"] = user.id
            await vector.upsert_chunk(
                chunk_id=chunk.chunk_id,
                text=chunk.text,
                metadata=chunk.metadata,
            )
            total_chunks += 1

        files_processed += 1
        log.info("file_ingested", filename=f.filename, chunks=len(chunks))

    log.info("appetite_ingested", user_id=user.id, files=files_processed, chunks=total_chunks)
    return {
        "status": "ok",
        "files_processed": files_processed,
        "chunks_indexed": total_chunks,
    }