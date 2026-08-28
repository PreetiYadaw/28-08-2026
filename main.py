"""
FastAPI Backend for PDF Processing and Test Case Generation
File: main.py
"""
import os
import multiprocessing
from pathlib import Path
from dotenv import load_dotenv

# Load .env relative to this file — works regardless of where you run from
_ENV_FILE = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=_ENV_FILE, override=True)

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from api.config.database import create_db_and_tables
from api.config.config import settings as SETTINGS

# ============================================================================
# ENVIRONMENT FLAGS — read AFTER load_dotenv()
# ============================================================================
# ENABLE_DOCS=true  → Swagger/ReDoc on (local dev only)
# ENABLE_DOCS=false → schema endpoints return 404 (production default)
_ENABLE_DOCS = os.getenv("ENABLE_DOCS", "false").lower() == "true"

# ENV=local  → relaxed cookie/CORS for HTTP localhost development
# ENV=production (default) → strict HTTPS-only rules
_ENV = os.getenv("ENV", "production").lower()
_IS_LOCAL = _ENV == "local"

FRONTEND_URL = os.getenv("FRONTEND_URL", "https://uat.qapariksha-ai.sbi.bank.in").rstrip("/")

if _IS_LOCAL:
    ALLOWED_ORIGINS = [
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
        FRONTEND_URL,
    ]
else:
    ALLOWED_ORIGINS = [FRONTEND_URL]


# ============================================================================
# MIDDLEWARE DEFINITIONS
# ============================================================================

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds security response headers to every response."""
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)

        # HSTS only on production HTTPS — not on local HTTP
        if not _IS_LOCAL:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains; preload"
            )

        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "font-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none';"
        )
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"

        # MutableHeaders has no .pop() — use del + try/except
        for header in ("Server", "X-Powered-By"):
            try:
                del response.headers[header]
            except KeyError:
                pass

        return response


# ============================================================================
# FASTAPI APP
# ============================================================================

app = FastAPI(
    title="RagBot",
    description="Testcase generation tool for Business requirements and Solution documents.",
    version="1.0.0",
    docs_url=f"/{SETTINGS.API_URL_PREFIX}/docs" if _ENABLE_DOCS else None,
    redoc_url=f"/{SETTINGS.API_URL_PREFIX}/redoc" if _ENABLE_DOCS else None,
    openapi_url=f"/{SETTINGS.API_URL_PREFIX}/openapi.json" if _ENABLE_DOCS else None,
    swagger_ui_parameters={"defaultModelsExpandDepth": -1},
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept"],
    expose_headers=["Content-Disposition"],
    max_age=600,
)


app.add_middleware(SecurityHeadersMiddleware)

import traceback as _traceback
from fastapi.responses import JSONResponse as _JSONResponse

@app.middleware("http")
async def catch_exceptions_middleware(request: Request, call_next):
    try:
        return await call_next(request)
    except Exception as exc:
        import logging as _logging
        _logging.getLogger("uvicorn.error").error(
            "Unhandled exception on %s %s\n%s",
            request.method, request.url, _traceback.format_exc()
        )
        return _JSONResponse(
            status_code=500,
            content={"detail": "Internal server error. Check server_crash.log."}
        )

# ============================================================================
# STARTUP
# ============================================================================

@app.on_event("startup")
def on_startup():
    create_db_and_tables()
    output_dir = Path("output_files")
    output_dir.mkdir(exist_ok=True)
    print(f"\u2713 Output directory ready: {output_dir}")
    print(f"\u2713 Environment : {_ENV}")
    print(f"\u2713 Docs enabled: {_ENABLE_DOCS}")
    print(f"\u2713 Allowed origins: {ALLOWED_ORIGINS}")


# ============================================================================
# REGISTER ROUTERS
# ============================================================================

from api.endpoints.v1.upload_api import upload_router
from api.endpoints.v1.generate_tests_api import testcase_router
from api.endpoints.v1.user_management_api import user_management_router
from api.endpoints.v1.user_activity_api import user_activity_router
from api.endpoints.v1.sso_auth_api import sso_router


app.include_router(upload_router)
app.include_router(testcase_router)
app.include_router(user_management_router)
app.include_router(user_activity_router)
app.include_router(sso_router)


# ============================================================================
# ROOT ENDPOINTS
# ============================================================================

@app.get("/")
async def root():
    return {
        "message": "PDF Processing and Test Case Generation API",
        "version": "1.0.0",
        "endpoints": {
            "upload": f"/api/{SETTINGS.API_VERSION}/{SETTINGS.API_URL_PREFIX}/upload-pdf-file",
            "generate_testcases": f"/api/{SETTINGS.API_VERSION}/{SETTINGS.API_URL_PREFIX}/generate-testcases",
            "user_activities": f"/api/{SETTINGS.API_VERSION}/{SETTINGS.API_URL_PREFIX}/activity/user/{{username}}"
        }
    }


@app.get("/health")
async def health_check():
    return {"status": "healthy"}


# ============================================================================
# ENTRY POINT
# ============================================================================

# if __name__ == "__main__":
#     import uvicorn

#     cpu_count = multiprocessing.cpu_count()
#     # optimal_workers = max(2, min(8, cpu_count // 2))
#     optimal_workers = 1
#     print(f"\n{'='*60}")
#     print(f"\U0001f680 Starting FastAPI Server")
#     print(f"{'='*60}")
#     print(f"CPU Cores : {cpu_count}")
#     print(f"Workers   : {optimal_workers}")
#     print(f"Port      : 1000")
#     print(f"Env       : {_ENV}")
#     print(f"Docs      : {_ENABLE_DOCS}")
#     print(f"{'='*60}\n")

#     # uvicorn.run("main:app", host="0.0.0.0", port=1000, workers=optimal_workers)
#     uvicorn.run(
#         "main:app",
#         host="0.0.0.0",
#         port=1000,
#         workers=optimal_workers,
#         timeout_keep_alive=900,    # 15 minutes — matches frontend timeout
#         timeout_graceful_shutdown=30,
#     )


if __name__ == "__main__":
    import uvicorn
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler("server_crash.log"),
            logging.StreamHandler(),
        ],
    )

    cpu_count = multiprocessing.cpu_count()
    optimal_workers = 1  # Must stay 1 — ChromaDB PersistentClient cannot be forked

    print(f"\n{'='*60}")
    print(f"\U0001f680 Starting FastAPI Server")
    print(f"{'='*60}")
    print(f"CPU Cores : {cpu_count}")
    print(f"Workers   : {optimal_workers}")
    print(f"Port      : 1000")
    print(f"Env       : {_ENV}")
    print(f"Docs      : {_ENABLE_DOCS}")
    print(f"{'='*60}\n")
    
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=1000,
        workers=optimal_workers,
        timeout_keep_alive=300,
        log_level="info",
    )
    # uvicorn.run(
    #     "main:app",
    #     host="0.0.0.0",
    #     port=1000,
    #     workers=optimal_workers,
    #     timeout_keep_alive=300,
    #     timeout_graceful_shutdown=60,
    #     log_level="info",
    # )
