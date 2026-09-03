#!/usr/bin/env bash
#
# Container entrypoint. Selects a role:
#
#   api      - uvicorn serving the REST API (runs migrations first)
#   worker   - the monitoring worker
#   migrate  - apply migrations and exit
#   shell    - interactive Python with the app importable
#
# Migrations run only in the "api" role. If the worker ran them too, two
# containers starting together would race; instead the worker waits for the
# schema the API creates.

set -euo pipefail

ROLE="${1:-api}"
shift || true

log() {
  printf '{"level":"info","logger":"entrypoint","event":"%s","role":"%s"}\n' "$1" "$ROLE"
}

fail() {
  printf '{"level":"error","logger":"entrypoint","event":"%s","role":"%s"}\n' "$1" "$ROLE" >&2
  exit 1
}

# ---------------------------------------------------------------- helpers
wait_for_database() {
  local attempts="${DB_WAIT_ATTEMPTS:-60}"
  local delay="${DB_WAIT_DELAY:-2}"
  local i=1

  log "waiting_for_database"
  while [ "$i" -le "$attempts" ]; do
    if python - <<'PY'
import sys

from sqlalchemy import create_engine, text

from app.core.config import settings

try:
    engine = create_engine(settings.sync_database_url, pool_pre_ping=True)
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    engine.dispose()
except Exception:
    sys.exit(1)
sys.exit(0)
PY
    then
      log "database_ready"
      return 0
    fi
    i=$((i + 1))
    sleep "$delay"
  done
  fail "database_unreachable"
}

wait_for_schema() {
  # The worker must not start before the API has migrated, or its first claim
  # query would fail against a missing table.
  local attempts="${SCHEMA_WAIT_ATTEMPTS:-90}"
  local delay="${SCHEMA_WAIT_DELAY:-2}"
  local i=1

  log "waiting_for_schema"
  while [ "$i" -le "$attempts" ]; do
    if python - <<'PY'
import sys

from sqlalchemy import create_engine, inspect

from app.core.config import settings

try:
    engine = create_engine(settings.sync_database_url)
    inspector = inspect(engine)
    required = {"endpoints", "monitoring_results", "system_settings"}
    present = set(inspector.get_table_names())
    engine.dispose()
    sys.exit(0 if required.issubset(present) else 1)
except Exception:
    sys.exit(1)
PY
    then
      log "schema_ready"
      return 0
    fi
    i=$((i + 1))
    sleep "$delay"
  done
  fail "schema_not_ready"
}

run_migrations() {
  log "running_migrations"
  alembic upgrade head
  log "migrations_applied"
}

seed_data() {
  # Runs once, before uvicorn forks its workers. Doing it here rather than
  # only in the app lifespan means API_WORKERS > 1 cannot leave the instance
  # unseeded, and a failure stops the container instead of serving an API
  # with no admin account.
  log "seeding_data"
  python -m app.bootstrap
  log "seed_complete"
}

# ------------------------------------------------------------------ roles
case "$ROLE" in
  api)
    wait_for_database
    run_migrations
    seed_data
    log "starting_api"
    exec uvicorn app.main:app \
      --host "${API_HOST:-0.0.0.0}" \
      --port "${API_PORT:-8000}" \
      --workers "${API_WORKERS:-2}" \
      --proxy-headers \
      --forwarded-allow-ips '*' \
      --timeout-keep-alive 30 \
      --no-access-log \
      "$@"
    ;;

  worker)
    wait_for_database
    wait_for_schema
    log "starting_worker"
    exec python -m app.workers.monitor_worker "$@"
    ;;

  migrate)
    wait_for_database
    run_migrations
    ;;

  seed)
    wait_for_database
    wait_for_schema
    seed_data
    ;;

  shell)
    exec python "$@"
    ;;

  *)
    # Anything else is treated as a raw command, which keeps
    # `docker compose run backend <cmd>` usable.
    exec "$ROLE" "$@"
    ;;
esac
