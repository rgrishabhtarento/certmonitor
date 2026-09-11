"""The worker's claim logic.

The suite runs against SQLite (see conftest.py), which does not support
``SELECT ... FOR UPDATE SKIP LOCKED`` - the worker itself only applies that
clause when the dialect is ``postgresql`` (monitor_worker.py,
``_claim_due_endpoints``). So real cross-connection row-lock contention isn't
something this suite can exercise; what it can and does verify is the
lease-based exclusion the locking exists to make safe: a claim stamps a
lease, a second claim while that lease is still valid must not re-select the
same endpoint, and a claim after the lease expires must.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.workers.monitor_worker import MonitorWorker


@pytest.fixture
def worker():
    return MonitorWorker()


class TestClaimFiltering:
    async def test_a_newly_created_endpoint_is_immediately_claimable(
        self, session, endpoint_factory, worker
    ):
        endpoint = await endpoint_factory()

        claimed = await worker._claim_due_endpoints(limit=10)

        assert endpoint.id in claimed

    async def test_a_paused_endpoint_is_not_claimed(
        self, session, endpoint_factory, worker
    ):
        endpoint = await endpoint_factory(is_paused=True)

        claimed = await worker._claim_due_endpoints(limit=10)

        assert endpoint.id not in claimed

    async def test_a_disabled_endpoint_is_not_claimed(
        self, session, endpoint_factory, worker
    ):
        endpoint = await endpoint_factory(monitoring_enabled=False)

        claimed = await worker._claim_due_endpoints(limit=10)

        assert endpoint.id not in claimed

    async def test_an_endpoint_not_yet_due_is_not_claimed(
        self, session, endpoint_factory, worker
    ):
        endpoint = await endpoint_factory()
        endpoint.next_check_at = datetime.now(timezone.utc) + timedelta(hours=1)
        await session.commit()

        claimed = await worker._claim_due_endpoints(limit=10)

        assert endpoint.id not in claimed


class TestLeaseExclusion:
    """What SKIP LOCKED protects against on Postgres, verified without it."""

    async def test_a_valid_lease_excludes_the_endpoint_from_a_second_claim(
        self, session, endpoint_factory, worker
    ):
        endpoint = await endpoint_factory()

        first_claim = await worker._claim_due_endpoints(limit=10)
        assert endpoint.id in first_claim

        second_claim = await worker._claim_due_endpoints(limit=10)
        assert endpoint.id not in second_claim

    async def test_an_expired_lease_is_reclaimable(
        self, session, endpoint_factory, worker
    ):
        endpoint = await endpoint_factory()

        first_claim = await worker._claim_due_endpoints(limit=10)
        assert endpoint.id in first_claim

        # Simulate the worker that held it dying mid-batch: its lease expires
        # rather than being renewed.
        endpoint.lease_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        await session.commit()

        reclaimed = await worker._claim_due_endpoints(limit=10)
        assert endpoint.id in reclaimed

    async def test_two_endpoints_claimed_across_two_batches_are_disjoint(
        self, session, endpoint_factory, worker
    ):
        """Stand-in for two workers polling one after another: no endpoint is
        ever handed out twice while its lease is still valid."""
        first = await endpoint_factory()
        second = await endpoint_factory()

        first_batch = await worker._claim_due_endpoints(limit=1)
        second_batch = await worker._claim_due_endpoints(limit=1)

        assert len(first_batch) == 1
        assert len(second_batch) == 1
        assert set(first_batch).isdisjoint(second_batch)
        assert {first.id, second.id} == set(first_batch) | set(second_batch)


class TestClaimFailureHandling:
    async def test_a_database_error_during_claim_is_handled_gracefully(
        self, session, endpoint_factory, worker, monkeypatch
    ):
        """The worker loop must survive a claim failure, not crash on it."""
        await endpoint_factory()

        from sqlalchemy.ext.asyncio import AsyncSession

        async def _broken_execute(self, *args, **kwargs):
            raise SQLAlchemyError("connection lost")

        monkeypatch.setattr(AsyncSession, "execute", _broken_execute)

        claimed = await worker._claim_due_endpoints(limit=10)

        assert claimed == []
