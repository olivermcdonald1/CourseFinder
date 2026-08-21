"""
CourseFinder API.

    .venv/bin/uvicorn app.main:app --reload
    http://localhost:8100/docs

In production this process also serves the frontend, so the page and the API
share an origin and there is no CORS to configure at all. See SERVE_FRONTEND.
"""

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.ratelimit import RateLimit
from app.routes import courses, sections

app = FastAPI(
    title="CourseFinder",
    description="Course search over McGill's catalogue with live seat data.",
    version="0.1.0",
)

# Public endpoint with no auth, three Postgres queries per search, on a
# free-tier database. Added before CORS so a throttled request never even
# reaches routing.
app.add_middleware(RateLimit)

# Origins come from the environment because the deployed hostname is not known
# at authoring time, and hardcoding localhost meant the deployed page could not
# call its own API. Empty by default: when this process serves the frontend
# itself, requests are same-origin and CORS never enters the picture.
#
#   CORS_ORIGINS=https://coursefinder.fly.dev,http://localhost:5173
_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
if _origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
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


# ── frontend ─────────────────────────────────────────────────────────────────
# Mounted LAST, so every API route above wins the path match. A StaticFiles
# mount at "/" would otherwise swallow /courses and return 404 for the API.
#
# One process serving both means: one deploy, one URL, one TLS certificate, no
# CORS, and no separate static host to keep in sync with the API's shape.
FRONTEND = Path(os.getenv("FRONTEND_DIR", "frontend"))

if FRONTEND.is_dir() and (FRONTEND / "index.html").is_file():
    @app.get("/", include_in_schema=False)
    def index():
        # no-store: index.html is the thing that names every other asset, so a
        # cached copy is how a deploy half-applies for someone.
        return FileResponse(FRONTEND / "index.html",
                            headers={"Cache-Control": "no-store"})

    app.mount("/", StaticFiles(directory=FRONTEND), name="frontend")
