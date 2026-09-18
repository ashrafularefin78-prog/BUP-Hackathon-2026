# ---------------------------------------------------------------------------
# GridWise — multi-stage build: wheels compiled once, slim runtime image.
# No secrets are baked in; configuration is injected at run time via env vars.
# ---------------------------------------------------------------------------

FROM python:3.14-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------------------

FROM python:3.14-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

# Non-root user
RUN useradd --create-home --shell /usr/sbin/nologin appuser

COPY --from=builder /opt/venv /opt/venv
COPY app /opt/app/app

WORKDIR /opt/app
ENV PATH="/opt/venv/bin:$PATH"

USER appuser

EXPOSE 8000

# Health check hits the readiness endpoint inside the container
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/health').read()"

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
