"""
api/endpoints/v1/generate_tests_api.py

Endpoints:
  POST /ingest-rag              — Admin only: ingest PDF into ChromaDB knowledge base
  GET  /rag-documents           — List ingested RAG documents
  DELETE /rag-documents/{doc_id}— Admin only: delete a RAG document
  POST /extract-reference-text  — Extract text from reference doc (kept for backward compat)
  POST /generate-testcases      — Generate test cases (RAG pipeline, testcase_client from user JWT)
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor

from httpcore import request

# Add a module-level executor for ingestion tasks
_ingest_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ingest")

# Track ongoing ingestion jobs: {job_id: {"status": "running"|"done"|"failed", "result": ...}}
_ingest_jobs: dict = {}
_ingest_jobs_lock = __import__("threading").Lock()


from typing import Annotated, Optional
from pathlib import Path
import json
import base64
import os
from datetime import datetime, timezone
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException, Depends, File, UploadFile,Form
from fastapi.responses import JSONResponse

from api.config.security import get_current_active_user, get_current_admin_user
from api.model import User, TestCaseRequest
from api.config.config import settings as SETTINGS
from api.config.storage import (
    get_file_record,
    delete_file_record,
    move_file_to_status,
    IN_PROGRESS_DIR,
    COMPLETED_DIR,
    FAILED_DIR,
)
from api.utils.rag_utils import (
    ingest_document_to_rag,
    list_rag_documents,
    delete_rag_document,
    extract_pages_with_images,
    retrieve_rag_chunks_for_page,
    generate_testcases_for_page_rag,
    detect_page_structure,
    PAGE_RAG_TOP_K,
)
from api.utils.utility import extract_text_from_file
from api.utils.azure_utility import process_and_clean_testcases
from api.config.config import settings as SETTINGS
from api.utils.feature_file_utils import generate_feature_file


testcase_router = APIRouter(
    prefix=f"/api/{SETTINGS.API_VERSION}/{SETTINGS.API_URL_PREFIX}",
    tags=["Testcase_generation"],
)

# Max ingest file size: 70 MB
MAX_INGEST_SIZE = 70 * 1024 * 1024

# ============================================================================
# Helpers
# ============================================================================

def save_output_to_file(
    result: dict,
    username: str,
    document_name: str,
    uuid: str,
    testcase_client: str = "UAT",
) -> str:
    base_output_dir = Path("output_files")
    user_output_dir = base_output_dir / username
    user_output_dir.mkdir(parents=True, exist_ok=True)

    name_without_ext = Path(document_name).stem
    parts = name_without_ext.split("_")

    if len(parts) >= 3:
        demand_id  = parts[0]
        project_id = parts[1]
        timestamp  = parts[2]
        output_filename = (
            f"{demand_id}_{project_id}_{timestamp}_{testcase_client}_testcases_result.json"
        )
    else:
        safe_name = "".join(
            c for c in document_name if c.isalnum() or c in (" ", "-", "_")
        ).strip()
        output_filename = f"{safe_name}_{testcase_client}_testcases_result.json"

    output_path = user_output_dir / output_filename
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    return str(output_path)


# ============================================================================
# RAG Knowledge Base — admin-only ingestion
# ============================================================================

# @testcase_router.post("/ingest-rag")
# async def ingest_rag_document(
#     file:          UploadFile = File(...),
#     department_id: str        = Form("general"),
#     application_name: str        = Form(""),          # NEW — optional
#     current_user:  Annotated[User, Depends(get_current_admin_user)] = ...,
# ):
#     """
#     Ingest a PDF into the RAG knowledge base (ChromaDB). Admin access required.
#     For files > 200 pages, automatically splits into parts before ingesting.
#     Max file size: 70 MB.
#     """
#     if not file.filename or not file.filename.lower().endswith(".pdf"):
#         raise HTTPException(400, "Only PDF files can be ingested into the knowledge base.")

#     file_bytes = await file.read()

#     if not file_bytes:
#         raise HTTPException(400, "Uploaded file is empty.")

#     if len(file_bytes) > MAX_INGEST_SIZE:
#         size_mb = len(file_bytes) / (1024 * 1024)
#         raise HTTPException(
#             413,
#             f"File too large ({size_mb:.1f} MB). Maximum allowed size is 70 MB."
#         )
#     try:
#         print(f"🔄 Starting full 4-pass ingestion for '{file.filename}' "
#               f"(dept={department_id}) — image-heavy docs may take several minutes…")
#         result = ingest_document_to_rag(
#             file_bytes    = file_bytes,
#             filename      = file.filename,
#             department_id = department_id,
#             application_name = (application_name or "").strip(),
#         )
#     except ValueError as e:
#         raise HTTPException(422, str(e))
#     except Exception as e:
#         import traceback
#         print(f"Ingestion error: {traceback.format_exc()}")
#         raise HTTPException(500, f"Ingestion failed: {e}")

#     return result


@testcase_router.post("/ingest-rag")
async def ingest_rag_document(
    file:             UploadFile = File(...),
    department_id:    str        = Form("general"),
    application_name: str        = Form(""),
    current_user:     Annotated[User, Depends(get_current_admin_user)] = ...,
):
    """
    Ingest a PDF into the RAG knowledge base (ChromaDB). Admin access required.
    Returns immediately with a job_id. Poll /ingest-rag/status/{job_id} for progress.
    """
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files can be ingested into the knowledge base.")

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(400, "Uploaded file is empty.")
    if len(file_bytes) > MAX_INGEST_SIZE:
        size_mb = len(file_bytes) / (1024 * 1024)
        raise HTTPException(413, f"File too large ({size_mb:.1f} MB). Maximum 70 MB.")

    import uuid as _uuid
    job_id   = str(_uuid.uuid4())[:8]
    filename = file.filename

    def _run_ingest():
        with _ingest_jobs_lock:
            _ingest_jobs[job_id] = {"status": "running", "filename": filename, "result": None, "error": None}
        try:
            print(f"🔄 [Job {job_id}] Starting ingestion for '{filename}' (dept={department_id})…")
            result = ingest_document_to_rag(
                file_bytes       = file_bytes,
                filename         = filename,
                department_id    = department_id,
                application_name = (application_name or "").strip(),
            )
            with _ingest_jobs_lock:
                _ingest_jobs[job_id] = {"status": "done", "filename": filename, "result": result, "error": None}
            print(f"✅ [Job {job_id}] Ingestion complete: {result.get('total_chunks', 0)} chunks")
        except Exception as e:
            import traceback
            print(f"✗ [Job {job_id}] Ingestion failed: {traceback.format_exc()}")
            with _ingest_jobs_lock:
                _ingest_jobs[job_id] = {"status": "failed", "filename": filename, "result": None, "error": str(e)}

    # Submit to background thread — returns immediately
    loop = asyncio.get_event_loop()
    loop.run_in_executor(_ingest_executor, _run_ingest)

    return {
        "job_id"   : job_id,
        "filename" : filename,
        "status"   : "running",
        "message"  : f"Ingestion started in background. Poll /ingest-rag/status/{job_id} for progress.",
    }


@testcase_router.get("/ingest-rag/status/{job_id}")
async def get_ingest_status(
    job_id: str,
    current_user: Annotated[User, Depends(get_current_admin_user)] = ...,
):
    """Poll ingestion job status."""
    with _ingest_jobs_lock:
        job = _ingest_jobs.get(job_id)
    if not job:
        raise HTTPException(404, f"Job {job_id} not found.")
    return job


@testcase_router.get("/rag-documents")
def get_rag_documents(
    current_user: Annotated[User, Depends(get_current_active_user)] = ...,
):
    try:
        if current_user.role == "admin":
            docs = list_rag_documents(
                department_id=None,
                application_name=None
            )
        else:
            dept = getattr(current_user, "departmentid", None) or "general"
            app_name = getattr(current_user, "application_name", None) or ""

            docs = list_rag_documents(
                department_id=dept,
                application_name=app_name if app_name.strip() else None,
            )

        return {"documents": docs}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@testcase_router.delete("/rag-documents/{doc_id}")
def remove_rag_document(
    doc_id: str,
    current_user: Annotated[User, Depends(get_current_admin_user)] = ...,
):
    """Delete a document from the RAG knowledge base. Admin access required."""
    try:
        deleted_chunks = delete_rag_document(doc_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    
    return {"deleted_chunks": deleted_chunks, "doc_id": doc_id}


# ============================================================================
# Extract reference text (kept for backward compatibility)
# ============================================================================

@testcase_router.post("/extract-reference-text")
async def extract_reference_text(
    request: dict,
    current_user: Annotated[User, Depends(get_current_active_user)] = ...,
):
    try:
        file_content_base64 = request.get("file_content")
        filename            = request.get("filename")

        if not file_content_base64 or not filename:
            raise HTTPException(400, "Missing file content or filename")

        file_content = base64.b64decode(file_content_base64)
        text         = extract_text_from_file(file_content, filename)
        return {"text": text, "filename": filename}

    except ValueError as e:
        raise HTTPException(400, str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Failed to extract text: {e}")

# ============================================================================
# Generate test cases — RAG pipeline, testcase_client sourced from user record
# ============================================================================

@testcase_router.post("/generate-testcases")
async def generate_testcases(
    request: TestCaseRequest,
    current_user: Annotated[User, Depends(get_current_active_user)] = ...,
    save_activity: bool = True,
):
    """
    Generate test cases using structure-aware sliding-window pipeline.

    Flow:
      1.  Resolve testcase_client from user profile.
      2.  Load file path from JSON storage (uuid).
      3.  Move file to in_progress.
      4.  Save initial UserActivity record.
      5.  extract_pages_with_images() — text + OCR + image descriptions.
      6.  For each page:
            a. detect_page_structure()       — regex, zero LLM cost.
            b. build_windowed_context()      — prev_tail + current + next_head.
            c. retrieve_rag_chunks_for_page()— scoped to user's dept/application,
                                                returns [] if no documents ingested.
            d. generate_testcases_for_page_rag() — LLM with structure metadata.
      7.  Combine all page test cases.
      8.  process_and_clean_testcases() — LLM dedup + renumber (flow-ordered).
      9.  Move to completed, save JSON output, update UserActivity.
      10. Return consolidated result.
    """
    try:
        # ── 1. Resolve testcase_client from user profile ──────────────────────
        tc_type = (getattr(current_user, "testcase_client", None) or "UAT").upper()
        if tc_type not in ("UAT", "SIT"):
            tc_type = "UAT"

        # Resolve department and application for RAG scoping
        user_dept = getattr(current_user, "departmentid", None) or ""
        user_app  = getattr(current_user, "application_name", None) or ""

        print(f"\n{'='*60}")
        print(f"Generate request for UUID : {request.uuid}")
        print(f"Requested by              : {current_user.email}  role={current_user.role}")
        print(f"Testcase client           : {tc_type}")
        print(f"Department                : {user_dept or 'none'}")
        print(f"Application               : {user_app or 'none'}")
        print(f"{'='*60}\n")

        # ── 2. Load from JSON storage ─────────────────────────────────────────
        storage_data = get_file_record(request.uuid)
        if not storage_data:
            raise HTTPException(
                404,
                f"File not found for UUID: {request.uuid}. Please upload first.",
            )

        total_pages = storage_data.get("total_pages")
        print(f"Found record for UUID: {request.uuid}  total_pages={total_pages}")

        # ── Determine original file type ──────────────────────────────────────
        name_without_ext   = Path(request.document_name).stem
        last_value         = name_without_ext.split("_")[-1]
        original_file_type = "docx" if last_value == "converted" else "pdf"

        # ── 3. Move to in-progress ────────────────────────────────────────────
        move_file_to_status(request.uuid, IN_PROGRESS_DIR)
        updated_record = get_file_record(request.uuid)
        if not updated_record:
            raise HTTPException(500, "File record lost after moving to in_progress.")
        file_path = updated_record["file_path"]
        print(f"File in progress: {file_path}")

        # ── 4. Save initial activity ──────────────────────────────────────────
        if save_activity:
            from sqlmodel import Session
            from sqlmodel import select as _select
            from api.config.database import engine
            from api.model import UserActivity

            parts      = name_without_ext.split("_")
            demand_id  = parts[0] if len(parts) >= 3 else None
            project_id = parts[1] if len(parts) >= 3 else None

            try:
                with Session(engine) as db_session:
                    existing = db_session.exec(
                        _select(UserActivity).where(UserActivity.uuid == request.uuid)
                    ).first()

                    if existing:
                        existing.generation_completed    = False
                        existing.generation_completed_at = None
                        existing.output_file_path        = None
                        existing.total_pages_processed   = None
                        existing.successful_generations  = None
                        existing.failed_generations      = None
                        existing.updated_at              = datetime.now(timezone.utc)
                        db_session.add(existing)
                        db_session.commit()
                        print(f"Activity record reset for retry: {current_user.username}")
                    else:
                        activity = UserActivity(
                            uuid                 = request.uuid,
                            user_id              = current_user.id,
                            username             = current_user.username,
                            document_name        = request.document_name,
                            file_type            = original_file_type,
                            total_pages          = total_pages,
                            selected_page_indices= json.dumps([]),
                            testcase_client      = tc_type,
                            user_prompt_provided = bool(request.user_prompt),
                            user_prompt_text     = request.user_prompt,
                            demand_id            = demand_id,
                            project_id           = project_id,
                        )
                        db_session.add(activity)
                        db_session.commit()
                        print(f"Activity record created for: {current_user.username}")
            except Exception as db_error:
                print(f"Warning: failed to save initial activity: {db_error}")

        # ── 5. Read file bytes ────────────────────────────────────────────────
        with open(file_path, "rb") as fh:
            file_bytes = fh.read()
        filename  = Path(file_path).name
        up_prompt = (request.user_prompt or "").strip() or None

        # ── 6. Extract pages (4-pass: logo detection, text, OCR, assemble) ───
        print("\n🔍 Extracting pages with image support…")
        import asyncio as _asyncio
        try:
            pages = await _asyncio.wait_for(
                _asyncio.get_event_loop().run_in_executor(
                    None, extract_pages_with_images, file_bytes, filename
                ),
                timeout=90000,  # 150 min max for extraction
            )
        except _asyncio.TimeoutError:
            move_file_to_status(request.uuid, FAILED_DIR)
            raise HTTPException(504, "Page extraction timed out. PDF may be too large or image-heavy.")
        except ValueError as e:
            move_file_to_status(request.uuid, FAILED_DIR)
            raise HTTPException(422, str(e))

        if not pages:
            move_file_to_status(request.uuid, FAILED_DIR)
            raise HTTPException(422, "No content could be extracted from the document.")

        # ── 7. Structure-aware generation ──────────────────────────────────────
        from api.utils.rag_utils import should_skip_page, is_continuation_page

        all_testcases:      list  = []
        page_results:       list  = []
        rag_chunks_index:   dict  = {}
        generated_scenarios: list = []   # track scenario names for cross-page dedup

        # Pre-merge continuation pages so requirements are never split across LLM calls
        merged_pages: list = []
        skip_next = False
        for idx, page in enumerate(pages):
            if skip_next:
                skip_next = False
                continue
            prev_text = pages[idx - 1]["complete_text"] if idx > 0 else ""
            if idx > 0 and is_continuation_page(page["complete_text"], prev_text):
                if merged_pages:
                    last = merged_pages[-1]
                    last["complete_text"] = (
                        last["complete_text"] + "\n\n" + page["complete_text"]
                    )
                    last["merged"] = True
                    print(f"   🔗 Page {page['page_number']} merged into page {last['page_number']} (continuation)")
                    skip_next = False
                    continue
            merged_pages.append(dict(page))

        print(f"\n📋 Pages after continuation merge: {len(merged_pages)} (was {len(pages)})")

        for idx, page in enumerate(merged_pages):
            pn        = page["page_number"]
            page_text = page["complete_text"]

            print(f"\n{'─'*55}")
            print(f"🔄 Page {pn}/{len(merged_pages)}  |  extracted chars={len(page_text)}")

            # Step A — pre-filter boilerplate (zero LLM cost)
            skip, skip_reason = should_skip_page(page_text)
            if skip:
                print(f"   ⏭  Skipped: {skip_reason}")
                page_results.append({
                    "page_number": pn, "testcases": [],
                    "status": "skipped", "error": skip_reason,
                })
                continue

            # Step B — detect structure (zero LLM cost, pure regex)
            page_meta = detect_page_structure(page_text)
            print(f"   section_type={page_meta['section_type']} "
                  f"| tx={page_meta['transaction_codes']} "
                  f"| screens={page_meta['screen_numbers']}")

            # Step C — build context window (prev_tail + next_head) SEPARATE from page_text
            prev_tail = ""
            if idx > 0:
                prev_full = merged_pages[idx - 1]["complete_text"]
                if prev_full.strip():
                    prev_tail = (
                        f"[Previous page {merged_pages[idx-1]['page_number']} — tail]:\n"
                        f"{prev_full[-600:].strip()}"
                    )
            next_head = ""
            if idx < len(merged_pages) - 1:
                next_full = merged_pages[idx + 1]["complete_text"]
                if next_full.strip():
                    next_head = (
                        f"[Next page {merged_pages[idx+1]['page_number']} — head]:\n"
                        f"{next_full[:400].strip()}"
                    )
            context_window = "\n\n".join(filter(None, [prev_tail, next_head]))

            # Step D — enriched RAG query using structural metadata
            rag_query_parts = [page_text]
            if page_meta["transaction_codes"]:
                rag_query_parts.append("Transaction codes: " + ", ".join(page_meta["transaction_codes"]))
            if page_meta["screen_numbers"]:
                rag_query_parts.append("Screen numbers: " + ", ".join(page_meta["screen_numbers"]))
            rag_query_parts.append(f"Section type: {page_meta['section_type']}")
            rag_query = " ".join(rag_query_parts)

            # RAG retrieval — strictly scoped to user's department/application.
            # Returns [] automatically if no documents are ingested for this scope.
            rag_chunks = retrieve_rag_chunks_for_page(
                rag_query,
                top_k            = PAGE_RAG_TOP_K,
                doc_ids          = None,
                department_id    = user_dept,
                application_name = user_app,
            )
            for c in rag_chunks:
                key = c["text"]
                if key not in rag_chunks_index:
                    rag_chunks_index[key] = c

            # Step E — generate with SEPARATED concerns:
            #   page_text       → source for generation
            #   context_window  → reading context only (passed separately)
            #   already_covered → prevents regenerating covered scenarios
            result = generate_testcases_for_page_rag(
                page_number                     = pn,
                page_text                       = page_text.strip(),
                document_name                   = request.document_name,
                rag_chunks                      = rag_chunks,
                user_prompt                     = up_prompt,
                testcase_type                   = tc_type,
                page_metadata                   = page_meta,
                prompt_file_content             = None,
                selected_department_description = None,
                department_id                   = user_dept,
                context_window                  = context_window,
                already_covered                 = list(generated_scenarios),
                selected_checkboxes             = request.selected_checkboxes or [],   # NEW
            )

            page_results.append(result)
            if result["status"] == "success":
                all_testcases.extend(result["testcases"])
                for tc in result["testcases"]:
                    sn = tc.get("Scenario Name") or tc.get("Sub Function Description", "")
                    if sn and sn not in generated_scenarios:
                        generated_scenarios.append(sn)

        successful = sum(1 for r in page_results if r["status"] == "success")
        skipped    = sum(1 for r in page_results if r["status"] == "skipped")
        failed_pg  = sum(1 for r in page_results if r["status"] == "failed")

        print(f"\n{'='*55}")
        print(f"✅ Generation done. Pages={len(pages)} "
              f"Success={successful} Skipped={skipped} Failed={failed_pg}")
        print(f"   Total testcases before dedup: {len(all_testcases)}")

        # ── 8. Build result structure ─────────────────────────────────────────
        result_obj = {
            "document_name"   : request.document_name,
            "uuid"            : request.uuid,
            "testcase_client" : tc_type,
            "summary"         : {
                "total_pages_processed"  : len(pages),
                "successful_generations" : successful,
                "failed_generations"     : failed_pg,
                "skipped_generations"    : skipped,
                "total_testcase_count"   : len(all_testcases),
            },
            "combined_testcases": all_testcases,
            "page_summary"    : [
                {
                    "page_number"    : r["page_number"],
                    "status"         : r["status"],
                    "testcases_count": len(r.get("testcases", [])),
                    "error"          : r.get("error", ""),
                }
                for r in page_results
            ],
            "rag_chunks_used" : list(rag_chunks_index.values()),
        }

        if not all_testcases:
            print("⚠ No test cases generated — skipping duplicate removal.")
        else:
            print("\n🔄 Running duplicate removal…")
            result_obj = process_and_clean_testcases(result_obj)

        # ── 9. Move to completed ──────────────────────────────────────────────
        move_file_to_status(request.uuid, COMPLETED_DIR)

        # ── 10. Save output to file ───────────────────────────────────────────
        output_file_path = save_output_to_file(
            result          = result_obj,
            username        = current_user.username,
            document_name   = request.document_name,
            uuid            = request.uuid,
            testcase_client = tc_type,
        )
        print(f"Output saved to: {output_file_path}")

        # ── 11. Update activity record ────────────────────────────────────────
        if save_activity:
            from api.config.database import engine
            from api.model import UserActivity
            from sqlmodel import Session
            from sqlmodel import select as _select

            try:
                with Session(engine) as db_session:
                    activity = db_session.exec(
                        _select(UserActivity).where(UserActivity.uuid == request.uuid)
                    ).first()
                    if activity:
                        activity.generation_completed    = True
                        activity.generation_completed_at = datetime.now(timezone.utc)
                        activity.output_file_path        = output_file_path
                        activity.total_pages_processed   = (
                            result_obj.get("summary", {}).get("total_pages_processed", 0)
                        )
                        activity.successful_generations  = (
                            result_obj.get("summary", {}).get("successful_generations", 0)
                        )
                        activity.failed_generations      = (
                            result_obj.get("summary", {}).get("failed_generations", 0)
                        )
                        activity.updated_at = datetime.now(timezone.utc)
                        db_session.add(activity)
                        db_session.commit()
                        print("Activity record updated with completion details.")
            except Exception as db_error:
                print(f"Warning: failed to update activity record: {db_error}")

        # ── 12. Cleanup ───────────────────────────────────────────────────────
        delete_file_record(request.uuid)

        result_obj["output_file_path"] = output_file_path
        return JSONResponse(content=result_obj)

    except HTTPException:
        try:
            record = get_file_record(request.uuid)
            if record and IN_PROGRESS_DIR.name in record.get("file_path", ""):
                move_file_to_status(request.uuid, FAILED_DIR)
        except Exception:
            pass
        raise

    except Exception as e:
        import traceback
        print(f"Unexpected error: {e}")
        print(traceback.format_exc())
        try:
            record = get_file_record(request.uuid)
            if record:
                move_file_to_status(request.uuid, FAILED_DIR)
        except Exception:
            pass
        raise HTTPException(500, str(e))





# ============================================================================
# Admin — export all users as list (with filters + sorting)
# ============================================================================

@testcase_router.get("/admin/export-users")
async def export_users(
    sort_by:     str  = "created_at",
    sort_order:  str  = "desc",
    department:  Optional[str] = None,
    role:        Optional[str] = None,
    is_active:   Optional[int] = None,
    current_user: Annotated[User, Depends(get_current_admin_user)] = ...,
):
    """
    Return all users with optional filters. Used for the admin dashboard table
    and Excel export.

    Query params:
      sort_by     : created_at | username | first_name | last_name | departmentid (default: created_at)
      sort_order  : asc | desc (default: desc)
      department  : filter by departmentid (optional)
      role        : filter by role — user | admin (optional)
      is_active   : filter by is_active — 0 | 1 (optional)
    """
    from sqlmodel import Session, select as _select
    from api.config.database import engine
    from api.model import User as _User

    VALID_SORT = {"created_at", "username", "first_name", "last_name", "departmentid", "email"}
    if sort_by not in VALID_SORT:
        sort_by = "created_at"
    sort_order = "desc" if sort_order.lower() != "asc" else "asc"

    try:
        with Session(engine) as db:
            stmt = _select(_User)
            if department:
                stmt = stmt.where(_User.departmentid == department)
            if role:
                stmt = stmt.where(_User.role == role)
            if is_active is not None:
                stmt = stmt.where(_User.is_active == is_active)

            col_attr = getattr(_User, sort_by, _User.created_at)
            stmt = stmt.order_by(col_attr.desc() if sort_order == "desc" else col_attr.asc())

            users = db.exec(stmt).all()

        return {
            "total": len(users),
            "filters": {
                "department": department,
                "role": role,
                "is_active": is_active,
                "sort_by": sort_by,
                "sort_order": sort_order,
            },
            "users": [
                {
                    "id"              : u.id,
                    "first_name"      : u.first_name,
                    "last_name"       : u.last_name,
                    "username"        : u.username,
                    "email"           : u.email,
                    "departmentid"    : u.departmentid or "",
                    "role"            : u.role,
                    "is_active"       : u.is_active,
                    "disabled"        : u.disabled,
                    "testcase_client" : u.testcase_client or "UAT",
                    "application_name": u.application_name or "",
                    "login_count"     : u.login_count or 0,
                    "created_at"      : u.created_at.isoformat() if u.created_at else "",
                    "updated_at"      : u.updated_at.isoformat() if u.updated_at else "",
                }
                for u in users
            ],
        }
    except Exception as e:
        import traceback
        print(traceback.format_exc())
        raise HTTPException(500, f"Failed to export users: {e}")


# ============================================================================
# Admin — activity dashboard with date range filter
# ============================================================================

@testcase_router.get("/admin/activity-dashboard")
async def activity_dashboard(
    start_date:  Optional[str] = None,   # ISO date string YYYY-MM-DD
    end_date:    Optional[str] = None,   # ISO date string YYYY-MM-DD
    today:       bool          = False,  # shortcut: today only
    username:    Optional[str] = None,   # filter by specific user
    department:  Optional[str] = None,   # filter by department
    testcase_client: Optional[str] = None,
    current_user: Annotated[User, Depends(get_current_admin_user)] = ...,
):
    """
    Return activity records filtered by date range.

    Query params:
      start_date      : YYYY-MM-DD (inclusive)
      end_date        : YYYY-MM-DD (inclusive, defaults to today if start_date given)
      today           : bool shortcut — overrides start_date/end_date
      username        : filter by username (optional)
      department      : filter by departmentid (optional)
      testcase_client : UAT | SIT (optional)
    """
    from sqlmodel import Session, select as _select
    from api.config.database import engine
    from api.model import UserActivity, User as _User
    from datetime import date, timedelta
    import json as _json

    try:
        now_date = datetime.now(timezone.utc).date()

        if today:
            dt_start = datetime(now_date.year, now_date.month, now_date.day,
                                0, 0, 0, tzinfo=timezone.utc)
            dt_end   = datetime(now_date.year, now_date.month, now_date.day,
                                23, 59, 59, tzinfo=timezone.utc)
        else:
            if start_date:
                sd       = date.fromisoformat(start_date)
                dt_start = datetime(sd.year, sd.month, sd.day, 0, 0, 0, tzinfo=timezone.utc)
            else:
                # default: last 30 days
                sd       = now_date - timedelta(days=30)
                dt_start = datetime(sd.year, sd.month, sd.day, 0, 0, 0, tzinfo=timezone.utc)

            if end_date:
                ed     = date.fromisoformat(end_date)
                dt_end = datetime(ed.year, ed.month, ed.day, 23, 59, 59, tzinfo=timezone.utc)
            else:
                dt_end = datetime(now_date.year, now_date.month, now_date.day,
                                  23, 59, 59, tzinfo=timezone.utc)

        with Session(engine) as db:
            stmt = _select(UserActivity).where(
                UserActivity.created_at >= dt_start,
                UserActivity.created_at <= dt_end,
            )
            if username:
                stmt = stmt.where(UserActivity.username == username)
            if testcase_client:
                stmt = stmt.where(UserActivity.testcase_client == testcase_client.upper())

            # department filter requires joining User table
            if department:
                stmt = stmt.join(_User, UserActivity.user_id == _User.id).where(
                    _User.departmentid == department
                )

            stmt = stmt.order_by(UserActivity.created_at.desc())
            activities = db.exec(stmt).all()

            # Also fetch user dept map for enriching response
            all_users = db.exec(_select(_User)).all()
            user_dept_map = {u.username: (u.departmentid or "") for u in all_users}
            user_app_map  = {u.username: (u.application_name or "") for u in all_users}

        records = []
        for a in activities:
            try:
                pg_idx = _json.loads(a.selected_page_indices) if a.selected_page_indices else []
            except Exception:
                pg_idx = []

            records.append({
                "id"                     : a.id,
                "uuid"                   : a.uuid,
                "username"               : a.username,
                "department"             : user_dept_map.get(a.username, ""),
                "application_name"       : user_app_map.get(a.username, ""),
                "document_name"          : a.document_name,
                "file_type"              : a.file_type,
                "total_pages"            : a.total_pages,
                "testcase_client"        : a.testcase_client,
                "generation_completed"   : a.generation_completed,
                "total_pages_processed"  : a.total_pages_processed,
                "successful_generations" : a.successful_generations,
                "failed_generations"     : a.failed_generations,
                "demand_id"              : a.demand_id or "",
                "project_id"             : a.project_id or "",
                "user_prompt_provided"   : a.user_prompt_provided,
                "created_at"             : a.created_at.isoformat() if a.created_at else "",
                "generation_completed_at": a.generation_completed_at.isoformat()
                                            if a.generation_completed_at else "",
            })

        # ── Summary statistics ──
        total          = len(records)
        completed      = sum(1 for r in records if r["generation_completed"])
        pending        = total - completed
        uat_count      = sum(1 for r in records if r["testcase_client"] == "UAT")
        sit_count      = sum(1 for r in records if r["testcase_client"] == "SIT")
        pdf_count      = sum(1 for r in records if r["file_type"] == "pdf")
        docx_count     = sum(1 for r in records if r["file_type"] == "docx")
        total_pages    = sum(r["total_pages_processed"] or 0 for r in records)
        total_success  = sum(r["successful_generations"] or 0 for r in records)
        total_failed   = sum(r["failed_generations"] or 0 for r in records)

        # Per-user summary
        user_summary: dict = {}
        for r in records:
            u = r["username"]
            if u not in user_summary:
                user_summary[u] = {
                    "username": u,
                    "department": r["department"],
                    "total_activities": 0,
                    "completed": 0,
                    "uat": 0,
                    "sit": 0,
                    "pages_processed": 0,
                }
            user_summary[u]["total_activities"] += 1
            if r["generation_completed"]:
                user_summary[u]["completed"] += 1
            if r["testcase_client"] == "UAT":
                user_summary[u]["uat"] += 1
            else:
                user_summary[u]["sit"] += 1
            user_summary[u]["pages_processed"] += r["total_pages_processed"] or 0

        return {
            "date_range": {
                "start": dt_start.isoformat(),
                "end"  : dt_end.isoformat(),
                "today": today,
            },
            "summary": {
                "total_activities"        : total,
                "completed_activities"    : completed,
                "pending_activities"      : pending,
                "uat_generations"         : uat_count,
                "sit_generations"         : sit_count,
                "pdf_uploads"             : pdf_count,
                "docx_uploads"            : docx_count,
                "total_pages_processed"   : total_pages,
                "total_successful_gen"    : total_success,
                "total_failed_gen"        : total_failed,
            },
            "user_summary": list(user_summary.values()),
            "activities"  : records,
        }

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        print(traceback.format_exc())
        raise HTTPException(500, f"Activity dashboard failed: {e}")


# ============================================================================
# Generate Feature File from test cases
# ============================================================================

class FeatureFileRequest(BaseModel):
    document_name: str
    testcases: list
    department_id: Optional[str] = None
    testcase_client: str = "UAT"


@testcase_router.post("/generate-feature-file")
async def generate_feature_file_endpoint(
    request: FeatureFileRequest,
    current_user: Annotated[User, Depends(get_current_active_user)] = ...,
):
    """
    Generate a Gherkin .feature file from provided test cases.

    Accepts the combined_testcases array from the generate-testcases response.
    Returns the feature file as plain text.

    The department_id determines which columns are selected for the LLM prompt:
    - Trade Finance (171): uses Function/Sub Function/Pre-Condition/Description columns
    - All others: uses Test Case Name/Scenario/Type/Steps/Test Data/Expected Result columns
    """
    try:
        if not request.testcases:
            raise HTTPException(400, "No test cases provided.")

        if len(request.testcases) > 500:
            raise HTTPException(
                400,
                f"Too many test cases ({len(request.testcases)}). Maximum 500 allowed per feature file generation."
            )

        dept_id = request.department_id or getattr(current_user, "departmentid", None)
        tc_client = (request.testcase_client or getattr(current_user, "testcase_client", "UAT") or "UAT").upper()

        print(f"\n{'='*50}")
        print(f"Feature file request: {request.document_name}")
        print(f"Test cases count    : {len(request.testcases)}")
        print(f"Department ID       : {dept_id}")
        print(f"Testcase client     : {tc_client}")
        print(f"Requested by        : {current_user.email}")
        print(f"{'='*50}\n")

        feature_content = generate_feature_file(
            testcases=request.testcases,
            document_name=request.document_name,
            department_id=dept_id,
            testcase_client=tc_client,
        )

        return JSONResponse(content={
            "feature_content": feature_content,
            "document_name": request.document_name,
            "testcase_client": tc_client,
            "total_testcases": len(request.testcases),
        })

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(422, str(e))
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    except Exception as e:
        import traceback
        print(f"Feature file generation error: {traceback.format_exc()}")
        raise HTTPException(500, f"Feature file generation failed: {str(e)}")
    
@testcase_router.get("/testcase-categories")
async def get_testcase_categories(
    current_user: Annotated[User, Depends(get_current_active_user)] = ...,
):
    """Return available testcase category checkboxes for the UI."""
    from api.utils.rag_utils import CHECKBOX_PROMPT_MAP
    return {
        "categories": [
            {"id": key, "label": val["label"]}
            for key, val in CHECKBOX_PROMPT_MAP.items()
        ]
    }



# ============================================================================
# Generate API Testcases via LLM
# ============================================================================

from api.utils.eis_api_utils import call_eis_api
from api.model import RunApiTestcasesRequest
from api.model import APITestCaseGenerateRequest
from api.utils.api_testcase_utils import generate_api_testcases_via_llm, apply_mandatory_fields, make_rrn

@testcase_router.post("/generate-api-testcases")
async def generate_api_testcases_endpoint(
    request: APITestCaseGenerateRequest,
    current_user: Annotated[User, Depends(get_current_active_user)] = ...,
):
    """
    Generate positive/negative test cases for a single API from a JSON spec
    (api_name, api_url, method, payload{field: {value, required, validation}}).

    Before calling the LLM:
      - REQUEST_AUTH_ID / REQUEST_TELLER_ID / BRANCH_CODE are auto-injected with
        default values if missing from the payload.
      - SOURCE_ID is validated as mandatory — request is rejected if absent.
      - REQUEST_REFERENCE_NUMBER (RRN) is derived from SOURCE_ID unless already
        supplied by the user.
    """
    tc_type = (getattr(current_user, "testcase_client", None) or "UAT").upper()
    if tc_type not in ("UAT", "SIT"):
        tc_type = "UAT"

    spec = request.api_spec
    if not spec.payload:
        raise HTTPException(422, "The 'payload' object must contain at least one field.")

    payload_dict = {k: v.dict() for k, v in spec.payload.items()}

    # ── Enforce mandatory fields / auto-inject defaults / derive RRN ────────
    try:
        payload_dict, generated_rrn = apply_mandatory_fields(payload_dict)
    except ValueError as e:
        raise HTTPException(422, str(e))

    print(f"\n{'='*60}")
    print(f"API testcase generation request: {spec.api_name} ({spec.method} {spec.api_url})")
    print(f"Requested by: {current_user.email}  testcase_client={tc_type}")
    print(f"{'='*60}\n")

    try:
        testcases = generate_api_testcases_via_llm(
            api_name=spec.api_name,
            api_url=spec.api_url,
            method=spec.method,
            payload=payload_dict,
            user_prompt=(request.user_prompt or "").strip() or None,
            testcase_type=tc_type,
        )
    except Exception as e:
        raise HTTPException(500, f"API test case generation failed: {e}")

    result_obj = {
        "document_name": spec.api_name,
        "api_url": spec.api_url,
        "method": spec.method.upper(),
        "testcase_client": tc_type,
        "generated_reference_number": generated_rrn,
        "summary": {
            "total_testcase_count": len(testcases),
            "positive_count": sum(1 for t in testcases if t["Test Case Type"] == "Positive"),
            "negative_count": sum(1 for t in testcases if t["Test Case Type"] == "Negative"),
        },
        "combined_testcases": testcases,
    }

    try:
        safe_name = "".join(c for c in spec.api_name if c.isalnum() or c in (" ", "-", "_")).strip() or "api"
        output_path = save_output_to_file(
            result=result_obj,
            username=current_user.username,
            document_name=f"{safe_name}_apispec",
            uuid=f"api-{safe_name}",
            testcase_client=tc_type,
        )
        result_obj["output_file_path"] = output_path
    except Exception as e:
        print(f"⚠ Could not save API testcase output file: {e}")

    return JSONResponse(content=result_obj)


@testcase_router.post("/run-api-testcases")
async def run_api_testcases_endpoint(
    request: RunApiTestcasesRequest,
    current_user: Annotated[User, Depends(get_current_active_user)] = ...,
):
    """
    Executes each generated test case's payload against the target API via the EIS
    encrypted flow, and attaches the response under "Actual_Response" for every row.

    RRN resolution per test case (in order):
      1. payload["REQUEST_REFERENCE_NUMBER"] — respected as-is IF the user supplied
         their own RRN in the original input JSON (it will already be present in
         every test case's payload in that case, since it's then a normal field).
      2. Otherwise — a BRAND NEW unique RRN is generated for THIS call via
         make_rrn(SOURCE_ID), so every API call gets its own reference number
         (same "SBI"+SOURCE_ID prefix, different random suffix each time). This
         prevents "REFERENCE NUMBER NOT UNIQUE" errors when re-running or when
         many test cases call the API in the same batch.
    """
    updated: list = []
    for tc in request.testcases:
        payload = dict(tc.get("Test Data") or {})
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}

        url = tc.get("API_URL") or request.api_url

        user_supplied_rrn = payload.get("REQUEST_REFERENCE_NUMBER")
        if user_supplied_rrn:
            rrn = user_supplied_rrn
        else:
            source_id = payload.get("SOURCE_ID")
            if source_id:
                try:
                    rrn = make_rrn(str(source_id))
                except ValueError as e:
                    updated.append({**tc, "Actual_Response": f"ERROR: {e}"})
                    continue
            else:
                rrn = request.generated_reference_number or ""

        try:
            actual_response = call_eis_api(json.dumps(payload, ensure_ascii=False), rrn, url)
        except Exception as e:
            actual_response = f"ERROR: {e}"

        updated.append({**tc, "Actual_Response": actual_response})

    return JSONResponse(content={"testcases": updated})



from api.model import EvaluateApiTestcasesRequest
from api.utils.api_testcase_utils import evaluate_api_testcases_via_llm

@testcase_router.post("/evaluate-api-testcases")
async def evaluate_api_testcases_endpoint(
    request: EvaluateApiTestcasesRequest,
    current_user: Annotated[User, Depends(get_current_active_user)] = ...,
):
    """
    "Run Automation" — takes already-executed test cases (must already carry
    Actual_Response) and uses the LLM to judge Pass/Fail per row, attaching
    "Pass_Fail" and "Remarks" to each test case.
    """
    if not request.testcases:
        raise HTTPException(422, "No test cases provided to evaluate.")

    missing_response = [
        tc.get("Test Case ID", "?") for tc in request.testcases
        if not tc.get("Actual_Response")
    ]
    if missing_response:
        raise HTTPException(
            422,
            f"Run the test cases first — {len(missing_response)} test case(s) have no "
            f"Actual_Response yet (e.g. {missing_response[0]})."
        )

    try:
        verdicts = evaluate_api_testcases_via_llm(request.testcases)
    except Exception as e:
        raise HTTPException(500, f"Pass/Fail evaluation failed: {e}")

    updated = []
    for tc in request.testcases:
        tc_id = tc.get("Test Case ID", "")
        verdict = verdicts.get(tc_id, {"Pass_Fail": "Fail", "Remarks": "No verdict returned by evaluator."})
        updated.append({
            **tc,
            "Pass_Fail": verdict["Pass_Fail"],
            "Remarks": verdict["Remarks"],
        })

    return JSONResponse(content={
        "testcases": updated,
        "summary": {
            "pass_count": sum(1 for t in updated if t["Pass_Fail"] == "Pass"),
            "fail_count": sum(1 for t in updated if t["Pass_Fail"] == "Fail"),
        },
    })
