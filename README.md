# InfraSight

Infrastructure endpoint and SSL certificate monitoring for DevOps/SRE teams.

InfraSight continuously checks HTTP(S), TLS and TCP endpoints, records real
response timings, tracks certificate expiry, opens and closes incidents from
observed state, and raises alerts through webhooks, Slack, Teams, PagerDuty or
e-mail. Everything on the dashboard comes from checks the monitoring worker
actually executed — there is no mock or seeded monitoring data anywhere in the
application.

Its [Diagnose](#15-diagnose) feature investigates a failing endpoint layer by
layer, ranks the probable causes against the evidence behind each, correlates
with recent deployments and incidents, and says plainly what it cannot see.
Alongside it, [RCA management](#16-rca-management) turns a resolved incident
into a record worth keeping — optional, never blocking, and draftable from the
data already collected.

**No external AI is used or required.** Every piece of that intelligence is
rules, statistics and correlation running on your own server. No API key, no
outbound call, and no infrastructure data leaving the machine.

It also carries a deliberately small [change management](#14-change-management)
workflow — request, approve, deploy — whose point is that starting a deployment
**pauses the monitoring for exactly the endpoints it touches**, so a planned
outage never opens an incident, never pages anyone, and never counts against
uptime.

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
14. [Change management](#14-change-management)
15. [Diagnose](#15-diagnose)
16. [RCA management](#16-rca-management)
17. [API reference](#17-api-reference)
18. [Local development](#18-local-development)
19. [Testing](#19-testing)
20. [Backup and recovery](#20-backup-and-recovery)
21. [Production deployment](#21-production-deployment)
22. [Kubernetes considerations](#22-kubernetes-considerations)
23. [Troubleshooting](#23-troubleshooting)
24. [Security notes](#24-security-notes)
25. [Project structure](#25-project-structure)

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

### Upgrading from CertMonitor

This application was previously called **CertMonitor**. The rename is
cosmetic in the code, but three identifiers name real infrastructure, and
letting them change silently would look exactly like data loss:

| | Was | Now |
|---|---|---|
| Compose project (and therefore the volume) | `certmonitor` → `certmonitor_postgres_data` | `infrasight` → `infrasight_postgres_data` |
| Postgres role and database | `certmonitor` | `infrasight` |
| Container names | `certmonitor-*` | `infrasight-*` |

**The safe upgrade is two lines in your existing `.env`:**

```bash
COMPOSE_PROJECT_NAME=certmonitor   # keeps the existing postgres volume
# leave POSTGRES_DB / POSTGRES_USER as certmonitor
```

Then rebuild as normal:

```bash
git pull
docker compose up -d --build
```

Your data, containers and volume stay exactly where they are; only the name
in the UI changes. **Without those lines, Compose creates a new empty volume
and the dashboard comes up with nothing in it** — your data is still safe in
`certmonitor_postgres_data`, but the application will not be looking at it.

Two other things carry across on their own:

- **Webhook signatures.** Payloads are signed with both
  `X-InfraSight-Signature` and the original `X-CertMonitor-Signature`, same
  value, so receivers verifying the old header keep working.
- **Browser sessions and theme.** The old `localStorage` keys are read once
  and migrated, so nobody is signed out by the upgrade.

One thing does change: the outbound `User-Agent` on checks is now
`InfraSight/1.0`. If any monitored service allow-lists it by name, update that
allow-list.

#### Actually renaming the database

The two lines above are the recommended route — they cost nothing and carry no
risk. Do the following only if you want the storage renamed to match the
product.

**Before you start:** copy `JWT_SECRET` and `ENCRYPTION_KEY` from the old
`.env` into the new one **unchanged**. Endpoint credentials and notification
configuration are encrypted with a key derived from them; a fresh secret makes
every stored credential permanently undecryptable, and no database restore
will bring them back.

```bash
cd /opt/infrasight          # wherever the stack lives
mkdir -p backups

# 1. Find the running postgres container. Named certmonitor-postgres before
#    the rename, infrasight-postgres after - so ask rather than assume.
docker ps --format '{{.Names}}'

# 2. Stop the writers, leave postgres up. pg_dump is consistent either way,
#    but this avoids capturing a deployment that is halfway through.
docker stop certmonitor-backend certmonitor-worker

# 3. Dump. --no-owner and --no-acl drop the "OWNER TO certmonitor" statements,
#    which would otherwise fail against a database owned by infrasight.
docker exec certmonitor-postgres pg_dump \
  -U certmonitor -d certmonitor -Fc --no-owner --no-acl \
  > "backups/certmonitor-$(date +%F-%H%M).dump"

ls -lh backups/            # a few hundred KB at minimum; 0 bytes means it failed

# 4. Take the old stack down. WITHOUT -v, so certmonitor_postgres_data
#    survives untouched as your rollback.
docker compose -p certmonitor down

# 5. Point .env at the new names, then bring up ONLY postgres so it creates
#    the empty infrasight database.
#      COMPOSE_PROJECT_NAME=infrasight
#      POSTGRES_DB=infrasight
#      POSTGRES_USER=infrasight
docker compose up -d postgres
docker compose logs -f postgres      # wait for "database system is ready"

# 6. Restore into it.
docker exec -i infrasight-postgres pg_restore \
  -U infrasight -d infrasight --no-owner --no-acl \
  < backups/certmonitor-<the file you just made>.dump

# 7. Verify before starting anything else.
docker exec infrasight-postgres psql -U infrasight -d infrasight -c \
  "select count(*) as endpoints from endpoints;
   select count(*) as results from monitoring_results;
   select version_num from alembic_version;"

# 8. Start the rest. The API applies any newer migrations on the way up.
docker compose up -d
docker compose logs -f backend
```

`pg_restore` prints warnings about roles and extensions it did not create.
Those are expected with `--no-owner --no-acl`; what matters is the row counts
in step 7 and a clean `migrations_applied` in step 8.

**Rolling back** costs one line, because nothing destroyed the old volume:
put `COMPOSE_PROJECT_NAME`, `POSTGRES_DB` and `POSTGRES_USER` back to
`certmonitor`, then `docker compose up -d`.

Once the new stack has run happily for a few days, reclaim the space:

```bash
docker volume rm certmonitor_postgres_data      # irreversible
```



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
Remove `container_name: infrasight-worker` from the Compose file first, since
a fixed container name prevents scaling.

### Data persistence

PostgreSQL data lives in the named volume `<project>_postgres_data`
(`infrasight_postgres_data` by default, or whatever `COMPOSE_PROJECT_NAME`
is set to), not in the container filesystem. `docker compose down` and image
rebuilds preserve it; only `docker compose down -v` destroys it.

```bash
docker volume ls | grep infrasight
docker volume inspect infrasight_postgres_data
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
`audit_logs`, `system_settings`, `worker_heartbeats`, `changes`,
`change_endpoints`, `change_comments`, `change_activity`, `diagnoses`,
`rcas`, `incident_comments`.

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

### Watching itself

**System Resources** (`/system`, needs `settings:read`) asks the same questions
of InfraSight that InfraSight asks of everything else. A monitoring tool that
runs out of disk stops monitoring, and it does so *silently* — checks simply
stop being recorded.

| | Reported | How |
|---|---|---|
| Disk free, and days of headroom | ✅ | The API container's root filesystem, which on the default Compose topology shares a host device with the postgres volume |
| Database size, growth per day, largest tables, connections, cache hit ratio | ✅ | SQL against `pg_database_size`, `pg_class`, `pg_stat_database` |
| API CPU and memory | ✅ | Its own cgroup |
| Worker CPU and memory | ✅ | Each worker measures its own cgroup and carries the numbers on the heartbeat it already writes |
| Redis memory, CPU, clients, keys | ✅ | `INFO` over the normal connection |
| **nginx CPU and memory** | ❌ | Listed under "Not measured" |
| **PostgreSQL process CPU and memory** | ❌ | Listed under "Not measured" |

Those last two are the deliberate part. Mounting `/var/run/docker.sock` into
the API would give a full `docker stats` view of all five services in about
three lines — and would also hand host root to anyone who compromised a
network-facing container. That is a bad trade for a resource graph, so
InfraSight does without and **says which numbers it does not have** rather
than leaving blank tiles that read like healthy zeros.

The growth projection is the figure worth watching. "4.2 GB" says nothing on
its own; "growing 180 MB/day, 61 days of headroom" is a date to act before.
Once history reaches `DATA_RETENTION_DAYS` the deletions balance the inserts
and the database stops growing, so the projection is only shown while it still
means something.

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

### Finding the health path

Health endpoints are not standardised. Across one fleet you will find
`/health`, Kubernetes-style `/healthz` and `/readyz`, Spring Boot's
`/actuator/health` and a few hand-rolled variants — and someone importing fifty
hosts at once cannot know which is which. Every one of those would otherwise
sit permanently red on a 404 that says nothing about whether the service works.

So when a check returns a status meaning **the path is not there** (404, 405,
410, 501), InfraSight tries the configured candidate paths in order and adopts
the first that answers correctly. The endpoint reports its real state, and
`resolved_health_path` records which path won — shown on the endpoints table
and the detail page, with your configured `url` left exactly as you wrote it.

The discipline that makes this safe is what *doesn't* trigger it:

| Result | Discovery runs? | Why |
|---|:-:|---|
| 404 / 405 / 410 / 501 | **yes** | Nothing is at that path |
| 500, 502, 503 | no | The application is there and broken — that is the answer |
| 401 / 403 | no | The path exists; it is protected |
| Timeout, connection refused, DNS or TLS failure | no | Not a path problem, and probing twelve paths on a dead host wastes the cycle |
| Body-substring mismatch | no | The path exists and returned the wrong thing |

Probing around a 5xx would report a broken service as healthy, which is worse
than the 404 it replaced.

Cost is bounded and paid once. A healthy endpoint makes exactly one request, as
before. A discovered path is stored and probed directly from then on, so the
search does not repeat every interval; it is forgotten and re-run only if that
path later 404s, or if you edit the URL. Each probe gets its own 5-second cap
so a slow host cannot turn one check into twelve timeouts.

Configure it under **Settings → Monitoring**: `health_path_discovery` (on by
default) and `health_path_candidates`. Emptying the candidate list disables
probing without turning the feature off.

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
  "source": "infrasight"
}
```

With a signing secret configured, an `X-InfraSight-Signature: sha256=…` HMAC
over the exact bytes sent lets the receiver verify origin.

PagerDuty uses Events API v2 and *resolves* the incident on recovery rather than
opening a second one.

Channel configuration — webhook URLs, SMTP passwords, routing keys — is stored
as a single encrypted blob. Reading a channel back returns only non-sensitive
parts (`target_host`, `port`, `recipient_count`).

---

## 13. User management and RBAC

Three built-in roles. **Approver** exists only for change management: it is a
viewer who can additionally approve or reject a change, which lets you separate
"who asks" from "who says yes" without handing out admin.

| | Admin | Approver | Viewer |
|---|:-:|:-:|:-:|
| View dashboards, endpoints, SSL, history, incidents | ✅ | ✅ | ✅ |
| Export configuration | ✅ | ✅ | ✅ |
| Read settings | ✅ | ✅ | ✅ |
| Raise and comment on change requests | ✅ | ✅ | ✅ |
| Approve / reject a change | ✅ | ✅ | ❌ |
| Start, complete or fail a deployment | ✅ | ❌ | ❌ |
| Add / edit / delete endpoints | ✅ | ❌ | ❌ |
| Run manual checks | ✅ | ❌ | ❌ |
| Import endpoints | ✅ | ❌ | ❌ |
| Manage users | ✅ | ❌ | ❌ |
| Change configuration, alerts, channels | ✅ | ❌ | ❌ |
| Read audit logs | ✅ | ❌ | ❌ |

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
after a configurable number of failures. Unknown-user and wrong-password
produce an identical response so accounts cannot be enumerated, and a miss
still spends comparable time hashing so response timing does not reveal
existence.

### Session timeout and lockouts

**Settings → Security & sessions**, adjustable at runtime — no redeploy:

| Key | Default | What it does |
|---|---|---|
| `session_timeout_minutes` | 60 | How long a sign-in lasts before the browser renews it |
| `session_refresh_days` | 7 | How long a browser can renew silently before the password is needed again |
| `account_lockout_attempts` | 8 | Consecutive failures that lock an account |
| `account_lockout_minutes` | 15 | How long a lockout lasts if nobody clears it |

The environment variables stay the fallback and the seeded default, so an
instance that never opens this page behaves exactly as before.

**Changing the timeout affects new sessions only.** A token already in a
browser carries its own expiry inside it; shortening the setting cannot reach
back and revoke it. What *does* revoke immediately is bumping the user's
`token_version` — which a password reset, a role change or disabling the
account already does.

**To clear a lockout without waiting it out:** Users → the locked account →
**Clear lockout**. That resets the counter and lets them sign in straight
away, which is almost always what you want when someone is locked out and
standing next to you.

### Audit log

**Audit Logs** (admin) records logins and failures, logout, password changes and
resets, endpoint create/update/delete/check, imports and exports, user and role
changes, configuration changes, tag/environment/channel changes — each with
actor, resource, before/after diff, outcome, IP, user agent and request path.

Entries are append-only, and `username` is denormalised so the trail survives
deletion of the acting user. Details pass through a scrubber, so no
credential-shaped value is ever written.

---

## 14. Change management

Deployments are the single largest source of false alerts in a monitoring tool:
the service goes down because you took it down, an incident opens, everyone is
paged, and the uptime figure for the month is wrong. **Change Management** ties
the deployment to the monitoring so that does not happen.

### The workflow

```
Draft ──submit──▶ Pending approval ──approve──▶ Approved ──start──▶ Deploying ──▶ Completed
  │                      │                          │                  │
  │                      └────reject───▶ Rejected   │                  └──▶ Failed
  └──────────────────── cancel ─────────────────────┴──▶ Cancelled
```

Six states, no more. A change targeting an environment that does **not** require
approval skips straight from *Draft* to *Approved* on submit, so low-risk work
is not slowed down by ceremony.

### What happens to monitoring

| Transition | Effect on monitoring |
|---|---|
| **Start deployment** | Every affected endpoint is paused, its status set to `Paused`, its failure streak cleared, and `pause_reason` set to `Deployment CHG-YYYY-NNNN`. |
| While deploying | The worker's claim query skips paused endpoints, so **no checks run at all** — no results, no incidents, no alerts, and the window never lands in the uptime figures. |
| **Complete** / **Fail** | Only the endpoints *this* change paused are resumed (an endpoint you had already paused by hand stays paused), and each is checked immediately. |

That last point is the one worth understanding: the pause is not a filter
applied after the fact, it stops the check being scheduled. There is nothing to
suppress downstream because nothing is produced.

The immediate post-deployment check is the payoff — you see whether the
deployment actually worked within seconds of marking it complete, rather than at
the next scheduled interval. Its result is stored on the change and shown as
**Post-deployment health check**, including HTTP status, response time and
certificate state per endpoint.

### Guards

- **One deployment at a time** per application + environment. A second `start`
  is refused and names the change already running.
- **A forgotten deployment cannot silence an endpoint forever.** Anything paused
  longer than `change_max_pause_minutes` (default 240) is flagged on the change
  dashboard as *running over*.
- **Approval is server-enforced**, not a UI convention — `start-deployment`
  refuses anything that is not `approved`.
- The **deployer is taken from the authenticated session**, never from the
  request body.
- Every transition writes both a change-activity entry and an audit-log entry.

### Settings

Under **Settings → Change management**:

| Key | Default | What it does |
|---|---|---|
| `change_approval_environments` | `production` | Changes targeting these environments need approval first. Comma-separated. |
| `change_health_check_on_resume` | `true` | Check the affected endpoints the moment monitoring resumes. |
| `change_max_pause_minutes` | `240` | Flag deployments whose pause runs longer than this. |

### Using it

1. **New change** — title, application, environment, risk, description, planned
   date/time and duration, plus the endpoints the deployment touches. Rollback
   plan optional but recommended.
2. **Submit.** Production goes to *Pending approval*; anything else is approved
   automatically.
3. An approver opens **Pending approval** and approves or rejects with a reason.
4. When you actually deploy, hit **Start deployment**. Monitoring pauses.
5. **Complete deployment** (with notes) or **Mark failed** (with a reason).
   Monitoring resumes and is checked immediately.

A paused endpoint shows *why* on the Endpoints table and its detail page, with a
link straight to the change that paused it.

### Endpoints

```
GET    /api/changes                        list, with search + status/application/environment/risk/date filters
GET    /api/changes/dashboard              counts, active, upcoming, overrunning
GET    /api/changes/options                statuses, risks, known applications
POST   /api/changes                        create (draft)
GET    /api/changes/{id}                   detail incl. endpoints, comments, activity, permissions
PUT    /api/changes/{id}                   edit (draft or rejected only)
POST   /api/changes/{id}/submit            draft -> pending approval (or approved)
POST   /api/changes/{id}/approve
POST   /api/changes/{id}/reject            requires a reason
POST   /api/changes/{id}/cancel
POST   /api/changes/{id}/start-deployment  pauses monitoring
POST   /api/changes/{id}/complete          resumes monitoring + health check
POST   /api/changes/{id}/fail              requires a reason; resumes monitoring + health check
POST   /api/changes/{id}/comments
```

`GET /api/changes/{id}` returns server-computed `can_edit`, `can_submit`,
`can_approve`, `can_deploy`, `can_finish`, `can_cancel` and `can_comment`, so the
UI never re-derives the workflow rules and cannot drift from the API.

---

## 15. Diagnose

A red dashboard tells you *that* something is broken. **Diagnose** is the part
that tells you which layer, why we think so, what changed, what to do, and how
to know it worked — the sequence a senior engineer runs through, made explicit.

Open it from any endpoint's detail page.

### It investigates from the outside in

```
DNS → TCP (per resolved address) → TLS → HTTP
```

Each stage is probed separately, so the fault is localised before a word of
prose is read. "TLS fine, HTTP 502" is a completely different problem from "TCP
refused", even though both show as DOWN on the dashboard. The report names the
deepest layer that still worked.

Then it reasons over everything else the platform already knows: the endpoint's
own history, its open incident, deployments from Change Management, how the
rest of its application is doing, sibling endpoints on the same host, and every
previous diagnosis of this endpoint.

### It ranks causes rather than asserting one

A 502 four minutes after a production release has an obvious leading
explanation and two plausible others. Presenting only the leader hides the fact
that it might be wrong, so every candidate is listed with the evidence that
scored it:

```
Most likely   The reverse proxy cannot reach its upstream        62% of evidence
              • HTTP 502 returned by the edge
              • TLS terminated cleanly, so the edge is serving normally

Possible      The CHG-2026-0018 deployment                       28% of evidence
              • completed 4 minutes before the failure started

Less likely   Resource saturation                                10% of evidence
```

The percentage is a share of accumulated evidence weight, and is labelled as
that — not as a probability. An engineer who disagrees can see exactly which
signal to challenge.

### Confidence is computed, not asserted

**High** requires both more than one independent signal *and* a clear margin
over the runner-up. A single observation with nothing contradicting it is
**Medium** — one probe can mislead, and sounding certain on the strength of it
is the failure mode that costs an hour. Two explanations fitting equally well
lowers confidence rather than picking one.

### It never invents infrastructure

This is the rule that matters most. InfraSight watches an endpoint from the
outside; it has no view of pods, containers, CPU, memory or databases. So it
says so, in a **Not observable** section as prominent as the evidence:

| | |
|---|---|
| Container / pod state | Not visible from InfraSight |
| Host resources | Not visible from InfraSight |
| Application logs | Not visible from InfraSight |
| Upstream dependencies | Not modelled |

Every statement in the report is tagged **Observed**, **Inferred** or **Not
checked**. A tool with no cluster access reporting "Pod is CrashLoopBackOff" is
a guess wearing a fact's clothing, and an engineer who trusts it loses an hour.
`kubectl` and `docker` commands are still suggested where they would help — but
prefixed `IF this runs on Kubernetes`, as suggestions rather than observations.

### Actions are ordered by risk, and it never runs them

Safest first, so someone who stops after step two has still done the sensible
thing. Each carries its blast radius:

| Band | Meaning |
|---|---|
| **Safe** | Read-only. Checking logs, status, certificates, resource usage. |
| **Disruptive** | Briefly interrupts service. Restarting, reloading, scaling. |
| **High risk** | Can cause an outage or lose data. Rollback, deleting resources, firewall changes. |

Nothing is ever executed automatically, and high-risk steps say so explicitly.
Where a rollback is the leading hypothesis, the advice is still to read the
logs first — rolling back on timing alone discards the evidence of what
actually broke.

### It detects what a single probe cannot

- **Intermittent failure.** A ✓/✗ strip of the last 30 checks with an
  availability figure. An endpoint that passes right now but failed 9 of the
  last 30 is not healthy, and a naive check would have said it was.
- **Performance degradation.** Current response time against the endpoint's own
  24-hour median, excluding the most recent checks so present slowness cannot
  hide inside its own baseline. Still HTTP 200, 12x slower, is worth knowing.
- **Recurrence.** How many times this same verdict has come back in 30 days,
  with whatever resolution was recorded last time shown verbatim.
- **Blast radius.** Whether every endpoint of the application is down (an
  outage) or just this one (a fault) — which changes the severity.

### Severity

`INFO` · `LOW` · `MEDIUM` · `HIGH` · `CRITICAL`, computed from the verdict, the
environment, blast radius, availability and certificate expiry. Production
weighs heavily: the same 502 is a different problem in staging than on the host
customers are using. Intermittent failure is deliberately rated High — it is
harder to catch than a clean outage and usually ignored until it becomes one.

### Deployment awareness

If a deployment is **in progress**, Diagnose says so and stops. Monitoring is
paused, the state is expected, and there is nothing to diagnose — no false
incident, no wasted investigation.

If one **completed recently**, the gap between it finishing and the failure
starting is measured and weighted: under 10 minutes is strong evidence, up to
30 moderate, up to 90 circumstantial. The wording is always *correlation*,
never causation.

### Verification, and the loop

Every diagnosis ends with concrete success criteria — expected status, the
numeric latency target from the endpoint's own baseline, the open incident
closing, and *N* consecutive passing checks rather than one. **Re-diagnose**
then runs it again and shows the before/after:

```
BEFORE   HTTP 502, 2.8s      →   NOW   HTTP 200, 210ms
                                       Issue appears resolved
```

### Diagnosis history

Every diagnosis is stored — conclusion only, never the raw probe payloads.
That is what makes "the fourth time this month" visible. You can record what
actually fixed it:

```
POST /api/endpoints/{id}/diagnoses/{diagnosis_id}/resolution
```

and the next time the same verdict comes back on that endpoint, your note is
surfaced verbatim. It is the one field the engine cannot derive, and the one
that turns a pile of diagnoses into something the next person on call can use.

### Endpoints

```
POST /api/endpoints/{id}/diagnose?focus=auto    run a diagnosis (endpoint:check)
GET  /api/endpoints/{id}/diagnoses              past diagnoses   (endpoint:read)
POST /api/endpoints/{id}/diagnoses/{did}/resolution   record the fix
```

`focus` accepts `auto` (default), `endpoint`, `ssl`, `availability`,
`performance`, `recent_failure` or `deployment_impact`.

Diagnose makes live outbound requests, so it needs `endpoint:check`. It writes
**nothing** to the monitoring history — diagnosing an endpoint never distorts
its uptime figures.

---

## 16. RCA management

Lightweight root-cause analysis, and the operational intelligence that feeds
it. All of it computed on this server, from this server's own database.

### No external AI, and none needed

There is no LLM, no API key, and no outbound call. The intelligence is rules,
statistics, historical data, correlation, pattern detection and weighted
scoring — the same engine described in [Diagnose](#15-diagnose). InfraSight
runs with no internet access beyond reaching the endpoints it monitors, and
`docker compose up -d` still starts everything.

That is a design choice, not a limitation. Infrastructure data — hostnames,
topology, failure modes — never leaves the machine, and a diagnosis is
reproducible: the same evidence always yields the same conclusion, which is
what makes it auditable.

### RCA is optional, and never in the way

The single most important rule in this section. An RCA does not gate incident
resolution, incident closure, deployment completion or monitoring restoration.
Completing an RCA changes nothing about the incident.

```
Incident:  Detected → Investigating → Resolved → Closed
RCA:       Not requested → Pending → In progress → Completed
```

Those run independently, so **Incident: CLOSED, RCA: PENDING** is a normal,
valid state. A process that holds up recovery for paperwork is a process
people learn to route around — and then the paperwork stops happening at all.

Every incident offers three answers: **Request RCA**, **Not required**, or
nothing at all. "Not required" is recorded with a reason, because *we looked
and decided not to* is a different state from *nobody has looked*, and only the
first should leave the pending queue.

### Ownership without a new role or a team table

An RCA belongs to a person **or** a team. Teams are the free-text labels
already used on endpoints, now also on users — `DevOps`, `Platform`, `Backend`.
No team table, no membership screen, no extra role.

| Who | Can |
|---|---|
| Admin | Everything |
| Anyone with `incident:write` | Request, assign, edit, complete |
| **The assigned owner** | Edit and complete their own, whatever their role |
| Anyone with `incident:read` | View, and comment |

That third row is the point: a **viewer** assigned an RCA — personally, or via
their team label — can complete it. Assigning work to someone who then cannot
do it is the failure mode this avoids.

### The form is deliberately small

Incident · Owner · Root cause · Category · Impact · Resolution · Preventive
actions · Timeline. Only **root cause** and **resolution** are required, and
only to *complete* — partial work saves freely, because an RCA written over
three days by two people is the common case.

### Generate RCA draft

Not AI-generated, and not called that. **Generate draft** assembles a starting
point from records this server already holds:

- the incident — timing, duration, failure reason, error, failed check count
- the [Diagnose](#15-diagnose) verdict run while it was open, if there was one
- the deployment that completed before it, and the gap in minutes
- monitoring history either side, for a latency baseline
- the incident comments

Where the data does not support a statement it says so, in the same words every
time:

```
Not available from monitoring data.
```

So an incident with no diagnosis produces a draft that says the cause was never
established — rather than a confident guess that outlives the incident and
misleads whoever reads it next. The banner says **"Generated from available
monitoring and incident data. Review before saving."** and the owner edits it.

### Timeline

Built automatically from real events, and every entry names its source, so a
derived fact never reads like something a person wrote:

```
23:30  Deployment      CHG-2026-0018 started by rishabh (Payment API)
23:38  Deployment      CHG-2026-0018 completed
23:42  Monitoring      Endpoint became unhealthy — Unexpected HTTP status 502
23:44  Diagnose        Upstream unavailable (high confidence)
23:50  Comment         amit: Rollback started
23:53  Monitoring      Endpoint recovered, incident closed automatically
```

Editable afterwards — the parts a human remembers (when the rollback was
decided, who was called) are exactly the ones no database holds.

### Comments

Incidents now have threaded comments. The investigation happens in
conversation, and that conversation is the raw material of the RCA — keeping it
on the incident rather than in chat means the owner inherits it instead of
reconstructing it. Anyone who can see the incident can comment.

### Reporting

Built **only** from stored RCA records, so an empty section means nobody has
written it yet — never that nothing happened.

- Completion rate, and average days to complete
- Top root-cause categories as a percentage
- Incidents by owner and by application
- Deployment-related share
- **Recurring root causes** — grouped by the written cause, not just the
  category, because "Database connection exhaustion" recorded five times by
  three people is the most valuable pattern this data holds and is invisible if
  you only count categories

### Similar past incidents

An RCA shows completed RCAs for comparable past incidents — same endpoint
first, then same application. Labelled as historical context, never as
evidence about the current one.

### Endpoints

```
GET  /api/rca                         list, with search + status/application/category filters
GET  /api/rca/dashboard               counts and the pending queue
GET  /api/rca/analytics               reporting, from stored records only
GET  /api/rca/options                 statuses, categories, teams, applications
GET  /api/rca/{id}                    details, comments, similar past RCAs
PUT  /api/rca/{id}                    save partial work
POST /api/rca/{id}/assign             to a person or a team
POST /api/rca/{id}/draft              generate a draft locally
POST /api/rca/{id}/complete           requires root cause + resolution

GET  /api/incidents/{id}/rca          null when none — the normal state
POST /api/incidents/{id}/rca          request one
POST /api/incidents/{id}/rca/not-required
GET  /api/incidents/{id}/comments
POST /api/incidents/{id}/comments
```

### Smart DevOps summary, and infrastructure search

Both on the dashboard, both computed locally.

The **summary** answers *what needs my attention* — a health score with its
four measured components and the plain reasons behind it, counts of critical
and degraded services, SSL attention, deployments, performance anomalies and
pending RCAs, plus a prioritised attention list ordered by environment, failure
kind and impact. Below it, a **daily operations summary** with what happened in
the last 24 hours and the one finding worth leading with.

**Search infrastructure** is a deterministic parser over a fixed vocabulary,
not a language model:

```
production services that are down
SSL certificates expiring in 30 days
endpoints with latency above 1 second
failed deployments this week
incidents without RCA
currently paused endpoints
RCA pending for more than 7 days
applications with recurring incidents
```

It shows **what it understood** above the results, so a misread question is
obvious rather than silently wrong — and a question it does not recognise says
so and lists what it can answer, instead of guessing. Nothing typed there
leaves the server, and the same question always returns the same rows.

### Configuration

**Settings → RCA** and **Settings → Monitoring**:

| Key | Default | What it does |
|---|---|---|
| `rca_reminder_days` | 7 | Highlight an RCA open longer than this |
| `rca_default_due_days` | 0 | Default deadline; 0 means none, and an RCA with no deadline is never overdue |
| `latency_anomaly_multiplier` | 3.0 | How much slower than baseline counts as degradation |
| `recovery_checks_required` | 3 | Consecutive passing checks before calling something recovered |
| `deployment_correlation_minutes` | 30 | Window for reporting a deployment/failure correlation |
| `incident_grouping_minutes` | 15 | Window for treating repeated failures as one problem |

---

## 17. API reference

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

## 18. Local development

### Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Just the dependencies from Compose
docker compose up -d postgres redis

export DATABASE_URL="postgresql+asyncpg://infrasight:yourpassword@localhost:5432/infrasight"
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

## 19. Testing

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
| `test_checker.py` | Healthy, down, timeouts, TLS errors, DNS failure, HTTP mismatch, degraded, body matching, auth headers, redirects, health-path discovery and the failures it must NOT probe around |
| `test_ssl.py` | Valid / expiring / critical / expired / invalid / self-signed / wildcard / hostname-mismatch certificates, chains, threshold classification |
| `test_monitoring_state.py` | One incident per outage, recovery and downtime, alert generation, cooldown, certificate rotation, re-grading |
| `test_endpoints_api.py` | CRUD, validation, duplicates, credential handling, filters, sorting, pagination, bulk actions |
| `test_import_export.py` | Valid/invalid CSV, missing fields, duplicates, aliases, Excel, idempotent re-import, credential-free export |
| `test_rca.py` | RCA never blocking the incident, team ownership without a new role, drafts that never invent a fact, and a search parser that refuses what it cannot parse |
| `test_diagnostics.py` | The Diagnose reasoning layer: evidence ranking, confidence bands, severity classification, and the honesty rules that stop it inventing infrastructure it cannot see |
| `test_health_and_settings.py` | Probes, security headers, settings validation, user management invariants, channels, OpenAPI completeness |
| `test_changes.py` | The change workflow and its effect on monitoring: approval routing, self-approval refused, pause on deploy, resume on complete/fail, an already-paused endpoint left alone, concurrent-deployment conflict, activity timeline |

HTTP responses are stubbed with `respx`, and certificates are generated
in-process with `cryptography`, so the valid/expiring/expired/invalid cases are
real X.509 material going through the same parsing path a live handshake uses.

---

## 20. Backup and recovery

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
  -U infrasight -d infrasight -Fc \
  > "backups/infrasight-$(date +%F-%H%M).dump"

# Plain SQL
docker compose exec -T postgres pg_dump -U infrasight -d infrasight \
  > "backups/infrasight-$(date +%F).sql"

# Configuration only (no history) - handy for seeding another instance
curl -s "http://localhost:8080/api/export?format=csv" \
  -H "Authorization: Bearer $TOKEN" -o backups/endpoints.csv

# And keep the secrets somewhere safe
cp .env backups/env-$(date +%F).bak     # store encrypted, never in git
```

A nightly cron entry:

```cron
0 2 * * * cd /opt/infrasight && docker compose exec -T postgres pg_dump -U infrasight -d infrasight -Fc > backups/infrasight-$(date +\%F).dump && find backups -name '*.dump' -mtime +30 -delete
```

### Restore

```bash
# 1. Stop the writers; leave postgres running
docker compose stop backend worker

# 2. Recreate the database
docker compose exec -T postgres psql -U infrasight -d postgres \
  -c "DROP DATABASE IF EXISTS infrasight;" -c "CREATE DATABASE infrasight;"

# 3. Restore
docker compose exec -T postgres pg_restore -U infrasight -d infrasight --clean --if-exists \
  < backups/infrasight-2026-09-03-0200.dump
# for a plain SQL dump:
#   docker compose exec -T postgres psql -U infrasight -d infrasight < backups/infrasight-2026-09-03.sql

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
docker run --rm -v infrasight_postgres_data:/data -v "$PWD/backups:/backup" \
  alpine tar czf /backup/pgdata-$(date +%F).tar.gz -C /data .
docker compose up -d
```

---

## 21. Production deployment

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

## 22. Kubernetes considerations

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

## 23. Troubleshooting

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

## 24. Security notes

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

## 25. Project structure

```
infrasight/
├── backend/
│   ├── alembic/
│   │   ├── env.py
│   │   └── versions/                     0001 initial schema,
│   │                                     0002 change management,
│   │                                     0003 health-path discovery,
│   │                                     0004 diagnosis history,
│   │                                     0005 RCA management,
│   │                                     0006 worker resource stats
│   ├── app/
│   │   ├── api/
│   │   │   ├── deps.py                       auth, RBAC, pagination
│   │   │   ├── health.py                     /health /ready /live /api/workers
│   │   │   └── v1/                           auth, endpoints, dashboard,
│   │   │                                     incidents, taxonomy, users,
│   │   │                                     settings, importexport, changes,
│   │   │                                     rca + intelligence
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
│   │   │                                     import/export, retention, settings,
│   │   │                                     diagnostics + reasoning, changes,
│   │   │                                     rca + draft, insights
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
│   │   ├── components/                       ui, charts, EndpointForm,
│   │   │                                     DiagnosticsPanel, ChangeForm
│   │   ├── hooks/                            useAuth, useToast
│   │   ├── layouts/AppLayout.jsx
│   │   ├── lib/                              api client, formatters
│   │   ├── pages/                            20 screens
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
