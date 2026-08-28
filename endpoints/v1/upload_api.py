"""
api/endpoints/v1/upload_api.py

Simplified upload: save file, convert DOCX→PDF if needed, get page count.
No page image rendering — the RAG pipeline handles per-page extraction at
generation time.
"""
from typing import Annotated
from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException
from fastapi.responses import JSONResponse
from api.config.security import get_current_active_user
from api.model import User
from api.config.config import settings as SETTINGS
from api.config.storage import (
    UPLOAD_DIR,
    UPLOADED_DIR,
    set_file_record,
)
import shutil
from datetime import datetime
from api.utils.docx_converter import prepare_file_for_processing

upload_router = APIRouter(
    prefix=f"/api/{SETTINGS.API_VERSION}/{SETTINGS.API_URL_PREFIX}",
    tags=["File Upload"],
)


def _get_total_pages(pdf_path: str) -> int:
    """Return page count of a PDF using PyMuPDF (fast, no rendering)."""
    try:
        import fitz
        doc   = fitz.open(pdf_path)
        count = doc.page_count
        doc.close()
        return count
    except Exception:
        return 0


@upload_router.post("/upload-pdf-file")
async def upload_pdf_file(
    file: UploadFile = File(...),
    demand_id:  str  = Form(...),
    project_id: str  = Form(...),
    current_user: Annotated[User, Depends(get_current_active_user)] = ...,
):
    """
    Upload a PDF or DOCX file.

    Returns {uuid, filename, total_pages, file_type} — no page images.
    The heavy per-page extraction happens during /generate-testcases.
    """
    try:
        # ── Validate file type ────────────────────────────────────────────────
        if not (file.filename.endswith(".pdf") or file.filename.endswith(".docx")):
            raise HTTPException(400, "Only PDF/DOCX files are allowed")

        # ── Validate and sanitise IDs ─────────────────────────────────────────
        if not demand_id or not demand_id.strip():
            raise HTTPException(400, "Demand ID is required")
        if not project_id or not project_id.strip():
            raise HTTPException(400, "Project ID is required")

        safe_demand_id = "".join(
            c for c in demand_id if c.isalnum() or c in ("-", "_")
        ).strip()
        safe_project_id = "".join(
            c for c in project_id if c.isalnum() or c in ("-", "_")
        ).strip()

        if not safe_demand_id or not safe_project_id:
            raise HTTPException(400, "Invalid Demand ID or Project ID format")

        # ── Save file ─────────────────────────────────────────────────────────
        timestamp      = datetime.now().strftime("%Y%m%d_%H%M%S")
        file_extension = ".pdf" if file.filename.endswith(".pdf") else ".docx"
        unique_filename = f"{safe_demand_id}_{safe_project_id}_{timestamp}{file_extension}"
        file_path       = UPLOADED_DIR / unique_filename

        print(f"File uploaded by: {current_user.email}")
        print(f"Demand ID : {safe_demand_id}")
        print(f"Project ID: {safe_project_id}")

        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        print(f"Saved as: {unique_filename}")

        # ── Convert DOCX → PDF if needed ──────────────────────────────────────
        processed_file_path, converted = prepare_file_for_processing(
            str(file_path), temp_dir="temp_conversions"
        )

        # ── Get total page count (no image rendering) ─────────────────────────
        total_pages = _get_total_pages(str(processed_file_path))
        if total_pages == 0:
            raise HTTPException(422, "Could not determine page count — file may be corrupt.")

        # ── Determine original file type ──────────────────────────────────────
        original_file_type = "docx" if file.filename.endswith(".docx") else "pdf"

        # ── Generate UUID and store record ────────────────────────────────────
        import uuid as _uuid
        file_uuid = str(_uuid.uuid4())

        set_file_record(
            uuid=file_uuid,
            data={
                "file_path"  : str(processed_file_path),
                "total_pages": total_pages,
            },
        )

        print(f"Stored UUID: {file_uuid}  total_pages={total_pages}")

        return JSONResponse(content={
            "uuid"       : file_uuid,
            "filename"   : unique_filename,
            "total_pages": total_pages,
            "file_type"  : original_file_type,
        })

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        await file.close()
