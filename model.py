from pydantic import BaseModel
from typing import List, Optional, Any, Dict
from sqlmodel import SQLModel, Field
from datetime import datetime, timezone
from pydantic import BaseModel

# ============================================================================
# TESTCASE REQUEST — simplified.
# prompt_file_content and reference_document_content removed.
# testcase_client moved to User model / JWT — no longer a per-request param.
# ============================================================================
class TestCaseRequest(BaseModel):
    uuid: str
    document_name: str
    user_prompt: Optional[str] = None
    selected_department: Optional[str] = None
    rag_doc_ids: Optional[List[str]] = None
    selected_checkboxes: Optional[List[str]] = None   # NEW
# ============================================================================
# USER MODELS
# ============================================================================
class UserBase(SQLModel):
    first_name: str = Field(index=True)
    last_name: str = Field(index=True)
    username: str = Field(unique=True, index=True)
    email: str = Field(unique=True, index=True)
    departmentid: Optional[str] = None
    role: str = Field(default="user", index=True)
    is_active: int = Field(default=1, index=True)
    testcase_client: str = Field(default="UAT", index=True)
    application_name: Optional[str] = Field(default=None, index=True)  # NEW


class User(UserBase, table=True):
    __tablename__ = "users"
    id: Optional[int] = Field(default=None, primary_key=True)
    password: str
    disabled: bool = Field(default=False)
    must_change_password: bool = Field(default=False)
    login_count: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class UserCreate(UserBase):
    password: str


class UserResponse(UserBase):
    id: int
    disabled: bool
    must_change_password: bool
    login_count: int
    created_at: datetime


# ============================================================================
# AUTH MODELS
# ============================================================================
class Token(BaseModel):
    access_token: str
    token_type: str


class TokenData(BaseModel):
    username: Optional[str] = None
    email: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    departmentid: Optional[str] = None
    user_id: Optional[int] = None
    role: Optional[str] = None
    is_active: Optional[int] = None
    testcase_client: Optional[str] = None   # NEW — carried in JWT
    application_name:Optional[str]=None


class LoginRequest(BaseModel):
    username: str
    password: str


# ============================================================================
# ADMIN MODELS
# ============================================================================
class UserCreateByAdmin(BaseModel):
    first_name: str
    last_name: str
    username: str
    email: str
    password: str
    departmentid: Optional[str] = None
    role: str = "user"
    is_active: int = 1
    testcase_client: str = "UAT"
    application_name: Optional[str] = None   # NEW — optional


class UserUpdateByAdmin(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    departmentid: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[int] = None
    testcase_client: Optional[str] = None
    application_name: Optional[str] = None   # NEW — optional


class PasswordUpdateByAdmin(BaseModel):
    new_password: str


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


class ChangePasswordRequest(BaseModel):
    new_password: str


# ============================================================================
# TOKEN BLACKLIST
# ============================================================================
class TokenBlacklist(SQLModel, table=True):
    __tablename__ = "token_blacklist"
    id: Optional[int] = Field(default=None, primary_key=True)
    jti: str = Field(unique=True, index=True, max_length=64)
    username: str = Field(index=True, max_length=150)
    expires_at: datetime = Field(index=True)
    blacklisted_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


# ============================================================================
# LOGIN ATTEMPTS
# ============================================================================
class LoginAttempt(SQLModel, table=True):
    __tablename__ = "login_attempts"
    id: Optional[int] = Field(default=None, primary_key=True)
    identifier: str = Field(index=True, max_length=256)
    attempted_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc), index=True
    )
    lockout_until: Optional[datetime] = Field(default=None)


# ============================================================================
# USER ACTIVITY
# ============================================================================
class UserActivity(SQLModel, table=True):
    __tablename__ = "user_activities"

    id: Optional[int] = Field(default=None, primary_key=True)
    uuid: str = Field(index=True, unique=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    username: str = Field(index=True)

    logged_in_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    document_name: str
    file_type: str
    total_pages: Optional[int] = None
    uploaded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    demand_id: Optional[str] = Field(default=None, max_length=50)
    project_id: Optional[str] = Field(default=None, max_length=50)

    selected_page_indices: str = Field(default="[]")
    testcase_client: str = Field(default="UAT")

    user_prompt_provided: bool = Field(default=False)
    user_prompt_text: Optional[str] = None

    generation_completed: bool = Field(default=False)
    generation_completed_at: Optional[datetime] = None
    output_file_path: Optional[str] = None

    total_pages_processed: Optional[int] = None
    successful_generations: Optional[int] = None
    failed_generations: Optional[int] = None

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class UserActivityCreate(BaseModel):
    uuid: str
    user_id: int
    username: str
    document_name: str
    file_type: str
    total_pages: Optional[int] = None
    selected_page_indices: List[int]
    testcase_client: str = "UAT"
    user_prompt_provided: bool = False
    user_prompt_text: Optional[str] = None


class UserActivityResponse(BaseModel):
    id: int
    uuid: str
    username: str
    logged_in_at: datetime
    document_name: str
    file_type: str
    total_pages: Optional[int]
    uploaded_at: datetime
    selected_page_indices: List[int]
    testcase_client: str
    user_prompt_provided: bool
    user_prompt_text: Optional[str]
    generation_completed: bool
    generation_completed_at: Optional[datetime]
    output_file_path: Optional[str]
    total_pages_processed: Optional[int]
    successful_generations: Optional[int]
    failed_generations: Optional[int]
    created_at: datetime

    class Config:
        from_attributes = True


class UserActivityUpdate(BaseModel):
    generation_completed: bool
    output_file_path: str
    total_pages_processed: int
    successful_generations: int
    failed_generations: int

# ============================================================================
# API testcase generation models
# ============================================================================

class APIFieldSpec(BaseModel):
    value: Any
    required: bool = True
    validation: Optional[str] = None


class APITestCaseSpec(BaseModel):
    api_name: str
    api_url: str
    method: str = "POST"
    payload: Dict[str, APIFieldSpec]

    class Config:
        extra = "ignore"

class RunApiTestcasesRequest(BaseModel):
    testcases: List[Dict[str, Any]]
    api_url: str
    generated_reference_number: Optional[str] = None   # NEW — fallback RRN when not in payload


    
class APITestCaseGenerateRequest(BaseModel):
    api_spec: APITestCaseSpec
    user_prompt: Optional[str] = None



class EvaluateApiTestcasesRequest(BaseModel):
    testcases: List[Dict[str, Any]]
