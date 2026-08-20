"""
Per-request database session.

WHY A GENERATOR AND NOT A RETURN
  FastAPI advances this to the `yield`, hands the session to the route, runs the
  route, then advances it again -- which is when `finally` fires. That gives one
  function ownership of both setup and teardown, with the request sandwiched in
  between.

WHY try/finally SPECIFICALLY
  If the route raises (a 404, a bug), FastAPI throws that exception INTO the
  generator at the yield. Without `finally`, the close never runs and the
  connection leaks on exactly the requests that failed. Fifteen of those and the
  pool is empty; request sixteen blocks for 30s and then errors, nowhere near
  the code that caused it.

WHY NOT A MODULE-LEVEL SESSION
  A Session is a unit of work with one transaction and one identity map. Shared
  across concurrent requests, one request's rollback discards another's work.
  It looks fine with one user and corrupts under load.
"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session as SQLASession

from app.db import Session


def get_session():
    session = Session()
    try:
        yield session
    finally:
        # Returns the connection to the pool. Does not close the TCP socket.
        session.close()


# So routes read `session: SessionDep` instead of repeating the annotation.
SessionDep = Annotated[SQLASession, Depends(get_session)]
