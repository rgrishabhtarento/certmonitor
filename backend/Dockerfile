# Backend image: serves both the API and the monitoring worker.
#
# One image, two entrypoints. Shipping a single artefact means the worker can
# never drift out of sync with the API's models or check logic - the exact
# scenario that produces "the dashboard says UP but the worker disagrees".

# ---------------------------------------------------------------- builder
FROM python:3.12-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# Build-only toolchain, needed to compile psycopg2 and cryptography wheels
# where a prebuilt wheel is unavailable. None of this reaches the runtime
# image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential \
      libpq-dev \
      libffi-dev \
      libssl-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
 && /opt/venv/bin/pip install -r requirements.txt

# ---------------------------------------------------------------- runtime
FROM python:3.12-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="CertMonitor Backend" \
      org.opencontainers.image.description="Endpoint and SSL certificate monitoring API and worker" \
      org.opencontainers.image.licenses="Proprietary"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PATH="/opt/venv/bin:$PATH" \
    APP_ENV=production

# libpq for psycopg2, ca-certificates so TLS verification of monitored
# endpoints actually has a trust store, curl for the container healthcheck,
# tini to reap zombies and forward SIGTERM to the worker.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libpq5 \
      ca-certificates \
      curl \
      tini \
 && rm -rf /var/lib/apt/lists/* \
 && update-ca-certificates

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# Run as an unprivileged user. Monitoring only needs outbound TCP, so no
# capabilities are required.
RUN groupadd --gid 10001 certmonitor \
 && useradd --uid 10001 --gid certmonitor --create-home --shell /usr/sbin/nologin certmonitor

COPY --chown=certmonitor:certmonitor alembic.ini ./
COPY --chown=certmonitor:certmonitor alembic ./alembic
COPY --chown=certmonitor:certmonitor app ./app
COPY --chown=certmonitor:certmonitor docker/entrypoint.sh /usr/local/bin/entrypoint.sh

RUN chmod +x /usr/local/bin/entrypoint.sh

USER certmonitor

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/live || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["api"]
