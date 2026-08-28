import logging
import uuid as uuid_lib
from fastapi import APIRouter, HTTPException, status, Query, Depends, Request, Response
from fastapi.responses import RedirectResponse, JSONResponse
from sqlmodel import Session, select
from datetime import timedelta, datetime, timezone
from urllib.parse import quote
from api.config.config import settings
from api.config.database import SessionDep
from api.config.sso_security import sso_manager
from api.config.security import (
    create_access_token,
    get_password_hash,
    ACCESS_TOKEN_EXPIRE_MINUTES,
    blacklist_token,
    get_current_active_user,
    check_rate_limit,
    record_failed_attempt,
    clear_failed_attempts,
    SECRET_KEY,
    ALLOWED_ALGORITHMS,
    COOKIE_SECURE,
    COOKIE_SAMESITE,
)
from api.model import User, Token
from dotenv import load_dotenv
load_dotenv()
import os
import jwt

FRONTEND_URL = os.getenv("FRONTEND_URL", "https://uat.qapariksha-ai.sbi.bank.in")
COOKIE_NAME = "access_token"

logger = logging.getLogger(__name__)

sso_router = APIRouter(
    prefix=f"/api/{settings.API_VERSION}/{settings.API_URL_PREFIX}",
    tags=["SSO Authentication"],
)


# ============================================================================
# Cookie helper
# ============================================================================

def _set_auth_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        path="/",
    )


# ============================================================================
# Logout — DB-backed blacklist, shared across all workers
# ============================================================================

@sso_router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    session: SessionDep,
):
    """
    Invalidate the current session token.

    The JTI is written to the token_blacklist table (PostgreSQL) so every
    other Uvicorn worker will also reject the token immediately — no
    in-memory state is involved.
    """
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        from fastapi.security.utils import get_authorization_scheme_param
        auth_header = request.headers.get("Authorization", "")
        scheme, param = get_authorization_scheme_param(auth_header)
        if scheme.lower() == "bearer" and param:
            token = param

    if token:
        try:
            payload = jwt.decode(
                token,
                SECRET_KEY,
                algorithms=ALLOWED_ALGORITHMS,
                options={"verify_exp": False},  # blacklist even if already expired
            )
            jti = payload.get("jti")
            username = payload.get("sub", "unknown")
            exp = payload.get("exp")

            if jti:
                expires_at = (
                    datetime.fromtimestamp(exp, tz=timezone.utc)
                    if exp
                    else datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
                )
                blacklist_token(jti, username, expires_at, session)
                logger.info(f"SSO logout: token jti={jti} blacklisted for user={username}")
            else:
                logger.warning("SSO logout: token has no jti — cannot blacklist")

        except jwt.exceptions.DecodeError as e:
            logger.warning(f"SSO logout: malformed token — {e}")
        except Exception as e:
            logger.error(f"SSO logout: unexpected error — {e}")

    response.delete_cookie(key=COOKIE_NAME, path="/", httponly=True, secure=COOKIE_SECURE)
    return {"message": "Logged out successfully"}


# ============================================================================
# SSO Login — redirect to ADFS
# ============================================================================

@sso_router.get("/sso/login")
async def sso_login():
    """Initiate SSO login flow — redirects user to ADFS authorization endpoint."""
    try:
        auth_data = sso_manager.build_authorization_url()
        logger.info("SSO login initiated")
        return {
            "authorization_url": auth_data["authorization_url"],
            "state": auth_data["state"],
        }
    except Exception as e:
        logger.exception("SSO login error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"SSO login failed: {str(e)}",
        )


# ============================================================================
# SSO Callback — token delivered via HttpOnly cookie, NOT the URL
# ============================================================================

@sso_router.get("/sso/callback")
async def sso_callback(
    session: SessionDep,
    code: str = Query(..., description="Authorization code from ADFS"),
    state: str = Query(..., description="State parameter for CSRF protection"),
):
    """
    Handle SSO callback from ADFS.

    - Exchanges authorization code for tokens.
    - Looks up the user in the local DB by AD ID (UPN prefix).
    - Issues a local JWT and delivers it via a secure HttpOnly cookie.
    - The URL never contains the token — frontend detects sso_success=1 and
      calls /users/me to retrieve user info.
    """
    try:
        logger.info("SSO callback received")

        token_response = await sso_manager.exchange_code_for_token(code, state)

        id_token = token_response.get("id_token")
        if not id_token:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No id_token received from ADFS",
            )

        user_info = sso_manager.decode_id_token(id_token)

        upn = user_info.get("upn")
        if not upn:
            logger.error(f"No UPN in ID token. Keys: {list(user_info.keys())}")
            error_msg = "Unable to retrieve user information from SSO. Please contact administrator."
            return RedirectResponse(url=f"{FRONTEND_URL}/?sso_error={quote(error_msg)}")

        ad_id = upn.split("@")[0]
        logger.info(f"AD ID extracted: {ad_id}")

        existing_user = session.exec(
            select(User).where(User.username == ad_id.lower())
        ).first()

        if not existing_user:
            logger.warning(f"SSO user not found in DB: {ad_id}")
            error_msg = "User not registered. Please register yourself first to use SSO login."
            return RedirectResponse(url=f"{FRONTEND_URL}/?sso_error={quote(error_msg)}")

        user = existing_user
        logger.info(f"SSO login: {user.username}")

        jti = str(uuid_lib.uuid4())
        access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        jwt_token = create_access_token(
            data={
                "sub": user.username,
                "email": user.email,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "departmentid": user.departmentid,
                "user_id": user.id,
                "role": user.role,
                "is_active": user.is_active,
                "jti": jti,
            },
            expires_delta=access_token_expires,
        )

        redirect_response = RedirectResponse(url=f"{FRONTEND_URL}/?sso_success=1")
        _set_auth_cookie(redirect_response, jwt_token)
        logger.info(f"SSO login OK for {user.username}; token set as HttpOnly cookie")
        return redirect_response

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("SSO callback error")
        error_msg = f"SSO authentication failed: {str(e)}"
        return RedirectResponse(url=f"{FRONTEND_URL}/?sso_error={quote(error_msg)}")


@sso_router.get("/sso/user-info")
async def get_sso_user_info(access_token: str = Query(...)):
    """Get user information from ADFS (for testing purposes)."""
    try:
        user_info = await sso_manager.get_user_info(access_token)
        return user_info
    except Exception as e:
        logger.exception("Failed to get user info")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to get user info: {str(e)}",
        )
