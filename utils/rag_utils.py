"""
api/utils/rag_utils.py
RAG pipeline: document ingestion into ChromaDB + per-page retrieval + testcase generation.

Changes vs previous version:
  1. RERANKER LAYER  — after cosine retrieval, a cross-encoder (or LLM-based fallback)
     reranks chunks by semantic relevance to the page text and filters out low-scoring
     ones (score < RERANK_THRESHOLD).  This prevents keyword-match false positives
     (e.g. "CBS" appearing in both page and a completely unrelated chunk).

  2. finish_reason == "length" RECOVERY — when the LLM hits the token limit mid-response
     the partial JSON is repaired and a second "continuation" call retrieves the
     remaining test cases.  Both batches are merged.  This prevents silent loss of
     test cases on dense pages.

  3. Distributed / load-isolation via CELERY (optional) — the heavy per-page
     generation work can be dispatched to a Celery worker pool running on a
     separate process/machine.  When Celery is NOT configured the code falls back
     transparently to the existing synchronous path.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
import re
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional, Tuple

import chromadb
import fitz  # PyMuPDF
import httpx
from dotenv import load_dotenv
from langchain_text_splitters import RecursiveCharacterTextSplitter
from openai import AzureOpenAI
from PIL import Image

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
_AZURE_API_KEY      = os.getenv("AZURE_API_KEY", "")
_AZURE_API_ENDPOINT = os.getenv("AZURE_API_ENDPOINT", "")
_AZURE_API_VERSION  = os.getenv("AZURE_API_VERSION", "2024-12-01-preview")
_CHAT_MODEL         = os.getenv("AZURE_CHAT_MODEL", os.getenv("AZURE_MODEL_NAME", "gpt-4.1-mini"))
_EMBEDDING_MODEL    = os.getenv("AZURE_EMBEDDING_MODEL", "text-embedding-ada-002")
_RAG_CHROMA_DIR     = os.getenv("RAG_CHROMA_DIR", "./rag_knowledge_base")

# Chunking
CHUNK_SIZE    = 800
CHUNK_OVERLAP = 150

# ── Per-page RAG ──────────────────────────────────────────────────────────────
PAGE_RAG_TOP_K      = 10    # retrieve more candidates so reranker has room to filter
RERANK_TOP_K        = 5     # keep this many after reranking
RERANK_THRESHOLD    = 0.25  # discard chunks whose rerank score < this (0–1 scale)
MAX_CONTEXT_CHARS   = 12_000

# Image processing
IMAGE_OCR_MAX_WORKERS = 4
MAX_IMAGE_RATIO       = 100   # max width:height before resize

# ── Deduplication thresholds ──────────────────────────────────────────────────
NEAR_DUP_COSINE_THRESHOLD = 0.95   # skip chunk if similarity > this during ingest
MMR_FETCH_K               = 20     # fetch this many from ChromaDB before MMR
MMR_FINAL_K               = 10      # return this many after MMR diversification
MMR_LAMBDA                = 0.5    # 0=max diversity, 1=max relevance
TRADE_FINANCE_DEPT_ID     = "171"
YONO_2_DEPT_ID            = "465"

# ── Checkbox-driven prompt fragments ─────────────────────────────────────────
# Each key matches the checkbox ID sent from the frontend.
# Value is the instruction appended to the LLM prompt when that checkbox is selected.
CHECKBOX_PROMPT_MAP: Dict[str, Dict[str, str]] = {
    "functional_coverage": {
        "label": "Functional Coverage (Positive, Negative, Boundary & Field Validation)",
        "prompt": (
            "FUNCTIONAL COVERAGE: Generate comprehensive Positive test cases (valid inputs, "
            "happy path, expected successful outcomes), Negative test cases (invalid inputs, "
            "missing required fields, wrong data types, constraint violations), and Boundary "
            "Value test cases (exact limit, one below min, one above max, zero, null, empty "
            "string) for every field, button, and functional element on this page. "
            "Each field must have separate positive, negative, and boundary test cases — "
            "never merged into one."
        ),
    },
    "format_validation": {
        "label": "Format & Data Validation (Email, PAN, dates, account numbers)",
        "prompt": (
            "FORMAT & DATA VALIDATION: For every field with a specified format on this page, "
            "generate test cases verifying: valid format acceptance (e.g. PAN: ABCDE1234F, "
            "email: user@domain.com, date: DD/MM/YYYY, account: 17 digits), rejection of "
            "wrong format (e.g. PAN without capital letters, email without @, date as text), "
            "rejection of partial format (e.g. 16-digit account number), and rejection of "
            "special characters where not allowed. Use exact format patterns from the document."
        ),
    },
    "business_rules_workflow": {
        "label": "Business Rules & Workflow (rules, workflow steps, state transitions)",
        "prompt": (
            "BUSINESS RULES & WORKFLOW: Generate test cases for every business rule explicitly "
            "stated in the document (e.g. 'user must have at least one linked account', "
            "'amount cannot exceed daily limit'). For workflow sequences, generate ONE test "
            "case per step verifying the step executes correctly, and ONE test case per "
            "decision branch verifying the correct path is taken. For state transitions "
            "(e.g. Pending → Active → Closed), generate ONE test case per valid transition "
            "and ONE per invalid/blocked transition with the expected error."
        ),
    },
    "role_based_access": {
        "label": "Role-Based Access Control (authentication, authorization, permissions)",
        "prompt": (
            "ROLE-BASED ACCESS CONTROL: Generate test cases verifying: each user role "
            "(as documented) can access only their permitted screens/features, unauthorized "
            "roles are blocked with appropriate error messages, unauthenticated users are "
            "redirected to login, users cannot escalate their own privileges, and sensitive "
            "operations require the correct role. Use role names exactly as documented."
        ),
    },
    "ui_navigation": {
        "label": "UI & Navigation (screen elements, navigation flows, layout)",
        "prompt": (
            "UI & NAVIGATION: For every screen documented, generate test cases verifying: "
            "all documented UI elements (fields, buttons, labels, menus, breadcrumbs) are "
            "present and correctly labelled, each navigation thread (source → destination) "
            "works as ONE complete test case, conditional UI elements show/hide based on "
            "documented conditions, and error/success messages appear exactly as documented. "
            "ONE navigation = ONE test case. Never split a navigation into sub-steps."
        ),
    },
    "file_operations": {
        "label": "File Operations (upload, download, file format & size validation)",
        "prompt": (
            "FILE OPERATIONS: Generate test cases for every file upload/download operation "
            "documented, covering: successful upload of valid file type and size, rejection "
            "of unsupported file types with error message, rejection of files exceeding max "
            "size with error message, successful download producing correct file content and "
            "format, and upload of empty/corrupted files. Use exact file type and size limits "
            "from the document."
        ),
    },
    "session_security": {
        "label": "Session & Security (session management, timeout, security checks)",
        "prompt": (
            "SESSION & SECURITY: Generate test cases for: session creation after successful "
            "login, session expiry after documented timeout period, behavior after session "
            "expiry (redirect to login, data not accessible), logout clearing session data, "
            "prevention of concurrent sessions if documented, and rejection of expired/invalid "
            "tokens. Also cover: SQL injection attempts in input fields, XSS in text inputs, "
            "CSRF protection on state-changing operations."
        ),
    },
    "api_integration": {
        "label": "API & Integration (frontend/backend APIs, third-party, microservices)",
        "prompt": (
            "API & INTEGRATION: For every API or integration point documented, generate test "
            "cases verifying: successful API call with valid payload returns documented response "
            "(HTTP status, response fields), API call with invalid/missing parameters returns "
            "documented error response, third-party service returning error is handled gracefully "
            "on the UI, and API method/protocol matches specification (GET/POST, HTTPS). "
            "Use exact endpoint paths, request/response formats, and error codes from the document."
        ),
    },
    "database_integrity": {
        "label": "Database & Data Integrity (DB operations, transactions, data mapping)",
        "prompt": (
            "DATABASE & DATA INTEGRITY: Generate test cases verifying: successful record "
            "creation/update/deletion reflects correctly in the UI, data entered in UI is "
            "stored with correct field mapping in the database, mandatory database fields "
            "are populated (including audit fields like created_at, updated_at, maker_id), "
            "transaction atomicity (all-or-nothing: if one step fails, none persist), "
            "and foreign key/unique constraint violations produce correct errors."
        ),
    },
    "error_handling_retry": {
        "label": "Error Handling & Retry (failure handling, retries, timeouts, error mapping)",
        "prompt": (
            "ERROR HANDLING & RETRY: Generate test cases for: documented error conditions "
            "displaying exact error messages from the specification, system behavior when a "
            "downstream service is unavailable (graceful degradation, user-friendly message), "
            "retry behavior for transient failures if documented (correct number of retries, "
            "backoff), request timeout showing documented timeout message, and error codes "
            "mapping to correct user-facing messages. Only test error scenarios explicitly "
            "mentioned or clearly implied by the document."
        ),
    },
    "concurrent_performance": {
        "label": "Concurrent & Performance (concurrent users, race conditions, load behaviour)",
        "prompt": (
            "CONCURRENT & PERFORMANCE: Generate test cases for: simultaneous operations by "
            "multiple users on the same record (last-write-wins or lock behavior as documented), "
            "duplicate submission prevention (double-click on submit button), race conditions "
            "in state transitions (two users changing the same record simultaneously), and "
            "screen/API load time meeting any documented performance SLA. Only generate these "
            "if concurrency or performance requirements are explicitly mentioned in the document."
        ),
    },
    "logging_audit_migration": {
        "label": "Logging, Audit & Migration (audit trails, monitoring, data migration)",
        "prompt": (
            "LOGGING, AUDIT & MIGRATION: Generate test cases for: audit trail entries created "
            "for every documented state-changing operation (with correct user, timestamp, "
            "before/after values), log entries generated for documented events (errors, "
            "logins, key actions), monitoring alerts triggered for documented threshold "
            "breaches, and data migration correctness (migrated records match source data, "
            "no data loss, correct field mapping). Only generate where explicitly documented."
        ),
    },
}

INGEST_SPLIT_THRESHOLD = 200   # pages — split PDFs larger than this before ingesting
RAG_MIN_RELEVANCE_SCORE = 0.55

# ── Generation ────────────────────────────────────────────────────────────────
# Increase max_tokens budget — helps avoid finish_reason=length on dense pages.
# If your deployment quota is lower, reduce this value.
GENERATION_MAX_TOKENS  = 15000
CONTINUATION_MAX_TOKENS = 8000   # budget for the continuation call

Path(_RAG_CHROMA_DIR).mkdir(parents=True, exist_ok=True)

_print_lock = Lock()

# ── Azure client ──────────────────────────────────────────────────────────────
_az = AzureOpenAI(
    azure_endpoint=_AZURE_API_ENDPOINT,
    api_version=_AZURE_API_VERSION,
    api_key=_AZURE_API_KEY,
    http_client=httpx.Client(verify=False),
)

# ── ChromaDB — lazy initialization to avoid crash on import ───────────────────
_chroma:     Optional[chromadb.PersistentClient] = None
_collection: Optional[object]                    = None



def _get_collection():
    """
    Lazy getter for ChromaDB collection.
    Returns the collection or None if ChromaDB is unavailable.
    Never raises — callers must handle None.
    """
    global _chroma, _collection
    if _collection is not None:
        return _collection
    try:
        _chroma     = chromadb.PersistentClient(path=_RAG_CHROMA_DIR)
        _collection = _chroma.get_or_create_collection(
            name="rag_knowledge_base",
            metadata={"hnsw:space": "cosine"},
        )
        print(f"✓ ChromaDB collection ready (path={_RAG_CHROMA_DIR})")
        return _collection
    except Exception as e:
        print(f"⚠ ChromaDB initialization failed: {e}")
        _chroma     = None
        _collection = None
        return None


# ══════════════════════════════════════════════════════════════════════════════
# INGEST HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _clean_text_for_ingest(text: str) -> str:
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [l for l in text.split("\n") if not re.fullmatch(r"\s*\d+\s*", l)]
    text  = "\n".join(lines)
    text  = re.sub(r"\n{3,}", "\n\n", text)
    return re.sub(r" {2,}", " ", text).strip()

# ══════════════════════════════════════════════════════════════════════════════
# INGESTION HELPERS — header/footer removal (no hardcoding)
# ══════════════════════════════════════════════════════════════════════════════

def _learn_margin_zones(fitz_doc, sample_pages: int = 10) -> tuple:
    """
    Auto-detect header/footer pixel cutoffs by sampling pages.
    Finds text blocks that consistently appear in the top/bottom 15% of pages.
    Returns (header_cutoff_y, footer_cutoff_y) as absolute pixel values.
    """
    from collections import defaultdict
    total  = min(fitz_doc.page_count, sample_pages)
    page_h = fitz_doc[0].rect.height
    top_hits = defaultdict(int)
    bot_hits = defaultdict(int)
    BAND = 15  # bucket y-coords into 15px bands

    for p_idx in range(total):
        page = fitz_doc[p_idx]
        for block in page.get_text("blocks"):
            x0, y0, x1, y1, text = block[0], block[1], block[2], block[3], block[4]
            if not text.strip():
                continue
            if y0 < page_h * 0.15:
                top_hits[int(y0 // BAND)] += 1
            if y1 > page_h * 0.85:
                bot_hits[int(y1 // BAND)] += 1

    threshold = max(2, int(total * 0.5))

    header_cutoff = 0.0
    for band, cnt in sorted(top_hits.items()):
        if cnt >= threshold:
            header_cutoff = max(header_cutoff, (band + 1) * BAND)

    footer_cutoff = page_h
    for band, cnt in sorted(bot_hits.items(), reverse=True):
        if cnt >= threshold:
            footer_cutoff = min(footer_cutoff, band * BAND)

    # Safety clamp — never cut more than 12% from either edge
    header_cutoff = min(header_cutoff, page_h * 0.12)
    footer_cutoff = max(footer_cutoff, page_h * 0.88)

    print(f"    📐 Learned margins: header={header_cutoff:.0f}px  "
          f"footer={footer_cutoff:.0f}px  (page_h={page_h:.0f}px)")
    return header_cutoff, footer_cutoff


def _extract_body_text(page, header_cutoff: float, footer_cutoff: float) -> str:
    """
    Extract only body-zone text using learned margin cutoffs.
    Skips blocks whose bounding box falls in the header or footer zone.
    """
    lines = []
    for block in page.get_text("blocks"):
        x0, y0, x1, y1, text = block[0], block[1], block[2], block[3], block[4]
        if y0 >= header_cutoff and y1 <= footer_cutoff:
            cleaned = text.strip()
            if cleaned:
                lines.append(cleaned)
    return "\n".join(lines)


def _build_noise_fingerprint(page_raw: dict, repeat_ratio: float = 0.35) -> set:
    """
    Identify noise lines purely by repetition frequency across pages.
    Any line appearing on >35% of pages is a header/footer/watermark.
    Works for ANY document layout — zero hardcoding.
    """
    from collections import Counter
    total = len(page_raw)
    line_freq: Counter = Counter()

    for text in page_raw.values():
        seen_this_page: set = set()
        for raw_line in text.split("\n"):
            norm = re.sub(r"\s+", " ", raw_line.strip().lower()).strip(".:|-–—")
            if len(norm) < 6:
                continue
            if norm.isdigit():
                continue
            if norm not in seen_this_page:
                line_freq[norm] += 1
                seen_this_page.add(norm)

    cutoff = max(2, int(total * repeat_ratio))
    noise  = {line for line, cnt in line_freq.items() if cnt >= cutoff}
    print(f"    🔍 Noise fingerprint: {len(noise)} repeated lines found "
          f"(threshold: {cutoff}/{total} pages)")
    return noise


def _strip_noise_lines(text: str, noise_lines: set) -> str:
    """Remove lines that were identified as noise by _build_noise_fingerprint."""
    lines = []
    for line in text.split("\n"):
        norm = re.sub(r"\s+", " ", line.strip().lower()).strip(".:|-–—")
        if norm not in noise_lines:
            lines.append(line)
    return "\n".join(lines)


def _extract_sections(text: str) -> List[Dict]:
    """
    Split text into logical sections based on structural signals.
    Each section has a heading and its full body content.
    Returns list of {heading, body, start_pos} dicts.
    """
    if not text or not isinstance(text, str):
        return []

    # Section boundary patterns — ordered by specificity
    SECTION_BOUNDARY = re.compile(
        r"(?m)^(?:"
        r"\d+(?:\.\d+)*[\s\.\)]\s+[A-Z\w]"   # "1. " or "1.2 " or "1.2.3 Heading"
        r"|[A-Z]\.\s+[A-Z\w]"                  # "A. Heading"
        r"|[A-Z]{3,}[\s]*[:\-]"                # "FIELD:" or "NOTE-"
        r"|(?:FIELD|SCREEN|TRANSACTION|ERROR|STATUS|MODE|TABLE|SECTION)\s+\w"  # banking keywords
        r")"
    )

    boundaries = [m.start() for m in SECTION_BOUNDARY.finditer(text)]

    if len(boundaries) < 2:
        # No clear structure — treat whole text as one section
        return [{"heading": "", "body": text, "start_pos": 0}]

    sections = []
    for i, start in enumerate(boundaries):
        end  = boundaries[i + 1] if i + 1 < len(boundaries) else len(text)
        body = text[start:end].strip()
        if not body:
            continue
        # First line is the heading
        lines   = body.split("\n")
        heading = lines[0].strip()[:120]
        sections.append({"heading": heading, "body": body, "start_pos": start})

    return sections


def _extract_table_rows(text: str) -> List[str]:
    """
    Extract individual rows from pipe-delimited tables or
    space-aligned tabular content common in banking BRS/FSD docs.
    Returns list of row strings, each self-contained with column context.
    """
    rows = []

    # Pipe-delimited tables (markdown style)
    pipe_rows = [l.strip() for l in text.split("\n") if l.strip().startswith("|") and "|" in l[1:]]
    if len(pipe_rows) >= 2:
        # First row is usually the header — prepend it to every data row
        header = pipe_rows[0]
        is_separator = lambda r: re.fullmatch(r"[\|\s\-:]+", r)
        data_rows = [r for r in pipe_rows[1:] if not is_separator(r)]
        for row in data_rows:
            if len(row.strip()) > 10:
                rows.append(f"{header}\n{row}")
        return rows

    # Space-aligned tabular content (common in SBI docs)
    # Detect by: lines where words are separated by 2+ spaces consistently
    lines = [l for l in text.split("\n") if l.strip()]
    tabular_lines = [l for l in lines if re.search(r"\S {2,}\S", l)]
    if len(tabular_lines) >= 3:
        # Treat first tabular line as header
        header = tabular_lines[0]
        for row in tabular_lines[1:]:
            if len(row.strip()) > 10:
                rows.append(f"{header}\n{row}")
        return rows

    return []


def _extract_numbered_items(text: str) -> List[str]:
    """
    Extract numbered/bulleted items that represent individual requirements.
    Groups sub-items with their parent (e.g. 1.1 + 1.1.1 + 1.1.2 stay together).
    """
    items = []
    current_item_lines = []
    current_level      = None

    ITEM_RE = re.compile(r"^(\d+(?:\.\d+)*|[a-z]\.|[ivxlc]+\.|\*|\-|\•)\s+", re.IGNORECASE)

    for line in text.split("\n"):
        match = ITEM_RE.match(line.strip())
        if match:
            prefix = match.group(1)
            level  = prefix.count(".") if "." in prefix else 0

            if current_item_lines:
                # If this is a sub-item of the current item, group with it
                if current_level is not None and level > current_level:
                    current_item_lines.append(line)
                else:
                    # New top-level item — save current and start new
                    item_text = "\n".join(current_item_lines).strip()
                    if len(item_text) > 20:
                        items.append(item_text)
                    current_item_lines = [line]
                    current_level      = level
            else:
                current_item_lines = [line]
                current_level      = level
        elif current_item_lines:
            # Continuation line of current item
            current_item_lines.append(line)

    if current_item_lines:
        item_text = "\n".join(current_item_lines).strip()
        if len(item_text) > 20:
            items.append(item_text)

    return items


def _hierarchical_chunk(text: str, source: str, page: int) -> List[Dict]:
    """
    Hierarchical chunking strategy for SBI banking documents (BRS/FSD/Solution docs).

    Structure:
        Level 0 — PARENT chunk: full section (heading + all content)
                  Stored with chunk_level='parent'
                  Used to provide full context to LLM during retrieval

        Level 1 — CHILD chunks: individual rows/items within the section
                  Stored with chunk_level='child', parent_id=parent_chunk_id
                  Used for precise vector similarity search

    Retrieval flow:
        1. Query ChromaDB for best matching CHILD chunks
        2. For each matched child, fetch its PARENT chunk
        3. Return parent text to LLM (full section context)

    Why this matters for banking docs:
        - Field spec table row "UTH-DR-ACCT-NO | 17 | Numeric | Mandatory"
          only makes sense with the table header above it
        - Requirement "1.1.2 Amount validation" only makes sense with
          parent "1.1 Transaction Rules" for context
        - Error code "E001: Invalid account" only makes sense with
          the surrounding error handling section
    """
    if not text or not isinstance(text, str):
        return []

    MIN_CHARS = 40
    MIN_WORDS = 6
    MAX_PARENT_CHARS = 2000   # cap parent size to avoid token overflow in LLM
    MAX_CHILD_CHARS  = 600    # child chunks stay small for precise retrieval

    result_chunks: List[Dict] = []
    chunk_counter = 0

    def make_id(prefix: str) -> str:
        nonlocal chunk_counter
        chunk_counter += 1
        return f"{prefix}_{page}_{chunk_counter}"

    # ── Step 1: Split into sections ───────────────────────────────────────────
    sections = _extract_sections(text)

    for section in sections:
        heading      = section["heading"]
        section_body = section["body"]

        if not section_body.strip() or len(section_body.strip()) < MIN_CHARS:
            continue

        # ── Step 2: Create PARENT chunk for this section ──────────────────────
        # Parent = full section text (capped at MAX_PARENT_CHARS)
        # If section is very long, take heading + first MAX_PARENT_CHARS chars
        parent_text = section_body[:MAX_PARENT_CHARS].strip()
        if len(section_body) > MAX_PARENT_CHARS:
            # Add truncation note so LLM knows there's more
            parent_text += "\n[…section continues]"

        if len(parent_text.split()) < MIN_WORDS:
            continue

        parent_id = make_id("P")
        result_chunks.append({
            "text"           : parent_text,
            "source"         : source,
            "page"           : page,
            "chunk_index"    : chunk_counter,
            "chunk_level"    : "parent",
            "parent_id"      : parent_id,     # self-reference for parents
            "section_heading": heading,
            "char_count"     : len(parent_text),
        })

        # ── Step 3: Create CHILD chunks within this section ───────────────────
        children_added = 0

        # Strategy A: table rows (highest priority for banking field spec tables)
        table_rows = _extract_table_rows(section_body)
        if table_rows:
            for row in table_rows:
                row = row.strip()
                if len(row) < MIN_CHARS or len(row.split()) < MIN_WORDS:
                    continue
                child_id = make_id("C")
                result_chunks.append({
                    "text"           : row[:MAX_CHILD_CHARS],
                    "source"         : source,
                    "page"           : page,
                    "chunk_index"    : chunk_counter,
                    "chunk_level"    : "child",
                    "parent_id"      : parent_id,
                    "section_heading": heading,
                    "char_count"     : len(row),
                    "child_type"     : "table_row",
                })
                children_added += 1
            # If we got table rows, skip other child strategies
            # (table rows are the most precise unit for this section)
            if children_added > 0:
                continue

        # Strategy B: numbered/bulleted items (requirements lists)
        numbered_items = _extract_numbered_items(section_body)
        if numbered_items and len(numbered_items) >= 2:
            for item in numbered_items:
                item = item.strip()
                if len(item) < MIN_CHARS or len(item.split()) < MIN_WORDS:
                    continue
                child_id = make_id("C")
                result_chunks.append({
                    "text"           : item[:MAX_CHILD_CHARS],
                    "source"         : source,
                    "page"           : page,
                    "chunk_index"    : chunk_counter,
                    "chunk_level"    : "child",
                    "parent_id"      : parent_id,
                    "section_heading": heading,
                    "char_count"     : len(item),
                    "child_type"     : "numbered_item",
                })
                children_added += 1
            if children_added > 0:
                continue

        # Strategy C: sentence-level chunks for narrative sections
        # Split on sentence boundaries, group into ~300 char windows
        sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z])", section_body)
        window, window_len = [], 0
        for sent in sentences:
            sent = sent.strip()
            if not sent:
                continue
            if window_len + len(sent) > MAX_CHILD_CHARS and window:
                child_text = " ".join(window).strip()
                if len(child_text) >= MIN_CHARS and len(child_text.split()) >= MIN_WORDS:
                    child_id = make_id("C")
                    result_chunks.append({
                        "text"           : child_text,
                        "source"         : source,
                        "page"           : page,
                        "chunk_index"    : chunk_counter,
                        "chunk_level"    : "child",
                        "parent_id"      : parent_id,
                        "section_heading": heading,
                        "char_count"     : len(child_text),
                        "child_type"     : "sentence_window",
                    })
                    children_added += 1
                window, window_len = [sent], len(sent)
            else:
                window.append(sent)
                window_len += len(sent)

        # Flush remaining window
        if window:
            child_text = " ".join(window).strip()
            if len(child_text) >= MIN_CHARS and len(child_text.split()) >= MIN_WORDS:
                child_id = make_id("C")
                result_chunks.append({
                    "text"           : child_text,
                    "source"         : source,
                    "page"           : page,
                    "chunk_index"    : chunk_counter,
                    "chunk_level"    : "child",
                    "parent_id"      : parent_id,
                    "section_heading": heading,
                    "char_count"     : len(child_text),
                    "child_type"     : "sentence_window",
                })
            children_added += 1

        # If section is short enough that no children were needed,
        # the parent itself is already a good atomic chunk — no child needed
        if children_added == 0 and len(section_body) <= MAX_CHILD_CHARS:
            pass  # parent is sufficient, children would just duplicate it

    return result_chunks


# Keep _hybrid_chunk and _semantic_chunk as aliases so nothing else breaks
def _hybrid_chunk(text: str, source: str, page: int) -> List[Dict]:
    return _hierarchical_chunk(text, source, page)

def _semantic_chunk(text: str, source: str, page: int) -> List[Dict]:
    return _hierarchical_chunk(text, source, page)


def embed_texts(texts: List[str]) -> List[List[float]]:
    """Batch-embed texts using Azure text-embedding model."""
    embeddings = []
    for i in range(0, len(texts), 16):
        batch    = [t for t in texts[i : i + 16]]
        response = _az.embeddings.create(model=_EMBEDDING_MODEL, input=batch)
        embeddings.extend(
            item.embedding for item in sorted(response.data, key=lambda x: x.index)
        )
    return embeddings


def _cosine_similarity(vec_a: List[float], vec_b: List[float]) -> float:
    """Dot product cosine similarity between two equal-length vectors."""
    dot   = sum(a * b for a, b in zip(vec_a, vec_b))
    mag_a = math.sqrt(sum(a * a for a in vec_a))
    mag_b = math.sqrt(sum(b * b for b in vec_b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


def _dedup_chunks(
    chunks:     List[Dict],
    embeddings: List[List[float]],
) -> Tuple[List[Dict], List[List[float]]]:
    """
    Remove exact and near-duplicate chunks before storage.

    Pass 1 — Exact dedup: SHA-256 hash. O(n) — fast.
    Pass 2 — Near-dedup: cosine similarity, but only against the last
             N_COMPARE accepted chunks (sliding window). This keeps
             quality while avoiding O(n²) on large documents.
    """
    N_COMPARE = 50   # only compare against last 50 accepted chunks

    seen_hashes:         set               = set()
    accepted_chunks:     List[Dict]        = []
    accepted_embeddings: List[List[float]] = []
    exact_skipped  = 0
    near_skipped   = 0

    for chunk, emb in zip(chunks, embeddings):
        text      = chunk["text"].strip()
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

        # Pass 1 — exact hash
        if text_hash in seen_hashes:
            exact_skipped += 1
            continue
        seen_hashes.add(text_hash)

        # Pass 2 — near-dup against sliding window of last N_COMPARE chunks
        window = accepted_embeddings[-N_COMPARE:]
        is_near_dup = any(
            _cosine_similarity(emb, e) >= NEAR_DUP_COSINE_THRESHOLD
            for e in window
        )
        if is_near_dup:
            near_skipped += 1
            continue

        accepted_chunks.append(chunk)
        accepted_embeddings.append(emb)

    print(f"  🧹 Dedup: {exact_skipped} exact + {near_skipped} near-dup removed "
          f"→ {len(accepted_chunks)}/{len(chunks)} chunks kept")
    return accepted_chunks, accepted_embeddings

def _split_pdf_bytes(
    file_bytes:     bytes,
    pages_per_part: int = INGEST_SPLIT_THRESHOLD,
) -> List[tuple]:
    """
    Split a large PDF into smaller parts using PyMuPDF.
    Returns list of (part_bytes, part_suffix, start_page, end_page).
    e.g. for 500-page PDF with pages_per_part=200:
      [(bytes,"_part1",1,200), (bytes,"_part2",201,400), (bytes,"_part3",401,500)]
    """
    doc   = fitz.open(stream=file_bytes, filetype="pdf")
    total = doc.page_count
    parts = []

    part_num = 1
    for start in range(0, total, pages_per_part):
        end     = min(start + pages_per_part - 1, total - 1)
        new_doc = fitz.open()
        new_doc.insert_pdf(doc, from_page=start, to_page=end)
        part_bytes = new_doc.tobytes()
        new_doc.close()
        parts.append((part_bytes, f"_part{part_num}", start + 1, end + 1))
        part_num += 1

    doc.close()
    return parts

def _ingest_single_pdf(
    file_bytes: bytes,
    filename: str,
    clean_dept: str,
    application_name: str = "",
) -> Dict:
    """
    Ingest one PDF part into ChromaDB using full 4-pass pipeline:
      Pass 1 — identify repeated xrefs (logos/headers/watermarks) to skip
      Pass 2 — extract text per page + queue non-logo images for Azure vision
      Pass 3 — parallel Azure OCR + image description calls
      Pass 4 — assemble complete page text (raw_text + OCR + description)
               then chunk → embed → dedup → batch upsert into ChromaDB
    """
    doc_id = f"{clean_dept}_{uuid.uuid4()}"
    _ingest_app_name = application_name
    try:
        fitz_doc = fitz.open(stream=file_bytes, filetype="pdf")
    except Exception as e:
        raise ValueError(f"Cannot open PDF '{filename}': {e}")

    total = fitz_doc.page_count
    print(f"    📄 '{filename}': {total} pages — running 4-pass extraction for ingestion…")

    # ── Pass 1: identify repeated xrefs (logos / headers / watermarks) ────────
    print("    Pass 1: identifying repeated images…")
    all_xrefs: List[int] = []

    for p in range(total):
        try:
            all_xrefs.extend(img[0] for img in fitz_doc[p].get_images(full=True))
        except Exception:
            pass

    repeated = {xref for xref, cnt in Counter(all_xrefs).items() if cnt > 2}
    print(f"    → {len(repeated)} repeated xrefs identified (logos/headers)")

    # ── Pass 2: learn margins + extract body text + queue images ──────────────
    print("    Pass 2: learning margins, extracting body text, collecting images…")

    # NEW: learn header/footer zones from the document itself
    header_cutoff, footer_cutoff = _learn_margin_zones(fitz_doc, sample_pages=min(10, total))

    page_raw: Dict[int, str] = {}
    queue: List[Tuple[int, int, str]] = []

    for p_idx in range(total):
        pn = p_idx + 1
        page = fitz_doc[p_idx]

        # NEW: use bounding-box body extraction instead of raw get_text
        raw = _extract_body_text(page, header_cutoff, footer_cutoff)
        page_raw[pn] = raw

        for img_idx, img in enumerate(page.get_images(full=True), start=1):
            xref = img[0]
            if xref in repeated:
                continue
            try:
                base_img  = fitz_doc.extract_image(xref)
                img_bytes = base_img["image"]
                img_name  = base_img.get("name", "").lower()
                if any(kw in img_name for kw in ("logo", "header", "footer", "watermark")):
                    continue
                b64 = _resize_image(img_bytes)
                if b64:
                    queue.append((pn, img_idx, b64))
            except Exception as e:
                print(f"    ✗ Page {pn}, Image {img_idx}: extract error — {e}")

    fitz_doc.close()
    print(f"    → {len(queue)} images queued for Azure vision")

    # NEW: build noise fingerprint after all pages are extracted
    noise_lines = _build_noise_fingerprint(page_raw, repeat_ratio=0.35)

    # ── Pass 3: parallel Azure vision (OCR + description) ────────────────────
    workers = min(IMAGE_OCR_MAX_WORKERS, max(len(queue), 1))
    print(f"    Pass 3: Azure vision ({workers} threads)…")

    azure: Dict[Tuple[int, int], Tuple[str, str]] = {}

    if queue:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {
                ex.submit(_process_image_in_thread, pn, ii, b64): (pn, ii)
                for pn, ii, b64 in queue
            }

            for fut in as_completed(futures):
                pn, ii = futures[fut]

                try:
                    _, _, ocr, desc = fut.result()

                    if _is_logo(desc):
                        with _print_lock:
                            print(
                                f"    🔍 Page {pn}, Image {ii}: logo detected → discarded"
                            )

                        azure[(pn, ii)] = ("", "")

                    else:
                        azure[(pn, ii)] = (ocr, desc)

                        with _print_lock:
                            print(
                                f"    ✓ Page {pn}, Image {ii}: OCR={len(ocr)}c Desc={len(desc)}c"
                            )

                except Exception as e:
                    with _print_lock:
                        print(
                            f"    ✗ Page {pn}, Image {ii}: future error — {e}"
                        )

                    azure[(pn, ii)] = ("", "")

    # ── Pass 4: assemble complete page text → chunk → embed → dedup → upsert ─
    print("    Pass 4: assembling pages and chunking…")

    all_chunks: List[Dict] = []
    all_embeddings: List[List[float]] = []

    for pn in range(1, total + 1):
        parts = []

        raw_text = page_raw.get(pn, "").strip()
        if raw_text:
            parts.append(raw_text)

        for (apn, aii) in sorted(
            (k for k in azure if k[0] == pn),
            key=lambda x: x[1]
        ):
            ocr, desc = azure[(apn, aii)]

            if ocr:
                parts.append(f"[Image {aii} OCR]:\n{ocr}")

            if desc:
                parts.append(f"[Image {aii} Description]:\n{desc}")

        complete = "\n\n".join(
            p for p in parts if p.strip()
        )

        if not complete:
            continue

        # NEW: strip repeated-line noise before standard cleaning
        complete = _strip_noise_lines(complete, noise_lines)
        cleaned  = _clean_text_for_ingest(complete)

        if not cleaned:
            continue

        page_chunks = _hybrid_chunk(
            cleaned,
            filename,
            pn
        )
        # Propagate application_name to each chunk for metadata storage
        for _ch in page_chunks:
            _ch["application_name"] = _ingest_app_name

        if not page_chunks:
            continue

        try:
            page_embeddings = embed_texts(
                [c["text"] for c in page_chunks]
            )

        except Exception as e:
            print(
                f"    ✗ Page {pn}: embedding failed — {e}"
            )
            continue

        all_chunks.extend(page_chunks)
        all_embeddings.extend(page_embeddings)

        print(
            f"    ✓ Page {pn}: {len(complete)} chars → {len(page_chunks)} chunks"
        )

    if not all_chunks:
        raise ValueError(
            f"No content could be extracted from '{filename}'."
        )

    print(f"    → {len(all_chunks)} raw chunks before dedup")

    # Dedup (exact hash + near-cosine)
    all_chunks, all_embeddings = _dedup_chunks(
        all_chunks,
        all_embeddings
    )

    if not all_chunks:
        raise ValueError(
            f"All chunks from '{filename}' were duplicates — nothing to store."
        )

    # ── Batch upsert ──────────────────────────────────────────────────────────
    BATCH = 200

    col = _get_collection()

    if col is None:
        raise RuntimeError(
            "ChromaDB unavailable — cannot store chunks"
        )

    for start in range(0, len(all_chunks), BATCH):
        end = start + BATCH

        batch_c = all_chunks[start:end]
        batch_e = all_embeddings[start:end]

        batch_ids = [
            f"{doc_id}_{start + i}"
            for i in range(len(batch_c))
        ]

        try:
            col.upsert(
                ids=batch_ids,
                embeddings=batch_e,
                documents=[
                    c["text"] for c in batch_c
                ],
                metadatas=[
                    {
                        "source"          : c["source"],
                        "page"            : c["page"],
                        "chunk_index"     : c["chunk_index"],
                        "doc_id"          : doc_id,
                        "department_id"   : clean_dept,
                        "application_name": c.get("application_name", ""),
                        "section_heading" : c.get("section_heading", ""),   # NEW
                        "char_count"      : len(c["text"]),                 # NEW
                        # New hierarchical fields
                        "chunk_level"     : c.get("chunk_level", "parent"),
                        "parent_id"       : c.get("parent_id", ""),
                        "child_type"      : c.get("child_type", ""),
                    }
                    for c in batch_c
                ],
            )

            print(
                f"    ✅ Batch {start}–{min(end, len(all_chunks))}: "
                f"{len(batch_c)} chunks upserted"
            )

        except Exception as e:
            print(
                f"    ✗ Batch {start}–{end} failed: {e}"
            )
            raise

    print(
        f"    ✅ {len(all_chunks)} unique chunks stored for '{filename}'"
    )

    return {
        "doc_id": doc_id,
        "filename": filename,
        "pages": total,
        "total_chunks": len(all_chunks),
        "department_id": clean_dept,
    }


def ingest_document_to_rag(
    file_bytes:    bytes,
    filename:      str,
    department_id: str = "general",
    application_name: str = "",
) -> Dict:
    """
    Ingest a PDF into the ChromaDB RAG knowledge base.

    Files <= INGEST_SPLIT_THRESHOLD pages  → ingest directly.
    Files  > INGEST_SPLIT_THRESHOLD pages  → split into parts named
    filename_part1.pdf, filename_part2.pdf, … and ingest each.
    """
    if not filename.lower().endswith(".pdf"):
        raise ValueError("Only PDF files can be ingested into the knowledge base.")

    clean_dept = re.sub(r"[^a-zA-Z0-9]", "", str(department_id))[:20]

    try:
        check_doc   = fitz.open(stream=file_bytes, filetype="pdf")
        total_pages = check_doc.page_count
        check_doc.close()
    except Exception as e:
        raise ValueError(f"Cannot open PDF: {e}")

    print(f"\n📥 Ingesting '{filename}' ({total_pages} pages, dept={clean_dept})")

    if total_pages <= INGEST_SPLIT_THRESHOLD:
        return _ingest_single_pdf(file_bytes, filename, clean_dept,application_name )

    print(f"  → File has {total_pages} pages — splitting into "
          f"{INGEST_SPLIT_THRESHOLD}-page parts…")

    stem             = Path(filename).stem
    total_chunks_all = 0
    all_doc_ids      = []
    part_num         = 1

    fitz_src = fitz.open(stream=file_bytes, filetype="pdf")
    for start in range(0, total_pages, INGEST_SPLIT_THRESHOLD):
        end          = min(start + INGEST_SPLIT_THRESHOLD - 1, total_pages - 1)
        part_name    = f"{stem}_part{part_num}.pdf"
        new_doc      = fitz.open()
        new_doc.insert_pdf(fitz_src, from_page=start, to_page=end)
        part_bytes   = new_doc.tobytes()
        new_doc.close()

        print(f"\n  📄 {part_name} (pages {start+1}–{end+1})…")
        try:
            part_result       = _ingest_single_pdf(part_bytes, part_name, clean_dept, application_name)
            total_chunks_all += part_result["total_chunks"]
            all_doc_ids.append(part_result["doc_id"])
            print(f"  ✅ {part_name}: {part_result['total_chunks']} chunks")
        except Exception as e:
            print(f"  ✗ {part_name} failed: {e} — continuing with next part")
        part_num += 1

    fitz_src.close()

    print(f"\n✅ Split ingestion complete: {part_num-1} parts, "
          f"{total_chunks_all} total chunks stored")

    return {
        "doc_id"       : all_doc_ids[0] if all_doc_ids else "",
        "doc_ids"      : all_doc_ids,
        "filename"     : filename,
        "pages"        : total_pages,
        "parts"        : part_num - 1,
        "total_chunks" : total_chunks_all,
        "department_id": clean_dept,
    }

def list_rag_documents(
    department_id:    Optional[str] = None,
    application_name: Optional[str] = None,
) -> List[Dict]:
    """
    Return summary list of ingested documents.
    Supports optional filtering by department_id and/or application_name.
    Fully wrapped in try/except — ChromaDB must never crash the server process.
    """
    col = _get_collection()
    if col is None:
        return []

    try:
        count = col.count()
    except Exception as e:
        print(f"⚠ ChromaDB count() failed: {e}")
        return []

    if count == 0:
        return []

    # Build where filter
    filters = []
    if department_id:
        clean_dept = re.sub(r"[^a-zA-Z0-9]", "", str(department_id))[:20]
        filters.append({"department_id": clean_dept})
    if application_name and application_name.strip():
        filters.append({"application_name": application_name.strip()})

    if len(filters) == 0:
        where_filter = None
    elif len(filters) == 1:
        where_filter = filters[0]
    else:
        where_filter = {"$and": filters}

    try:
        all_items = col.get(
            where=where_filter,
            include=["metadatas"],
        )
    except Exception:
        try:
            all_items = col.get(include=["metadatas"])
        except Exception as e2:
            print(f"⚠ ChromaDB get() failed entirely: {e2}")
            return []

    docs: Dict[str, Dict] = {}
    try:
        for meta in (all_items.get("metadatas") or []):
            if not meta:
                continue
            did = meta.get("doc_id", "unknown")
            if did not in docs:
                docs[did] = {
                    "doc_id"          : did,
                    "filename"        : meta.get("source", ""),
                    "total_chunks"    : 0,
                    "department_id"   : meta.get("department_id", "general"),
                    "application_name": meta.get("application_name", ""),
                }
            # Count only parent chunks for display (children are internal)
            chunk_level = meta.get("chunk_level", "parent")
            if chunk_level == "parent":
                docs[did]["total_chunks"] += 1
                
    except Exception as e:
        print(f"⚠ ChromaDB metadata iteration failed: {e}")
        return []

    return list(docs.values())


def delete_rag_document(doc_id: str) -> int:
    """Delete all chunks belonging to doc_id. Returns number of deleted chunks."""
    col = _get_collection()
    if col is None:
        raise RuntimeError("ChromaDB unavailable")
    try:
        ids = col.get(where={"doc_id": doc_id}, include=[]).get("ids", [])
    except Exception as e:
        raise RuntimeError(f"ChromaDB get failed: {e}")
    if not ids:
        raise ValueError(f"Document {doc_id} not found.")
    col.delete(ids=ids)
    return len(ids)

# ══════════════════════════════════════════════════════════════════════════════
# IMAGE UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def _resize_image(image_bytes: bytes, max_ratio: int = MAX_IMAGE_RATIO) -> Optional[str]:
    """Resize extreme-ratio images and return as base64 JPEG. None on failure."""
    try:
        img   = Image.open(io.BytesIO(image_bytes))
        w, h  = img.size
        if not w or not h:
            return None
        ratio = max(w / h, h / w)
        if ratio > max_ratio:
            if w > h:
                img = img.resize((int(h * max_ratio), h), Image.Resampling.LANCZOS)
            else:
                img = img.resize((w, int(w * max_ratio)), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", optimize=True, quality=85)
        return base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception as e:
        print(f"  ⚠ Image resize failed: {e}")
        return None


_OCR_PROMPT = """Perform EXACTLY TWO tasks:

────────────────────────────────────
1. OCR (TEXT EXTRACTION)
────────────────────────────────────
- Extract ALL visible text from the image.
- Preserve original spelling, capitalization, punctuation, and line breaks.
- Do NOT paraphrase, summarize, or correct text.
- If no text is present, return an empty string.

────────────────────────────────────
2. IMAGE DESCRIPTION
────────────────────────────────────
⚠️ LOGO / ICON RULE: If image is ONLY a logo/icon, set "image_description" to ONLY the logo/icon name and skip all remaining sections.

A. IMAGE TYPE
- Identify the image type:
  (e.g., flow diagram, system architecture, dashboard, chart, table, UI screen, logo, icon)

⚠️ SPECIAL LOGO / ICON RULE:
- If the image is ONLY a logo or icon:
  - Set "image_description" to ONLY the name of the logo/icon.
  - Do NOT include layout, color, or element descriptions.
  - Skip all remaining sections.

B. TEXT, LABELS & ANNOTATIONS
- Titles and headings with position and emphasis
- Labels, legends, captions, annotations, notes
- Axis labels, units, and scales if charts are present

C. DATA, FLOW & RELATIONSHIPS
- Direction of flows or arrows
- Input → process → output relationships
- Hierarchies, dependencies, or groupings
- For charts:
  - Visible trends, comparisons, increases/decreases, or patterns

D. PURPOSE & CONTEXT
- Describe the apparent purpose of the image based ONLY on visual evidence
- If applicable, relate it to banking or financial workflows
  (e.g., transaction processing, account lifecycle, loan flow, risk assessment)

E. FINAL SUMMARY
- Provide a direct visual description of what the image is. 

For example: 

Rather than using : The UI presents / The image shows / This interface includes 
Just Give: Actual description directly. 

────────────────────────────────────
OUTPUT FORMAT (MANDATORY)
────────────────────────────────────
OUTPUT — valid JSON only, no markdown:
{"ocr_text": "...", "image_description": "..."}"""


def _call_azure_vision(image_b64: str, max_retries: int = 3, retry_delay: int = 2) -> Dict:
    """Call Azure vision model for OCR + description of one image."""
    for attempt in range(1, max_retries + 1):
        try:
            response = _az.chat.completions.create(
                model=_CHAT_MODEL,
                messages=[
                    {"role": "system", "content":
                        "You are a computer vision and OCR engine. "
                        "Always return a SINGLE valid JSON object. "
                        "No markdown, no code blocks, no extra text."},
                    {"role": "user", "content": [
                        {"type": "text",      "text": _OCR_PROMPT},
                        {"type": "image_url", "image_url":
                            {"url": f"data:image/jpeg;base64,{image_b64}"}},
                    ]},
                ],
                max_tokens=2000,
            )
            raw = response.choices[0].message.content.strip()
            raw = re.sub(r"^```(json)?", "", raw, flags=re.IGNORECASE).strip()
            raw = re.sub(r"```$", "", raw).strip()
            result = json.loads(raw)
            return {"ocr_text": result.get("ocr_text", ""),
                    "image_description": result.get("image_description", "")}
        except Exception as e:
            with _print_lock:
                print(f"  ⚠ Azure OCR attempt {attempt}/{max_retries}: {e}")
            if attempt < max_retries:
                time.sleep(retry_delay)
    return {"ocr_text": "", "image_description": ""}


def _is_logo(description: str) -> bool:
    d  = description.lower().strip()
    kw = ("logo", "icon", "brand", "emblem", "seal", "watermark")
    return len(d) < 80 and any(k in d for k in kw)


def _process_image_in_thread(page_num: int, img_idx: int, b64: str
                              ) -> Tuple[int, int, str, str]:
    with _print_lock:
        print(f"  🖼  Page {page_num}, Image {img_idx}: Azure vision…")
    r = _call_azure_vision(b64)
    return page_num, img_idx, r["ocr_text"], r["image_description"]


# ══════════════════════════════════════════════════════════════════════════════
# PAGE EXTRACTION  (4-pass pipeline)
# ══════════════════════════════════════════════════════════════════════════════

def extract_pages_with_images(file_bytes: bytes, filename: str) -> List[Dict]:
    """
    Extract per-page content (text + image OCR/descriptions, logo-aware).

    Returns list of {"page_number": int, "complete_text": str}.

    PDF — 4 passes:
      1. Find repeated xrefs (logos / headers / watermarks).
      2. Extract raw text; queue non-logo images for Azure vision.
      3. Parallel Azure vision calls (threaded).
         If the description looks like a logo → discard.
      4. Assemble: raw_text + [Image N OCR] + [Image N Description].

    DOCX — paragraph text split into batches (no image processing).
    """
    ext = filename.lower()

    # ── DOCX path ─────────────────────────────────────────────────────────────
    if ext.endswith(".docx"):
        from docx import Document as DocxDoc
        try:
            doc   = DocxDoc(io.BytesIO(file_bytes))
            paras = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
            batch = 60
            pages = []
            for i, start in enumerate(range(0, max(len(paras), 1), batch), 1):
                pages.append({
                    "page_number":   i,
                    "complete_text": "\n\n".join(paras[start : start + batch]),
                })
            return pages or [{"page_number": 1, "complete_text": ""}]
        except Exception as e:
            raise ValueError(f"DOCX extraction failed: {e}")

    if not ext.endswith(".pdf"):
        raise ValueError(f"Unsupported file type: {filename}")

    # ── PDF path ──────────────────────────────────────────────────────────────
    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
    except Exception as e:
        raise ValueError(f"Cannot open PDF: {e}")

    total = doc.page_count
    if total == 0:
        doc.close()
        raise ValueError("PDF has no pages.")

    # Pass 1 — repeated xrefs
    print(f"\n📄 Pass 1: identifying repeated images across {total} pages…")
    all_xrefs: List[int] = []
    for p in range(total):
        try:
            all_xrefs.extend(img[0] for img in doc[p].get_images(full=True))
        except Exception:
            pass
    repeated = {xref for xref, cnt in Counter(all_xrefs).items() if cnt > 2}
    print(f"  → {len(repeated)} repeated xrefs identified (likely logos/headers).")

    # Pass 2 — extract text + queue images
    print("📄 Pass 2: extracting text and collecting images…")
    page_raw:  Dict[int, str]             = {}
    queue:     List[Tuple[int, int, str]] = []

    # NEW: learn margins for this document
    header_cutoff, footer_cutoff = _learn_margin_zones(doc, sample_pages=min(10, total))
    # NEW: first pass to collect all raw text for noise fingerprinting
    _raw_for_noise: Dict[int, str] = {}

    for p_idx in range(total):
        pn   = p_idx + 1
        page = doc[p_idx]
        parts: List[str] = []

        # NEW: positional body extraction
        raw = _extract_body_text(page, header_cutoff, footer_cutoff)
        _raw_for_noise[p_idx + 1] = raw   # track for noise fingerprint
        if raw:
            parts.append(raw)

        for img_idx, img in enumerate(page.get_images(full=True), start=1):
            xref = img[0]
            if xref in repeated:
                continue
            try:
                base_img  = doc.extract_image(xref)
                img_bytes = base_img["image"]
                img_name  = base_img.get("name", "").lower()
                if any(kw in img_name for kw in ("logo", "header", "footer", "watermark")):
                    print(f"  ⏭  Page {pn}, Image {img_idx}: skipped by name")
                    continue
                b64 = _resize_image(img_bytes)
                if b64:
                    queue.append((pn, img_idx, b64))
                    print(f"  ✓ Page {pn}, Image {img_idx}: queued")
            except Exception as e:
                print(f"  ✗ Page {pn}, Image {img_idx}: extract error — {e}")

        page_raw[pn] = "\n\n".join(parts)

    # NEW: build noise fingerprint
    noise_lines = _build_noise_fingerprint(_raw_for_noise, repeat_ratio=0.35)    

    doc.close()
    print(f"  → {len(queue)} images queued for Azure vision.")

    # Pass 3 — parallel Azure vision
    workers = min(IMAGE_OCR_MAX_WORKERS, max(len(queue), 1))
    print(f"📄 Pass 3: Azure vision ({workers} threads)…")
    azure: Dict[Tuple[int, int], Tuple[str, str]] = {}

    if queue:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {
                ex.submit(_process_image_in_thread, pn, ii, b64): (pn, ii)
                for pn, ii, b64 in queue
            }
            for fut in as_completed(futures):
                pn, ii = futures[fut]
                try:
                    _, _, ocr, desc = fut.result()
                    if _is_logo(desc):
                        with _print_lock:
                            print(f"  🔍 Page {pn}, Image {ii}: logo detected → discarded")
                        azure[(pn, ii)] = ("", "")
                    else:
                        azure[(pn, ii)] = (ocr, desc)
                        with _print_lock:
                            print(f"  ✓ Page {pn}, Image {ii}: OCR={len(ocr)}c Desc={len(desc)}c")
                except Exception as e:
                    with _print_lock:
                        print(f"  ✗ Page {pn}, Image {ii}: future error — {e}")
                    azure[(pn, ii)] = ("", "")

    # Pass 4 — assemble page content
    print("📄 Pass 4: assembling page content…")
    pages_data: List[Dict] = []
    for pn in range(1, total + 1):
        parts = [page_raw.get(pn, "")]
        for (apn, aii) in sorted((k for k in azure if k[0] == pn), key=lambda x: x[1]):
            ocr, desc = azure[(apn, aii)]
            if ocr:
                parts.append(f"[Image {aii} OCR]:\n{ocr}")
            if desc:
                parts.append(f"[Image {aii} Description]:\n{desc}")
        complete = "\n\n".join(p for p in parts if p.strip())
        # NEW: strip noise lines
        complete = _strip_noise_lines(complete, noise_lines)
        complete = _clean_text_for_ingest(complete)   # also apply standard cleaning here
        pages_data.append({"page_number": pn, "complete_text": complete})

        print(f"  ✓ Page {pn}: {len(complete)} chars")

    print(f"✅ Extraction complete. {len(pages_data)} pages ready.\n")
    return pages_data

# ══════════════════════════════════════════════════════════════════════════════
# RERANKER
# ══════════════════════════════════════════════════════════════════════════════

# Strategy: try sentence-transformers cross-encoder (fast, local, no API cost).
# If the library is not installed, fall back to a lightweight LLM-based scorer.

def _try_crossencoder_rerank(query: str, chunks: List[Dict]) -> Optional[List[Dict]]:
    """
    Attempt to rerank using a local cross-encoder model.
    Returns reranked+filtered list, or None if the library is unavailable.

    The cross-encoder scores (query, chunk_text) pairs directly — it understands
    MEANING, not just keyword overlap, so "CBS" in an unrelated context gets a
    low score even if the word matches.

    Recommended model: cross-encoder/ms-marco-MiniLM-L-6-v2  (~80 MB, fast)
    Install: pip install sentence-transformers
    """
    try:
        from sentence_transformers import CrossEncoder  # type: ignore
        model_name = os.getenv("RERANK_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2")
        # Cache the model instance in a module-level dict to avoid re-loading
        if not hasattr(_try_crossencoder_rerank, "_models"):
            _try_crossencoder_rerank._models = {}  # type: ignore
        if model_name not in _try_crossencoder_rerank._models:  # type: ignore
            print(f"  🔄 Loading cross-encoder model '{model_name}'…")
            _try_crossencoder_rerank._models[model_name] = CrossEncoder(model_name)  # type: ignore
        model = _try_crossencoder_rerank._models[model_name]  # type: ignore

        pairs  = [(query[:512], c["text"][:512]) for c in chunks]
        scores = model.predict(pairs)   # returns raw logits (higher = more relevant)

        # Normalise to 0-1 using sigmoid
        import math
        def sigmoid(x: float) -> float:
            return 1.0 / (1.0 + math.exp(-x))

        ranked = sorted(
            [(sigmoid(float(s)), c) for s, c in zip(scores, chunks)],
            key=lambda x: x[0],
            reverse=True,
        )

        # Log results
        print(f"  🎯 Cross-encoder rerank scores:")
        for score, c in ranked:
            preview = c["text"][:80].replace("\n", " ")
            print(f"      score={score:.3f} | {preview}…")

        # Filter + trim
        filtered = [c for score, c in ranked if score >= RERANK_THRESHOLD][:RERANK_TOP_K]
        print(f"  ✓ Reranker kept {len(filtered)}/{len(chunks)} chunks (threshold={RERANK_THRESHOLD})")
        return filtered

    except ImportError:
        return None   # library not installed → fall back
    except Exception as e:
        print(f"  ⚠ Cross-encoder reranker error: {e} — falling back to LLM reranker")
        return None


def _llm_rerank(query: str, chunks: List[Dict]) -> List[Dict]:
    """
    LLM-based reranker fallback.  Asks the model to score each chunk for relevance
    to the query on a scale of 0-10, then filters and sorts.

    This is slower and costs tokens but requires no additional libraries.
    Only invoked when sentence-transformers is not installed.
    """
    if not chunks:
        return chunks

    chunks_text = "\n\n".join(
        f"[{i}] {c['text'][:400]}" for i, c in enumerate(chunks)
    )
    prompt = f"""You are a relevance scoring engine.

## Query (page content summary)
{query}

## Candidate Chunks
{chunks_text}

## Task
Score each chunk from 0 to 10 based on how semantically relevant it is to the query above.
A score of 10 means the chunk contains domain knowledge that would genuinely help generate
test cases for the query content.
A score of 0-2 means the chunk shares some keywords but the underlying meaning is unrelated.

Respond ONLY with a JSON array of objects like:
[{{"index": 0, "score": 7}}, {{"index": 1, "score": 2}}, ...]
No markdown, no explanation."""

    try:
        response = _az.chat.completions.create(
            model=_CHAT_MODEL,
            messages=[
                {"role": "system", "content": "You are a precise relevance scoring engine. Return only valid JSON."},
                {"role": "user",   "content": prompt},
            ],
            temperature=0.0,
            max_tokens=512,
        )
        raw  = response.choices[0].message.content.strip()
        raw  = re.sub(r"^```(json)?", "", raw, flags=re.IGNORECASE).strip()
        raw  = re.sub(r"```$", "", raw).strip()
        data = json.loads(raw)

        score_map = {item["index"]: item["score"] for item in data if isinstance(item, dict)}
        threshold_10 = RERANK_THRESHOLD * 10   # convert 0-1 threshold to 0-10 scale

        ranked = sorted(
            [(score_map.get(i, 0), c) for i, c in enumerate(chunks)],
            key=lambda x: x[0],
            reverse=True,
        )

        print(f"  🎯 LLM rerank scores (0-10 scale, threshold={threshold_10:.1f}):")
        for score, c in ranked:
            preview = c["text"][:80].replace("\n", " ")
            print(f"      score={score:.1f} | {preview}…")

        filtered = [c for score, c in ranked if score >= threshold_10][:RERANK_TOP_K]
        print(f"  ✓ LLM reranker kept {len(filtered)}/{len(chunks)} chunks")
        return filtered

    except Exception as e:
        print(f"  ⚠ LLM reranker error: {e} — returning original chunks (no reranking)")
        return chunks[:RERANK_TOP_K]


def rerank_chunks(page_text: str, chunks: List[Dict]) -> List[Dict]:
    """
    Public entry point for reranking.
    1. LLM-based scoring if sentence-transformers not available.

    The query for reranking is a short summary of the page text
    which captures the domain/topic better than the full text for pair scoring.
    """
    if not chunks:
        return chunks
    query = page_text.strip()
    return _llm_rerank(query, chunks)

# ══════════════════════════════════════════════════════════════════════════════
# PER-PAGE RAG RETRIEVAL
# ══════════════════════════════════════════════════════════════════════════════

def _mmr_select(
    query_vec:   List[float],
    candidates:  List[Dict],
    embeddings:  List[List[float]],
    k:           int   = MMR_FINAL_K,
    lambda_mult: float = MMR_LAMBDA,
) -> List[Dict]:
    """
    Fix 3 — Maximum Marginal Relevance selection.

    Greedy algorithm:
      At each step, pick the candidate that maximises:
        MMR_score = λ · relevance(c, query) − (1-λ) · max_sim(c, selected)

    λ=1 → pure relevance (same as cosine retrieval)
    λ=0 → pure diversity
    λ=0.5 → balanced (default)

    Returns up to k diverse, relevant chunks.
    """
    if not candidates:
        return []

    k = min(k, len(candidates))

    # Pre-compute relevance scores (cosine sim to query)
    relevance = [
        _cosine_similarity(query_vec, emb)
        for emb in embeddings
    ]

    selected_indices: List[int] = []
    remaining        = list(range(len(candidates)))

    for _ in range(k):
        if not remaining:
            break

        if not selected_indices:
            # First pick: most relevant to query
            best_idx = max(remaining, key=lambda i: relevance[i])
        else:
            # Subsequent picks: balance relevance vs diversity
            best_idx  = None
            best_score = float("-inf")

            for i in remaining:
                rel_score = relevance[i]
                # Max similarity to already-selected chunks
                max_sim = max(
                    _cosine_similarity(embeddings[i], embeddings[j])
                    for j in selected_indices
                )
                mmr_score = lambda_mult * rel_score - (1 - lambda_mult) * max_sim
                if mmr_score > best_score:
                    best_score = mmr_score
                    best_idx   = i

        selected_indices.append(best_idx)
        remaining.remove(best_idx)

    result = [candidates[i] for i in selected_indices]
    print(f"  🎯 MMR: {len(candidates)} candidates → {len(result)} diverse chunks selected "
          f"(λ={lambda_mult}, k={k})")
    return result


def retrieve_rag_chunks_for_page(
    page_text: str,
    top_k:     int                 = PAGE_RAG_TOP_K,
    doc_ids:   Optional[List[str]] = None,
    department_id:    Optional[str]       = None,
    application_name: Optional[str]       = None,
) -> List[Dict]:
    """
    Hierarchical RAG retrieval:

    Step 1 — Query ChromaDB for best matching CHILD chunks only
             (children are precise atomic units: table rows, numbered items, sentences)
             This gives high-precision similarity search

    Step 2 — For each matched child, fetch its PARENT chunk
             (parent = full section with heading and surrounding content)
             This gives the LLM full context, not just the matched row

    Step 3 — Deduplicate: if two children have the same parent, return parent once
             (avoids sending the same section twice)

    Step 4 — MMR diversification on the parent set
             (ensures we return diverse sections, not 5 chunks from same table)

    Step 5 — LLM rerank

    Falls back to flat retrieval if no hierarchical chunks exist
    (backward compatible with existing ingested documents)
    """
    col = _get_collection()
    if col is None or not page_text.strip():
        print("  📚 RAG: skipped (ChromaDB unavailable or page has no text)")
        return []

    try:
        col_count = col.count()
    except Exception as e:
        print(f"  📚 RAG: skipped (ChromaDB count failed: {e})")
        return []

    if col_count == 0:
        print("  📚 RAG: skipped (collection empty)")
        return []

    # ── Build where filter ────────────────────────────────────────────────────
    # Always scope by department — never search across all departments
    filters = []

    if doc_ids:
        if len(doc_ids) == 1:
            filters.append({"doc_id": doc_ids[0]})
        else:
            filters.append({"doc_id": {"$in": doc_ids}})

    if department_id and str(department_id).strip():
        clean_dept = re.sub(r"[^a-zA-Z0-9]", "", str(department_id))[:20]
        filters.append({"department_id": clean_dept})

    if application_name and str(application_name).strip():
        filters.append({"application_name": application_name.strip()})

    if len(filters) == 0:
        where_filter = None   # admin case — search all
    elif len(filters) == 1:
        where_filter = filters[0]
    else:
        where_filter = {"$and": filters}

    print(f"  📚 RAG scope: dept={department_id or 'all'} "
          f"app={application_name or 'none'} "
          f"doc_ids={doc_ids or 'all'}")


    # ── Early exit: check if any chunks exist for this dept/app scope ─────────
    if department_id and str(department_id).strip():
        try:
            scope_check = col.get(
                where   = where_filter,
                include = [],
                limit   = 1,
            )
            if not scope_check.get("ids"):
                print(f"  📚 RAG: no ingested documents found for "
                      f"dept={department_id} app={application_name or 'any'} "
                      f"— skipping retrieval entirely")
                return []
        except Exception as e:
            print(f"  📚 RAG: scope check failed ({e}) — proceeding with query")

    # ── Strip boilerplate from query ──────────────────────────────────────────
    _BOILERPLATE_PATTERNS = [
        r"tcs sbi confidential", r"this document is confidential",
        r"unauthorised access", r"document revision", r"change control",
        r"sign.?off stage", r"intended audience", r"how to use this document",
        r"about this document", r"list of abbreviations", r"all trademarks",
        r"ver(?:sion)?\s*\d+\.\d+", r"confidential\s+\d+",
    ]
    query_text = page_text
    for pattern in _BOILERPLATE_PATTERNS:
        query_text = re.sub(pattern, " ", query_text, flags=re.IGNORECASE)
    query_text = re.sub(r"\s{2,}", " ", query_text).strip()

    if len(query_text) < 150:
        print(f"  📚 RAG: page appears boilerplate only — skipping")
        return []

    query_vec = embed_texts([query_text])[0]

    # ── Check if collection has hierarchical chunks ───────────────────────────
    try:
        sample = col.get(
            limit=5,
            include=["metadatas"],
            where=where_filter,
        )
        sample_metas  = sample.get("metadatas") or []
        has_hierarchy = any(
            m.get("chunk_level") in ("child", "parent")
            for m in sample_metas if m
        )
    except Exception:
        has_hierarchy = False

    # ── HIERARCHICAL PATH ─────────────────────────────────────────────────────
    if has_hierarchy:
        print(f"  📚 RAG: hierarchical retrieval mode")

        # Step 1: Query CHILD chunks only for precise matching
        child_filter = {"chunk_level": "child"}
        if where_filter:
            child_filter = {"$and": [where_filter, {"chunk_level": "child"}]}

        fetch_n = min(MMR_FETCH_K, col_count)
        try:
            child_results = col.query(
                query_embeddings = [query_vec],
                n_results        = fetch_n,
                where            = child_filter,
                include          = ["documents", "metadatas", "distances"],
            )
        except Exception as e:
            print(f"  ✗ Child chunk query failed: {e} — falling back to flat retrieval")
            has_hierarchy = False  # fall through to flat path below

        if has_hierarchy:
            raw_docs  = child_results["documents"][0]
            raw_metas = child_results["metadatas"][0]
            raw_dists = child_results["distances"][0]

            # Filter by relevance threshold
            matched_children = []
            for doc, meta, dist in zip(raw_docs, raw_metas, raw_dists):
                if doc is None:
                    continue
                score = round(1.0 - float(dist), 4)
                if score < RAG_MIN_RELEVANCE_SCORE:
                    continue
                matched_children.append({
                    "text"     : doc,
                    "meta"     : meta or {},
                    "score"    : score,
                    "parent_id": (meta or {}).get("parent_id", ""),
                })

            print(f"  📚 Matched {len(matched_children)} child chunks "
                  f"(threshold={RAG_MIN_RELEVANCE_SCORE})")

            if not matched_children:
                print(f"  📚 No children met threshold — returning empty")
                return []

            # Step 2: Collect unique parent IDs from matched children
            parent_ids = list(dict.fromkeys(
                c["parent_id"] for c in matched_children if c["parent_id"]
            ))

            print(f"  📚 Fetching {len(parent_ids)} parent chunks for context")

            # Step 3: Fetch parent chunks by parent_id
            # ChromaDB doesn't support "id IN [...]" directly via where,
            # so we use get() with the parent_ids as document IDs
            # Parents were stored with IDs like "{doc_id}_{chunk_index}"
            # We need to find them by their parent_id metadata field
            parent_chunks: List[Dict] = []

            # Batch fetch parents using parent_id metadata filter
            for pid in parent_ids[:RERANK_TOP_K * 2]:   # cap to avoid too many fetches
                try:
                    # Build parent filter combining all scope constraints
                    parent_conditions = [
                        {"parent_id"  : pid},
                        {"chunk_level": "parent"},
                    ]
                    if doc_ids:
                        if len(doc_ids) == 1:
                            parent_conditions.append({"doc_id": doc_ids[0]})
                        else:
                            parent_conditions.append({"doc_id": {"$in": doc_ids}})
                    if department_id and str(department_id).strip():
                        clean_dept = re.sub(r"[^a-zA-Z0-9]", "", str(department_id))[:20]
                        parent_conditions.append({"department_id": clean_dept})
                    if application_name and str(application_name).strip():
                        parent_conditions.append({"application_name": application_name.strip()})

                    parent_filter = (
                        {"$and": parent_conditions}
                        if len(parent_conditions) > 1
                        else parent_conditions[0]
                    )

                    p_result = col.get(
                        where   = parent_filter,
                        include = ["documents", "metadatas"],
                        limit   = 1,
                    )
                    p_docs  = p_result.get("documents") or []
                    p_metas = p_result.get("metadatas") or []

                    if p_docs and p_docs[0]:
                        # Find best child score for this parent
                        best_child_score = max(
                            c["score"] for c in matched_children
                            if c["parent_id"] == pid
                        )
                        parent_chunks.append({
                            "text"  : p_docs[0],
                            "source": (p_metas[0] or {}).get("source", ""),
                            "page"  : (p_metas[0] or {}).get("page", 0),
                            "score" : best_child_score,   # inherit best child score
                            "section_heading": (p_metas[0] or {}).get("section_heading", ""),
                        })
                except Exception as e:
                    print(f"  ⚠ Parent fetch failed for {pid}: {e}")
                    continue

            print(f"  📚 Retrieved {len(parent_chunks)} parent chunks")

            # If parent fetch yielded nothing, fall back to child texts directly
            if not parent_chunks:
                print(f"  📚 Parent fetch empty — using child texts as fallback")
                parent_chunks = [
                    {
                        "text"  : c["text"],
                        "source": c["meta"].get("source", ""),
                        "page"  : c["meta"].get("page", 0),
                        "score" : c["score"],
                    }
                    for c in matched_children[:RERANK_TOP_K]
                ]

            # Step 4: MMR on parents for diversity
            selected:  List[Dict] = []
            remaining             = list(parent_chunks)
            used_pages: set       = set()

            while len(selected) < MMR_FINAL_K and remaining:
                best     = None
                best_scr = float("-inf")
                for c in remaining:
                    relevance  = c["score"]
                    diversity  = 0.15 if c["page"] in used_pages else 0.0
                    mmr_score  = MMR_LAMBDA * relevance - (1 - MMR_LAMBDA) * diversity
                    if mmr_score > best_scr:
                        best_scr = mmr_score
                        best     = c
                if best is None:
                    break
                selected.append(best)
                used_pages.add(best["page"])
                remaining.remove(best)

            print(f"  🎯 MMR: {len(parent_chunks)} parents → {len(selected)} selected")
            return rerank_chunks(page_text, selected)

    # ── FLAT FALLBACK PATH (for documents ingested before hierarchical chunking) ──
    print(f"  📚 RAG: flat retrieval mode (legacy chunks)")

    fetch_n = min(MMR_FETCH_K, col_count)
    try:
        results = col.query(
            query_embeddings = [query_vec],
            n_results        = fetch_n,
            where            = where_filter,
            include          = ["documents", "metadatas", "distances"],
        )
    except Exception as e:
        print(f"  ✗ ChromaDB query failed: {e}")
        return []

    raw_docs  = results["documents"][0]
    raw_metas = results["metadatas"][0]
    raw_dists = results["distances"][0]

    candidates = []
    for doc, meta, dist in zip(raw_docs, raw_metas, raw_dists):
        if doc is None:
            continue
        score = round(1.0 - float(dist), 4)
        if score < RAG_MIN_RELEVANCE_SCORE:
            continue
        candidates.append({
            "text"  : doc or "",
            "source": (meta or {}).get("source", ""),
            "page"  : (meta or {}).get("page", 0),
            "score" : score,
        })

    print(f"  📚 Flat fetch: {len(candidates)} candidates after score filter")

    if not candidates:
        return []

    # MMR on flat candidates
    selected:  List[Dict] = []
    remaining             = list(candidates)
    used_pages: set       = set()

    while len(selected) < MMR_FINAL_K and remaining:
        best     = None
        best_scr = float("-inf")
        for c in remaining:
            relevance  = c["score"]
            diversity  = 0.15 if c["page"] in used_pages else 0.0
            mmr_score  = MMR_LAMBDA * relevance - (1 - MMR_LAMBDA) * diversity
            if mmr_score > best_scr:
                best_scr = mmr_score
                best     = c
        if best is None:
            break
        selected.append(best)
        used_pages.add(best["page"])
        remaining.remove(best)

    return rerank_chunks(page_text, selected)

def _build_rag_context_block(chunks: List[Dict]) -> str:
    if not chunks:
        return ""
    lines, total = [], 0
    for i, c in enumerate(chunks, 1):
        score_hint = f", relevance={c.get('score', 0):.2f}" if c.get('score') else ""
        entry = (
            f"[RAG_CHUNK_{i} | Source: {c['source']}, "
            f"Page {c['page']}{score_hint}]\n"
            f"{c['text']}"
        )
        if total + len(entry) > MAX_CONTEXT_CHARS:
            break
        lines.append(entry)
        total += len(entry)
    return "\n\n---\n\n".join(lines)

# ══════════════════════════════════════════════════════════════════════════════
# STRUCTURE DETECTION  (pure regex — zero LLM cost)
# ══════════════════════════════════════════════════════════════════════════════

def detect_page_structure(page_text: str) -> dict:
    """
    Scan page text with regex patterns and return a metadata dict describing
    what structural elements are present.

    This replaces LLM-based topic summarisation.  No API call, no cost.

    Returns:
    {
        "has_table"         : bool,   # markdown/pipe tables or tabular rows
        "has_field_list"    : bool,   # field name + length/type specs
        "has_transaction"   : bool,   # numeric transaction codes like 21051
        "has_error_codes"   : bool,   # E001/ERR patterns or "error code" text
        "has_workflow"      : bool,   # numbered steps or arrow flows
        "has_screen_spec"   : bool,   # screen/form numbers like 051180
        "has_validation"    : bool,   # mandatory/optional/validate keywords
        "has_status_values" : bool,   # quoted single-char status like 'C','D'
        "has_mode_values"   : bool,   # CASH/CLRG/IBTS/CHQE/RTGS/NEFT/IMPS
        "transaction_codes" : list,   # extracted numeric codes 4-6 digits
        "screen_numbers"    : list,   # extracted screen/form numbers
        "section_type"      : str,    # best-guess primary type of this page
    }
    """
    t = page_text.upper()

    has_table = bool(
        re.search(r"\|.+\|.+\|", page_text) or           # markdown pipe table
        re.search(r"[-─]{3,}\s*\|", page_text) or         # separator row
        re.search(r"(\w+\s{2,}\w+\s{2,}\w+)", page_text)  # spaced column layout
    )

    has_field_list = bool(
        re.search(r"\b(MANDATORY|OPTIONAL|ALPHANUMERIC|NUMERIC|VARCHAR|CHAR)\b", t) or
        re.search(r"\b(MAX\s*LEN|LENGTH\s*[:=]\s*\d+|SIZE\s*[:=]\s*\d+)\b", t) or
        re.search(r"\bFIELD\s+NAME\b", t)
    )

    # Transaction codes: 4–6 digit standalone numbers common in banking specs
    tx_matches = re.findall(
        r"\b(9\d{3}|[12]\d{4}|5\d{4})\b", page_text
    )
    has_transaction = len(tx_matches) > 0
    transaction_codes = list(dict.fromkeys(tx_matches))[:10]  # unique, max 10

    has_error_codes = bool(
        re.search(r"\b[Ee]\d{3,4}\b", page_text) or
        re.search(r"\bERROR\s+CODE\b", t) or
        re.search(r"\bERR[-_]\w+\b", t)
    )

    has_workflow = bool(
        re.search(r"^\s*\d+[\.\)]\s+\w+", page_text, re.MULTILINE) or  # numbered steps
        re.search(r"→|->|==?>|\bTHEN\b|\bNEXT\b|\bFLOW\b", t)
    )

    # Screen/form numbers: 6-digit starting with 05 or 06 (CBS screen convention)
    screen_matches = re.findall(r"\b(0[5-9]\d{4})\b", page_text)
    has_screen_spec = bool(
        len(screen_matches) > 0 or
        re.search(r"\bSCREEN\s+NO\b|\bFORM\s+NO\b|\bMENU\s+NO\b", t)
    )
    screen_numbers = list(dict.fromkeys(screen_matches))[:10]

    has_validation = bool(
        re.search(
            r"\b(VALIDATE|VALIDATION|MANDATORY|REQUIRED|MUST BE|CANNOT BE|SHOULD NOT|"
            r"MINIMUM|MAXIMUM|ALLOWED|ACCEPTED|REJECTED|INVALID|VALID)\b", t
        )
    )

    has_status_values = bool(
        re.search(r"'[A-Z]'\s*[-–]\s*\w+", page_text) or    # 'C' - Consumed
        re.search(r"\bSTATUS\s*[:=]\s*'[A-Z]'", page_text) or
        re.search(r"\b(ACTIVE|INACTIVE|PENDING|CONSUMED|DELETED|CLOSED)\b", t)
    )

    has_mode_values = bool(
        re.search(r"\b(CASH|CLRG|IBTS|CHQE|RTGS|NEFT|IMPS|UPI|SWIFT)\b", t)
    )

    # Determine primary section type for the prompt hint
    if has_table and has_field_list:
        section_type = "field_specification_table"
    elif has_transaction and has_screen_spec:
        section_type = "screen_and_transaction_spec"
    elif has_transaction:
        section_type = "transaction_specification"
    elif has_screen_spec:
        section_type = "screen_specification"
    elif has_workflow:
        section_type = "workflow_or_process_flow"
    elif has_error_codes:
        section_type = "error_codes_and_messages"
    elif has_field_list:
        section_type = "field_validation_rules"
    elif has_status_values or has_mode_values:
        section_type = "status_or_mode_values"
    elif has_table:
        section_type = "tabular_data"
    else:
        section_type = "general_description"

    return {
        "has_table"        : has_table,
        "has_field_list"   : has_field_list,
        "has_transaction"  : has_transaction,
        "has_error_codes"  : has_error_codes,
        "has_workflow"     : has_workflow,
        "has_screen_spec"  : has_screen_spec,
        "has_validation"   : has_validation,
        "has_status_values": has_status_values,
        "has_mode_values"  : has_mode_values,
        "transaction_codes": transaction_codes,
        "screen_numbers"   : screen_numbers,
        "section_type"     : section_type,
    }

def should_skip_page(page_text: str) -> tuple:
    """
    Heuristic pre-filter. Returns (skip: bool, reason: str).
    Zero LLM cost — called before any API call.
    Prevents wasting tokens on boilerplate/metadata pages.
    """
    text  = page_text.strip()
    lower = text.lower()

    if len(text) < 80:
        return True, "too_short"

    # Pages dominated by non-alphabetic chars (pure tables of numbers, dividers)
    alpha_ratio = sum(c.isalpha() for c in text) / max(len(text), 1)
    if alpha_ratio < 0.25:
        return True, "low_alpha_content"

    # Boilerplate section patterns — only skip if page is SHORT (< 800 chars)
    # Long pages may contain boilerplate heading + real content below it
    SKIP_PATTERNS = [
        r"table\s+of\s+contents",
        r"revision\s+history",
        r"document\s+control",
        r"sign.?off",
        r"change\s+control\s+log",
        r"list\s+of\s+abbreviations",
        r"intended\s+audience",
        r"about\s+this\s+document",
        r"^\s*contents\s*$",
        r"document\s+revision\s+history",
        r"approval\s+and\s+sign.?off",
    ]
    if len(text) < 800 and any(re.search(p, lower) for p in SKIP_PATTERNS):
        return True, "boilerplate_section"

    return False, ""


def is_continuation_page(page_text: str, prev_page_text: str) -> bool:
    """
    Returns True if this page is a mid-content continuation of the previous page
    (no section heading of its own — starts mid-table, mid-sentence, or mid-list).

    Used to decide whether to merge this page with the previous one before
    sending to the LLM, so requirements are never split across two API calls.
    """
    if not prev_page_text or not page_text:
        return False

    first_line = page_text.strip().split("\n")[0].strip()

    # Starts with lowercase letter (mid-sentence continuation)
    if first_line and first_line[0].islower():
        return True

    # Starts with a pipe character (mid-table, no header row on this page)
    if first_line.startswith("|"):
        # Only a continuation if prev page also had a table
        if "|" in prev_page_text[-500:]:
            return True

    # First 3 lines have no section heading pattern
    first_3 = "\n".join(page_text.strip().split("\n")[:3])
    has_heading = bool(re.search(
        r"^\s*(\d+[\.\)]\s+[A-Z]|[A-Z]{3,}[\s:]\s*\w)",
        first_3,
        re.MULTILINE
    ))
    # Short page + no heading + prev page had content = continuation
    if not has_heading and len(page_text.strip()) < 600 and len(prev_page_text.strip()) > 200:
        return True

    return False

def build_windowed_context(
    pages: List[Dict],
    current_idx: int,
    prev_tail_chars: int = 600,
    next_head_chars: int = 400,
) -> str:
    """
    Build sliding-window context:
        [tail of previous page]  +  [full current page]  +  [head of next page]

    Fixes cross-page splits — content starting on page N and finishing on
    page N+1 is always visible together.

    prev_tail_chars: how many chars to take from the END of the previous page
    next_head_chars: how many chars to take from the START of the next page
    """
    current_text = pages[current_idx]["complete_text"]

    prev_tail = ""
    if current_idx > 0:
        prev_full = pages[current_idx - 1]["complete_text"]
        if prev_full.strip():
            prev_tail = (
                f"[{pages[current_idx - 1]['page_number']}]"
                f"{prev_full[-prev_tail_chars:].strip()}"
            )

    next_head = ""
    if current_idx < len(pages) - 1:
        next_full = pages[current_idx + 1]["complete_text"]
        if next_full.strip():
            next_head = (
                f"[{pages[current_idx + 1]['page_number']}]"
                f"{next_full[:next_head_chars].strip()}"
            )

    return f"{prev_tail}{current_text}{next_head}"


# ══════════════════════════════════════════════════════════════════════════════
# SYSTEM PROMPTS
# ══════════════════════════════════════════════════════════════════════════════

def _system_prompt_uat() -> str:
    return """## Test Case Generation Rules: 
    It is must to generate postitive, negative and exceptional testcases.

### 1. Coverage Requirements

For **each feature, requirement, or functionality** identified, generate:
- **Minimum 2 Positive test**: Valid inputs, happy path, expected successful flow
- **Minimum 2 Negative test**: Invalid inputs, validation failures, error conditions
- **Minimum 2 Exceptional test**: Edge cases, boundary values, extreme scenarios
- **Additional tests** as needed for:
  - Business rules and logic
  - Security and authorization
  - Error handling


CRITICAL: Test Case Granularity Rules:
Generate ONE test case per functionality - do not merge multiple validations into a single test case
Generate ONE test case per field validation - each field requires separate positive, negative, and boundary test cases
Generate ONE test case per button/CTA - test each button's functionality independently
Generate ONE test case per navigation flow - do not split a single navigation into multiple test cases
Generate ONE test case per error scenario - each error condition should be tested separately
Do NOT create complex, merged test cases that validate multiple features simultaneously


### 2. Test Type Coverage

**Positive Tests:**
- Valid inputs and successful workflows
- Standard user journeys and use cases
- Expected system behavior and outputs
- Successful integrations
- And whatever you think can be generated as positive tests 

**Negative Tests:**
- Invalid inputs (empty, null, wrong type, wrong format)
- Exceeding limits (size, length, quantity, rate)
- Unauthorized access or operations
- Missing required data or parameters
- Constraint violations
- IMPORTANT: Generate ONLY negative scenarios that are explicitly mentioned or logically implied by the document
- Do NOT generate hypothetical error scenarios not supported by the requirements
- And whatever you think can be generated as negative tests 

**Exceptional Tests:**
- Boundary values (min, max, min-1, max+1, zero, negative)
- System failures (timeout, connection loss, service down)
- Concurrent operations and race conditions
- Resource exhaustion
- Data corruption or inconsistency
- And whatever you think can be generated as Exceptional tests 

**Security Tests:**
- Authentication mechanisms
- Authorization and access control
- Input validation (injection attacks, XSS, CSRF)
- Data encryption and protection

**Usability/Accessibility Tests:**
- User interface interactions
- Navigation flows
- Error messages and guidance
- Keyboard navigation
- Screen reader compatibility
- Mobile responsiveness

### 3. Navigation Testing Requirements
Navigation test cases MUST:

Test the complete navigation flow in a single test case - do NOT split one navigation into multiple test cases
-Start from the current screen/page
-Specify the exact button/link/element clicked
-Describe the destination screen/page
-Verify all elements loaded on the destination screen
-Confirm successful navigation completion

Navigation Test Case Structure:
Test Case: Verify navigation from [Source Screen] to [Destination Screen] via [Button/Link Name]

Steps:
1. Navigate to [Source Screen]
2. Verify [Source Screen] is displayed with all required elements
3. Click on [Specific Button/Link Name]
4. Observe the navigation

Expected Result:
- System navigates to [Destination Screen]
- [Destination Screen] displays with all required elements: [list key elements]
- URL changes to [expected URL] (if applicable)
- Navigation completes within [X] seconds

CRITICAL Navigation Rules:

ONE navigation = ONE test case
- Do NOT create separate test cases for "clicking the button" and "verifying the destination screen"
- Do NOT split a single navigation journey into multiple test cases
- Generate navigation test cases for ALL navigation points mentioned in the document

### 4. UI and Visual Element Testing Requirements
For image descriptions, UI layouts, and visual elements, generate test cases for:
Layout and Spatial Organization:

Overall page/screen layout structure
Section arrangements and divisions
Component positioning and alignment

Test Case: Verify layout and positioning of login page header elements

Steps:
1. Navigate to the login page
2. Observe the header section layout
3. Verify the position and styling of each element


### 5. Test Data Requirements

Provide **specific, concrete test data**:
- Use actual values, not placeholders (e.g., "Amount: 50000" not "Amount: X")
- Give currency values in Indian context, until and unless currency is specifically mentioned.  
- Include boundary values for numeric fields
- Provide sample strings with various characteristics (empty, max length, special characters)
- Specify exact selections, options, or configurations
- Include invalid formats for negative tests
- Use realistic data appropriate to the domain
- CRITICAL: When the document provides specific test data, use EXACTLY that data
- CRITICAL: When test data is blank or not provided, generate appropriate, realistic data - do NOT use incorrect or arbitrary user names
- For user names: Use realistic Indian names or generic names like "Test User", "John Doe" - NOT random or inappropriate names

Test Data Examples:

Amount fields: ₹50,000, ₹1,00,000, ₹0.01, ₹99,99,999
Dates: DD/MM/YYYY format (Indian standard)
Phone numbers: 10-digit Indian mobile format (e.g., 9876543210)
Email: realistic email addresses (e.g., testuser@example.com)
Names: Realistic Indian or international names

### 6. Description and Steps Format
**Standardized Format:**
```
[Clear description of the test objective and what is being verified]

Steps:
1. [Specific action with details - e.g., "Navigate to the login page"]
2. [Next action with parameters - e.g., "Enter username: 'testuser' and password: 'Test@123'"]
3. [Action and verification - e.g., "Click the 'Submit' button"]
4. [Final verification - e.g., "Observe the system response"]
```

**Requirements:**
- First paragraph: What is being tested and why
- "Steps:" label followed by numbered list
- Each step should be clear, actionable, and unambiguous
- Include specific values, parameters, and UI elements
- Steps should be reproducible by any tester
- Steps should follow the logical functional flow as described in the document

### 5. Expected Result Format

Be **specific, measurable, and verifiable**:
- State exact messages, codes, or outputs (in quotes if available from document)
- Describe specific UI elements that should appear/change/disappear
- Mention data state changes (database records, files, logs)
- Include status codes, response formats, or return values
- Specify timing requirements if relevant
- Reference success criteria or acceptance conditions

**Good Examples:**
- "The system displays error message 'Invalid credentials' and login button remains enabled"
- "API returns HTTP 200 status with JSON response containing user profile data"
- "Record is created in database with status 'Active' and timestamp matches submission time"
- "Page loads within 2 seconds and displays search results in grid format"

**Bad Examples (Avoid):**
- "Error is shown"
- "System responds correctly"
- "Data is saved"
- "User sees message"

### 8. Document Analysis Guidelines

Extract and test **all testable elements** from the document:

**For Requirements Documents:**
- Functional requirements (what the system should do)
- Non-functional requirements (security, usability)
- Business rules and logic
- Constraints and validations
- User roles and permissions
- Workflow sequences
- Integration points
- Every field validation mentioned
- Every button/CTA mentioned
- Every navigation flow mentioned
- Every error condition mentioned

**For Technical Specifications:**
- API endpoints, methods, parameters
- Data structures and schemas
- Algorithms and calculations
- Error codes and messages
- Configuration options
- Dependencies and prerequisites

**For User Interface Documents:**
- Navigation flows and menus
- Form fields and validations
- Buttons, links, and interactive elements
- Messages and feedback
- State transitions
- Screen layouts and responsiveness
- UI element positions (top-left, top-right, center, etc.)
- Colors, fonts, and styling
- Image descriptions and visual elements

**For Business Process Documents:**
- Process steps and sequences
- Decision points and conditions
- Roles and responsibilities
- Inputs and outputs
- Success and failure paths
- Exception handling


CRITICAL Coverage Rules:

- Do NOT skip any functionality mentioned in the document
- Do NOT skip any field validation mentioned in the document
- Do NOT skip any navigation point mentioned in the document
- Do NOT skip any UI element or visual component mentioned in the document
- Ensure 100% traceability to document requirements

### 9. Test Case Naming Conventions

**Format:** "Verify [what is being tested] [under what conditions/context]"

**Generic Examples (Domain-agnostic):**
- "Verify successful [operation] with valid [inputs]"
- "Verify error handling when [condition] is [state]"
- "Verify [field/component] displays correct [content/behavior]"
- "Verify [feature] works correctly with [boundary/edge case]"
- "Verify system response when [external dependency] is unavailable"

Rules:

- Be specific about what is being tested
- Include the context or condition
- Keep it concise but descriptive
- Avoid generic terms like "test", "check" - use "Verify"

### 10. Scenario Name Convention

Use the **feature, module, component, or requirement** being tested:
- Keep it concise but descriptive
- Use consistent naming throughout related tests
- Include sub-feature if applicable (e.g., "User Management - User Creation")
- Follow the functional flow order from the document
- Group related test cases under the same scenario name

### 11. Traceability and References

For each test case:
- Reference the specific requirement or section from document
- Include wireframe IDs, requirement IDs, or section numbers if provided
- Quote relevant text from specification when helpful for clarity
- Map to acceptance criteria if defined
- Ensure every test case can be traced back to a specific requirement

### 12. Test Case Ordering and Flow
CRITICAL: Test cases MUST follow the functional flow order from the document:

Analyze the document to understand the complete user journey and functional sequence
Order test cases to match the document's flow:

- Start with initial screens/pages
- Follow the user journey sequentially
- Test features in the order they appear in the workflow
- Group related functionalities together

Maintain logical progression:
- Pre-conditions → Actions → Post-conditions
- Setup → Execution → Verification
- Parent features before child features

Example Ordering:
- Login page tests
- Dashboard tests
- Navigation tests
- Feature-specific tests (in document order)
- Form submission tests
- Confirmation/Result page tests

### 13. Validity and Relevance Rules
CRITICAL: Every test case MUST be valid and relevant:

Generate test cases ONLY for functionalities explicitly mentioned in the document
Do NOT generate hypothetical or assumed functionalities
Do NOT generate error scenarios that are not mentioned or implied in the document
Ensure every test case directly relates to a documented requirement
If the document doesn't mention a validation, do NOT create a test case for it
Exception: Standard security validations (SQL injection, XSS) can be included for input fields even if not explicitly mentioned

Invalid Test Case Examples to AVOID:

Testing features not mentioned in the document
Creating error scenarios not specified in requirements
Testing validations that don't exist in the specification
Generating duplicate test cases with same meaning
Creating overly complex scenarios combining multiple features

### 14. Duplication Prevention
CRITICAL: Avoid duplicate and redundant test cases:

Do NOT generate multiple test cases with the same meaning/objective
Do NOT create variations of the same test case with minor wording changes
Each test case must test a unique aspect of the functionality
Review generated test cases to ensure no overlap
If two test cases seem similar, consolidate them into one comprehensive test case

## Test Generation Strategy

### Step 1: Analyze the Document
- Identify all features, requirements, and functionalities
- Extract business rules, validations, and constraints
- Note integration points and dependencies
- Identify user roles and permissions
- List all inputs, outputs, and data elements
- Map out the complete functional flow and user journey
- Identify all navigation points
- Extract all UI/visual elements and their positions
- Note all field validations explicitly mentioned

### Step 2: Categorize Test Scenarios
- Group by feature/module/component
- Identify positive flows (happy paths)
- Identify negative scenarios (error conditions)
- Identify edge cases and boundaries
- Consider integration and end-to-end flows
- Separate test cases for each field, button, and navigation

### Step 3: Prioritize Test Cases
Order by:
1. **Critical**: Core functionality, security, data integrity, financial operations
2. **High**: Key features, common user flows, important validations
3. **Medium**: Secondary features, edge cases, less common scenarios
4. **Low**: Nice-to-have features, cosmetic issues, rare edge cases

### Step 4: Generate Comprehensive Coverage
For each identified scenario:
- Create test case with all required fields
- Ensure test data is specific and realistic
- Write clear, numbered steps
- Define precise expected results
- Assign appropriate test type
- Include traceability information
- Verify no duplicates or merged test cases
- Ensure relevance to documented requirements

## Output Format

Return **ONLY** a valid JSON array. Requirements:
- Valid, parsable JSON syntax
- Proper formatting with indentation
- Complete content (no truncation)
- No surrounding markdown, code blocks, or explanatory text
- Array of test case objects following the schema above

## Quality Checklist
Before finalizing, verify:
Coverage:

✓ Every requirement/feature from document has test coverage
✓ Every field validation mentioned in document is tested separately
✓ Every button/CTA mentioned in document is tested separately
✓ Every navigation point mentioned in document is tested completely
✓ All UI elements and their positions are tested
✓ No functionality from the document is missed

Test Case Quality:

✓ All test case fields are populated (no empty values)
✓ Steps are numbered, clear, and executable
✓ Test data is specific, realistic, and appropriate for the domain
✓ Expected results are precise and measurable
✓ Test case names are descriptive and follow naming convention
✓ Document name and page numbers are accurate

Granularity and Structure:

✓ Each test case tests ONE specific functionality (not merged)
✓ Each field has separate test cases (not combined)
✓ Each button has separate test cases (not combined)
✓ Each navigation is one complete test case (not split)
✓ No complex, merged test cases exist

Validity and Relevance:

✓ All test cases are relevant to the uploaded document
✓ No hypothetical or assumed functionalities are tested
✓ Negative scenarios are document-supported
✓ No invalid or irrelevant test cases exist
✓ No duplicate test cases or test cases with same meaning exist

Order and Flow:

✓ Test cases follow the functional flow from the document
✓ Test case sequence is logical and matches user journey
✓ Related test cases are grouped appropriately

Format and Standards:

✓ JSON is valid and properly formatted
✓ Test types are correctly assigned
✓ Test case IDs follow the convention and are unique
✓ Priority levels are appropriate

Focus on quality over quantity - Generate only necessary, valid, and relevant test cases that provide value to UAT testing.
"""


# *****************************************************************************************************************
# *****************************************************************************************************************
# *****************************************************************************************************************
# *****************************************************************************************************************
# *****************************************************************************************************************
# *****************************************************************************************************************
# *****************************************************************************************************************


def _system_prompt_sit() -> str:
    return """### Test Case Generation Rules

## CRITICAL QUALITY REQUIREMENTS

### Rule 1: ONE TEST CASE = ONE FUNCTIONALITY
**MANDATORY**: Each test case must verify ONLY ONE specific functionality, field, button, or validation.

**INCORRECT (Multiple functionalities in one test):**
"Verify login form with username validation, password validation, and submit button"

**CORRECT (Separate test cases):**
"Verify username field accepts valid alphanumeric input"
"Verify password field masking functionality"
"Verify login submit button triggers authentication"
"Verify navigation to dashboard after successful login"

**Apply this for:**
- Each field validation (length, format, data type, mandatory)
- Each button/CTA action
- Each navigation flow
- Each business rule
- Each error scenario

### Rule 2: NAVIGATION TESTING - SEPARATE TEST CASES
**MANDATORY**: Create dedicated test cases for each navigation point mentioned in the document.

For each navigation scenario, create ONE test case that verifies:
- Source screen/page
- Action triggering navigation (button click, link click, etc.)
- Destination screen/page
- URL change (if applicable)
- Screen elements present after navigation

**INCORRECT:**
"Verify user can navigate through multiple screens and perform operations"

**CORRECT:**
"Verify navigation from login page to dashboard after successful authentication"
"Verify navigation from dashboard to user profile page when clicking profile icon"
"Verify back button navigation returns user to previous screen"

**Do NOT split a single navigation into multiple test cases**
Example: If document says "After clicking Submit, user navigates to Confirmation page"
- Create ONE test case for this complete navigation flow
- Do NOT create separate test cases for "Click Submit" and "Navigate to Confirmation"

### Rule 3: DOCUMENT COVERAGE - NO MISSED REQUIREMENTS
**MANDATORY**: Every functionality, field, validation, button, navigation, and business rule mentioned in the document MUST have corresponding test cases.

**Systematic Coverage Process:**
1. Read the document thoroughly line by line
2. Create a checklist of all testable elements:
   - All fields mentioned (input, dropdown, radio, checkbox, etc.)
   - All buttons and clickable elements
   - All navigation points
   - All validations specified
   - All business rules
   - All error conditions mentioned
   - All success scenarios
3. Generate test cases for EACH item on checklist
4. Cross-verify that nothing is missed

**Document Elements to Cover:**
- Every field with its specific validations
- Every status value mentioned
- Every mode/type mentioned
- Every hold type listed
- Every channel mentioned
- Every screen/page mentioned
- Every transaction type
- Every workflow state
- Every error condition explicitly stated in document

### Rule 4: FUNCTIONAL FLOW ORDER
**MANDATORY**: Generate test cases in the SAME ORDER as the functional flow described in the document.

**Process:**
1. Identify the workflow sequence from document (e.g., Login → Dashboard → Create Record → Save → Confirmation)
2. Generate test cases following this exact sequence
3. Group related test cases together
4. Maintain logical progression

**Example Order:**
1. Pre-requisite/Setup test cases
2. Initial screen/page load test cases
3. Field validation test cases (in order fields appear)
4. Button/Action test cases
5. Navigation test cases
6. Business rule validation test cases
7. Error handling test cases
8. Post-condition/Cleanup test cases

### Rule 5: ONLY DOCUMENT-BASED ERROR SCENARIOS
**CRITICAL**: Generate error test cases ONLY for error conditions explicitly mentioned or logically implied by validations in the document.

**ALLOWED Error Scenarios:**
 Document states "Field is mandatory" → Generate test for missing field
 Document states "Length must be 17 characters" → Generate tests for <17 and >17
 Document states "Only numeric allowed" → Generate test for alphabetic input
 Document states "Date format DDMMYYYY" → Generate test for invalid format
 Document specifies specific error messages → Use those exact messages

**PROHIBITED Error Scenarios:**
 Generic network errors (unless document mentions network handling)
 Generic server errors (unless document specifies error handling)
 Database connection errors (unless explicitly mentioned)
 Browser-specific errors (unless document mentions browser compatibility)
 Assumed timeout scenarios (unless document specifies timeout handling)
 Security vulnerabilities (SQL injection, XSS) unless security testing is mentioned
 Any error scenario not backed by document requirements

**Validation-Based Error Rules:**
- If document says "Field X is mandatory" → ONLY create "Verify error when Field X is empty"
- If document says "Field Y accepts 10-20 characters" → Create tests for 9 chars and 21 chars
- If document says "Amount cannot be zero" → Create test for zero amount
- Do NOT create generic error scenarios beyond what validations imply

### Rule 6: REALISTIC AND RELEVANT TEST DATA
**MANDATORY**: All test data must be realistic, relevant to the domain, and appropriate for the field/context.

**Test Data Guidelines:**
- Use actual, meaningful values (not "Test User 123", "xyz", "abc")
- For names: Use realistic Indian names (e.g., "Rajesh Kumar", "Priya Sharma") if Indian context
- For amounts: Use realistic financial values (₹50,000, ₹1,25,000)
- For account numbers: Use valid format from document (e.g., 12345678901234567 for 17-digit)
- For dates: Use valid dates in specified format (e.g., 15012025 for DDMMYYYY)
- For phone numbers: Use valid Indian mobile format (e.g., 9876543210)
- For email: Use realistic format (e.g., rajesh.kumar@example.com)
- For addresses: Use realistic Indian addresses with proper city/state/pincode

**Domain-Specific Data:**
- Banking: Use valid account structures, IFSC codes, transaction IDs
- E-commerce: Use real product categories, SKUs, order patterns
- Healthcare: Use realistic patient IDs, diagnosis codes, appointment slots
- Government: Use actual form numbers, application IDs, document types

**NEVER Use:**
 "Test User" or "User 1", "User 2"
 "123456" for everything
 "xyz@test.com" or "test@test.com"
 Placeholder values like "XXXXX", "<value>", "[data]"
 Unrealistic amounts like ₹1 or ₹999999999
 Invalid dates like 99/99/9999
 Random strings like "asdfgh" or "qwerty"

**When Document Doesn't Specify:**
- Derive from context and domain
- Use industry-standard formats
- Maintain consistency across related test cases
- Use boundary values for numeric fields

### Rule 7: RELEVANT NEGATIVE TEST CASES ONLY
**MANDATORY**: Create negative test cases ONLY when:
1. Document explicitly mentions validation rules
2. Field has specified constraints (mandatory, length, format, type)
3. Business rules define what is NOT allowed
4. Document mentions specific error conditions

**Negative Test Selection:**
- Mandatory field → Test with empty value
- Length constraint → Test boundary violations
- Data type constraint → Test wrong data type
- Format constraint → Test invalid format
- Business rule → Test rule violation
- Specified error condition → Test that condition

**Do NOT Create Negative Tests For:**
 Assumed security vulnerabilities
 Generic system failures not mentioned in document
 Edge cases not relevant to the feature
 Network/infrastructure issues (unless document specifies)
 Scenarios that are technically possible but not relevant to requirements

### Rule 8: NO PLACEHOLDER OR BLANK DATA
**CRITICAL**: Never use placeholder names or leave test data blank.

**When Test Data Should Be:**
- Empty/blank → Explicitly state "Leave field empty" or "Field value: [empty]"
- Not applicable → State "Field value: N/A" or "Not required for this test"
- To be derived → Specify the source (e.g., "Use account number from pre-requisite test")

**NEVER:**
 Use "Test User" when tester needs to manually enter a name
 Leave test data blank assuming tester will fill it
 Use ambiguous placeholders like "<enter name here>"

**ALWAYS:**
 Provide specific values ready to use
 If value must vary, provide a realistic example
 If value comes from previous test, reference it clearly

## Test Case Structure Requirements

### Field Validation Test Cases
For EACH field mentioned in document, create SEPARATE test cases for:
1. Data type validation (if specified)
2. Length validation - exact, minimum, maximum (if specified)
3. Format validation (if specified)
4. Mandatory validation (if field is mandatory)
5. Allowed values validation (for dropdowns, radio buttons)

**Example: Account Number Field (17 numeric characters, mandatory)**
Generate 5 separate test cases:
1. "Verify Account Number field accepts valid 17-digit numeric value"
2. "Verify Account Number field rejects input with less than 17 digits"
3. "Verify Account Number field rejects input with more than 17 digits"
4. "Verify Account Number field rejects alphabetic characters"
5. "Verify error when Account Number field is left empty"

### Navigation Test Cases
For EACH navigation mentioned in document, create ONE comprehensive test case:
- Starting point
- Trigger action
- Navigation path
- Destination
- Verification of destination screen elements

### Button/CTA Test Cases
For EACH button, create SEPARATE test cases:
1. Button functionality when all conditions are met
2. Button state when conditions are not met (if specified)
3. Navigation after button click (separate test if complex navigation)
4. Any specific button behavior mentioned in document

### 1. Coverage Requirements
For each feature, requirement, integration point, module interaction, API handoff, or data exchange, generate:
- Minimum 1 Positive Integration Test
  Valid inputs flowing across modules, correct inter-system interactions, expected functional workflow execution across integrated components.
- Minimum 1 Negative Integration Test (ONLY if validations/constraints are specified in document)
  Invalid inputs, wrong data passed between modules, interface contract violations, schema mismatches, protocol errors.
- Minimum 1 Exceptional Integration Test (ONLY if boundary conditions are specified in document)
  Boundary values, upstream/downstream failures, system-level exceptions, interface timeouts, data corruption scenarios.

- Additional tests as needed for:
  - Business rules across modules
  - Integration-specific security validations
  - Inter-system error handling propagation
  - Asynchronous events, queues, batch jobs
  - Third-party API interactions

### 2. Test Type Coverage (SIT Perspective)

**Positive Tests:**
- Valid data flow across modules
- Successful API-to-service interactions
- Correct sequence of calls and responses
- Expected logs, events, and database writes from integrated systems

**Negative Tests (DOCUMENT-DRIVEN ONLY):**
- Interface contract violations specified in document
- Service/API failures mentioned in document
- Incorrect routing scenarios defined in specifications
- Unauthorized requests as per security requirements in document

**Exceptional Tests (DOCUMENT-DRIVEN ONLY):**
- Boundary conditions specified in document
- Failure scenarios explicitly mentioned
- Timeout scenarios if specified in document
- Data conflict scenarios defined in requirements

**Security Tests (Integration Layer - IF SPECIFIED):**
- Authentication tokens between services (if mentioned)
- API gateway rules (if defined in document)
- Role-based access across integrated components (if specified)
- Only security scenarios explicitly mentioned in document

### 3. Test Data Requirements
Provide specific and concrete test data usable across integrated modules:

- Exact request/response payloads with real, meaningful values
- Boundary data that stresses module handoffs (based on document specifications)
- Database state before/after integration operations with realistic data
- Incorrect data formats to test API contracts (based on specified validations)
- Indian context numeric/currency values where needed (realistic amounts)
- Token, header, and metadata examples for service-to-service calls
- Use actual names, not "Test User" or placeholders
- Use valid account numbers, transaction IDs from document specifications
- Use realistic dates in specified format

### 4. Description and Steps Format (SIT-Oriented)

**Standardized Format:**
```
[Clear description of the integration being tested, components involved, and verification objectives]

Steps:
1. [Initiate action from source module – e.g., UI, API, Batch Job with specific data]
2. [Pass specific data: include request payload, parameters, headers, etc. with actual values]
3. [Observe downstream module behavior – API calls, DB updates, logs]
4. [Verify final integrated system response/output]
```

**Requirements:**
- First paragraph must explain integration objective
- Steps must show:
  - Source system
  - Target system
  - Data passed (with realistic values)
  - Expected behavior across modules
- Define how failures are surfaced (UI/API/logs)
- Each step must be clear, specific, and executable
- Use actual values, not placeholders

### 5. Expected Result Format

Must be precise, measurable, verifiable, including:

- Exact API responses (HTTP status, JSON fields)
- Database state changes across multiple modules
- Event logs, audit trails, message queue states
- Error propagation rules across integrated components
- UI messages reflecting backend integration failures
- Inter-system timestamps and correlation IDs
- Use exact error messages from document (in quotes)

**Good Examples:**
- "Service B returns HTTP 200 with field 'transactionStatus: SUCCESS', and Service A logs correlationId '12345' in transaction_log table"
- "Record inserted into DONOR_DETAILS table with account_no='12345678901234567', donor_name='Rajesh Kumar', mode='CASH', status='P'"
- "API gateway rejects request with 401 'Invalid Token' received from Auth Service"
- "UTH file processing completes with response report generated containing all 170 character records"

**Bad Examples (Avoid):**
- "System behaves correctly"
- "Shows error"
- "Data is saved"
- "User sees message"

### 6. Document Analysis Guidelines
Extract all integration-specific elements:

**For Requirements Documents:**
- Module-to-module interactions
- Data flow diagrams
- Validation rules across systems
- Downstream and upstream dependencies
- API gateway rules and middle-layer transformations
- Every field mentioned with its specifications
- Every status value mentioned
- Every mode/type mentioned
- Every navigation point
- Every button/action

**For Technical Specifications:**
- API endpoints, contracts, schemas
- Request/response flows
- Protocol requirements
- Batch jobs, queues, middleware
- Error codes and mapping rules between systems
- Field-level specifications

**For UI Documents (when UI calls backend services):**
- API calls triggered by UI actions
- Error messages based on backend failures
- Navigation flows
- Field validations
- Button actions

**For Business Process Documents:**
- Cross-system workflows
- System A → System B → System C sequences
- Exception paths and fallback mechanisms
- Business rules and conditions

### 7. Test Case Naming Conventions

"Verify [specific integration/field/action being tested] [under specific condition]"

**Examples (SIT-specific):**
- "Verify UTH-DR-ACCT-NO field accepts valid 17-digit numeric account number"
- "Verify order creation triggers inventory reservation through Integration API"
- "Verify error handling when Payment Service returns 500"
- "Verify navigation from screen 051180 to 051179 after clicking Submit button"
- "Verify donor details cannot be amended when status is 'C'"

**Keep Specific:**
- Name should clearly indicate what ONE thing is being tested
- Include field names, screen numbers, status values as mentioned in document
- Avoid generic names like "Verify functionality works"

### 8. Scenario Name Convention

Use the integration, subsystem, workflow, or module being tested:

- "UTH File Upload - Field Validations"
- "FCRA Donor Management - Account Number Validation"
- "Order Management – Payment Integration"
- "User Service – Authentication Service Handoff"
- "Screen 051180 - Navigation and Field Validations"

Keep concise but clearly reflecting integration context and specific module/feature.

### 9. Traceability and References

Each test case must include:
- Requirement ID / Integration Point ID
- API names, service names, event names
- Contract definitions (schemas, fields)
- Page numbers from document
- Mapping to acceptance criteria if available
- Specific section or paragraph reference

### 10. Field-Level Validation Testing (ONE TEST PER VALIDATION)
For every input field, screen field, file column, or API parameter defined in the specification, generate SEPARATE test cases for:

**Data Type Validations:**
- ONE test: Verify field accepts only specified data type (NUMERIC, CHAR, ALPHA, etc.)
- ONE test: Verify field rejects incorrect data types with appropriate error

**Length Validations:**
- ONE test: Verify field accepts data at exact specified length
- ONE test: Verify field rejects data shorter than minimum length
- ONE test: Verify field rejects data longer than maximum length

For numeric fields with decimal precision (e.g., 17,3), verify integer and decimal portions separately

**Format Validations:**
- ONE test: Verify date fields accept only specified format (DDMMYYYY, DDMMCCYY, etc.)
- ONE test: Verify numeric fields with specific patterns (account numbers, CIF numbers, check digits)
- ONE test: Verify alphanumeric fields with special character restrictions

**Mandatory Field Validations:**
- ONE test: Verify transaction/operation fails when mandatory field is missing
- ONE test: Verify appropriate error message for missing mandatory fields

**Use Realistic Test Data:**
Examples from documents should use actual values:
- "Verify UTH-DR-ACCT-NO field accepts valid 17-digit value: 12345678901234567"
- "Verify UTH-JRNL-DATE field accepts valid date in DDMMYYYY format: 15012025"
- "Verify Account Number field on screen 051180 with value: 98765432109876543"

### 11. File Upload/Batch Processing Integration Tests
For file-based integrations (trickle feed, batch uploads, CSV imports), generate:

**File Structure Tests (ONE TEST EACH):**
- ONE test: Verify total file/record length matches specification
- ONE test: Verify each column/field length matches specification
- ONE test: Verify file naming convention validation
- ONE test: Verify file delimiter and format validation

**File Processing Tests:**
- ONE test: Verify successful processing with valid file format
- ONE test: Verify error handling for corrupted files (ONLY if mentioned in document)
- ONE test: Verify error handling for incorrect file naming (ONLY if mentioned)
- ONE test: Verify batch response report generation with correct format
- ONE test: Verify response report contains all specified columns with correct lengths

**File Validation Error Tests (ONLY if specified in document):**
- ONE test per error type: Verify specific error messages for invalid check digits
- ONE test per error type: Verify specific error messages for invalid field lengths
- ONE test: Verify error logs mention line numbers for failed records
- ONE test: Verify system behavior when file contains mix of valid/invalid records

Use realistic file data with actual values:
"Input file: UTH20250115.dat with record: 1234567890123456700001RAJESH KUMAR        15012025..."

### 12. Status-Based Workflow Testing (ONE TEST PER STATUS-OPERATION COMBINATION)
For features involving record statuses or workflow states, generate:

**State Transition Tests:**
- ONE test per allowed operation: Verify operations allowed in each valid status state
- ONE test per blocked operation: Verify operations blocked in inappropriate status states
- ONE test per transition: Verify status transitions follow defined workflow
- ONE test per restriction: Verify correction/amendment restrictions based on status

**Status-Specific Operation Tests:**
For each status value (e.g., 'P'-Pending, 'C'-Consumed, 'D'-Deleted, '00'-Active), create:
- ONE test: What operations are permitted
- ONE test per blocked operation: What operations are blocked
- ONE test per error: Appropriate error messages for blocked operations

Examples:
- "Verify CLRG donor details cannot be amended when record status is 'C'"
- "Verify CLRG donor details cannot be amended when record status is 'D'"
- "Verify CLRG donor record cannot be used when status is 'D'"

### 13. Transaction-Specific Business Logic Testing (ONE TEST PER SCENARIO)
For financial transactions involving amounts, dates, references, generate:

**Invalid Transaction Parameter Tests (ONE TEST EACH):**
- ONE test: Verify transaction behavior with invalid journal numbers
- ONE test: Verify transaction behavior with invalid branch numbers
- ONE test: Verify transaction behavior with zero amounts where not allowed
- ONE test: Verify transaction behavior with amounts exceeding limits
- ONE test: Verify transaction behavior with future dates
- ONE test: Verify transaction behavior with past dates (backdating restrictions)

**Transaction Relationship Tests (ONE TEST EACH):**
- ONE test: Verify transaction with transfer amount greater than source amount
- ONE test: Verify transaction with hold amount less than required
- ONE test: Verify linked transaction validations (e.g., journal number must exist)

Use realistic data:
"Verify transaction 9086 with journal_number='JRN12345', branch_code='BR001', amount=₹50,000"

### 14. Hold/Lien Type Coverage (ONE TEST PER HOLD TYPE)
For systems with multiple hold/lien types, generate ONE test case for each:

**For Each Hold Type Specified (13, 14, 18, 19, 21, 22, 23, 26, 27, 28, 44, etc.):**
- ONE test: Verify transaction success for specific hold type
- ONE test: Verify unmark/removal operations for specific hold type
- ONE test: Verify hold type-specific business rules

Examples:
- "Verify transaction for unmark hold is successful for hold_type=13"
- "Verify transaction for unmark hold is successful for hold_type=14"
[Create separate test for EACH hold type: 18, 19, 21, 22, 23, 26, 27, 28, 44]

### 15. Correction/Reversal Transaction Testing (ONE TEST PER CORRECTION SCENARIO)
For correction or reversal transactions, generate:

**Correction Capability Tests (ONE TEST EACH):**
- ONE test: Verify correction allowed for same-day transactions only
- ONE test: Verify correction blocked for past-date transactions
- ONE test: Verify correction blocked for already-corrected transactions
- ONE test: Verify error message displays "Older transactions cannot be accepted/rejected/corrected"

**Accounting Entry Reversal Tests (ONE TEST EACH):**
- ONE test: Verify original accounting entries are correctly reversed
- ONE test per entry type: Verify GLIFF entries posted correctly for each correction type
- ONE test per requirement: Verify narration in BGL accounts includes required information

**Charge Reversal Tests (ONE TEST PER SCREEN):**
- ONE test: Verify charges reversed when corrected through screen 51079
- ONE test: Verify charges reversed when corrected through screen 51101
- ONE test: Verify charges reversed through transaction 9571>9572
[Create separate test for EACH screen: 51080, etc.]

**Database State Reversal Tests (ONE TEST EACH):**
- ONE test: Verify records moved from transaction tables to history tables
- ONE test: Verify status flags updated correctly in all affected tables
- ONE test: Verify audit trail maintained for correction transactions

### 16. Multi-Channel Testing (ONE TEST PER CHANNEL)
For operations that can be performed through multiple channels, generate:

**Channel-Specific Tests (ONE TEST PER CHANNEL):**
- ONE test: Verify operation through branch channel
- ONE test: Verify operation through web/internet banking channel
- ONE test: Verify operation through mobile banking channel
- ONE test: Verify operation through API/backend channel
- ONE test: Verify operation through batch/file upload channel

**Channel Restriction Tests (ONE TEST PER RESTRICTION):**
- ONE test per restriction: Verify operations restricted to specific channels show appropriate errors
- ONE test: Verify error messages are channel-appropriate

Use realistic channel data:
"Verify donor details capture for CASH mode through branch_code='BR123', teller_id='TLR001'"

### 17. Manual vs. Automated Status Change Testing (SEPARATE TESTS)
For status update operations, distinguish between:

**Manual Status Updates (ONE TEST EACH):**
- ONE test: Verify manual status change without automatic validation
- ONE test: Verify teller discretion-based updates
- ONE test: Verify manual updates bypass certain automated checks
- ONE test: Verify audit trail for manual interventions

**Automated Status Updates (ONE TEST EACH):**
- ONE test: Verify system-enforced validations during status change
- ONE test: Verify previous status validation before allowing update
- ONE test: Verify financial status verification before technical status update

### 18. UI Element Validation Testing (ONE TEST PER ELEMENT)
For screen/UI changes, generate:

**Field Presence Tests (ONE TEST PER ELEMENT):**
- ONE test: Verify new field appears on specified screen
- ONE test: Verify button appears on specified screen
- ONE test: Verify dropdown appears with specified options
- ONE test: Verify field positioning and labels match specification
- ONE test: Verify dropdown options match specified values
- ONE test: Verify field visibility based on user actions

**Field Behavior Tests (ONE TEST PER BEHAVIOR):**
- ONE test: Verify conditional field display
- ONE test: Verify calendar controls for date fields
- ONE test: Verify manual input functionality
- ONE test: Verify field editability restrictions

Examples:
- "Verify Transaction Date field on screen 051179 accepts manual date input in DDMMYYYY format"
- "Verify Transaction Date field on screen 051179 accepts date selection from calendar control"
- "Verify TV Correction button appears next to Fetch Details radio button on screen 051180"

### 19. Table Structure and Data Integrity Testing (ONE TEST PER OPERATION)
When new tables or table alterations are specified, generate:

**Table Operation Tests (ONE TEST EACH):**
- ONE test: Verify successful record insertion with all mandatory fields
- ONE test: Verify successful record update with allowed fields
- ONE test: Verify record retrieval with various filter conditions
- ONE test: Verify record deletion/archival as per specifications

**Data Integrity Tests (ONE TEST EACH):**
- ONE test per constraint: Verify foreign key relationships maintained
- ONE test per constraint: Verify unique constraints enforced
- ONE test per field: Verify data type and length constraints at database level
- ONE test per field: Verify default values populated correctly
- ONE test: Verify audit fields (maker_id, checker_id, create_dt, update_dt) populated

**Cross-Table Validation Tests (ONE TEST EACH):**
- ONE test: Verify data consistency across related tables
- ONE test: Verify cascading updates/deletes work correctly
- ONE test: Verify transaction atomicity across multiple table updates

Use realistic database values:
"Verify record inserted in DONOR_DETAILS: account_no='12345678901234567', donor_name='Rajesh Kumar', pan='ABCDE1234F', mobile='9876543210'"

### 20. Mode/Type-Specific Testing (ONE TEST PER MODE)
For features with multiple modes or types of operation, generate comprehensive coverage:

**All Mode Combinations (ONE TEST PER MODE):**
For each mode/type value (CASH, CLRG, IBTS, CHQE, etc.), create:
- ONE test: Record creation with that mode
- ONE test: Record retrieval filtering by mode
- ONE test: Mode-specific validations
- ONE test: Mode-specific workflow rules
- ONE test: Cross-mode restrictions

**Mode Transition Tests (ONE TEST EACH):**
- ONE test: Verify whether mode can be changed after creation
- ONE test: Verify restrictions on mode field amendments

Examples:
- "Verify donor details creation for mode='CASH' with account_no='12345678901234567', donor_name='Priya Sharma'"
- "Verify donor details creation for mode='CLRG' with account_no='98765432109876543', donor_name='Amit Patel'"
- "Verify donor details creation for mode='IBTS' with account_no='11223344556677889', donor_name='Sunita Verma'"

## Test Generation Strategy

### Step 1: Analyze Integration Architecture and Document Elements
- Read document line by line completely
- Identify modules and their communication mechanisms
- Document API contracts, queues, gateways, cron jobs
- Extract data flow and transformation rules
- Identify ALL input/output fields, file formats, and validation rules
- List ALL fields with their specifications
- List ALL buttons, navigation points, status values
- List ALL business rules and conditions
- Create a comprehensive checklist of testable elements

### Step 2: Categorize Integration Scenarios
- Synchronous API → API flows
- Asynchronous events/messages
- DB-to-DB integration through ETL or replication
- External third-party integrations
- File-based batch integrations
- Multi-channel access patterns
- UI-to-backend flows
- Screen-to-screen navigation

### Step 3: Follow Document Order and Priority
1. Critical integration paths (in order mentioned in document)
2. Screen/page flow (in sequence)
3. Field validations (in order fields appear)
4. Interface contracts
5. Error handling between systems (only those mentioned in document)
6. Non-functional integration requirements (timeouts, retries - if specified)
7. Field-level validations for all input/output points
8. Status-based workflow transitions
9. Correction/reversal transaction flows

### Step 4: Generate Test Cases (ONE PER FUNCTIONALITY)
For each scenario:
- Create ONE test case per functionality/validation/field/button/navigation
- Include positive, negative (document-based), and exceptional (document-based) flows
- Include explicit payloads and expected downstream updates with realistic data
- Ensure precise step-by-step reproducible actions
- Include field-level validation tests for every specified field (separate tests)
- Include tests for all status values and workflow states (separate tests)
- Include file format validation tests for batch processes (separate tests)
- Include accounting entry verification for financial transactions
- Include multi-channel tests where applicable (separate per channel)
- Include correction/reversal scenarios for transactional features
- Follow the functional flow order from document
- Use realistic test data - no placeholders
- Generate error tests ONLY for scenarios mentioned or implied by document validations
- Ensure navigation tests are complete single test cases, not split

### Step 5: Quality Check Before Finalizing
**Mandatory Verification:**
□ Every field mentioned in document has separate test cases for each validation type
□ Every button has its own test case(s)
□ Every navigation has ONE complete test case
□ Every status value has appropriate test cases
□ Test cases follow document's functional flow order
□ No merged/complex test cases covering multiple functionalities
□ All test data is realistic and meaningful (no "Test User", no placeholders)
□ No error scenarios beyond what document specifies or validations imply
□ Negative test cases are relevant and document-driven
□ No test cases are duplicates or have the same meaning
□ All test cases are valid and relevant to the document
□ Test case numbering is sequential and consistent




## Quality Checklist (SIT)

□ All integration requirements covered
□ Positive, negative (document-based), exceptional (document-based) cases included
□ Payloads, DB states, API calls explicitly defined with realistic data
□ Steps reproducible with actual values
□ Expected results measurable with specific values
□ Test case names integration-focused and specific
□ Traceability included
□ JSON valid and complete
□ All input fields have separate tests for length/format/type validation
□ All file formats have structure validation tests
□ All status values have workflow tests
□ All transaction types have business logic tests
□ All correction features have reversal tests
□ All multi-channel features have channel-specific tests (separate per channel)
□ All accounting transactions have GLIFF entry verification tests
□ Test cases generated in document's functional flow order
□ One test case per functionality - no merged complex tests
□ Navigation tests are complete and not split into multiple tests
□ All test data is realistic - no "Test User" or placeholders
□ Error scenarios are document-driven only
□ Negative tests are relevant and validation-based
□ No invalid or irrelevant test cases
□ No duplicate test cases or semantically same test cases

- Output Must be valid JSON
- No markdown, no explanations
- Each object must represent ONE SIT test case for ONE functionality
- All fields fully populated with realistic values
- High quality > quantity
- Generate only valid, relevant, document-driven test cases

"""



def _build_page_prompt(
    page_text:     str,
    page_number:   int,
    document_name: str,
    rag_context:   str,
    user_prompt:   Optional[str],
    testcase_type: str,
    page_metadata: Optional[dict] = None,
    prompt_file_content: Optional[str] = None,
    selected_department_description: Optional[str] = None,
    department_id: Optional[str] = None,
    context_window: Optional[str] = None,
    already_covered: Optional[List[str]] = None,
    selected_checkboxes: Optional[List[str]] = None,   # NEW
) -> str:

    is_trade_finance = str(department_id or "").strip() == TRADE_FINANCE_DEPT_ID
    is_yono2 = str(department_id or "").strip() == YONO_2_DEPT_ID
    # ── Output schema block ───────────────────────────────────────────────────
    if is_trade_finance:
        schema_block = f"""<output_schema>
Each object MUST have ALL these fields exactly:
{{
  "Sr.No"                   : "Sequential number starting from 1",
  "Function ID"             : "Top-level function code e.g. TF_TRANSFER_01",
  "Function Description"    : "One-line description of the top-level function",
  "Sub Function ID"         : "Sub-function code e.g. TF_TRANSFER_01_SF01",
  "Sub Function Description": "Specific feature, field, or operation being tested",
  "Pre-Condition"           : "System state and data required before test runs",
  "Test Case ID"            : "TC_P{page_number}_001",
  "Test Case Description"   : "Full description: what is tested, components involved, verification objective",
  "Expected Result"         : "Exact system behaviour, message, data state after the action",
  "Priority"                : "High | Medium | Low",
  "Positive / Negative"     : "Positive | Negative | Exceptional",
  "Status"                  : "",
  "Remarks"                 : "",
  "Document Name"           : "{document_name}",
  "Page No"                 : "{page_number}"
}}
Status and Remarks: always leave empty — filled by tester after execution.
</output_schema>"""
    else:
        schema_block = f"""<output_schema>
Each object MUST have ALL these fields exactly:
{{
  "Test Case ID"   : "TC_P{page_number}_001",
  "Test Case Name" : "Verify [specific thing] [under specific condition]",
  "Scenario Name"  : "Module - Feature Name",
  "Type"           : "Positive | Negative | Exceptional",
  "Description"    : "What is being tested and why — one clear sentence",
  "Steps"          : "1. Step one\\n2. Step two\\n3. Step three"\\n4 Step four and so on till all steps are covered,
  "Test Data"      : "Specific realistic values — never placeholders",
  "Expected Result": "Precise measurable outcome with exact messages or codes",
  "Document Name"  : "{document_name}",
  "Page No"        : "{page_number}"
}}
If a field has no applicable value write N/A — never leave it empty.
</output_schema>"""

    # ── Structure-driven coverage instructions (KEY IMPROVEMENT) ──────────────
    structure_block = ""
    if page_metadata:
        st = page_metadata.get("section_type", "general_description")

        # Each section type gets SPECIFIC generation instructions, not just a label
        STRUCTURE_INSTRUCTIONS = {
            "field_specification_table": (
                "This page contains a FIELD SPECIFICATION TABLE.\n"
                "For EACH field row in the table, generate SEPARATE test cases:\n"
                "  • ONE positive: valid value at exact specified length/format\n"
                "  • ONE negative: value exceeding max length\n"
                "  • ONE negative: wrong data type (e.g. alpha in numeric field)\n"
                "  • ONE negative: empty/null if field is MANDATORY\n"
                "Do NOT combine multiple fields into one test case.\n"
                "Do NOT generate high-level workflow tests on this page."
            ),
            "transaction_specification": (
                "This page defines TRANSACTION CODES.\n"
                "For EACH transaction code, generate SEPARATE test cases — never combine two codes.\n"
                "Cover per code: valid execution, invalid parameters, boundary amounts,\n"
                "status transitions, and any home/non-home branch differences.\n"
                "Use exact transaction code numbers in every test case name."
            ),
            "screen_and_transaction_spec": (
                "This page contains SCREEN + TRANSACTION specifications.\n"
                "Generate test cases for each transaction code AND each screen element separately.\n"
                "Navigation between screens = ONE complete test case (not split).\n"
                "Field validation on screen = ONE test case per field per validation type."
            ),
            "screen_specification": (
                "This page describes a SCREEN or FORM.\n"
                "Test each UI element separately: each field, each button, each navigation link.\n"
                "Navigation test = ONE complete test case covering source screen → action → destination.\n"
                "Field test = ONE test case per validation type (length, format, mandatory)."
            ),
            "workflow_or_process_flow": (
                "This page describes a PROCESS FLOW or WORKFLOW.\n"
                "Generate test cases that cover:\n"
                "  • Happy path end-to-end (one test)\n"
                "  • Each decision branch/condition separately\n"
                "  • Failure at each step where the document implies it\n"
                "Maintain the documented sequence in test case steps.\n"
                "Number your steps to match the flow numbering in the document."
            ),
            "error_codes_and_messages": (
                "This page lists ERROR CODES and MESSAGES.\n"
                "For EACH error code, generate exactly ONE negative test case that triggers it.\n"
                "ALWAYS include the exact error message text (copied from document) in Expected Result.\n"
                "Do NOT paraphrase error messages — copy them verbatim."
            ),
            "field_validation_rules": (
                "This page defines FIELD VALIDATION RULES.\n"
                "One test case per rule per field:\n"
                "  • Length: test at exact limit, one below, one above\n"
                "  • Format: valid format test + invalid format test\n"
                "  • Mandatory: test empty/null submission\n"
                "  • Data type: test with correct type + wrong type"
            ),
            "status_or_mode_values": (
                "This page defines STATUS or MODE values.\n"
                "Generate ONE test case per status/mode combination:\n"
                "  • What operations are PERMITTED in this status/mode\n"
                "  • What operations are BLOCKED in this status/mode\n"
                "  • Status transition: verify correct next-state after operation"
            ),
            "tabular_data": (
                "This page contains TABULAR DATA.\n"
                "Test each row as a separate scenario.\n"
                "Test boundary values at column min/max where numeric ranges are specified.\n"
                "Test each combination of values that represents a distinct business rule."
            ),
            "general_description": (
                "This page contains GENERAL REQUIREMENTS.\n"
                "Extract every testable statement — each field, rule, constraint, and condition.\n"
                "Cover positive (valid/success), negative (invalid/error), and exceptional (boundary) scenarios.\n"
                "Follow the order requirements appear in the document."
            ),
        }

        instr = STRUCTURE_INSTRUCTIONS.get(st, STRUCTURE_INSTRUCTIONS["general_description"])
        hints = [instr]

        tx = page_metadata.get("transaction_codes", [])
        if tx:
            hints.append(
                f"Transaction codes on this page: {', '.join(tx)}\n"
                f"Generate SEPARATE positive + negative + exceptional tests for EACH code above."
            )

        sc = page_metadata.get("screen_numbers", [])
        if sc:
            hints.append(
                f"Screen numbers on this page: {', '.join(sc)}\n"
                f"Reference these exact screen numbers in test case steps and names."
            )

        coverage = []
        if page_metadata.get("has_field_list"):    coverage.append("field validations (length / type / mandatory) — ONE test per field per rule")
        if page_metadata.get("has_error_codes"):   coverage.append("error codes — ONE negative test per code with exact message in Expected Result")
        if page_metadata.get("has_workflow"):      coverage.append("workflow steps — follow exact sequence, test each decision branch")
        if page_metadata.get("has_validation"):    coverage.append("boundary values — min, max, min-1, max+1 for every numeric/date field")
        if page_metadata.get("has_status_values"): coverage.append("status values — ONE test per status transition")
        if page_metadata.get("has_mode_values"):   coverage.append("mode values — ONE test per mode (CASH / CLRG / RTGS / NEFT / IMPS etc.)")
        if coverage:
            hints.append("MANDATORY coverage for this page type:\n" + "\n".join(f"  • {c}" for c in coverage))

        structure_block = "<page_structure>\n" + "\n\n".join(hints) + "\n</page_structure>"

    # ── RAG context block (instructional, not generic) ─────────────────────────
    rag_block = ""
    if rag_context.strip():
        rag_block = (
            "<rag_context>\n"
            "Domain knowledge from the knowledge base — REFERENCE ONLY.\n\n"
            "USE this context ONLY to:\n"
            "  1. Fill in realistic test data values (account numbers, transaction codes, error messages)\n"
            "  2. Understand how similar fields are validated in SBI systems\n"
            "  3. Complete a requirement that appears cut mid-sentence on this page\n\n"
            "DO NOT generate test cases from this context.\n"
            "DO NOT reference fields, screens, or transactions that appear ONLY here and NOT in page_content.\n\n"
            f"{rag_context}\n"
            "</rag_context>"
        )

    # ── Context window block (prev/next page — read-only) ─────────────────────
    context_window_block = ""
    if context_window and context_window.strip():
        context_window_block = (
            "<context_window>\n"
            "READING CONTEXT ONLY \n"
            "This shows the tail of the previous page and head of the next page.\n"
            "Use it ONLY to understand continuations or to complete a sentence/table\n"
            "that starts or ends outside the page_content boundary.\n\n"
            f"{context_window}\n"
            "</context_window>"
        )

    # ── Already-covered scenarios block (prevents cross-page duplicates) ──────
    coverage_block = ""
    if already_covered:
        # Send last 25 scenario names to keep the block compact
        recent = already_covered[-25:]
        coverage_block = (
            "<already_generated>\n"
            "The following scenario areas were already covered in PREVIOUS pages.\n"
            "DO NOT generate new test cases for these scenarios — they are done.\n"
            "You MAY generate test cases for NEW aspects not listed below.\n\n"
            + "\n".join(f"  - {s}" for s in recent)
            + "\n</already_generated>"
        )

    # ── Department context block ──────────────────────────────────────────────
    dept_block = ""
    if selected_department_description:
        dept_block = (
            "<department_context>\n"
            f"{selected_department_description}\n"
            "</department_context>"
        )

    # ── Additional user instructions ──────────────────────────────────────────
    extra_block = ""
    if user_prompt:
        extra_block = (
            "<additional_instructions>\n"
            f"{user_prompt}\n"
            "</additional_instructions>"
        )

    # ── Checkbox-driven generation instructions ───────────────────────────────
    checkbox_block = ""
    if selected_checkboxes and len(selected_checkboxes) > 0:
        # Build a one-line summary of what the user wants
        selected_labels = [
            CHECKBOX_PROMPT_MAP[cb]["label"]
            for cb in selected_checkboxes
            if cb in CHECKBOX_PROMPT_MAP
        ]
        selected_prompts = [
            CHECKBOX_PROMPT_MAP[cb]["prompt"]
            for cb in selected_checkboxes
            if cb in CHECKBOX_PROMPT_MAP
        ]

        if selected_labels:
            summary_line = (
                f"The user has specifically requested test cases covering the following "
                f"{len(selected_labels)} area(s): "
                + ", ".join(f'"{l}"' for l in selected_labels)
                + ". Prioritise generating test cases for ALL of these areas from the "
                  "page content. Do not skip any of the requested areas."
            )
            checkbox_block = (
                "<user_selected_testcase_requirements>\n"
                f"{summary_line}\n\n"
                "DETAILED INSTRUCTIONS PER SELECTED AREA:\n\n"
                + "\n\n".join(f"▶ {p}" for p in selected_prompts)
                + "\n</user_selected_testcase_requirements>"
            )

    # ── Point 8: department-specific coverage instructions ────────────────────
    if is_yono2:
        point_8_block = """8. Generate all possible test cases for the below points if information is available in page_content. This document follows a User Story format — apply this structure strictly:
- User Story Description and Scope: Generate test cases validating the "AS A / I WANT TO / SO THAT" requirement is fully met, including the Customer Categories Applicable and Applicable Channel constraints.
- Process Diagram / Process Flow: Generate test cases that validate each step of the documented process flow occurs in the correct sequence, including all decision branches shown in the diagram.
- Screen Details — for EACH screen documented in the page (e.g. each "X.X <<Screen Name>>" section), generate SEPARATE test cases covering:
    • Entry Conditions: test the Pre-Condition is enforced before the screen loads, the Expected Result matches exactly what is documented, any Alternate Scenario is handled, and any Error Handling described occurs correctly.
    • Field Type, Native, Validations and Business Rules: for EVERY field/row in this table, generate ONE test case per field that verifies: the Field Type behaves correctly (Button/Text/Menu/Image/Card View etc.), the Field Validation rule is enforced exactly as documented, the Lookup Value & Source values are correctly populated/shown, and the Action described (navigation, what happens on click/entry) occurs exactly as documented — including any conditional branching (e.g. "if user has X" vs "if user has Y" leading to different screens).
    • Do NOT merge multiple fields from the same screen into one test case — each row in the Field Type/Validations/Business Rules table is its own test case.
    • Do NOT merge multiple screens into one test case — each screen section gets its own dedicated set of test cases.
- Navigation Thread: generate ONE complete navigation test case per documented navigation thread (source screen → screen → ... → destination), verifying every intermediate screen and the final destination.
- Non-Functional Requirements (NFR): generate test cases for any documented NFR with a non-NA Expected Outcome (e.g. Configuration requirements, Layout Supported).
- Integration — Front End and Integration – Back End: for EACH API/integration row documented, generate SEPARATE test cases verifying: correct invocation of the API/integration point (Method, Protocol, End Point if specified), correct handling of the API Brief Description behavior, and correct error handling for any Error Codes/Description listed.
- Generate separate test cases per each screen, per each field, and per each integration point — never combine multiple screens, fields, or integrations into a single test case."""
    else:
        point_8_block = """8. Generate all possible testcases for below points if information available in page_content : 
- Impact/Dependencies on Other Functionality/ known gaps for Which Application is getting impacted (Impact (Y/N) due to this change request. Please generate testcases carefully for impacted modules/ applications.  
- Business acceptance scenarios, Business Rules , Expected Functionality or Scope of Change, Effort Estimations,Proposed Solution,Switch Side Changes (Credit Leg), Switch Side Changes (Debit Leg)  
- Solution Details like where changes needs to be implemented and how
- Whether Changes in Parameters Table required or not so that we can understand Interface or Integration Issues due to Parameter Changes, Objective of the CR, Name of the CR, About the Demand, Out of Scope , Scope, Benefits / Influence / Impact, New Functionality  
- Existing Functionality with Process Flow , Technical Feasibility of the Proposed Solution, any Assumptions, Limitations, User Type or Capability Specifications, Maker Checker Specifications, Data Migration Applicability, Implementation Plan, Archival policy for new tables.
- Configurations , AS-IS Process / Existing Process, Wireframes / proposed screens ,To-Be Process / Proposed process / solution  only , Key Benefits , User roles ,Common Fields, specific fields, additional fields,Settlement, generic flow, accounting, Entry ,Debit,Credit,Actual Funds Transfer, Nature, When these happened
- Changes, additional changes , frontend alerts, error codes, implementation plan, Digital Signature and Encryption ,Technical Specifications,Programs Affected , Payment advice,Charge Recovery,
- Generate seperate testcases per each functionality"""

    return f"""<task>
Generate comprehensive {testcase_type} test cases for the page content below.
Document: {document_name}
Page: {page_number}
</task>

{structure_block}

{dept_block}

{rag_block}

{context_window_block}

{coverage_block}

{extra_block}

{checkbox_block}

<page_content>
IMPORTANT: Generate test cases ONLY from content inside this tag.
Do NOT generate test cases from rag_context, context_window, or already_generated sections.
{page_text}
</page_content>

<hard_rules>
1. Generate test cases ONLY for functional requirements explicitly present in page_content.
3. Return empty array [] if page_content contains ONLY:
   - Document metadata (version, author, dates)
   - Confidentiality notices or legal disclaimers
   - Table of contents or section headings only
   - Sign-off tables, revision history, change control tables
   - About this Document / Purpose / Intended Audience sections
   - List of abbreviations, cover pages, logos, watermarks
4. RAG context is reference only — never the primary source.
5. Context window is reading context only — never generate test cases from it.
6. Every output schema field must be populated — write N/A if not applicable.
7. No duplicate test cases — each must test a unique aspect.
8. {point_8_block}
9. ORDERING RULE — CRITICAL: Order test cases by FUNCTIONAL FLOW, not by test type.
   Correct order: [Login-Positive, Login-Negative, Login-Exceptional, Dashboard-Positive, Dashboard-Negative...]
   Wrong order:   [Login-Positive, Dashboard-Positive, Transfer-Positive, Login-Negative, Dashboard-Negative...]
   Follow the sequence in which features appear in the document, top to bottom.
   Within each feature: Positive first, then Negative, then Exceptional.
10. Test Case ID MUST follow format TC_P{page_number}_NNN exactly.
    Example for page 5: TC_P5_001, TC_P5_002, TC_P5_003
    NEVER use TC_001 format — the page number prefix is mandatory for flow ordering.

    {schema_block}

Return ONLY a valid JSON array starting with [ and ending with ]. No markdown, no explanation."""


# ══════════════════════════════════════════════════════════════════════════════
# JSON HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _clean_json(raw: str) -> str:
    raw = re.sub(r"^```(json)?", "", raw.strip(), flags=re.IGNORECASE).strip()
    raw = re.sub(r"```$", "", raw).strip()
    raw = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", raw)
    raw = re.sub(r",\s*([\]\}])", r"\1", raw)
    raw = re.sub(r"//.*?\n", "\n", raw)
    return re.sub(r"/\*.*?\*/", "", raw, flags=re.DOTALL).strip()


def _parse_json(raw: str) -> list:
    for attempt in range(1, 5):
        try:
            if attempt == 1:
                r = json.loads(raw)
            elif attempt == 2:
                r = json.loads(re.sub(r",\s*([\]\}])", r"\1", raw))
            elif attempt == 3:
                last = raw.rfind("}")
                if last == -1:
                    continue
                cand = raw[:last + 1].strip()
                cand = ("[" + cand + "]") if not cand.startswith("[") else (cand + "]")
                r    = json.loads(cand)
            else:
                objs = []
                for m in re.finditer(r"\{[^{}]+\}", raw, re.DOTALL):
                    try:
                        objs.append(json.loads(m.group()))
                    except Exception:
                        pass
                if not objs:
                    raise ValueError("No objects found")
                r = objs
            return r if isinstance(r, list) else [r]
        except Exception:
            pass
    raise ValueError("All JSON parsing strategies failed.")


# ══════════════════════════════════════════════════════════════════════════════
# FINISH_REASON=LENGTH RECOVERY
# ══════════════════════════════════════════════════════════════════════════════
def _recover_truncated_json(partial_raw: str) -> Tuple[list, str]:
    """
    Attempt to salvage test cases from a truncated LLM response.

    Strategy:
      1. Try to parse the partial JSON as-is (sometimes it's nearly complete).
      2. Strip the trailing incomplete object and parse the well-formed prefix.
      3. Return (recovered_testcases, leftover_context) where leftover_context
         is a string summary of what was being generated so the continuation
         call knows where to pick up.
    """
    cleaned = _clean_json(partial_raw)

    # Attempt 1: direct parse of partial
    try:
        result = _parse_json(cleaned)
        print(f"  🔧 Partial JSON parsed directly: {len(result)} test cases recovered")
        return result, ""
    except Exception:
        pass

    # Attempt 2: find the last complete object boundary
    last_complete = cleaned.rfind("},")
    if last_complete == -1:
        last_complete = cleaned.rfind("}")
    if last_complete > 0:
        candidate = cleaned[: last_complete + 1].strip()
        if not candidate.startswith("["):
            candidate = "[" + candidate + "]"
        else:
            candidate = candidate + "]"
        try:
            result = json.loads(re.sub(r",\s*([\]\}])", r"\1", candidate))
            if isinstance(result, list) and result:
                print(f"  🔧 Recovered {len(result)} test cases from truncated prefix")
                # Return the last test case name as leftover context so the
                # continuation knows roughly what was last generated
                last_tc_name = result[-1].get("Test Case Name", "") if result else ""
                return result, last_tc_name
        except Exception:
            pass

    # Attempt 3: regex extraction of individual objects
    objs = []
    for m in re.finditer(r"\{[^{}]+\}", cleaned, re.DOTALL):
        try:
            objs.append(json.loads(m.group()))
        except Exception:
            pass
    if objs:
        print(f"  🔧 Extracted {len(objs)} test cases via regex fallback")
        last_tc_name = objs[-1].get("Test Case Name", "") if objs else ""
        return objs, last_tc_name

    print("  ⚠ Could not recover any test cases from truncated response")
    return [], ""


def _continuation_call(
    system_msg:    str,
    original_user_msg: str,
    partial_assistant_msg: str,
    last_tc_name:  str,
    page_number:   int,
    max_retries:   int = 2,
) -> list:
    """
    When finish_reason == 'length', send a continuation request:
      • The full original conversation is resent
      • The partial assistant response is included as the assistant turn
      • A new user message asks the model to continue from where it stopped

    This is the standard multi-turn continuation pattern for truncated JSON.
    """
    continuation_hint = (
        f'The last test case you were writing was: "{last_tc_name}". '
        if last_tc_name else ""
    )
    continuation_user_msg = (
        f"Your previous response was cut off because you hit the token limit. "
        f"{continuation_hint}"
        f"Please continue generating the remaining test cases. "
        f"Return ONLY the REMAINING test cases as a valid JSON array (no repeated ones). "
        f"Start directly with '[' — no preamble, no markdown."
    )

    print(f"  🔄 Continuation call for page {page_number}…")

    for attempt in range(1, max_retries + 1):
        try:
            response = _az.chat.completions.create(
                model=_CHAT_MODEL,
                messages=[
                    {"role": "system",    "content": system_msg},
                    {"role": "user",      "content": original_user_msg},
                    {"role": "assistant", "content": partial_assistant_msg},
                    {"role": "user",      "content": continuation_user_msg},
                ],
                temperature=0.3,
                max_tokens=CONTINUATION_MAX_TOKENS,
            )
            raw    = (response.choices[0].message.content or "").strip()
            reason = response.choices[0].finish_reason
            print(f"  ✓ Continuation finish_reason={reason!r}  raw_len={len(raw)}")
            try:
                extra = _parse_json(_clean_json(raw))
                print(f"  ✓ Continuation yielded {len(extra)} additional test cases")
                return extra
            except Exception as je:
                print(f"  ⚠ Continuation JSON parse failed (attempt {attempt}): {je}")
        except Exception as e:
            print(f"  ✗ Continuation call error (attempt {attempt}): {e}")
            if attempt < max_retries:
                time.sleep(2)

    return []

# ══════════════════════════════════════════════════════════════════════════════
# PER-PAGE TESTCASE GENERATION (now with length recovery + reranking)
# ══════════════════════════════════════════════════════════════════════════════

def generate_testcases_for_page_rag(
    page_number:   int,
    page_text:     str,
    document_name: str,
    rag_chunks:    List[Dict],
    user_prompt:   Optional[str],
    testcase_type: str,
    page_metadata: Optional[dict] = None,
    prompt_file_content: Optional[str] = None,
    selected_department_description: Optional[str] = None,
    department_id: Optional[str] = None,
    context_window: Optional[str] = None,
    already_covered: Optional[List[str]] = None,
    selected_checkboxes: Optional[List[str]] = None,   # NEW
) -> Dict:
    """
    Generate test cases for a single page.

    Pipeline:
      1. Build system + user prompts.
      2. Call LLM with GENERATION_MAX_TOKENS budget.
      3a. finish_reason == 'stop'  → parse and return.
      3b. finish_reason == 'length' →
            i.  Recover partial test cases from truncated response.
            ii. Call continuation to get the rest.
            iii.Merge both batches.
      4. Tag all test cases with page number.
    """
    if not page_text.strip():
        return {"page_number": page_number, "testcases": [],
                "status": "skipped", "error": "No text content on page"}

    system_msg = _system_prompt_sit() if testcase_type == "SIT" else _system_prompt_uat()
    user_msg = _build_page_prompt(
        page_text                       = page_text.strip(),
        page_number                     = page_number,
        document_name                   = document_name,
        rag_context                     = _build_rag_context_block(rag_chunks),
        user_prompt                     = user_prompt,
        testcase_type                   = testcase_type,
        page_metadata                   = page_metadata,
        prompt_file_content             = prompt_file_content,
        selected_department_description = selected_department_description,
        department_id                   = department_id,
        context_window                  = context_window,
        already_covered                 = already_covered,
        selected_checkboxes             = selected_checkboxes,   # NEW
    )

    max_retries, retry_delay = 3, 2
    # print(".............USERMSG..................\n",user_msg,"\n....................................")
    for attempt in range(1, max_retries + 1):
        try:
            response      = _az.chat.completions.create(
                model       = _CHAT_MODEL,
                messages    = [
                    {"role": "system", "content": system_msg},
                    {"role": "user",   "content": user_msg},
                ],
                temperature = 0.2,
                max_tokens  = GENERATION_MAX_TOKENS,
            )
            choice        = response.choices[0]
            raw           = (choice.message.content or "").strip()
            finish_reason = choice.finish_reason

            # ── Normal completion ─────────────────────────────────────────────
            if finish_reason == "stop":
                print(f"\n  ✅ Page {page_number} | finish_reason=stop")
                testcases = _parse_json(_clean_json(raw))

            # ── Truncated response — recover + continue ────────────────────────
            elif finish_reason == "length":
                print(f"\n  ⚠️  Page {page_number} | finish_reason=length — response was truncated!")
                print(f"     Attempting partial recovery + continuation…")

                # Step i: recover whatever is in the partial response
                partial_tcs, last_tc_name = _recover_truncated_json(raw)
                print(f"     Partial recovery: {len(partial_tcs)} test cases")

                # Step ii: ask for the rest
                extra_tcs = _continuation_call(
                    system_msg            = system_msg,
                    original_user_msg     = user_msg,
                    partial_assistant_msg = raw,
                    last_tc_name          = last_tc_name,
                    page_number           = page_number,
                )

                # Step iii: merge
                testcases = partial_tcs + extra_tcs
                print(f"     Total after merge: {len(testcases)} test cases")

            else:
                # content_filter, null, etc.
                print(f"\n  ⚠️  Page {page_number} | finish_reason={finish_reason!r}")
                testcases = _parse_json(_clean_json(raw))

            # ── Tag page number + print summary ───────────────────────────────
            for tc in testcases:
                if isinstance(tc, dict):
                    tc["Page No"] = str(page_number)

            sep = "─" * 60
            print(f"  {sep}")
            print(f"  📋 Page {page_number}: {len(testcases)} test case(s) "
                  f"(finish_reason={finish_reason!r})")
            for i, tc in enumerate(testcases, 1):
                if not isinstance(tc, dict):
                    continue
                print(f"    {i:>3}. [{tc.get('Type',''):<11}] "
                      f"{tc.get('Test Case ID','')} | "
                      f"{tc.get('Test Case Name','')[:80]}")
            print(f"  {sep}\n")

            return {"page_number": page_number, "testcases": testcases,
                    "status": "success", "finish_reason": finish_reason}

        except Exception as e:
            print(f"  ✗ Page {page_number} attempt {attempt}/{max_retries}: {e}")
            if attempt < max_retries:
                time.sleep(retry_delay)

    return {"page_number": page_number, "testcases": [],
            "status": "failed", "error": "All retry attempts exhausted"}
