# Two stages so the runtime image carries no build tooling and no uv cache.
FROM python:3.14-slim AS build

# uv resolves from uv.lock, so the image gets the exact versions that were
# tested locally rather than whatever is newest at build time.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependencies before source: this layer is cached until the lockfile changes,
# so an edit to a route does not reinstall psycopg.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project --no-dev

COPY . .
RUN uv sync --locked --no-dev


FROM python:3.14-slim AS runtime

# Runs unprivileged. The container needs no write access to anything.
RUN useradd --create-home --uid 10001 app
WORKDIR /app

COPY --from=build --chown=app:app /app /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
USER app

EXPOSE 8080

# --proxy-headers so request URLs reflect the https the client actually used;
# without it redirects and generated links come out as http behind Fly's edge.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
