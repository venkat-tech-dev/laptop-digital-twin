# Laptop Digital Twin backend (FastAPI). Build context: ./backend
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

COPY pyproject.toml requirements.lock ./
COPY app ./app
# Phase 10: install within the tested versions (constraints), not whatever is newest at build time
RUN pip install -c requirements.lock .
COPY alembic.ini ./
COPY alembic ./alembic

RUN useradd --system --uid 10001 ldt && chown -R ldt /app
USER ldt
EXPOSE 8000
# Apply migrations, then serve. API_HOST=0.0.0.0 inside the container; compose publishes on 127.0.0.1 only.
# exec: python becomes PID 1 and receives SIGTERM, so the graceful shutdown flushes buffered samples,
# events and ingest receipts (without it Docker SIGKILLs after the grace period and they are lost).
CMD ["sh", "-c", "alembic upgrade head && exec python -m app.main"]
