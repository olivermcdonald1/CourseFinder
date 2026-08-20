#!/bin/bash
#
# One sweep: fetch every scheduled course from VSB, load it into Postgres.
# This is the whole scheduled job -- there is nothing else to run.
#
#   collector/vsb_collect.py   ~100s   3,793 courses in 76 batched requests
#   load_observations.py         ~2s   UPSERT current_seats + seat_daily
#
# WHY A WRAPPER AND NOT TWO CRON LINES
#   cron runs with a near-empty environment: no PATH beyond /usr/bin:/bin, no
#   shell profile, and a working directory of $HOME. Every path here is
#   absolute for that reason, and the cd makes .env discovery behave the same
#   as when you run the scripts by hand.
#
# INSTALL
#   crontab -e
#   0 */2 * * * /Users/oliver/CourseFinder/scripts/collect.sh
#
#   See PLANNER.md Phase 0 for the macOS caveats (Full Disk Access, sleep).

set -uo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$PROJECT/.venv/bin/python"
LOG="$PROJECT/logs/collect.log"
LOCK="$PROJECT/.collect.lock"
MAX_LOG_BYTES=$((10 * 1024 * 1024))

cd "$PROJECT" || exit 1
mkdir -p "$(dirname "$LOG")"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

# A sweep is ~100s and the schedule is every 2h, so overlap should be
# impossible -- but a hung request shouldn't let two sweeps race on the raw
# directory. mkdir is atomic; macOS has no flock(1).
if ! mkdir "$LOCK" 2>/dev/null; then
    log "SKIP: previous sweep still running ($LOCK)"
    exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

# Keep the log from growing without bound; one truncation beats a logrotate
# dependency for a single file.
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt "$MAX_LOG_BYTES" ]; then
    tail -c $((MAX_LOG_BYTES / 2)) "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
    log "(log truncated)"
fi

log "=== sweep start ==="

# Postgres lives in Docker. If the Mac rebooted and Docker didn't come back,
# every sweep would fail silently for days -- so say so plainly.
if ! "$PYTHON" -c "
from sqlalchemy import text
from app.db import engine
with engine.connect() as c: c.execute(text('select 1'))
" >> "$LOG" 2>&1; then
    log "ABORT: Postgres unreachable -- try 'docker compose up -d db'"
    exit 1
fi

# The collector exits nonzero if ANY course fails. Across 3,793 courses a few
# failures are routine (a timeout, a course pulled since the catalogue
# snapshot). Partial data is still worth loading, so log and continue.
if ! "$PYTHON" collector/vsb_collect.py >> "$LOG" 2>&1; then
    log "WARN: collector reported failures, loading what it saved"
fi

if ! "$PYTHON" load_observations.py >> "$LOG" 2>&1; then
    log "ERROR: load failed"
    exit 1
fi

log "=== sweep done ==="
