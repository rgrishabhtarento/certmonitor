"""RCA management, and the local intelligence endpoints.

The rules worth protecting with tests are the ones a future change is most
likely to break by accident:

* RCA never blocks anything about the incident,
* ownership works for a team without a team table or a new role,
* the draft never invents a fact it did not read from a stored record,
* the search parser refuses what it does not understand instead of guessing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.incident import Incident
from app.models.rca import Rca
from app.services import rca_draft, user_service


async def _login(client, username: str, password: str) -> dict[str, str]:
    response = await client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture
async def incident(session, seeded, endpoint_factory):
    """A resolved incident to hang an RCA off."""
    endpoint = await endpoint_factory(
        name="Payment API", url="https://payments.example.com/health"
    )
    started = datetime.now(timezone.utc) - timedelta(hours=2)
    row = Incident(
        endpoint_id=endpoint.id,
        status="resolved",
        severity="critical",
        started_at=started,
        resolved_at=started + timedelta(minutes=11),
        duration_seconds=660,
        reason="http_status_mismatch",
        error_message="Unexpected HTTP status 502 (expected 200)",
        first_failure_status_code=502,
        failed_check_count=4,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


@pytest.fixture
async def team_member(session, seeded):
    """A viewer who belongs to the DevOps team.

    Deliberately a *viewer*: the point of team ownership is that it works
    without granting a new role.
    """
    user = await user_service.create_user(
        session,
        username="devops1",
        password="DevOpsPass@123",
        role_name="viewer",
        email="devops1@example.test",
        team="DevOps",
        must_change_password=False,
    )
    await session.commit()
    return user


@pytest.fixture
async def team_headers(client, team_member):
    return await _login(client, "devops1", "DevOpsPass@123")


class TestOptionality:
    """RCA must never be in the way of anything."""

    async def test_an_incident_starts_with_no_rca(
        self, client, admin_headers, incident
    ):
        response = await client.get(
            f"/api/incidents/{incident.id}/rca", headers=admin_headers
        )
        assert response.status_code == 200
        assert response.json() is None

    async def test_an_incident_can_be_updated_with_an_rca_still_open(
        self, client, admin_headers, incident
    ):
        await client.post(
            f"/api/incidents/{incident.id}/rca", json={}, headers=admin_headers
        )
        response = await client.patch(
            f"/api/incidents/{incident.id}",
            json={"notes": "Handled, rollback done."},
            headers=admin_headers,
        )
        assert response.status_code == 200

        rca = await client.get(
            f"/api/incidents/{incident.id}/rca", headers=admin_headers
        )
        assert rca.json()["status"] == "pending"

    async def test_completing_an_rca_does_not_touch_the_incident(
        self, client, admin_headers, incident, session
    ):
        created = await client.post(
            f"/api/incidents/{incident.id}/rca", json={}, headers=admin_headers
        )
        rca_id = created.json()["id"]

        await client.put(
            f"/api/rca/{rca_id}",
            json={"root_cause": "Backend crashed", "resolution": "Rolled back"},
            headers=admin_headers,
        )
        response = await client.post(
            f"/api/rca/{rca_id}/complete", headers=admin_headers
        )
        assert response.status_code == 200
        assert response.json()["status"] == "completed"

        session.expire_all()
        row = (
            await session.execute(select(Incident).where(Incident.id == incident.id))
        ).scalars().unique().one()
        assert row.status == "resolved"

    async def test_not_required_is_a_recorded_decision(
        self, client, admin_headers, incident
    ):
        response = await client.post(
            f"/api/incidents/{incident.id}/rca/not-required",
            json={"reason": "Brief self-resolved blip."},
            headers=admin_headers,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "not_required"
        assert "blip" in body["not_required_reason"]

    async def test_a_declined_rca_can_be_requested_later(
        self, client, admin_headers, incident
    ):
        await client.post(
            f"/api/incidents/{incident.id}/rca/not-required",
            json={}, headers=admin_headers,
        )
        response = await client.post(
            f"/api/incidents/{incident.id}/rca", json={}, headers=admin_headers
        )
        assert response.status_code == 201
        assert response.json()["status"] == "pending"

    async def test_requesting_twice_does_not_create_two(
        self, client, admin_headers, incident, session
    ):
        first = await client.post(
            f"/api/incidents/{incident.id}/rca", json={}, headers=admin_headers
        )
        second = await client.post(
            f"/api/incidents/{incident.id}/rca", json={}, headers=admin_headers
        )
        assert first.json()["id"] == second.json()["id"]

        count = len(
            (
                await session.execute(
                    select(Rca).where(Rca.incident_id == incident.id)
                )
            ).scalars().all()
        )
        assert count == 1


class TestCompletionRules:
    async def test_a_root_cause_is_required(
        self, client, admin_headers, incident
    ):
        created = await client.post(
            f"/api/incidents/{incident.id}/rca", json={}, headers=admin_headers
        )
        response = await client.post(
            f"/api/rca/{created.json()['id']}/complete", headers=admin_headers
        )
        assert response.status_code == 400
        assert "root cause" in response.json()["detail"].lower()

    async def test_a_resolution_is_required(
        self, client, admin_headers, incident
    ):
        created = await client.post(
            f"/api/incidents/{incident.id}/rca", json={}, headers=admin_headers
        )
        rca_id = created.json()["id"]
        await client.put(
            f"/api/rca/{rca_id}",
            json={"root_cause": "Backend crashed"},
            headers=admin_headers,
        )
        response = await client.post(
            f"/api/rca/{rca_id}/complete", headers=admin_headers
        )
        assert response.status_code == 400
        assert "resolution" in response.json()["detail"].lower()

    async def test_writing_content_moves_it_to_in_progress(
        self, client, admin_headers, incident
    ):
        """Otherwise every RCA sits at Pending with a completed body."""
        created = await client.post(
            f"/api/incidents/{incident.id}/rca", json={}, headers=admin_headers
        )
        response = await client.put(
            f"/api/rca/{created.json()['id']}",
            json={"root_cause": "Investigating"},
            headers=admin_headers,
        )
        assert response.json()["status"] == "in_progress"

    async def test_an_unknown_category_is_rejected(
        self, client, admin_headers, incident
    ):
        created = await client.post(
            f"/api/incidents/{incident.id}/rca", json={}, headers=admin_headers
        )
        response = await client.put(
            f"/api/rca/{created.json()['id']}",
            json={"root_cause_category": "gremlins"},
            headers=admin_headers,
        )
        assert response.status_code == 422


class TestOwnership:
    """Team ownership without a team table, a membership screen or a new role."""

    async def test_a_viewer_who_owns_it_can_edit_it(
        self, client, admin_headers, team_headers, incident
    ):
        created = await client.post(
            f"/api/incidents/{incident.id}/rca",
            json={"owner_type": "team", "owner_team": "DevOps"},
            headers=admin_headers,
        )
        rca_id = created.json()["id"]

        detail = await client.get(f"/api/rca/{rca_id}", headers=team_headers)
        assert detail.json()["can_edit"] is True

        response = await client.put(
            f"/api/rca/{rca_id}",
            json={"root_cause": "Connection pool exhausted"},
            headers=team_headers,
        )
        assert response.status_code == 200

    async def test_a_viewer_who_does_not_own_it_cannot_edit_it(
        self, client, admin_headers, viewer_headers, incident
    ):
        created = await client.post(
            f"/api/incidents/{incident.id}/rca",
            json={"owner_type": "team", "owner_team": "Platform"},
            headers=admin_headers,
        )
        response = await client.put(
            f"/api/rca/{created.json()['id']}",
            json={"root_cause": "Guessing"},
            headers=viewer_headers,
        )
        assert response.status_code == 403

    async def test_team_matching_ignores_case_and_padding(
        self, client, admin_headers, team_headers, incident
    ):
        created = await client.post(
            f"/api/incidents/{incident.id}/rca",
            json={"owner_type": "team", "owner_team": "  devops  "},
            headers=admin_headers,
        )
        detail = await client.get(
            f"/api/rca/{created.json()['id']}", headers=team_headers
        )
        assert detail.json()["can_edit"] is True

    async def test_assigning_to_a_team_clears_the_individual(
        self, client, admin_headers, incident, seeded
    ):
        admin = seeded["admin"]
        created = await client.post(
            f"/api/incidents/{incident.id}/rca",
            json={"owner_type": "individual", "owner_user_id": str(admin.id)},
            headers=admin_headers,
        )
        rca_id = created.json()["id"]
        assert created.json()["owner_user_name"] == "admin"

        response = await client.post(
            f"/api/rca/{rca_id}/assign",
            json={"owner_type": "team", "owner_team": "Platform"},
            headers=admin_headers,
        )
        body = response.json()
        assert body["owner_team"] == "Platform"
        assert body["owner_user_name"] is None

    async def test_a_viewer_cannot_request_an_rca(
        self, client, viewer_headers, incident
    ):
        response = await client.post(
            f"/api/incidents/{incident.id}/rca", json={}, headers=viewer_headers
        )
        assert response.status_code == 403

    async def test_mine_covers_both_the_person_and_their_team(
        self, client, admin_headers, team_headers, incident
    ):
        await client.post(
            f"/api/incidents/{incident.id}/rca",
            json={"owner_type": "team", "owner_team": "DevOps"},
            headers=admin_headers,
        )
        response = await client.get(
            "/api/rca", params={"mine": "true"}, headers=team_headers
        )
        assert response.json()["meta"]["total"] == 1


class TestOverdue:
    async def test_no_due_date_means_never_overdue(
        self, client, admin_headers, incident
    ):
        """RCA is optional, so an RCA with no deadline is not late - it simply
        has no deadline."""
        created = await client.post(
            f"/api/incidents/{incident.id}/rca", json={}, headers=admin_headers
        )
        assert created.json()["due_at"] is None
        assert created.json()["is_overdue"] is False

    async def test_a_completed_rca_is_never_overdue(
        self, client, admin_headers, incident, session
    ):
        created = await client.post(
            f"/api/incidents/{incident.id}/rca",
            json={"due_in_days": 1}, headers=admin_headers,
        )
        rca_id = created.json()["id"]

        row = (
            await session.execute(select(Rca).where(Rca.id == rca_id))
        ).scalars().one()
        row.due_at = datetime.now(timezone.utc) - timedelta(days=3)
        await session.commit()

        await client.put(
            f"/api/rca/{rca_id}",
            json={"root_cause": "Cause", "resolution": "Fix"},
            headers=admin_headers,
        )
        response = await client.post(
            f"/api/rca/{rca_id}/complete", headers=admin_headers
        )
        assert response.json()["is_overdue"] is False


class TestDraft:
    """The draft may only restate what is stored."""

    async def test_it_says_so_when_data_is_missing(
        self, client, admin_headers, incident
    ):
        created = await client.post(
            f"/api/incidents/{incident.id}/rca", json={}, headers=admin_headers
        )
        response = await client.post(
            f"/api/rca/{created.json()['id']}/draft", headers=admin_headers
        )
        assert response.status_code == 200
        body = response.json()

        # No diagnosis was ever run for this incident, so the draft must not
        # produce a root cause out of thin air.
        assert rca_draft.UNAVAILABLE in body["root_cause"]
        assert "Review before saving" in body["notice"]

    async def test_it_uses_the_real_incident_facts(
        self, client, admin_headers, incident
    ):
        created = await client.post(
            f"/api/incidents/{incident.id}/rca", json={}, headers=admin_headers
        )
        response = await client.post(
            f"/api/rca/{created.json()['id']}/draft", headers=admin_headers
        )
        body = response.json()

        assert "Payment API" in body["impact"]
        assert "11 minutes" in body["impact"]
        assert body["evidence"]["failed_checks"] >= 0
        assert body["evidence"]["duration_seconds"] == 660

    async def test_it_never_claims_user_impact_it_cannot_measure(
        self, client, admin_headers, incident
    ):
        created = await client.post(
            f"/api/incidents/{incident.id}/rca", json={}, headers=admin_headers
        )
        response = await client.post(
            f"/api/rca/{created.json()['id']}/draft", headers=admin_headers
        )
        assert "not measured by InfraSight" in response.json()["impact"]

    async def test_the_timeline_is_built_from_real_events(
        self, client, admin_headers, incident
    ):
        created = await client.post(
            f"/api/incidents/{incident.id}/rca", json={}, headers=admin_headers
        )
        await client.post(
            f"/api/incidents/{incident.id}/comments",
            json={"body": "Rollback completed."},
            headers=admin_headers,
        )
        response = await client.post(
            f"/api/rca/{created.json()['id']}/draft", headers=admin_headers
        )
        timeline = response.json()["timeline"]

        kinds = [entry["kind"] for entry in timeline]
        assert "incident_started" in kinds
        assert "incident_resolved" in kinds
        assert "comment" in kinds
        # Every entry names where it came from, so a derived fact is never
        # mistaken for something a person wrote.
        assert all(entry["source"] for entry in timeline)


class TestComments:
    async def test_a_viewer_can_comment(
        self, client, viewer_headers, incident
    ):
        response = await client.post(
            f"/api/incidents/{incident.id}/comments",
            json={"body": "Started right after the deploy."},
            headers=viewer_headers,
        )
        assert response.status_code == 201
        assert response.json()["username"] == "viewer1"

    async def test_an_empty_comment_is_rejected(
        self, client, admin_headers, incident
    ):
        response = await client.post(
            f"/api/incidents/{incident.id}/comments",
            json={"body": "   "},
            headers=admin_headers,
        )
        assert response.status_code in (400, 422)


class TestDashboardAndAnalytics:
    async def test_incidents_without_an_rca_are_counted_as_a_backlog(
        self, client, admin_headers, incident
    ):
        response = await client.get("/api/rca/dashboard", headers=admin_headers)
        body = response.json()
        assert body["total_incidents"] == 1
        assert body["not_requested"] == 1

    async def test_analytics_reports_only_recorded_data(
        self, client, admin_headers, incident
    ):
        """An empty section means nobody has written it, not that nothing
        happened."""
        response = await client.get("/api/rca/analytics", headers=admin_headers)
        body = response.json()
        assert body["completed"] == 0
        assert body["top_root_causes"] == []
        assert body["completion_rate_percent"] is None

    async def test_a_completed_rca_appears_in_the_reports(
        self, client, admin_headers, incident
    ):
        created = await client.post(
            f"/api/incidents/{incident.id}/rca", json={}, headers=admin_headers
        )
        rca_id = created.json()["id"]
        await client.put(
            f"/api/rca/{rca_id}",
            json={
                "root_cause": "Database connection exhaustion",
                "root_cause_category": "database",
                "resolution": "Increased the pool",
            },
            headers=admin_headers,
        )
        await client.post(f"/api/rca/{rca_id}/complete", headers=admin_headers)

        response = await client.get("/api/rca/analytics", headers=admin_headers)
        body = response.json()
        assert body["completed"] == 1
        assert body["completion_rate_percent"] == 100.0
        assert body["top_root_causes"][0]["category"] == "database"


class TestIntelligence:
    async def test_the_summary_explains_its_score(
        self, client, admin_headers, endpoint_factory
    ):
        await endpoint_factory(name="One")
        response = await client.get(
            "/api/intelligence/summary", headers=admin_headers
        )
        assert response.status_code == 200
        body = response.json()
        assert 0 <= body["health_score"] <= 100
        # A score with no reasons is a number nobody can act on.
        assert body["health_reasons"]
        assert set(body["health_components"]) == {
            "availability", "ssl", "incidents", "deployments",
        }

    async def test_the_daily_summary_runs_on_an_empty_instance(
        self, client, admin_headers
    ):
        response = await client.get("/api/intelligence/daily", headers=admin_headers)
        assert response.status_code == 200
        assert response.json()["window_hours"] == 24


class TestSearch:
    """A parser that refuses what it does not understand."""

    async def test_it_admits_when_it_does_not_understand(
        self, client, admin_headers
    ):
        response = await client.get(
            "/api/intelligence/search",
            params={"q": "what is the airspeed velocity of an unladen swallow"},
            headers=admin_headers,
        )
        body = response.json()
        assert body["understood"] is False
        assert body["rows"] == []
        # And it says what it *can* answer, rather than just failing.
        assert "SSL certificates" in body["description"]

    async def test_it_reports_what_it_understood(
        self, client, admin_headers, endpoint_factory
    ):
        await endpoint_factory(name="Broken", url="https://broken.example.com/h")
        response = await client.get(
            "/api/intelligence/search",
            params={"q": "production services that are down"},
            headers=admin_headers,
        )
        body = response.json()
        assert body["understood"] is True
        assert body["intent"] == "unhealthy_endpoints"
        assert "Production" in body["description"]

    async def test_it_extracts_a_day_count(self, client, admin_headers):
        response = await client.get(
            "/api/intelligence/search",
            params={"q": "SSL certificates expiring in 15 days"},
            headers=admin_headers,
        )
        body = response.json()
        assert body["intent"] == "ssl_expiring"
        assert "15 days" in body["description"]

    async def test_it_extracts_a_latency_threshold(self, client, admin_headers):
        response = await client.get(
            "/api/intelligence/search",
            params={"q": "endpoints with latency above 2 seconds"},
            headers=admin_headers,
        )
        body = response.json()
        assert body["intent"] == "slow_endpoints"
        assert "2000 ms" in body["description"]

    async def test_paused_endpoints_are_findable(
        self, client, admin_headers, endpoint_factory
    ):
        await endpoint_factory(name="Sleeping", is_paused=True)
        response = await client.get(
            "/api/intelligence/search",
            params={"q": "currently paused endpoints"},
            headers=admin_headers,
        )
        body = response.json()
        assert body["intent"] == "paused_endpoints"
        assert body["count"] == 1

    async def test_incidents_without_rca_are_findable(
        self, client, admin_headers, incident
    ):
        response = await client.get(
            "/api/intelligence/search",
            params={"q": "incidents without RCA"},
            headers=admin_headers,
        )
        body = response.json()
        assert body["intent"] == "incidents_without_rca"
        assert body["count"] == 1

    async def test_an_empty_question_is_handled(self, client, admin_headers):
        response = await client.get(
            "/api/intelligence/search", params={"q": "   "}, headers=admin_headers
        )
        assert response.json()["understood"] is False
