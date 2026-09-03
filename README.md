# CertMonitor

Infrastructure endpoint and SSL certificate monitoring for DevOps/SRE teams.

CertMonitor continuously checks HTTP(S), TLS and TCP endpoints, records real
response timings, tracks certificate expiry, opens and closes incidents from
observed state, and raises alerts through webhooks, Slack, Teams, PagerDuty or
e-mail. Everything on the dashboard comes from checks the monitoring worker
actually executed — there is no mock or seeded monitoring data anywhere in the
application.

```
docker compose up -d
```

Then open <http://localhost:8080> and sign in.

---

## Contents

1. [Architecture](#1-architecture)
2. [Prerequisites](#2-prerequisites)
3. [Quick start](#3-quick-start)
4. [Docker deployment](#4-docker-deployment)
5. [Environment variables](#5-environment-variables)
6. [Database setup and migrations](#6-database-setup-and-migrations)
7. [Initial admin setup](#7-initial-admin-setup)
8. [Adding endpoints](#8-adding-endpoints)
9. [Bulk import and export](#9-bulk-import-and-export)
10. [Monitoring configuration](#10-monitoring-configuration)
11. [How monitoring actually works](#11-how-monitoring-actually-works)
12. [Alerts and notifications](#12-alerts-and-notifications)
13. [User management and RBAC](#13-user-management-and-rbac)
14. [API reference](#14-api-reference)
15. [Local development](#15-local-development)
16. [Testing](#16-testing)
17. [Backup and recovery](#17-backup-and-recovery)
18. [Production deployment](#18-production-deployment)
19. [Kubernetes considerations](#19-kubernetes-considerations)
20. [Troubleshooting](#20-troubleshooting)
21. [Security notes](#21-security-notes)
22. [Project structure](#22-project-structure)

---

## 1. Architecture

```
                        ┌──────────────────────────┐
   browser  ──────────► │  frontend (nginx)        │
                        │  React SPA + /api proxy  │
                        └────────────┬─────────────┘
                                     │  http
                        ┌────────────▼─────────────┐
                        │  backend  (FastAPI)      │
                        │  REST API, RBAC, OpenAPI │
                        └──────┬───────────┬───────┘
                               │           │
              ┌────────────────▼──┐   ┌────▼─────────────┐
              │  PostgreSQL       │   │  Redis           │
              │  all state        │   │  rate limits,    │
              └────────▲──────────┘   │  import previews │
                       │              └──────────────────┘
                       │
              ┌────────┴──────────────────┐
              │  worker (same image)      │
              │  claim → probe → record   │
              └────────┬──────────────────┘
                       │  outbound checks
        ┌──────────────┼──────────────┬──────────────┐
        ▼              ▼              ▼              ▼
    endpoint       endpoint       endpoint       endpoint
```

**Four containers, one backend image.** The API and the worker run the same
image with different entrypoint roles (`api` / `worker`). Shipping one artefact
means the worker can never disagree with the API about check semantics or the
database schema.

**No monitoring in request handlers.** The API never performs an endpoint check
on a page load. The worker claims due endpoints with
`SELECT … FOR UPDATE SKIP LOCKED` plus a short lease, probes them concurrently
under a bounded semaphore, and writes the results. A slow or unreachable
monitored host cannot delay an API request, and any number of worker replicas
can run against the same database without ever checking one endpoint twice.

| Component | Responsibility |
|---|---|
| `frontend` | Serves the built SPA; reverse-proxies `/api`, `/health`, `/ready` so the browser sees one origin (no CORS). |
| `backend` (role `api`) | REST API, authentication, RBAC, aggregation, OpenAPI. Applies migrations on start. |
| `backend` (role `worker`) | Check scheduling and execution, status machine, incidents, alerts, retention sweep, SSL re-grading. |
| `postgres` | Every piece of state. Persisted in a named volume. |
| `redis` | Cross-replica login rate limiting and import previews. Optional — the app degrades to in-process equivalents. |

---

## 2. Prerequisites

**To run it:** Docker Engine 24+ and Compose v2. Nothing else.

**To develop on it:** Python 3.12+, Node 20+, and a PostgreSQL 16 instance
(or just the `postgres` service from the Compose file).

Roughly 2 GB RAM and 2 CPUs is comfortable for a few hundred endpoints.

---

## 3. Quick start

```bash
# 1. Configure
cp .env.example .env

# 2. Set the values marked CHANGE-ME. At minimum:
#      POSTGRES_PASSWORD
#      JWT_SECRET          (openssl rand -hex 32)
#      ADMIN_PASSWORD
#    Compose refuses to start if POSTGRES_PASSWORD is unset.

# 3. Start
docker compose up -d

# 4. Watch it come up (migrations run first, then the worker starts)
docker compose logs -f backend worker

# 5. Confirm health
curl -s http://localhost:8080/health | jq
```

A healthy response looks like:

```json
{
  "status": "healthy",
  "database": "healthy",
  "monitoring_worker": "healthy",
  "version": "1.0.0",
  "environment": "production"
}
```

Open <http://localhost:8080>, sign in with `ADMIN_USERNAME` / `ADMIN_PASSWORD`,
and you will be required to set a new password before anything else is
reachable.

---

## 4. Docker deployment

```bash
docker compose up -d              # start everything
docker compose down               # stop (the postgres volume is preserved)
docker compose down -v            # stop AND DELETE all monitoring data
docker compose logs -f            # follow all logs
docker compose logs -f worker     # just the worker
docker compose restart worker     # restart one service
docker compose ps                 # service and health status
docker compose build --no-cache   # rebuild images
docker compose pull               # refresh postgres/redis/nginx base images
```

### Scaling the worker

```bash
docker compose up -d --scale worker=3
```

Safe by design: workers claim disjoint sets of endpoints via `SKIP LOCKED`.
Remove `container_name: certmonitor-worker` from the Compose file first, since
a fixed container name prevents scaling.

### Data persistence

PostgreSQL data lives in the named volume `certmonitor_postgres_data`, not in
the container filesystem. `docker compose down` and image rebuilds preserve it;
only `docker compose down -v` destroys it.

```bash
docker volume ls | grep certmonitor
docker volume inspect certmonitor_postgres_data
```

---

## 5. Environment variables

Every environment-specific value is read from the environment — nothing is
hardcoded in the application. `.env.example` documents all of them; the ones
that matter most:

### Required in any real deployment

| Variable | Purpose |
|---|---|
| `POSTGRES_PASSWORD` | Database password. Compose fails fast if unset. |
| `JWT_SECRET` | Signs access/refresh tokens. Use ≥ 32 random chars. Changing it invalidates every session. |
| `ADMIN_PASSWORD` | Initial administrator password. Used only when the account is first created. |

### Frequently tuned

| Variable | Default | Notes |
|---|---|---|
| `HTTP_PORT` | `8080` | Published dashboard port. |
| `ENCRYPTION_KEY` | derived from `JWT_SECRET` | Encrypts endpoint credentials and channel configs. Set it explicitly if you want to rotate `JWT_SECRET` independently. |
| `DEFAULT_MONITOR_INTERVAL` | `60` | Seconds. Also editable at runtime. |
| `DEFAULT_TIMEOUT` | `10` | Seconds. |
| `MIN_MONITOR_INTERVAL` | `30` | Hard floor, enforced server-side. |
| `SSL_WARNING_DAYS` / `SSL_CRITICAL_DAYS` | `30` / `7` | Certificate state thresholds. |
| `FAILURE_THRESHOLD` | `3` | Consecutive failures before an incident opens. |
| `RESPONSE_TIME_THRESHOLD_MS` | `2000` | Above this, a success is reported as degraded. |
| `ALERT_COOLDOWN_MINUTES` | `30` | Suppresses repeat alerts of the same type per endpoint. |
| `DATA_RETENTION_DAYS` | `90` | Check-result retention. Incidents and audit logs are kept far longer. |
| `WORKER_CONCURRENCY` | `50` | Simultaneous in-flight checks per worker process. |
| `ALLOW_LOOPBACK_TARGETS` | `false` | Loopback and link-local (incl. cloud metadata) targets are refused unless enabled. |

> **Env vs. runtime settings.** Environment variables provide the *boot
> defaults*. Rows in `system_settings`, edited from **Settings** in the UI,
> override them without a redeploy. `GET /api/settings` shows both — the
> `effective` block is what the worker actually reads.

---

## 6. Database setup and migrations

The schema is created and versioned by **Alembic**. Nothing relies on
`create_all` at runtime, and the initial migration
(`backend/alembic/versions/0001_initial_schema.py`) is handwritten and
reviewable rather than a generated dump.

Migrations run automatically when the `api` container starts, before uvicorn
binds its port. The worker waits for the schema to exist rather than racing to
migrate it.

```bash
# Apply migrations manually
docker compose run --rm backend migrate

# Inspect state
docker compose exec backend alembic current
docker compose exec backend alembic history

# Preview the SQL without executing it (for a DBA-applied change)
docker compose exec backend alembic upgrade head --sql

# Roll back one revision
docker compose exec backend alembic downgrade -1
```

### Tables

`users`, `roles`, `permissions`, `role_permissions`, `endpoints`,
`endpoint_tags`, `tags`, `environments`, `monitoring_results`,
`ssl_certificates`, `incidents`, `alerts`, `notification_channels`,
`audit_logs`, `system_settings`, `worker_heartbeats`.

Indexes worth knowing about:

- `ix_endpoints_due (monitoring_enabled, is_paused, next_check_at)` — the
  worker's claim query.
- `ix_monitoring_results_endpoint_time (endpoint_id, checked_at)` — history,
  statistics and the retention sweep's range delete.
- `uq_incidents_one_open_per_endpoint` — a **partial unique index** on
  `endpoint_id WHERE status = 'open'`. This is the database-level guarantee
  behind "four consecutive failed checks are one incident, not four", and it
  holds even with several workers racing.

---

## 7. Initial admin setup

On first start the application seeds roles, permissions, default settings, the
five default environments and one administrator, taken from the environment:

```env
ADMIN_USERNAME=admin
ADMIN_PASSWORD=Passwd@123
```

The password is stored **only** as a bcrypt digest (cost 12). The plaintext
never reaches the database, a log line, or any API response.

Because the bundled default is public knowledge,
`ADMIN_FORCE_PASSWORD_CHANGE=true` sets `must_change_password` on the account.
Until it is changed, every route except `/api/auth/*` returns 403 with
`X-Password-Change-Required: true`, and the UI routes straight to the password
screen.

Seeding is idempotent and takes a PostgreSQL advisory lock, so several API
replicas starting at once cannot race. **An existing admin account is never
overwritten** — changing `ADMIN_PASSWORD` later has no effect on it. Use
**Users → Reset password**, or:

```bash
docker compose exec backend python - <<'PY'
import asyncio
from app.core.database import session_scope
from app.services import user_service

async def main():
    async with session_scope() as session:
        user = await user_service.get_user_by_username(session, "admin")
        await user_service.reset_password(session, user, new_password="NewAdminPass@2026")
        print("reset; all existing sessions invalidated")

asyncio.run(main())
PY
```

---

## 8. Adding endpoints

**Endpoints → Add endpoint.** All of these are accepted:

```
https://example.com
https://api.example.com/health
http://10.10.10.10:8080/health
https://api.example.com:8443/status
api.example.com                      # https:// is assumed
```

The URL is parsed once at write time into protocol, hostname, port and path, so
the SSL page and every filter can query on host without re-parsing thousands of
URLs per request.

### Fields

| Group | Fields |
|---|---|
| Identity | Name, URL, check type (`http` / `tls` / `tcp`), HTTP method, port |
| Organisation | Environment, tags, description, owner, team, application |
| Scheduling | Monitoring enabled, paused, interval, timeout |
| Expectations | Expected HTTP status (`200`, `200,204`, `2xx`, `200-299`), expected body content, follow redirects |
| TLS | SSL monitoring enabled, verify chain, per-endpoint warning/critical days |
| Request | Custom headers (JSON), request body |
| Authentication | None / Bearer / Basic / Custom header |
| Alerting | Alerts enabled, failure threshold, response-time threshold |
| Audit | Created by/date, last modified (recorded automatically) |

A new endpoint is scheduled immediately, so a real status appears within one
worker poll (≤ 5 s by default) rather than after a full interval.

### Endpoint authentication

Bearer tokens, Basic passwords and custom header values are encrypted with
Fernet before storage. **They are never returned by the API in any form** —
only `has_auth_secret: true` and a masked hint like `****oken`. On edit, leaving
the field blank keeps the stored credential; setting the type to `none` clears
it.

`Authorization` is rejected in *Custom headers* precisely so credentials cannot
be smuggled into a plaintext field.

### Testing before committing

**Test now** in the edit dialog runs the real check and shows the outcome
without writing a result, opening an incident, or affecting uptime — useful for
validating a new configuration. `POST /api/endpoints/{id}/check?persist=false`
is the same thing over the API.

---

## 9. Bulk import and export

**Import / Export** (needs `endpoint:import`). Two-step by design: nothing is
written until you confirm, so a bad file cannot leave a half-finished import.

Only `url` is required.

```csv
name,url,environment,tags,interval,timeout,description
Translation API,https://api.example.com/health,production,"backend,critical",60,10,Translation backend
Portal,https://portal.example.com,production,"frontend",60,10,Main portal
Dev API,https://dev-api.example.com/health,development,"backend,dev",120,10,Development API
```

Recognised columns: `name, url, environment, tags, interval, timeout,
description, owner, team, application, method, expected_status, check_type,
monitoring_enabled, ssl_monitoring, verify_ssl, follow_redirects,
failure_threshold, response_time_threshold_ms`.

Common alternative spellings are mapped automatically (`endpoint_name`, `env`,
`host`, `target`, `interval_seconds`, `labels`, `retries`, …). Unknown columns
are reported and ignored rather than failing the file.

The import flow:

1. Reads CSV (comma/semicolon/tab, any of UTF-8/BOM/CP1252) or `.xlsx`.
2. Validates every row — URL, method, status spec, booleans, numbers.
3. Clamps aggressive intervals and reports the adjustment as a warning.
4. Derives a name from the hostname when it is missing, with a warning.
5. Detects duplicates **within the file and against the database**.
6. Shows a preview with per-row errors and warnings; rows can be deselected.
7. On confirm, creates each row in its own `SAVEPOINT` and reports created,
   failed and skipped rows separately.

Duplicates are re-checked at confirm time, so **re-importing the same file
creates nothing** — the flow is idempotent.

**Export** produces CSV or a formatted `.xlsx` (frozen header, auto-filter,
sized columns) honouring the current filters. Credentials are never exported —
only the authentication *type* — because an export leaves the application.

---

## 10. Monitoring configuration

**Settings** (needs `settings:write`). Editable without a redeploy:

| Setting | Effect |
|---|---|
| Default monitoring interval | Applied to new endpoints. Choices: 30 s, 1, 5, 10, 30 min, 1 h. |
| Default timeout | Per-check timeout, capped at the interval. |
| Consecutive failures before an incident | The incident threshold. |
| Consecutive successes before recovery | Guards against flapping. |
| Response-time threshold | Above this, a success is *degraded*. |
| SSL warning / critical days | Certificate state bands. |
| Alert cooldown | Suppression window per endpoint per alert type. |
| Alerting / notifications enabled | Master switches. |
| Data / incident / audit / alert retention | Independent retention windows. |
| Uptime SLA target | The reference line on the dashboard. |
| Selectable intervals | Which intervals the endpoint form offers. |

Values are validated as a batch — one bad value rejects the whole update rather
than applying half of it. Intervals below `MIN_MONITOR_INTERVAL` are refused
even if the row is edited directly, so a monitor cannot be turned into a load
generator.

Changing an SSL threshold **re-grades every stored certificate immediately**
rather than waiting for each endpoint's next check.

---

## 11. How monitoring actually works

### The worker loop

1. **Claim** — `SELECT id … WHERE monitoring_enabled AND NOT is_paused AND
   next_check_at <= now() AND (lease_expires_at IS NULL OR lease_expires_at <
   now()) ORDER BY next_check_at LIMIT n FOR UPDATE SKIP LOCKED`, then stamp a
   lease.
2. **Probe** — concurrently, bounded by `WORKER_CONCURRENCY`.
3. **Record** — each check in its own short transaction.
4. **Reschedule** — `next_check_at = now + interval ± 10 % jitter`, lease
   released.

Jitter matters: without it, endpoints created by one bulk import share a due
time forever and arrive as a thundering herd every interval.

A worker that dies mid-batch strands nothing — its leases expire and the
endpoints become claimable again.

### What one check captures

DNS resolution time and resolved IP; TCP connect time; TLS handshake time; TTFB;
total duration; HTTP status; redirect count, chain and final URL; content
length; selected response headers; TLS version and cipher; full certificate
details; error message and a classified failure reason.

Connect and TLS timings come from a wrapped httpcore network backend, measured
on **the very socket the request uses** — not a second probe. If a future
httpcore changes that interface, the wrapper degrades to reporting those two
sub-timings as `null` and the check keeps working.

Connections are deliberately **not** pooled across checks: a monitor should
measure a cold, representative request rather than reuse a warm keep-alive
socket that hides connection-level problems.

**Response bodies are never stored.** Only the byte count, and an optional
substring the operator explicitly configured, are evaluated. Session cookies
are excluded from the captured headers. Monitoring must not become an
accidental data-exfiltration path.

### Certificate inspection

For HTTPS endpoints the certificate is read from the live connection. A
verifying handshake is attempted first; if verification fails, the worker
reconnects **without** verification so the certificate can still be described,
preserving the original verification error. That is how an expired or
self-signed certificate ends up with complete details *and* an accurate
`chain_verified: false`.

Captured: issuer, subject, common name, SANs, valid from/to, days remaining,
signature algorithm, key algorithm and size, TLS version, chain summary,
self-signed and wildcard flags, hostname match, verification status, SHA-256
fingerprint.

States: `Valid`, `Expiring Soon`, `Critical`, `Expired`, `Invalid`,
`Unable to Check`.

| Remaining | State |
|---|---|
| > 30 days | Valid |
| 8–30 days | Expiring Soon |
| ≤ 7 days | Critical |
| negative | Expired |

Day counts round toward zero, so 23 hours remaining reads as `0 days` and a
"< 7 days" alert never fires a day late.

A new `ssl_certificates` row is written when the observed fingerprint changes,
giving a renewal history per endpoint; `is_current` marks the latest.

An hourly SSL sweep re-grades stored certificates and raises expiry alerts, so
an endpoint on a one-hour interval cannot cross the warning threshold and stay
silent until its next check.

### Status machine

```
UNKNOWN ──► UP ──► DOWN ──► RECOVERED ──► UP
                    │
                    └──► (incident stays open, failed_check_count++)
```

- Any failure sets the endpoint `DOWN` immediately (responsive dashboard).
- An **incident** opens only at `failure_threshold` consecutive failures.
- Further failures extend that one incident; a changed failure reason is
  appended to its timeline.
- Recovery closes the incident, computes `duration_seconds`, records the
  recovery status and response time, and raises a recovery alert.
- A slow-but-successful response is `DEGRADED` — reachable, not healthy — and
  closes an open incident.
- A paused endpoint is `PAUSED`, so it is read as neither healthy nor failing.

### Availability

Two different notions of "down" are reported on purpose:

- **Uptime %** — failed ÷ total checks in the window. "How often did we observe
  a problem."
- **Downtime seconds** — merged, window-clipped incident intervals. "For how
  long was it actually broken." This is the SLA number.

Windows: 24 h, 7 d, 30 d, 90 d, with avg / min / max / p95 latency
(`percentile_cont` on PostgreSQL).

### Retention

An hourly sweep deletes old rows in bounded batches, committing between them, so
it never holds a long transaction or a table-wide lock while checks are being
written. Check results are pruned aggressively; incidents and audit logs are
kept far longer because they are the record of what happened.

---

## 12. Alerts and notifications

Alerts are generated for: endpoint down, endpoint recovered, high response
time, repeated failures, SSL expiring, SSL expired, SSL invalid.

Cooldown suppresses a repeat of the same type for the same endpoint.
**Recovery alerts ignore the cooldown** — suppressing an all-clear is worse than
sending one too many.

An alert row is always recorded even if delivery fails, so the UI still shows it
and the failure is visible with its error.

### Channels

**Settings → Notification channels.** A generic webhook is the baseline;
Slack, Teams, PagerDuty and SMTP e-mail are built on the same abstraction.

Per channel: minimum severity, event-type filter, environment filter, tag
filter, enable/disable, and a **Send test** button.

Delivery retries three times with exponential backoff.

Webhook payload:

```json
{
  "event": "endpoint_down",
  "severity": "critical",
  "title": "Endpoint DOWN: Translation API",
  "message": "https://api.example.com/health failed 3 consecutive check(s). Connection timeout",
  "occurred_at": "2026-09-03T15:46:20+00:00",
  "alert_id": 1024,
  "incident_id": 512,
  "endpoint": {
    "id": "…", "name": "Translation API", "url": "https://api.example.com/health",
    "hostname": "api.example.com", "environment": "production",
    "tags": ["backend", "critical"], "owner": "platform@example.com",
    "current_status": "down"
  },
  "details": { "failure_reason": "connection_timeout", "consecutive_failures": 3 },
  "source": "certmonitor"
}
```

With a signing secret configured, an `X-CertMonitor-Signature: sha256=…` HMAC
over the exact bytes sent lets the receiver verify origin.

PagerDuty uses Events API v2 and *resolves* the incident on recovery rather than
opening a second one.

Channel configuration — webhook URLs, SMTP passwords, routing keys — is stored
as a single encrypted blob. Reading a channel back returns only non-sensitive
parts (`target_host`, `port`, `recipient_count`).

---

## 13. User management and RBAC

Two built-in roles:

| | Admin | Viewer |
|---|:-:|:-:|
| View dashboards, endpoints, SSL, history, incidents | ✅ | ✅ |
| Export configuration | ✅ | ✅ |
| Read settings | ✅ | ✅ |
| Add / edit / delete endpoints | ✅ | ❌ |
| Run manual checks | ✅ | ❌ |
| Import endpoints | ✅ | ❌ |
| Manage users | ✅ | ❌ |
| Change configuration, alerts, channels | ✅ | ❌ |
| Read audit logs | ✅ | ❌ |

Admins can create, enable, disable and delete users, reset passwords, change
roles, clear brute-force lockouts, and see last login with source IP.

**Invariants enforced server-side:**

- The last active administrator cannot be demoted, disabled or deleted, so an
  instance can never lock itself out of its own administration.
- You cannot delete or disable your own account, or remove your own admin role.
- A password reset, role change or disable **takes effect immediately** — each
  bumps `token_version`, and every previously issued JWT stops validating. No
  server-side session store is needed.

Password policy: ≥ 10 characters (configurable) with upper case, lower case, a
digit and a symbol; must differ from the username and from the current
password. Enforced identically in the API and mirrored live in the UI.

Login is rate limited per source IP **and** per username, and the account locks
after `ACCOUNT_LOCKOUT_ATTEMPTS` failures. Unknown-user and wrong-password
produce an identical response so accounts cannot be enumerated, and a miss
still spends comparable time hashing so response timing does not reveal
existence.

### Audit log

**Audit Logs** (admin) records logins and failures, logout, password changes and
resets, endpoint create/update/delete/check, imports and exports, user and role
changes, configuration changes, tag/environment/channel changes — each with
actor, resource, before/after diff, outcome, IP, user agent and request path.

Entries are append-only, and `username` is denormalised so the trail survives
deletion of the acting user. Details pass through a scrubber, so no
credential-shaped value is ever written.

---

## 14. API reference

Interactive docs: **<http://localhost:8080/api/docs>** (Swagger UI) and
`/api/redoc`. Machine-readable: `/api/openapi.json`.

Authenticate, then send `Authorization: Bearer <access_token>`.

```bash
TOKEN=$(curl -sX POST http://localhost:8080/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"YourPassword"}' | jq -r .access_token)

curl -s http://localhost:8080/api/endpoints -H "Authorization: Bearer $TOKEN" | jq
```

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/auth/login` | Sign in |
| POST | `/api/auth/refresh` | New access token |
| POST | `/api/auth/logout` | Sign out |
| GET | `/api/auth/me` | Current user and permissions |
| POST | `/api/auth/change-password` | Change own password |
| GET | `/api/endpoints` | List (search, filter, sort, paginate) |
| POST | `/api/endpoints` | Create |
| GET | `/api/endpoints/{id}` | Detail |
| PUT | `/api/endpoints/{id}` | Update (partial) |
| DELETE | `/api/endpoints/{id}` | Delete |
| PATCH | `/api/endpoints/{id}/monitoring` | Enable / disable / pause / resume |
| POST | `/api/endpoints/{id}/check` | Check now (`?persist=false` for a dry run) |
| GET | `/api/endpoints/{id}/history` | Paginated check history |
| GET | `/api/endpoints/{id}/stats` | Availability windows + series |
| GET | `/api/endpoints/{id}/ssl` | Current certificate |
| GET | `/api/endpoints/{id}/ssl/history` | Certificate history |
| POST | `/api/endpoints/bulk` | Bulk enable/disable/pause/resume/delete/check/tag |
| GET | `/api/endpoints/filters` | Filter options |
| GET | `/api/dashboard` | Full dashboard in one request |
| GET | `/api/dashboard/summary` | Just the summary cards |
| GET | `/api/dashboard/availability` | Grouped availability |
| GET | `/api/ssl` | Certificate table |
| GET | `/api/ssl/summary` | Certificate state counts |
| GET | `/api/incidents` | Incident history |
| PATCH | `/api/incidents/{id}` | Acknowledge / annotate |
| GET | `/api/alerts` | Alert history |
| POST | `/api/alerts/acknowledge` | Acknowledge alerts |
| GET/POST | `/api/tags`, `/api/environments` | Taxonomy |
| GET/POST/PUT/DELETE | `/api/users`, `/api/users/{id}` | User management |
| POST | `/api/users/{id}/reset-password` | Admin reset |
| GET/PUT | `/api/settings` | Runtime configuration |
| GET/POST/PUT/DELETE | `/api/notification-channels` | Channels |
| GET | `/api/audit-logs` | Audit trail |
| POST | `/api/import` | Upload + validate (preview) |
| POST | `/api/import/confirm` | Create the previewed rows |
| GET | `/api/import/template` | CSV template |
| GET | `/api/export` | Export (`?format=csv\|xlsx`) |
| GET | `/api/workers` | Worker fleet status |
| GET | `/health`, `/ready`, `/live` | Probes (unauthenticated) |

List endpoints return `{items, meta}` with `total`, `page`, `page_size`,
`pages`, `has_next`, `has_previous` — the UI always paginates.

Errors are consistent: `{detail, code, fields?}`, where `fields` maps a field
name to its message. Internal errors return a `request_id` that ties the
response to the full detail in the log; exception text is never echoed.

### Health and readiness

- `/live` — process liveness only. Cheapest probe; kept separate so a database
  outage does not cause Kubernetes to restart healthy API pods.
- `/health` — 200 when serving, 503 when the database is unreachable. A stale
  worker degrades the response but does not fail it.
- `/ready` — 503 until the schema exists and is seeded, so a container that
  started before migrations completed stays out of the load balancer.

---

## 15. Local development

### Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Just the dependencies from Compose
docker compose up -d postgres redis

export DATABASE_URL="postgresql+asyncpg://certmonitor:yourpassword@localhost:5432/certmonitor"
export JWT_SECRET="dev-secret-at-least-32-characters-long"
export APP_ENV=development LOG_FORMAT=console

alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

The worker in a second terminal, same environment:

```bash
python -m app.workers.monitor_worker
```

### Frontend

```bash
cd frontend
npm install
npm run dev      # http://localhost:5173, proxies /api to :8000
```

Point it elsewhere with `VITE_API_TARGET=http://localhost:8000 npm run dev`.

---

## 16. Testing

```bash
cd backend
pytest                              # whole suite
pytest -v                           # verbose
pytest tests/test_ssl.py            # one file
pytest -k "incident"                # by name
pytest --cov=app --cov-report=term-missing
```

Or in the container:

```bash
docker compose exec backend pytest
```

The suite runs against a throwaway SQLite file — no services required. The
schema comes from the same declarative metadata the migration was written from,
and the JSON/BigInteger columns carry SQLite variants for exactly this reason.

| File | Covers |
|---|---|
| `test_validators.py` | URL parsing and rejection, status specs, interval/timeout clamping, blocked addresses |
| `test_auth.py` | Login, invalid login, account enumeration, lockout, tokens, password policy, forced change, role authorisation, audit |
| `test_checker.py` | Healthy, down, timeouts, TLS errors, DNS failure, HTTP mismatch, degraded, body matching, auth headers, redirects |
| `test_ssl.py` | Valid / expiring / critical / expired / invalid / self-signed / wildcard / hostname-mismatch certificates, chains, threshold classification |
| `test_monitoring_state.py` | One incident per outage, recovery and downtime, alert generation, cooldown, certificate rotation, re-grading |
| `test_endpoints_api.py` | CRUD, validation, duplicates, credential handling, filters, sorting, pagination, bulk actions |
| `test_import_export.py` | Valid/invalid CSV, missing fields, duplicates, aliases, Excel, idempotent re-import, credential-free export |
| `test_health_and_settings.py` | Probes, security headers, settings validation, user management invariants, channels, OpenAPI completeness |

HTTP responses are stubbed with `respx`, and certificates are generated
in-process with `cryptography`, so the valid/expiring/expired/invalid cases are
real X.509 material going through the same parsing path a live handshake uses.

---

## 17. Backup and recovery

### What to back up

1. **PostgreSQL** — endpoints, history, incidents, users, settings, audit log.
2. **`.env`** — secrets. Critically, `JWT_SECRET` / `ENCRYPTION_KEY`: without
   them, stored endpoint credentials and channel configurations **cannot be
   decrypted**, even from a good database dump.

### Backup

```bash
mkdir -p backups

# Compressed custom-format dump (recommended)
docker compose exec -T postgres pg_dump \
  -U certmonitor -d certmonitor -Fc \
  > "backups/certmonitor-$(date +%F-%H%M).dump"

# Plain SQL
docker compose exec -T postgres pg_dump -U certmonitor -d certmonitor \
  > "backups/certmonitor-$(date +%F).sql"

# Configuration only (no history) - handy for seeding another instance
curl -s "http://localhost:8080/api/export?format=csv" \
  -H "Authorization: Bearer $TOKEN" -o backups/endpoints.csv

# And keep the secrets somewhere safe
cp .env backups/env-$(date +%F).bak     # store encrypted, never in git
```

A nightly cron entry:

```cron
0 2 * * * cd /opt/certmonitor && docker compose exec -T postgres pg_dump -U certmonitor -d certmonitor -Fc > backups/certmonitor-$(date +\%F).dump && find backups -name '*.dump' -mtime +30 -delete
```

### Restore

```bash
# 1. Stop the writers; leave postgres running
docker compose stop backend worker

# 2. Recreate the database
docker compose exec -T postgres psql -U certmonitor -d postgres \
  -c "DROP DATABASE IF EXISTS certmonitor;" -c "CREATE DATABASE certmonitor;"

# 3. Restore
docker compose exec -T postgres pg_restore -U certmonitor -d certmonitor --clean --if-exists \
  < backups/certmonitor-2026-09-03-0200.dump
# for a plain SQL dump:
#   docker compose exec -T postgres psql -U certmonitor -d certmonitor < backups/certmonitor-2026-09-03.sql

# 4. Put back the ORIGINAL .env (same JWT_SECRET / ENCRYPTION_KEY)

# 5. Start, applying any newer migrations
docker compose up -d backend worker
docker compose logs -f backend

# 6. Verify
curl -s http://localhost:8080/health | jq
```

If `ENCRYPTION_KEY` was lost, everything else still works: endpoints with stored
credentials record an explicit "credentials could not be decrypted" failure
instead of crashing, and channels report that their configuration must be
re-entered. Re-enter those secrets and monitoring resumes.

### Volume-level snapshot

```bash
docker compose down
docker run --rm -v certmonitor_postgres_data:/data -v "$PWD/backups:/backup" \
  alpine tar czf /backup/pgdata-$(date +%F).tar.gz -C /data .
docker compose up -d
```

---

## 18. Production deployment

**Before going live**

- [ ] `JWT_SECRET` — 32+ random bytes, unique per environment.
- [ ] `ENCRYPTION_KEY` — set explicitly so `JWT_SECRET` can rotate alone.
- [ ] `POSTGRES_PASSWORD` — strong and unique.
- [ ] `ADMIN_PASSWORD` — changed from the default; sign in and rotate it.
- [ ] `APP_ENV=production`, `DEBUG=false`, `LOG_FORMAT=json`.
- [ ] TLS terminated in front of the stack (see below).
- [ ] `ALLOWED_HOSTS` set if the API is exposed directly.
- [ ] Comment out the `postgres` port mapping unless you need host access.
- [ ] Automated backups scheduled **and a restore rehearsed**.
- [ ] `DATA_RETENTION_DAYS` sized for your disk (see the estimate below).
- [ ] Log shipping configured; alerts routed to a channel someone watches.

### TLS

The bundled nginx serves plain HTTP on `HTTP_PORT`. In production put a
terminating proxy in front of it and keep `HSTS_ENABLED=true`:

```nginx
server {
    listen 443 ssl http2;
    server_name monitor.example.com;

    ssl_certificate     /etc/letsencrypt/live/monitor.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/monitor.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

`X-Forwarded-For` matters: the audit log records the client address from it.

### Sizing

One check writes one `monitoring_results` row of roughly 0.5–1 KB.

```
rows/day = endpoints × 86400 / interval
```

| Endpoints | Interval | Rows/day | ~90 days |
|---:|---:|---:|---:|
| 100 | 60 s | 144 k | ~10 GB |
| 500 | 60 s | 720 k | ~50 GB |
| 500 | 300 s | 144 k | ~10 GB |
| 2 000 | 300 s | 576 k | ~40 GB |

Longer intervals and shorter retention are the two levers. Incidents and audit
logs are negligible by comparison.

Rough worker capacity: `WORKER_CONCURRENCY × (interval / avg_check_seconds)`
endpoints per replica. With the defaults (50 concurrent, 60 s interval, ~0.3 s
checks) one worker handles a few thousand endpoints comfortably; scale
replicas beyond that.

---

## 19. Kubernetes considerations

The design already assumes it. Notes for a chart:

**Workloads** — the frontend and API as `Deployment`s; the worker as a separate
`Deployment` (same image, `args: ["worker"]`). Do not run the worker as a
sidecar; it must scale independently.

**Migrations** — run `args: ["migrate"]` as a `Job` with a
`helm.sh/hook: pre-install,pre-upgrade` annotation, and drop migration-on-start
from the API by keeping the API replica count at 1 during the upgrade window,
or leave it as-is (the advisory lock makes concurrent starts safe).

**Probes**

```yaml
livenessProbe:
  httpGet: { path: /live, port: 8000 }
  initialDelaySeconds: 20
  periodSeconds: 15
readinessProbe:
  httpGet: { path: /ready, port: 8000 }
  initialDelaySeconds: 10
  periodSeconds: 10
```

Use `/live` for liveness and `/ready` for readiness — never `/health` for
liveness, or a database blip restarts every pod.

The worker has no HTTP port; use an `exec` probe on the process, and watch the
`monitoring_worker` field of the API's `/health` for real liveness.

**Configuration** — non-secret values in a `ConfigMap`; `JWT_SECRET`,
`ENCRYPTION_KEY`, `POSTGRES_PASSWORD`, `ADMIN_PASSWORD` in a `Secret` (or
External Secrets / Vault). Mount both with `envFrom`.

**Redis is no longer optional** with more than one API replica: login rate
limits and import previews need to be shared. Without it, previews require
sticky sessions and rate limits are per-pod.

**Database** — use managed PostgreSQL and set `DATABASE_URL` directly rather
than running the `postgres` service.

**Scaling** — the worker scales horizontally with no coordination (`SKIP
LOCKED` + leases). Give each replica a distinct `WORKER_ID` (the pod name via
`fieldRef: metadata.name` works well) so `/api/workers` is readable.

**Egress** — the worker needs outbound access to everything it monitors. Write
`NetworkPolicy` egress rules accordingly; the API needs none.

**Security context** — the image already runs as UID 10001 and needs no
capabilities:

```yaml
securityContext:
  runAsNonRoot: true
  runAsUser: 10001
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities: { drop: ["ALL"] }
```

**Graceful shutdown** — keep `terminationGracePeriodSeconds` at 45+ so the
worker can finish in-flight checks and release its leases.

---

## 20. Troubleshooting

**Everything: check the logs first.**

```bash
docker compose logs --tail=100 backend
docker compose logs --tail=100 worker
docker compose ps
```

### The stack will not start

```
POSTGRES_PASSWORD must be set in .env
```
`cp .env.example .env` and fill it in.

### `/health` reports `"monitoring_worker": "unhealthy"`

The worker has not written a heartbeat within `WORKER_STALE_AFTER_SECONDS`.

```bash
docker compose ps worker
docker compose logs --tail=50 worker
docker compose restart worker
curl -s http://localhost:8080/api/workers -H "Authorization: Bearer $TOKEN" | jq
```

### Endpoints stay `unknown`

The worker is not claiming them. Check, in order:

1. `docker compose ps worker` — is it running?
2. Is `WORKER_ENABLED=true`?
3. Is the endpoint enabled and not paused?
4. `docker compose logs worker | grep -i claim`

### Every check fails with a DNS error

The worker container cannot resolve. Test from inside it:

```bash
docker compose exec worker python -c \
  "import socket; print(socket.getaddrinfo('api.example.com', 443))"
```

For an internal resolver, add `dns:` to the worker service in Compose.

### `Refusing to check … loopback addresses are not monitored`

The target resolved to `127.0.0.1` or a link-local address. Inside a container
that is almost always a misconfiguration — use the service name or the host's
real address. If you genuinely mean it, set `ALLOW_LOOPBACK_TARGETS=true`.

### An internal certificate shows `Invalid`

The chain does not verify against the public trust store. Either mount your CA
bundle into the worker, or turn **Verify the certificate chain** off for that
endpoint — the certificate is still inspected and its expiry still tracked.

### Certificate state is `Unable to Check`

No TLS handshake has succeeded yet. Confirm the endpoint is HTTPS, SSL
monitoring is on, and the port is reachable; then use **Check now**.

### Migrations fail on start

```bash
docker compose logs backend | grep -i alembic
docker compose exec backend alembic current
docker compose run --rm backend migrate
```

If a migration was interrupted, the whole upgrade runs in one transaction, so
the schema is untouched — fix the cause and re-run.

### Locked out

```bash
docker compose exec backend python - <<'PY'
import asyncio
from app.core.database import session_scope
from app.services import user_service

async def main():
    async with session_scope() as session:
        user = await user_service.get_user_by_username(session, "admin")
        user.locked_until = None
        user.failed_login_attempts = 0
        user.is_active = True
        await user_service.reset_password(session, user, new_password="Recovery@2026Pass")
        print("recovered")

asyncio.run(main())
PY
```

### The database is growing too fast

Lower `data_retention_days` in **Settings**, lengthen check intervals, and
check the sweep is running: `docker compose logs worker | grep retention`.

### Notifications are not arriving

**Settings → Notification channels → Send test** reports the exact delivery
error, which is also stored on the channel. Check the alert's
`notification_status` in **Alerts** — `skipped` means no channel matched its
severity/event/environment/tag filters.

---

## 21. Security notes

- Passwords: bcrypt cost 12. Never logged, never returned, never exported.
- Endpoint credentials and channel configs: Fernet-encrypted at rest,
  write-only through the API, displayed only as a masked hint.
- Tokens: short-lived JWTs with a `token_version` claim, so a password reset,
  role change or disable revokes access immediately.
- Login: per-IP and per-username rate limiting, account lockout, constant-ish
  response timing, non-enumerable errors.
- Input: Pydantic validation everywhere; URLs parsed and rejected before
  storage; credentials in URLs refused; `Authorization` blocked from custom
  headers.
- SQL injection: every query goes through SQLAlchemy with bound parameters.
- XSS: React escapes by default; the API returns JSON only under a
  `default-src 'none'` CSP, and the SPA under a self-only CSP.
- Clickjacking: `X-Frame-Options: DENY`, `frame-ancestors 'none'`.
- CSRF: tokens are sent in the `Authorization` header, not cookies, so there is
  no ambient credential to forge. Cookie auth would need CSRF tokens.
- Errors: exception text is never echoed to a client; a `request_id` correlates
  the response with the log.
- Logs: a processor scrubs anything credential-shaped before it is emitted.
- SSRF-ish protection: loopback, link-local (incl. cloud metadata), multicast
  and reserved addresses are refused by default.
- Containers: run as UID 10001, no capabilities, no build toolchain in the
  runtime image.
- Response bodies are never stored.

---

## 22. Project structure

```
certmonitor/
├── backend/
│   ├── alembic/
│   │   ├── env.py
│   │   └── versions/0001_initial_schema.py   handwritten initial migration
│   ├── app/
│   │   ├── api/
│   │   │   ├── deps.py                       auth, RBAC, pagination
│   │   │   ├── health.py                     /health /ready /live /api/workers
│   │   │   └── v1/                           auth, endpoints, dashboard,
│   │   │                                     incidents, taxonomy, users,
│   │   │                                     settings, importexport
│   │   ├── core/                             config, database, security,
│   │   │                                     logging, enums, ratelimit
│   │   ├── models/                           SQLAlchemy models
│   │   ├── monitoring/
│   │   │   ├── checker.py                    executes one real check
│   │   │   ├── ssl_inspect.py                X.509 parsing and classification
│   │   │   ├── transport.py                  per-phase timing instrumentation
│   │   │   └── validators.py                 URL/target validation
│   │   ├── schemas/                          Pydantic request/response models
│   │   ├── services/                         monitoring, stats, endpoints,
│   │   │                                     users, alerts, notifications,
│   │   │                                     import/export, retention, settings
│   │   ├── workers/monitor_worker.py         the monitoring process
│   │   ├── bootstrap.py                      first-boot seeding
│   │   └── main.py                           app factory, middleware, handlers
│   ├── docker/entrypoint.sh                  api | worker | migrate | shell
│   ├── tests/
│   ├── Dockerfile
│   ├── alembic.ini
│   ├── pytest.ini
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── components/                       ui, charts, EndpointForm
│   │   ├── hooks/                            useAuth, useToast
│   │   ├── layouts/AppLayout.jsx
│   │   ├── lib/                              api client, formatters
│   │   ├── pages/                            14 screens
│   │   ├── App.jsx
│   │   └── main.jsx
│   ├── Dockerfile
│   ├── nginx.conf                            static serving + /api proxy
│   ├── package.json
│   ├── tailwind.config.js
│   └── vite.config.js
├── docker-compose.yml
├── .env.example
├── .dockerignore
└── README.md
```

### Extending it

The design is modular where it matters:

- **A new check type** — add a `CheckType` value and a `_run_*_check` in
  `monitoring/checker.py`. Storage, scheduling, incidents and alerting are
  type-agnostic already.
- **A new notification provider** — add one function to `_DELIVERY` in
  `services/notification_service.py` plus its required-config entry.
- **A new setting** — add a `SettingSpec` in `services/settings_service.py`; it
  appears in the UI, validated, with no other change.
- **A new dashboard metric** — add a query to `services/stats_service.py` and a
  field to the dashboard schema.
