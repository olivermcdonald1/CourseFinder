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

Create a project at https://neon.tech (free tier: 0.5GB). Copy the **pooled**
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

    fly auth login
    fly launch --no-deploy --copy-config --name mcgill-course-finder
    fly secrets set DATABASE_URL='postgresql+psycopg://...'
    fly deploy

`release_command = "alembic upgrade head"` in fly.toml runs migrations before
traffic shifts, so a bad migration aborts the deploy rather than half-applying.

## 4. The daily sweep

Add the same connection string as a repository secret named `DATABASE_URL`
(Settings -> Secrets and variables -> Actions), then force one run to prove it
works end to end rather than waiting for 11:00 UTC:

    gh workflow run "collect seats"
    gh run watch

## 5. Finish the link preview

`og:image` points at `/og.png`, which does not exist yet — link previews render
blank until it does, and group chats are the distribution channel. Drop a
1200x630 screenshot at `frontend/og.png` and make the tag absolute:

    <meta property="og:image" content="https://mcgill-course-finder.fly.dev/og.png">

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
