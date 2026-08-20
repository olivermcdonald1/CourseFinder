from app.movement import get_movement
from app.schemas.sections import MovementResponse
from app.search import DEFAULT_TERM


def movement(session, crn, term=DEFAULT_TERM):
    """Seat history for one section, or None if the section doesn't exist."""
    data = get_movement(session, crn, term)
    if data is None:
        return None
    return MovementResponse.model_validate(data)
