from typing import Annotated
from fastapi import APIRouter, HTTPException, Query 
from app.dependencies.db import SessionDep
from app.schemas.courses import CourseFilters, SearchResponse
from app.services import courses as course_service

router = APIRouter(prefix="/courses", tags=["courses"])


@router.get("", response_model=SearchResponse)
def search(filters: Annotated[CourseFilters, Query()], session: SessionDep):
    return course_service.search(session, filters)