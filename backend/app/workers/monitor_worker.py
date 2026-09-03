"""Monitoring worker.

A separate process from the API. It repeatedly:

1. claims a batch of endpoints whose next check is due,
2. probes them concurrently under a bounded semaphore,
3. records each result, applies status transitions, manages incidents and
   raises alerts,
4. schedules the next check with jitter.

Claiming uses ``SELECT ... FOR UPDATE SKIP LOCKED`` plus a short lease, so any
number of worker replicas can run against the same database without ever
checking the same endpoint twice, and a worker that dies mid-batch releases its
endpoints when the lease expires rather than stranding them.

Two periodic tasks run alongside the check loop: a retention sweep and an SSL
re-grade that keeps ``days_remaining`` accurate (and fires expiry alerts) even
for endpoints on long intervals.
"""

from __future__ import annotations

import asyncio
import os
import platform
import signal
import socket
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import SessionFactory, dispose_engine
from app.core.logging import configure_logging, get_logger
from app.models.endpoint import Endpoint
from app.models.monitoring import WorkerHeartbeat
from app.services import (
    alert_service,
    monitoring_service,
    retention_service,
    settings_service,
)

configure_logging()
logger = get_logger(__name__)

WORKER_VERSION = "1.0.0"
SSL_SWEEP_INTERVAL_SECONDS = 3600


def _now() -> datetime:
    return datetime.now(timezone.utc)


class MonitorWorker:
    def __init__(self) -> None:
        self.worker_id = (
            settings.WORKER_ID
            or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        )[:64]
        self.concurrency = max(1, settings.WORKER_CONCURRENCY)
        self._semaphore = asyncio.Semaphore(self.concurrency)
        self._shutdown = asyncio.Event()
        self._started_at = _now()
        self._checks_completed = 0
        self._checks_failed = 0
        self._in_flight = 0

    # ------------------------------------------------------------ signals
    def install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.request_shutdown, sig.name)
            except NotImplementedError:
                # Windows/ProactorEventLoop: fall back to the default handler.
                signal.signal(sig, lambda *_: self.request_shutdown(sig.name))

    def request_shutdown(self, reason: str = "signal") -> None:
        if not self._shutdown.is_set():
            logger.info("worker_shutdown_requested", reason=reason)
            self._shutdown.set()

    # -------------------------------------------------------- heartbeats
    async def _write_heartbeat(self) -> None:
        async with SessionFactory() as session:
            try:
                row = (
                    await session.execute(
                        select(WorkerHeartbeat).where(
                            WorkerHeartbeat.worker_id == self.worker_id
                        )
                    )
                ).scalar_one_or_none()
                if row is None:
                    session.add(
                        WorkerHeartbeat(
                            worker_id=self.worker_id,
                            started_at=self._started_at,
                            last_seen_at=_now(),
                            checks_completed=self._checks_completed,
                            checks_failed=self._checks_failed,
                            in_flight=self._in_flight,
                            version=WORKER_VERSION,
                            hostname=platform.node()[:128],
                        )
                    )
                else:
                    row.last_seen_at = _now()
                    row.checks_completed = self._checks_completed
                    row.checks_failed = self._checks_failed
                    row.in_flight = self._in_flight
                    row.version = WORKER_VERSION
                await session.commit()
            except SQLAlchemyError as exc:
                await session.rollback()
                logger.warning("heartbeat_write_failed", error=str(exc))

    async def _retire_dead_workers(self) -> None:
        """Delete heartbeats belonging to workers that are gone for good.

        A worker takes a new identity whenever its container is recreated, so
        without this the table accumulates one orphan row per rebuild and
        /health reports "degraded" forever even though the live worker is fine.
        """
        cutoff = _now() - timedelta(seconds=settings.WORKER_RETIRE_AFTER_SECONDS)
        try:
            async with SessionFactory() as session:
                result = await session.execute(
                    delete(WorkerHeartbeat).where(
                        WorkerHeartbeat.last_seen_at < cutoff,
                        WorkerHeartbeat.worker_id != self.worker_id,
                    )
                )
                await session.commit()
                if result.rowcount:
                    logger.info(
                        "retired_dead_workers", removed=int(result.rowcount)
                    )
        except SQLAlchemyError as exc:
            logger.warning("worker_retire_failed", error=str(exc))

    async def _heartbeat_loop(self) -> None:
        # Sweep once at startup so a restart immediately clears the row its
        # predecessor left behind.
        await self._retire_dead_workers()
        cycles = 0
        while not self._shutdown.is_set():
            await self._write_heartbeat()
            cycles += 1
            # Roughly every 5 minutes at the default 15s heartbeat.
            if cycles % 20 == 0:
                await self._retire_dead_workers()
            try:
                await asyncio.wait_for(
                    self._shutdown.wait(), timeout=settings.WORKER_HEARTBEAT_SECONDS
                )
            except asyncio.TimeoutError:
                continue

    # ------------------------------------------------------------ claiming
    async def _claim_due_endpoints(self, limit: int) -> list[uuid.UUID]:
        """Reserve up to ``limit`` due endpoints for this worker.

        Row locks are taken with SKIP LOCKED so concurrent workers claim
        disjoint sets without blocking each other, and a lease is stamped so a
        crashed worker's endpoints become claimable again.
        """
        now = _now()
        async with SessionFactory() as session:
            try:
                candidate_stmt = (
                    select(Endpoint.id)
                    .where(
                        Endpoint.monitoring_enabled.is_(True),
                        Endpoint.is_paused.is_(False),
                        (Endpoint.next_check_at.is_(None))
                        | (Endpoint.next_check_at <= now),
                        (Endpoint.lease_expires_at.is_(None))
                        | (Endpoint.lease_expires_at < now),
                    )
                    .order_by(Endpoint.next_check_at.asc().nulls_first())
                    .limit(limit)
                )
                dialect = session.bind.dialect.name if session.bind else ""
                if dialect == "postgresql":
                    candidate_stmt = candidate_stmt.with_for_update(
                        skip_locked=True, of=Endpoint
                    )

                ids = list((await session.execute(candidate_stmt)).scalars().all())
                if not ids:
                    await session.commit()
                    return []

                # Lease long enough to cover the slowest possible check plus
                # the time spent writing its result.
                lease_until = now + timedelta(
                    seconds=max(60, settings.DEFAULT_TIMEOUT * 3 + 60)
                )
                await session.execute(
                    update(Endpoint)
                    .where(Endpoint.id.in_(ids))
                    .values(lease_expires_at=lease_until, leased_by=self.worker_id)
                )
                await session.commit()
                return ids
            except SQLAlchemyError as exc:
                await session.rollback()
                logger.error("claim_failed", error=str(exc))
                return []

    # -------------------------------------------------------------- checks
    async def _check_and_record(self, endpoint_id: uuid.UUID, config: dict) -> None:
        """Run one endpoint's check in its own session and transaction."""
        async with self._semaphore:
            self._in_flight += 1
            try:
                async with SessionFactory() as session:
                    endpoint = (
                        await session.execute(
                            select(Endpoint)
                            .options(
                                selectinload(Endpoint.tags),
                                selectinload(Endpoint.environment),
                            )
                            .where(Endpoint.id == endpoint_id)
                        )
                    ).scalars().unique().one_or_none()

                    if endpoint is None:
                        # Deleted between claim and execution.
                        return
                    if not endpoint.monitoring_enabled or endpoint.is_paused:
                        endpoint.lease_expires_at = None
                        endpoint.leased_by = None
                        endpoint.next_check_at = None
                        await session.commit()
                        return

                    try:
                        outcome = await monitoring_service.execute_check(
                            endpoint, config
                        )
                        await monitoring_service.record_check_result(
                            session,
                            endpoint,
                            outcome,
                            config=config,
                            checked_by=self.worker_id,
                            is_manual=False,
                        )
                        self._checks_completed += 1
                        if not outcome.is_up:
                            self._checks_failed += 1
                    finally:
                        # Always re-arm the schedule and drop the lease, even
                        # if recording raised - otherwise a persistently
                        # failing endpoint would be retried in a tight loop.
                        endpoint.next_check_at = monitoring_service.next_check_time(
                            endpoint.interval_seconds
                        )
                        endpoint.lease_expires_at = None
                        endpoint.leased_by = None

                    await session.commit()
            except Exception as exc:
                logger.error(
                    "check_cycle_error",
                    endpoint_id=str(endpoint_id),
                    error=str(exc),
                    exc_info=True,
                )
                await self._release_lease(endpoint_id)
            finally:
                self._in_flight -= 1

    async def _release_lease(self, endpoint_id: uuid.UUID) -> None:
        """Best-effort lease release after a failed check cycle."""
        try:
            async with SessionFactory() as session:
                await session.execute(
                    update(Endpoint)
                    .where(Endpoint.id == endpoint_id)
                    .values(
                        lease_expires_at=None,
                        leased_by=None,
                        next_check_at=_now() + timedelta(seconds=60),
                    )
                )
                await session.commit()
        except SQLAlchemyError as exc:  # pragma: no cover
            logger.warning(
                "lease_release_failed", endpoint_id=str(endpoint_id), error=str(exc)
            )

    async def _run_cycle(self) -> int:
        async with SessionFactory() as session:
            config = await settings_service.load_settings(session)

        # Never claim more than the concurrency budget can absorb at once;
        # otherwise leases start expiring while work is still queued.
        limit = min(settings.WORKER_BATCH_SIZE, self.concurrency * 4)
        ids = await self._claim_due_endpoints(limit)
        if not ids:
            return 0

        logger.debug("cycle_claimed", count=len(ids))
        await asyncio.gather(
            *(self._check_and_record(endpoint_id, config) for endpoint_id in ids),
            return_exceptions=True,
        )
        return len(ids)

    async def _check_loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                processed = await self._run_cycle()
            except Exception as exc:
                logger.error("cycle_failed", error=str(exc), exc_info=True)
                processed = 0

            # A full batch means there is probably more work waiting, so poll
            # again immediately rather than idling.
            if processed >= min(settings.WORKER_BATCH_SIZE, self.concurrency * 4):
                continue
            try:
                await asyncio.wait_for(
                    self._shutdown.wait(),
                    timeout=settings.WORKER_POLL_INTERVAL_SECONDS,
                )
            except asyncio.TimeoutError:
                continue

    # ---------------------------------------------------- periodic tasks
    async def _retention_loop(self) -> None:
        # Stagger the first sweep so several replicas starting together do not
        # all begin deleting at the same moment.
        await self._sleep_or_stop(30 + (os.getpid() % 60))
        while not self._shutdown.is_set():
            try:
                async with SessionFactory() as session:
                    config = await settings_service.load_settings(
                        session, use_cache=False
                    )
                    await retention_service.run_retention_sweep(session, config)
            except Exception as exc:
                logger.error("retention_sweep_error", error=str(exc))
            await self._sleep_or_stop(settings.RETENTION_SWEEP_INTERVAL_SECONDS)

    async def _ssl_sweep_loop(self) -> None:
        """Keep certificate states current between checks.

        ``days_remaining`` is recomputed from the stored expiry, and expiry
        alerts are raised here as well - otherwise an endpoint on a one-hour
        interval could cross the warning threshold and stay silent until its
        next check.
        """
        await self._sleep_or_stop(45)
        while not self._shutdown.is_set():
            try:
                async with SessionFactory() as session:
                    config = await settings_service.load_settings(
                        session, use_cache=False
                    )
                    warning_days = int(config.get("ssl_warning_days", 30))
                    critical_days = int(config.get("ssl_critical_days", 7))
                    updated = await monitoring_service.regrade_certificates(
                        session,
                        warning_days=warning_days,
                        critical_days=critical_days,
                    )
                    await session.commit()

                    if config.get("alerts_enabled", True):
                        await self._raise_expiry_alerts(session, config)
                        await session.commit()

                    if updated:
                        logger.info("ssl_sweep_completed", regraded=updated)
            except Exception as exc:
                logger.error("ssl_sweep_error", error=str(exc))
            await self._sleep_or_stop(SSL_SWEEP_INTERVAL_SECONDS)

    async def _raise_expiry_alerts(self, session, config: dict) -> None:
        """Alert on certificates now inside a warning window.

        The alert cooldown keeps this from re-notifying every hour for the
        same certificate.
        """
        from app.core.enums import SslStatus
        from app.models.monitoring import SslCertificate

        alertable = (
            SslStatus.EXPIRING_SOON.value,
            SslStatus.CRITICAL.value,
            SslStatus.EXPIRED.value,
            SslStatus.INVALID.value,
        )
        rows = (
            await session.execute(
                select(SslCertificate, Endpoint)
                .join(Endpoint, Endpoint.id == SslCertificate.endpoint_id)
                .options(
                    selectinload(Endpoint.tags), selectinload(Endpoint.environment)
                )
                .where(
                    SslCertificate.is_current.is_(True),
                    SslCertificate.status.in_(alertable),
                    Endpoint.ssl_monitoring_enabled.is_(True),
                    Endpoint.alerts_enabled.is_(True),
                )
            )
        ).unique().all()

        for certificate, endpoint in rows:
            # Reuse the same evaluation the checker uses by adapting the row
            # into the shape evaluate_ssl_alert expects.
            info = _CertificateView(certificate)
            await alert_service.evaluate_ssl_alert(
                session, endpoint, info, config=config
            )

    async def _sleep_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._shutdown.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return

    # ----------------------------------------------------------- lifecycle
    async def run(self) -> None:
        self.install_signal_handlers()
        logger.info(
            "worker_started",
            worker_id=self.worker_id,
            concurrency=self.concurrency,
            poll_interval=settings.WORKER_POLL_INTERVAL_SECONDS,
            batch_size=settings.WORKER_BATCH_SIZE,
            version=WORKER_VERSION,
        )

        tasks = [
            asyncio.create_task(self._heartbeat_loop(), name="heartbeat"),
            asyncio.create_task(self._check_loop(), name="checks"),
            asyncio.create_task(self._retention_loop(), name="retention"),
            asyncio.create_task(self._ssl_sweep_loop(), name="ssl-sweep"),
        ]

        try:
            await self._shutdown.wait()
        finally:
            logger.info(
                "worker_draining",
                in_flight=self._in_flight,
                checks_completed=self._checks_completed,
            )
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

            # Release anything still leased so a restart picks it up at once
            # rather than after the lease expires.
            try:
                async with SessionFactory() as session:
                    await session.execute(
                        update(Endpoint)
                        .where(Endpoint.leased_by == self.worker_id)
                        .values(lease_expires_at=None, leased_by=None)
                    )
                    await session.commit()
            except SQLAlchemyError as exc:  # pragma: no cover
                logger.warning("lease_cleanup_failed", error=str(exc))

            await dispose_engine()
            logger.info(
                "worker_stopped",
                worker_id=self.worker_id,
                checks_completed=self._checks_completed,
                checks_failed=self._checks_failed,
            )


class _CertificateView:
    """Adapter presenting a stored certificate row as a CertificateInfo.

    Lets the SSL sweep reuse the same alert evaluation as a live check without
    duplicating the message-building logic.
    """

    __slots__ = (
        "status",
        "days_remaining",
        "valid_to",
        "issuer_common_name",
        "issuer",
        "common_name",
        "verification_status",
        "verification_error",
        "error",
    )

    def __init__(self, row) -> None:
        self.status = row.status
        self.days_remaining = row.days_remaining
        self.valid_to = row.valid_to
        self.issuer_common_name = row.issuer_common_name
        self.issuer = row.issuer
        self.common_name = row.common_name
        self.verification_status = row.verification_status
        self.verification_error = row.verification_error
        self.error = None


async def main() -> None:
    if not settings.WORKER_ENABLED:
        logger.warning(
            "worker_disabled",
            detail="WORKER_ENABLED is false; exiting without monitoring anything",
        )
        return
    worker = MonitorWorker()
    await worker.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:  # pragma: no cover
        pass
