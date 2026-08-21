#!/usr/bin/env bash
# Seed the database, set the secret, deploy, smoke-test.
#
# Run it with the Neon connection string in the environment, so the password
# lives in your shell and nowhere else -- not in a file, not in the repo, not
# in a chat log:
#
#   DATABASE_URL='postgresql://user:pass@host/db?sslmode=require' ./scripts/deploy.sh
#
# Safe to re-run. The restore step is skipped once the database has rows.

set -euo pipefail

APP="mcgill-course-finder"
DUMP="data/coursefinder.dump"

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "DATABASE_URL is not set. Get the POOLED connection string from your"
  echo "Neon project (Dashboard -> Connect) and run:"
  echo
  echo "  DATABASE_URL='postgresql://...' ./scripts/deploy.sh"
  exit 1
fi

# SQLAlchemy needs the +psycopg driver in the scheme; libpq tools (pg_restore,
# psql) do not understand it and fail on the URL. So we keep two forms of the
# same string and never mix them up.
LIBPQ_URL="${DATABASE_URL/postgresql+psycopg:/postgresql:}"
SQLA_URL="${DATABASE_URL/postgresql:/postgresql+psycopg:}"

case "$LIBPQ_URL" in
  postgresql://*) ;;
  *) echo "DATABASE_URL must start with postgresql:// or postgresql+psycopg://"; exit 1 ;;
esac
if [[ "$LIBPQ_URL" != *sslmode=* ]]; then
  echo "note: no sslmode in the URL; Neon requires TLS. Append ?sslmode=require"
fi

echo "==> 1/4  checking the database is reachable"
docker run --rm postgres:16 psql "$LIBPQ_URL" -tAc "select version()" \
  | head -1 | sed 's/^/         /'

echo "==> 2/4  seeding"
rows=$(docker run --rm postgres:16 psql "$LIBPQ_URL" -tAc \
  "select coalesce((select count(*) from courses), 0)" 2>/dev/null || echo 0)
if [[ "$rows" -gt 0 ]]; then
  echo "         $rows courses already present, skipping restore"
else
  [[ -f "$DUMP" ]] || { echo "missing $DUMP -- regenerate it with pg_dump"; exit 1; }
  # --no-owner/--no-privileges because the local role names do not exist on
  # Neon and would otherwise abort every GRANT.
  docker run --rm -i postgres:16 pg_restore --no-owner --no-privileges \
    --dbname "$LIBPQ_URL" < "$DUMP" 2>&1 | tail -5 | sed 's/^/         /' || true
  after=$(docker run --rm postgres:16 psql "$LIBPQ_URL" -tAc "select count(*) from courses")
  echo "         restored, $after courses"
fi

echo "==> 3/4  setting the secret and deploying"
# --stage so the secret is stored without restarting a machine that does not
# exist yet; the deploy below picks it up.
fly secrets set DATABASE_URL="$SQLA_URL" --app "$APP" --stage >/dev/null
fly deploy --app "$APP" 2>&1 | tail -12 | sed 's/^/         /'

echo "==> 4/4  smoke test"
BASE="https://$APP.fly.dev"
for path in /health / /og.png "/courses?limit=1&undergrad_only=true"; do
  code=$(curl -s --retry 12 --retry-delay 2 --retry-all-errors \
         -o /dev/null -w '%{http_code}' "$BASE$path")
  printf '         %-38s %s\n' "$path" "$code"
done
echo
echo "live at $BASE"
