# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# Multi-stage: deps are built once and copied, so an application-code change
# rebuilds in seconds instead of re-resolving pip and re-downloading Chromium.
# ---------------------------------------------------------------------------

FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --upgrade pip && \
    /opt/venv/bin/pip install -r requirements.txt


FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

COPY --from=builder /opt/venv /opt/venv

# Chromium's runtime libraries. `playwright install --with-deps` resolves the
# correct set for this Debian release rather than us guessing package names.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl tini \
    && playwright install --with-deps chromium \
    && apt-get purge -y curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY sql/ ./sql/
COPY web/ ./web/
RUN pip install --no-deps -e .

# Non-root: the browser runs untrusted remote content, so it must not be root.
RUN useradd --create-home --uid 10001 homz && \
    mkdir -p /app/data/raw && \
    chown -R homz:homz /app /opt/playwright
USER homz

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import homz, sys; sys.exit(0)"

# tini reaps zombie Chromium processes; without it a long-running scheduler
# accumulates defunct children until it hits the process limit.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "homz.search.api:app", "--host", "0.0.0.0", "--port", "8000"]
