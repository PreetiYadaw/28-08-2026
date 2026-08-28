"""
api/config/security.py
Only changes vs original:
  • TokenData gains  testcase_client: Optional[str] = None
  • get_current_user extracts testcase_client from JWT payload and populates TokenData
  • User object returned will now have .testcase_client available via DB lookup
Everything else (rate-limiter, blacklist, password hashing, etc.) is unchanged.
"""
from datetime import datetime, timedelta, timezone
from typing import Annotated
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from fastapi.security.utils import get_authorization_scheme_param
from passlib.context import CryptContext
from jwt.exceptions import InvalidTokenError
import jwt
import logging
from sqlmodel import Session, select, delete
from dotenv import load_dotenv
load_dotenv()
import os

from api.model import User, TokenData, TokenBlacklist, LoginAttempt
from api.config.database import SessionDep

logger = logging.getLogger(__name__)

SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM  = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "30"))

_IS_LOCAL    = os.getenv("ENV", "production").lower() == "local"
COOKIE_SECURE  = not _IS_LOCAL
COOKIE_SAMESITE = "lax" if _IS_LOCAL else "strict"
ALLOWED_ALGORITHMS = ["HS256"]

oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl="api/v1/testcase-generation/token",
    auto_error=False,
)
COOKIE_NAME = "access_token"


async def _extract_token(request: Request) -> str | None:
    token = request.cookies.get(COOKIE_NAME)
    if token:
        return token
    auth_header = request.headers.get("Authorization", "")
    scheme, param = get_authorization_scheme_param(auth_header)
    if scheme.lower() == "bearer" and param:
        return param
    return None


pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
from passlib.hash import argon2

def get_password_hash(password: str) -> str:
    return argon2.hash(password)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return argon2.verify(plain_password, hashed_password)


# ── Rate limiter ───────────────────────────────────────────────────────────────
MAX_ATTEMPTS    = 5
WINDOW_SECONDS  = 300
LOCKOUT_SECONDS = 300

def _prune_old_attempts(identifier: str, session: Session) -> None:
    cutoff = datetime.utcnow() - timedelta(seconds=WINDOW_SECONDS)
    stmt = delete(LoginAttempt).where(
        LoginAttempt.identifier == identifier,
        LoginAttempt.attempted_at < cutoff,
        LoginAttempt.lockout_until.is_(None),
    )
    session.exec(stmt)
    session.commit()

def _as_utc(dt: datetime) -> datetime:
    if dt is None:
        return dt
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

def check_rate_limit(identifier: str, session: Session) -> None:
    now      = datetime.now(timezone.utc)
    now_naive = now.replace(tzinfo=None)
    lockout_row = session.exec(
        select(LoginAttempt).where(
            LoginAttempt.identifier == identifier,
            LoginAttempt.lockout_until > now_naive,
        )
    ).first()
    if lockout_row:
        remaining = max(0, int((_as_utc(lockout_row.lockout_until) - now).total_seconds()))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed login attempts. Try again in {remaining} seconds.",
            headers={"Retry-After": str(remaining)},
        )
    _prune_old_attempts(identifier, session)
    cutoff_naive = now_naive - timedelta(seconds=WINDOW_SECONDS)
    recent = len(session.exec(
        select(LoginAttempt).where(
            LoginAttempt.identifier == identifier,
            LoginAttempt.attempted_at >= cutoff_naive,
            LoginAttempt.lockout_until.is_(None),
        )
    ).all())
    if recent >= MAX_ATTEMPTS:
        session.exec(delete(LoginAttempt).where(
            LoginAttempt.identifier == identifier,
            LoginAttempt.lockout_until.is_(None),
        ))
        session.add(LoginAttempt(
            identifier=identifier,
            attempted_at=now_naive,
            lockout_until=now_naive + timedelta(seconds=LOCKOUT_SECONDS),
        ))
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed login attempts. Try again in {LOCKOUT_SECONDS // 60} minutes.",
            headers={"Retry-After": str(LOCKOUT_SECONDS)},
        )

def record_failed_attempt(identifier: str, session: Session) -> None:
    session.add(LoginAttempt(identifier=identifier))
    session.commit()

def clear_failed_attempts(identifier: str, session: Session) -> None:
    session.exec(delete(LoginAttempt).where(LoginAttempt.identifier == identifier))
    session.commit()


# ── Token blacklist ────────────────────────────────────────────────────────────
def _prune_expired_blacklist(session: Session) -> None:
    session.exec(delete(TokenBlacklist).where(
        TokenBlacklist.expires_at < datetime.now(timezone.utc)
    ))
    session.commit()

def blacklist_token(jti: str, username: str, expires_at: datetime, session: Session) -> None:
    _prune_expired_blacklist(session)
    if session.exec(select(TokenBlacklist).where(TokenBlacklist.jti == jti)).first():
        return
    session.add(TokenBlacklist(jti=jti, username=username, expires_at=expires_at))
    session.commit()
    logger.info(f"Token jti={jti} blacklisted for user={username}")

def is_token_blacklisted(jti: str, session: Session) -> bool:
    return session.exec(
        select(TokenBlacklist).where(TokenBlacklist.jti == jti)
    ).first() is not None


# ── Core auth ──────────────────────────────────────────────────────────────────
def get_user_by_username(username: str, session: Session) -> User | None:
    return session.exec(select(User).where(User.username == username)).first()

def authenticate_user(username: str, password: str, session: Session):
    user = get_user_by_username(username, session)
    if not user or not verify_password(password, user.password):
        return False
    return user

def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    to_encode = data.copy()
    expire = (
        datetime.now(timezone.utc) + expires_delta
        if expires_delta
        else datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode.update({"exp": expire, "iat": datetime.now(timezone.utc)})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALLOWED_ALGORITHMS[0])


async def get_current_user(request: Request, session: SessionDep) -> User:
    token = await _extract_token(request)
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not token:
        raise credentials_exception
    try:
        payload = jwt.decode(
            token, SECRET_KEY, algorithms=ALLOWED_ALGORITHMS,
            options={"verify_exp": True, "require": ["exp", "iat", "sub", "jti"]},
        )
    except InvalidTokenError:
        raise credentials_exception

    jti: str | None = payload.get("jti")
    if not jti or is_token_blacklisted(jti, session):
        raise credentials_exception

    username: str | None = payload.get("sub")
    if username is None:
        raise credentials_exception

    # ── Populate TokenData (testcase_client now included) ─────────────────
    token_data = TokenData(
        username=username,
        email=payload.get("email"),
        first_name=payload.get("first_name"),
        last_name=payload.get("last_name"),
        departmentid=payload.get("departmentid"),
        user_id=payload.get("user_id"),
        role=payload.get("role"),
        is_active=payload.get("is_active"),
        testcase_client=payload.get("testcase_client", "UAT"),   # NEW
        application_name=payload.get("application_name"),
    )

    user = get_user_by_username(token_data.username, session)
    if user is None:
        raise credentials_exception
    return user


async def get_current_active_user(
    current_user: Annotated[User, Depends(get_current_user)]
) -> User:
    if current_user.disabled or current_user.is_active == 0:
        raise HTTPException(status_code=400, detail="Inactive user")
    return current_user


async def get_current_admin_user(
    current_user: Annotated[User, Depends(get_current_active_user)]
) -> User:
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return current_user
