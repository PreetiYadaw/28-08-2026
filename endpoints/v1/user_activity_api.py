import logging
import json
from typing import Annotated, List, Optional
from enum import Enum

from fastapi import APIRouter, HTTPException, Depends, Query
from sqlmodel import Session, select
from datetime import datetime, timezone

from api.config.config import settings as SETTINGS
from api.config.database import SessionDep
from api.config.security import get_current_active_user, get_current_admin_user
from api.model import (
    User,
    UserActivity,
    UserActivityCreate,
    UserActivityUpdate,
    UserActivityResponse,
)

logger = logging.getLogger(__name__)

user_activity_router = APIRouter(
    prefix=f"/api/{SETTINGS.API_VERSION}/{SETTINGS.API_URL_PREFIX}",
    tags=["User Activity"],
)


class SortOrder(str, Enum):
    ascending = "ascending"
    descending = "descending"


@user_activity_router.get("/user-activities/{username}", include_in_schema=False)
async def get_user_activities(
    username: str,
    session: SessionDep,
    # ── Auth required ────────────────────────────────────────────────────────
    # A regular user may only fetch their own activity.
    # An admin may fetch any user's activity.
    # Missing/invalid token → 401 before the handler body is reached.
    current_user: Annotated[User, Depends(get_current_active_user)] = ...,
    sort: SortOrder = Query(
        default=SortOrder.descending,
        description="Sort order: 'ascending' or 'descending'",
    ),
    limit: int = Query(default=100, le=500, description="Max records to return"),
    skip: int = Query(default=0, ge=0, description="Records to skip (pagination)"),
):
    """
    Get all activities for a specific user.

    Access rules:
    - Regular users can only access their own activity (username must match token).
    - Admins can access any user's activity.

    Requires a valid session token (cookie or Authorization header).
    """
    # ── Authorization: regular users can only see their own data ─────────────
    if current_user.role != "admin" and current_user.username != username:
        raise HTTPException(
            status_code=403,
            detail="You can only access your own activity records.",
        )

    try:
        # Verify the requested user exists
        user = session.exec(select(User).where(User.username == username)).first()
        if not user:
            raise HTTPException(
                status_code=404,
                detail=f"User '{username}' not found",
            )

        # Build query with sorting
        statement = select(UserActivity).where(UserActivity.username == username)
        if sort == SortOrder.descending:
            statement = statement.order_by(UserActivity.created_at.desc())
        else:
            statement = statement.order_by(UserActivity.created_at.asc())

        statement = statement.offset(skip).limit(limit)
        activities = session.exec(statement).all()

        # Convert selected_page_indices from JSON string to list
        response_activities = []
        for activity in activities:
            activity_dict = activity.dict()
            activity_dict["selected_page_indices"] = json.loads(
                activity.selected_page_indices
            )
            response_activities.append(activity_dict)

        # Summary statistics (over ALL records, not just the current page)
        all_activities = session.exec(
            select(UserActivity).where(UserActivity.username == username)
        ).all()

        total_activities = len(all_activities)
        completed_activities = sum(1 for a in all_activities if a.generation_completed)
        pending_activities = total_activities - completed_activities

        total_pages = sum(a.total_pages_processed or 0 for a in all_activities)
        total_successful = sum(a.successful_generations or 0 for a in all_activities)
        total_failed = sum(a.failed_generations or 0 for a in all_activities)

        uat_count = sum(1 for a in all_activities if a.testcase_client == "UAT")
        sit_count = sum(1 for a in all_activities if a.testcase_client == "SIT")
        pdf_count = sum(1 for a in all_activities if a.file_type == "pdf")
        docx_count = sum(1 for a in all_activities if a.file_type == "docx")

        logger.info(
            f"User {current_user.username} fetched {len(response_activities)} "
            f"activities for '{username}' (sort={sort})"
        )

        return {
            "username": username,
            "user_info": {
                "first_name": user.first_name,
                "last_name": user.last_name,
                "email": user.email,
                "department_id": user.departmentid,
            },
            "summary_statistics": {
                "total_activities": total_activities,
                "completed_activities": completed_activities,
                "pending_activities": pending_activities,
                "total_pages_processed": total_pages,
                "total_successful_generations": total_successful,
                "total_failed_generations": total_failed,
                "uat_generations": uat_count,
                "sit_generations": sit_count,
                "pdf_uploads": pdf_count,
                "docx_uploads": docx_count,
            },
            "pagination": {
                "returned_count": len(response_activities),
                "skip": skip,
                "limit": limit,
                "sort_order": sort,
            },
            "activities": response_activities,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error fetching activities for '{username}'")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch activities: {str(e)}",
        )
