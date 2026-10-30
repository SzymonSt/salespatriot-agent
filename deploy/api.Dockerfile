# Agent backend (FastAPI + Google ADK). Build from the repository root:
#   docker build -f deploy/api.Dockerfile -t salespatriot-api .
# Secrets come from the environment (Cloud Run: Secret Manager); never bake .env into the image.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /app

# Dependencies first, so code changes don't reinstall them.
COPY app/backend/pyproject.toml app/backend/uv.lock app/backend/
RUN uv sync --frozen --no-dev --project app/backend

COPY app/ app/
COPY data/profiles/ data/profiles/
COPY knowledge_base/ knowledge_base/
RUN rm -f app/backend/.env && useradd --create-home --uid 10001 app && chown -R app /app
USER app

ENV PATH=/opt/venv/bin:$PATH
EXPOSE 8080
CMD ["sh", "-c", "exec uvicorn app.backend.main:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers"]
