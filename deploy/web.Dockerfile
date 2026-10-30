# Web UI (serve.py): static web/, /data/... and the live-requests demo API (/api/offers).
# Standard library only. Build from the repository root:
#   docker build -f deploy/web.Dockerfile -t salespatriot-web .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY *.py ./
COPY web/ web/
COPY data/ data/

# serve.py writes live requests to data/demo/ and dd_sources caches to .cache/ (ephemeral on Cloud Run).
RUN mkdir -p data/demo .cache && useradd --create-home --uid 10001 app && chown -R app /app
USER app

EXPOSE 8080
CMD ["sh", "-c", "exec python3 serve.py --host 0.0.0.0 --port ${PORT:-8080}"]
