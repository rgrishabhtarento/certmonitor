"""Self-monitoring schemas.

InfraSight watching its own services. Every field here is either self-reported
by a process this project runs, read from a service over its normal protocol,
or measured from a filesystem the API container can already see - see
``app/services/resource_service.py`` for why the Docker socket is deliberately
not involved.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class DiskUsage(BaseModel):
    """Free space on the filesystem this container writes to.

    On the bundled Compose topology that is the same host device the postgres
    volume lives on, so it is the number that decides whether the database can
    keep writing. ``path`` is returned so the reading can be checked against a
    setup where postgres has been moved elsewhere.
    """

    path: str
    available: bool
    total_gb: float | None = None
    used_gb: float | None = None
    free_gb: float | None = None
    used_percent: float | None = None
    error: str | None = None


class TableSize(BaseModel):
    name: str
    bytes: int
    pretty: str
    rows: int


class DatabaseUsage(BaseModel):
    available: bool
    size_bytes: int | None = None
    size_pretty: str | None = None
    tables: list[TableSize] = Field(default_factory=list)
    connections: int | None = None
    max_connections: int | None = None
    cache_hit_percent: float | None = None

    oldest_result_at: datetime | None = None
    monitoring_results: int | None = None
    growth_bytes_per_day: int | None = None
    retention_days: int | None = None
    # Once history reaches the retention window, deletions balance inserts and
    # the database stops growing. A projection past that point is meaningless,
    # so this says which regime the figure belongs to.
    at_steady_state: bool = False
    error: str | None = None


class RedisUsage(BaseModel):
    """Redis reports its own memory and CPU through INFO.

    Optional infrastructure - the application falls back to in-process rate
    limiting - so unavailable is a state, not a fault.
    """

    available: bool
    reason: str | None = None
    version: str | None = None
    memory_mb: float | None = None
    memory_limit_mb: float | None = None
    memory_percent: float | None = None
    cpu_seconds: float | None = None
    connected_clients: int | None = None
    uptime_seconds: int | None = None
    keys: int | None = None


class ProcessUsage(BaseModel):
    """cgroup CPU and memory for one of our own containers.

    ``cpu_percent`` is null on the first reading of a process: a rate needs
    two samples, and reporting zero would be a lie rather than a gap.
    """

    cpu_percent: float | None = None
    cpu_cores: float | None = None
    memory_mb: float | None = None
    memory_limit_mb: float | None = None
    memory_percent: float | None = None


class WorkerUsage(ProcessUsage):
    worker_id: str
    hostname: str | None = None
    last_seen_at: datetime
    seconds_since_heartbeat: int
    healthy: bool
    uptime_seconds: int
    checks_completed: int
    checks_failed: int
    in_flight: int


class NotMeasured(BaseModel):
    """Something a resource view would normally show, and why this one cannot.

    Listed rather than left as a blank tile, so nobody reads a gap as a
    healthy reading.
    """

    service: str
    reason: str


class ResourceSnapshot(BaseModel):
    generated_at: datetime
    disk: DiskUsage
    database: DatabaseUsage
    redis: RedisUsage
    api: ProcessUsage
    workers: list[WorkerUsage] = Field(default_factory=list)
    # Only computed while the database is still growing - see
    # DatabaseUsage.at_steady_state.
    days_until_disk_full: int | None = None
    not_measured: list[NotMeasured] = Field(default_factory=list)
