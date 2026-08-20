"""
CourseFinder API.

    .venv/bin/uvicorn app.main:app --reload
    http://localhost:8000/docs
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routes import courses, sections

app = FastAPI(
    title="CourseFinder",
    description="Course search over McGill's catalogue with live seat data.",
    version="0.1.0",
)

# The Vite dev server is a different origin, so the browser blocks the request
# without this. In production the frontend is served from the same origin (or
# proxied), and this list should shrink accordingly.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

app.include_router(courses.router)
app.include_router(sections.router)


@app.get("/health", tags=["meta"])
def health():
    """Liveness check. Deliberately does not touch the database -- this answers
    'is the process up', which is a different question from 'can it serve'."""
    return {"status": "ok"}
