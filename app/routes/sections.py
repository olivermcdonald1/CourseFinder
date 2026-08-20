from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, status

from app.dependencies.db import SessionDep
from app.schemas.sections import MovementResponse
from app.search import DEFAULT_TERM
from app.services import sections as section_service

router = APIRouter(prefix="/sections", tags=["sections"])


@router.get("/{crn}/movement", response_model=MovementResponse)
def movement(session: SessionDep,
             crn: Annotated[str, Path(examples=["3784"])],
             term: Annotated[str, Query()] = DEFAULT_TERM):
    """
    Seat and waitlist movement for one section.

    `term` is required in practice even though it defaults: CRNs repeat across
    terms, so a CRN alone does not identify a section.
    """
    result = section_service.movement(session, crn, term)
    if result is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=f"No section with crn {crn!r} in term {term!r}")
    return result
