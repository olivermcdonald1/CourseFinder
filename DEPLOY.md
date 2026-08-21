# Deploying

One process serves the API and the frontend, so there is one URL, one TLS
certificate, and no CORS. Postgres is managed elsewhere. The daily seat sweep
runs in GitHub Actions and needs no server at all.

    Fly.io machine  (API + frontend, this repo's Dockerfile)
        |
        +-- Neon Postgres  (free tier, scale-to-zero)
                ^
                |
    GitHub Actions cron  (collector, 11:00 UTC daily)

Everything in this repo is ready. What follows needs accounts only you can
authenticate.

## 0. Install the two CLIs

    brew install flyctl gh

## 1. Postgres

Create a project at https://neon.tech (free tier: 0.5GB). Fly's own Managed
Postgres starts at $38/month, so it is the wrong tool here. Copy the **pooled**
connection string and change the scheme so SQLAlchemy uses psycopg 3:

    postgresql+psycopg://USER:PASS@HOST/DB?sslmode=require

The pooled endpoint matters: it is PgBouncer, which absorbs the connection
churn a scale-to-zero machine produces.

## 2. Seed it

`fetch_catalogue.py` does not exist, so the catalogue cannot be rebuilt from
scratch — restore the dump instead. `data/coursefinder.dump` (1.5MB, all 7
tables) was taken from the local database and is gitignored.

    docker run --rm -i postgres:16 pg_restore \
      --no-owner --no-privileges --dbname "postgresql://USER:PASS@HOST/DB?sslmode=require" \
      < data/coursefinder.dump

This carries the ratings backfill with it, which is the part that would be
painful to recreate.

## 3. Deploy

The app `mcgill-course-finder` already exists, so seeding, the secret and the
deploy are one script. Pass the connection string in the environment so the
password stays in your shell -- not in a file, not in the repo:

    DATABASE_URL='postgresql://USER:PASS@HOST/DB?sslmode=require' ./scripts/deploy.sh

It checks the database is reachable, restores the dump only if the database is
empty, stages the secret, deploys, and smoke-tests the four public paths. Safe
to re-run.

`release_command = "alembic upgrade head"` in fly.toml runs migrations before
traffic shifts, so a bad migration aborts the deploy rather than half-applying.

Note the two URL forms: SQLAlchemy needs `postgresql+psycopg://`, while
pg_restore and psql only understand `postgresql://`. The script derives both
from whichever you give it.

## 4. The daily sweep

Add the same connection string as a repository secret named `DATABASE_URL`
(Settings -> Secrets and variables -> Actions), then force one run to prove it
works end to end rather than waiting for 11:00 UTC:

    gh workflow run "collect seats"
    gh run watch

## 5. Link preview

Done: `frontend/og.png` is a 1200x630 dark-mode capture and the tag is absolute
at `https://mcgill-course-finder.fly.dev/og.png`. If the app is ever renamed,
that URL and `og:url` in `frontend/index.html` must change with it, or previews
in group chats go blank.

## Operational notes

- **Retention.** `seat_daily` grows ~2.6 MB/day once both terms sweep daily.
  The workflow prunes past 90 days, which settles it near 235 MB.
- **Rate limit.** 120 GET/min per IP, in-process (`RATE_LIMIT_PER_MIN`). It
  resets on deploy and does not coordinate across machines — scaling out means
  moving it to a shared store, not raising the number.
- **Be a good citizen.** VSB and mcgill.courses are student-run. Keep the sweep
  daily, keep the batch pauses, and do not add a second schedule.
- **Costs.** Neon free, Actions free on a public repo, Fly machine suspends when
  idle. Expect $0-3/month.
