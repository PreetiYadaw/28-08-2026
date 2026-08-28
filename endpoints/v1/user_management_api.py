"""
api/endpoints/v1/user_management_api.py
Changes vs original:
  • register: accepts testcase_client in UserCreate payload
  • login: includes testcase_client in JWT claims
  • create_user_by_admin: accepts testcase_client
  • update_user_by_admin: can update testcase_client
  • change_password: new JWT also carries testcase_client
"""
import logging
import uuid as uuid_lib
from datetime import timedelta, datetime, timezone
from typing import Annotated
from dotenv import load_dotenv
load_dotenv()
import os

ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "30"))

from fastapi import APIRouter, HTTPException, Depends, Request, Response, status
from sqlmodel import Session, select
import jwt

from api.config.config import settings as SETTINGS
from api.config.database import SessionDep, create_db_and_tables
from api.config.security import (
    get_password_hash, authenticate_user, create_access_token,
    get_current_active_user, get_current_admin_user,
    check_rate_limit, record_failed_attempt, clear_failed_attempts,
    blacklist_token, SECRET_KEY, ALLOWED_ALGORITHMS,
)
from api.model import (
    User, UserCreate, UserResponse, Token, LoginRequest,
    UserCreateByAdmin, UserUpdateByAdmin, PasswordUpdateByAdmin, ChangePasswordRequest,
)

logger     = logging.getLogger(__name__)
COOKIE_NAME = "access_token"

user_management_router = APIRouter(
    prefix=f"/api/{SETTINGS.API_VERSION}/{SETTINGS.API_URL_PREFIX}",
    tags=["User Management"],
)
create_db_and_tables()


def _set_auth_cookie(response: Response, token: str) -> None:
    from api.config.security import COOKIE_SECURE, COOKIE_SAMESITE
    response.set_cookie(
        key=COOKIE_NAME, value=token, httponly=True,
        secure=COOKIE_SECURE, samesite=COOKIE_SAMESITE,
        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60, path="/",
    )


def _user_jwt_payload(user: User, jti: str) -> dict:
    """Build the standard JWT claims dict for a user."""
    return {
        "sub"             : user.username,
        "email"           : user.email,
        "first_name"      : user.first_name,
        "last_name"       : user.last_name,
        "departmentid"    : user.departmentid,
        "user_id"         : user.id,
        "role"            : user.role,
        "is_active"       : user.is_active,
        "must_change_password": getattr(user, "must_change_password", False),
        "login_count"     : getattr(user, "login_count", 0),
        "testcase_client" : getattr(user, "testcase_client", "UAT"),   # NEW
        "application_name" : getattr(user, "application_name", None),   # NEW
        "jti"             : jti,
    }


# ── Register ───────────────────────────────────────────────────────────────────
@user_management_router.post("/register", response_model=UserResponse)
async def register(user_data: UserCreate, session: SessionDep):
    try:
        if session.exec(select(User).where(User.username == user_data.username)).first():
            raise HTTPException(400, "Username already taken.")
        if session.exec(select(User).where(User.email == user_data.email)).first():
            raise HTTPException(400, "Email already exists.")

        db_user = User(
            first_name=user_data.first_name,
            last_name=user_data.last_name,
            username=user_data.username.lower(),
            email=user_data.email,
            password=get_password_hash(user_data.password),
            departmentid=user_data.departmentid,
            role="user",
            is_active=1,
            disabled=False,
            testcase_client=getattr(user_data, "testcase_client", "UAT"),  # NEW
        )
        session.add(db_user)
        session.commit()
        session.refresh(db_user)
        return db_user
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(500, f"Registration error: {e}")


# ── Login ──────────────────────────────────────────────────────────────────────
@user_management_router.post("/login", response_model=Token)
async def login(login_data: LoginRequest, request: Request, response: Response, session: SessionDep):
    client_ip    = request.client.host if request.client else "unknown"
    username_key = f"username:{login_data.username.lower()}"
    ip_key       = f"ip:{client_ip}"

    try:
        check_rate_limit(username_key, session)
        check_rate_limit(ip_key, session)

        user = authenticate_user(login_data.username, login_data.password, session)
        if not user:
            record_failed_attempt(username_key, session)
            record_failed_attempt(ip_key, session)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect username or password",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if user.disabled or user.is_active == 0:
            raise HTTPException(400, "User account is disabled")

        clear_failed_attempts(username_key, session)
        clear_failed_attempts(ip_key, session)

        user.login_count = (user.login_count or 0) + 1
        session.add(user)
        session.commit()
        session.refresh(user)

        jti          = str(uuid_lib.uuid4())
        access_token = create_access_token(
            data=_user_jwt_payload(user, jti),
            expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
        )
        _set_auth_cookie(response, access_token)
        logger.info(f"Login OK: {user.username} — role={user.role} client={user.testcase_client}")
        return Token(access_token=access_token, token_type="bearer")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Login failed: {e}")


# ── Login status ───────────────────────────────────────────────────────────────
@user_management_router.get("/login-status")
async def login_status(request: Request, session: SessionDep):
    from api.model import LoginAttempt
    client_ip  = request.client.host if request.client else "unknown"
    ip_key     = f"ip:{client_ip}"
    now_naive  = datetime.utcnow()
    lockout    = session.exec(
        select(LoginAttempt).where(
            LoginAttempt.identifier == ip_key,
            LoginAttempt.lockout_until > now_naive,
        )
    ).first()
    if lockout:
        lu = lockout.lockout_until
        if lu.tzinfo: lu = lu.replace(tzinfo=None)
        return {"locked": True, "retry_after_seconds": max(0, int((lu - now_naive).total_seconds()))}
    return {"locked": False, "retry_after_seconds": 0}


# ── Logout ─────────────────────────────────────────────────────────────────────
@user_management_router.post("/logout")
async def logout(request: Request, response: Response, session: SessionDep):
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        from fastapi.security.utils import get_authorization_scheme_param
        scheme, param = get_authorization_scheme_param(request.headers.get("Authorization", ""))
        if scheme.lower() == "bearer" and param:
            token = param
    if token:
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=ALLOWED_ALGORITHMS,
                                 options={"verify_exp": False})
            jti = payload.get("jti")
            if jti:
                exp = payload.get("exp")
                expires_at = (
                    datetime.fromtimestamp(exp, tz=timezone.utc) if exp
                    else datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
                )
                blacklist_token(jti, payload.get("sub", "unknown"), expires_at, session)
        except Exception as e:
            logger.warning(f"Logout token error: {e}")
    from api.config.security import COOKIE_SECURE
    response.delete_cookie(key=COOKIE_NAME, path="/", httponly=True, secure=COOKIE_SECURE)
    return {"message": "Logged out successfully"}


# ── Current user ───────────────────────────────────────────────────────────────
@user_management_router.get("/users/me", response_model=UserResponse)
async def get_current_user_info(current_user: Annotated[User, Depends(get_current_active_user)]):
    return current_user


# ── Change password ────────────────────────────────────────────────────────────
@user_management_router.post("/users/change-password")
async def change_password(
    password_data: ChangePasswordRequest,
    request: Request, response: Response,
    session: SessionDep,
    current_user: Annotated[User, Depends(get_current_active_user)],
):
    try:
        current_user.password           = get_password_hash(password_data.new_password)
        current_user.must_change_password = False
        current_user.updated_at         = datetime.now(timezone.utc)
        session.add(current_user)
        session.commit()

        # Blacklist old token
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            from fastapi.security.utils import get_authorization_scheme_param
            scheme, param = get_authorization_scheme_param(request.headers.get("Authorization", ""))
            if scheme.lower() == "bearer" and param:
                token = param
        if token:
            try:
                p = jwt.decode(token, SECRET_KEY, algorithms=ALLOWED_ALGORITHMS, options={"verify_exp": False})
                jti = p.get("jti")
                if jti:
                    exp = p.get("exp")
                    blacklist_token(jti, current_user.username,
                                    datetime.fromtimestamp(exp, tz=timezone.utc) if exp
                                    else datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
                                    session)
            except Exception:
                pass

        # Issue new token
        jti_new   = str(uuid_lib.uuid4())
        new_token = create_access_token(
            data=_user_jwt_payload(current_user, jti_new),
            expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
        )
        _set_auth_cookie(response, new_token)
        return {"message": "Password changed successfully."}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(500, str(e))


# ── Admin endpoints ────────────────────────────────────────────────────────────
@user_management_router.post("/admin/users", response_model=UserResponse, include_in_schema=False)
async def create_user_by_admin(
    user_data: UserCreateByAdmin,
    session: SessionDep,
    admin_user: Annotated[User, Depends(get_current_admin_user)],
):
    try:
        if user_data.role not in ["user", "admin"]:
            raise HTTPException(400, "Invalid role")
        if user_data.is_active not in [0, 1]:
            raise HTTPException(400, "Invalid is_active")
        if user_data.testcase_client not in ["UAT", "SIT"]:
            raise HTTPException(400, "testcase_client must be 'UAT' or 'SIT'")
        if session.exec(select(User).where(User.username == user_data.username)).first():
            raise HTTPException(400, "Username already exists")
        if session.exec(select(User).where(User.email == user_data.email)).first():
            raise HTTPException(400, "Email already exists")

        db_user = User(
            first_name=user_data.first_name, last_name=user_data.last_name,
            username=user_data.username, email=user_data.email,
            password=get_password_hash(user_data.password),
            departmentid=user_data.departmentid,
            role=user_data.role, is_active=user_data.is_active,
            disabled=False if user_data.is_active == 1 else True,
            must_change_password=True, login_count=0,
            testcase_client=user_data.testcase_client,   # NEW
            application_name= user_data.application_name or None,
        )
        session.add(db_user)
        session.commit()
        session.refresh(db_user)
        logger.info(f"Admin {admin_user.username} created user {user_data.username} client={user_data.testcase_client}")
        return db_user
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(500, str(e))


@user_management_router.get("/admin/users/{username}", response_model=UserResponse, include_in_schema=False)
async def get_user_by_admin(username: str, session: SessionDep,
                             admin_user: Annotated[User, Depends(get_current_admin_user)]):
    user = session.exec(select(User).where(User.username == username)).first()
    if not user:
        raise HTTPException(404, "User not found")
    return user


@user_management_router.patch("/admin/users/{username}", response_model=UserResponse, include_in_schema=False)
async def update_user_by_admin(
    username: str, user_data: UserUpdateByAdmin,
    session: SessionDep,
    admin_user: Annotated[User, Depends(get_current_admin_user)],
):
    try:
        user = session.exec(select(User).where(User.username == username)).first()
        if not user:
            raise HTTPException(404, "User not found")
        if user_data.first_name  is not None: user.first_name  = user_data.first_name
        if user_data.last_name   is not None: user.last_name   = user_data.last_name
        if user_data.email       is not None:
            if session.exec(select(User).where(User.email == user_data.email, User.id != user.id)).first():
                raise HTTPException(400, "Email already in use")
            user.email = user_data.email
        if user_data.departmentid is not None: user.departmentid = user_data.departmentid
        if user_data.role        is not None:
            if user_data.role not in ["user", "admin"]: raise HTTPException(400, "Invalid role")
            user.role = user_data.role
        if user_data.is_active   is not None:
            if user_data.is_active not in [0, 1]: raise HTTPException(400, "Invalid is_active")
            user.is_active = user_data.is_active
            user.disabled  = False if user_data.is_active == 1 else True
        if user_data.testcase_client is not None:  # NEW
            if user_data.testcase_client not in ["UAT", "SIT"]: raise HTTPException(400, "Must be UAT or SIT")
            user.testcase_client = user_data.testcase_client
        if user_data.application_name is not None:
            user.application_name = user_data.application_name or None

        user.updated_at = datetime.now(timezone.utc)
        session.add(user)
        session.commit()
        session.refresh(user)
        return user
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(500, str(e))


@user_management_router.patch("/admin/users/{username}/password", include_in_schema=False)
async def update_user_password_by_admin(
    username: str, password_data: PasswordUpdateByAdmin,
    session: SessionDep,
    admin_user: Annotated[User, Depends(get_current_admin_user)],
):
    try:
        user = session.exec(select(User).where(User.username == username)).first()
        if not user:
            raise HTTPException(404, "User not found")
        user.password           = get_password_hash(password_data.new_password)
        user.must_change_password = True
        user.login_count        = 0
        user.updated_at         = datetime.now(timezone.utc)
        session.add(user)
        session.commit()
        return {"message": "Password updated", "username": username}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(500, str(e))


@user_management_router.delete("/admin/users/{username}", include_in_schema=False)
async def delete_user_by_admin(
    username: str, session: SessionDep,
    admin_user: Annotated[User, Depends(get_current_admin_user)],
):
    try:
        if username == admin_user.username:
            raise HTTPException(400, "Cannot delete your own account")
        user = session.exec(select(User).where(User.username == username)).first()
        if not user:
            raise HTTPException(404, "User not found")
        session.delete(user)
        session.commit()
        return {"message": "User deleted", "username": username}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(500, str(e))


@user_management_router.get("/admin/users-list", include_in_schema=False)
async def list_all_users_by_admin(
    session: SessionDep,
    admin_user: Annotated[User, Depends(get_current_admin_user)],
):
    users = session.exec(select(User).order_by(User.created_at.desc())).all()
    return {
        "total_users": len(users),
        "users": [
            {
                "id": u.id, "first_name": u.first_name, "last_name": u.last_name,
                "username": u.username, "email": u.email, "departmentid": u.departmentid,
                "role": u.role, "is_active": u.is_active, "disabled": u.disabled,
                "testcase_client": u.testcase_client,   # NEW
                "application_name" : u.application_name or "",
                "created_at": u.created_at, "updated_at": u.updated_at,
            }
            for u in users
        ],
    }
