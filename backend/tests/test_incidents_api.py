"""Incident API: acknowledgement, and who did it.

"Acknowledged" without a name is a record of nothing - the point of
acknowledging is that a specific person took it. The detail route resolved
the username but the list route did not, so a acknowledged incident showed
no owner anywhere the operator actually looks.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models.incident import Incident


@pytest.fixture
async def incident(session, seeded, endpoint_factory):
    """An open incident to acknowledge."""
    endpoint = await endpoint_factory(
        name="Payments API", url="https://payments.example.com/health"
    )
    started = datetime.now(timezone.utc) - timedelta(minutes=20)
    row = Incident(
        endpoint_id=endpoint.id,
        status="open",
        severity="critical",
        started_at=started,
        reason="connection_timeout",
        error_message="Connection timeout",
        failed_check_count=3,
    )
    session.add(row)
    await session.commit()
    return row


class TestAcknowledgement:
    async def test_acknowledging_records_the_user(
        self, client, admin_headers, incident
    ):
        response = await client.patch(
            f"/api/incidents/{incident.id}",
            json={"acknowledge": True},
            headers=admin_headers,
        )

        assert response.status_code == 200
        body = response.json()
        assert body["acknowledged_by"] == "admin"
        assert body["acknowledged_at"] is not None

    async def test_the_detail_route_reports_who_acknowledged(
        self, client, admin_headers, incident
    ):
        await client.patch(
            f"/api/incidents/{incident.id}",
            json={"acknowledge": True},
            headers=admin_headers,
        )

        response = await client.get(
            f"/api/incidents/{incident.id}", headers=admin_headers
        )

        assert response.json()["acknowledged_by"] == "admin"

    async def test_the_list_route_reports_who_acknowledged(
        self, client, admin_headers, incident
    ):
        """The list is where an operator scans for unowned incidents."""
        await client.patch(
            f"/api/incidents/{incident.id}",
            json={"acknowledge": True},
            headers=admin_headers,
        )

        response = await client.get("/api/incidents", headers=admin_headers)

        rows = response.json()["items"]
        assert len(rows) == 1
        assert rows[0]["acknowledged_by"] == "admin"
        assert rows[0]["acknowledged_at"] is not None

    async def test_an_unacknowledged_incident_names_nobody(
        self, client, admin_headers, incident
    ):
        response = await client.get("/api/incidents", headers=admin_headers)

        row = response.json()["items"][0]
        assert row["acknowledged_by"] is None
        assert row["acknowledged_at"] is None

    async def test_un_acknowledging_clears_the_user(
        self, client, admin_headers, incident
    ):
        await client.patch(
            f"/api/incidents/{incident.id}",
            json={"acknowledge": True},
            headers=admin_headers,
        )

        response = await client.patch(
            f"/api/incidents/{incident.id}",
            json={"acknowledge": False},
            headers=admin_headers,
        )

        assert response.json()["acknowledged_by"] is None
        assert response.json()["acknowledged_at"] is None

    async def test_a_viewer_cannot_acknowledge(
        self, client, viewer_headers, incident
    ):
        """Acknowledging is a claim of ownership, so it needs incident:write."""
        response = await client.patch(
            f"/api/incidents/{incident.id}",
            json={"acknowledge": True},
            headers=viewer_headers,
        )

        assert response.status_code == 403
