"""Change management: the workflow, and its effect on monitoring.

The point of the feature is the monitoring interaction, so most of what is
asserted here is about endpoint state - paused, resumed, and *not* resumed when
someone had already paused it by hand.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.endpoint import Endpoint
from app.services import user_service


async def _login(client, username: str, password: str) -> dict[str, str]:
    response = await client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _payload(**overrides) -> dict:
    body = {
        "title": "Translation API 2.4.0",
        "application": "Translation API",
        "description": "Rolling release of the translation service.",
        "expected_start_at": (
            datetime.now(timezone.utc) + timedelta(hours=1)
        ).isoformat(),
        "expected_duration_minutes": 30,
        "risk": "low",
    }
    body.update(overrides)
    return body


@pytest.fixture
async def approver(session, seeded):
    """A second account that can approve - the requester never can."""
    user = await user_service.create_user(
        session,
        username="approver1",
        password="ApproverPass@123",
        role_name="approver",
        email="approver@example.test",
        must_change_password=False,
    )
    await session.commit()
    return user


@pytest.fixture
async def approver_headers(client, approver):
    return await _login(client, "approver1", "ApproverPass@123")


@pytest.fixture
async def production_id(client, admin_headers):
    response = await client.get("/api/environments", headers=admin_headers)
    return next(
        item["id"] for item in response.json() if item["name"] == "production"
    )


@pytest.fixture
async def staging_id(client, admin_headers):
    response = await client.get("/api/environments", headers=admin_headers)
    return next(item["id"] for item in response.json() if item["name"] == "staging")


@pytest.fixture(autouse=True)
async def no_post_deploy_probe(client, admin_headers):
    """Disable the post-deployment health check.

    It performs a real network probe, which has no place in a unit test. The
    tests that care about it enable it explicitly.
    """
    await client.put(
        "/api/settings",
        json={"updates": {"change_health_check_on_resume": False}},
        headers=admin_headers,
    )


async def _endpoint(client, admin_headers, name="Translate", **extra):
    response = await client.post(
        "/api/endpoints",
        json={
            "name": name,
            "url": f"https://{name.lower()}.example.com/health",
            **extra,
        },
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


class TestCreate:
    async def test_starts_as_a_draft_with_a_reference(self, client, admin_headers):
        response = await client.post(
            "/api/changes", json=_payload(), headers=admin_headers
        )
        assert response.status_code == 201, response.text

        body = response.json()
        assert body["status"] == "draft"
        assert body["reference"].startswith("CHG-")
        assert body["requester_name"] == "admin"
        assert body["can_submit"] is True
        assert body["can_deploy"] is False

    async def test_references_are_unique_and_sequential(self, client, admin_headers):
        references = []
        for index in range(3):
            response = await client.post(
                "/api/changes",
                json=_payload(title=f"Release {index}"),
                headers=admin_headers,
            )
            references.append(response.json()["reference"])
        assert len(set(references)) == 3

    async def test_rejects_an_unknown_endpoint(self, client, admin_headers):
        response = await client.post(
            "/api/changes",
            json=_payload(endpoint_ids=["11111111-1111-1111-1111-111111111111"]),
            headers=admin_headers,
        )
        assert response.status_code == 400
        assert "unknown endpoint" in response.json()["detail"].lower()

    async def test_a_viewer_may_raise_one(self, client, viewer_headers):
        """Anyone can request a change; the gate is approval, not creation."""
        response = await client.post(
            "/api/changes", json=_payload(), headers=viewer_headers
        )
        assert response.status_code == 201
        assert response.json()["requester_name"] == "viewer1"


class TestApprovalRouting:
    async def test_production_needs_approval(
        self, client, admin_headers, production_id
    ):
        created = await client.post(
            "/api/changes",
            json=_payload(environment=production_id),
            headers=admin_headers,
        )
        change_id = created.json()["id"]
        assert created.json()["requires_approval"] is True

        response = await client.post(
            f"/api/changes/{change_id}/submit", headers=admin_headers
        )
        assert response.status_code == 200
        assert response.json()["status"] == "pending_approval"

    async def test_other_environments_are_approved_on_submit(
        self, client, admin_headers, staging_id
    ):
        created = await client.post(
            "/api/changes",
            json=_payload(environment=staging_id),
            headers=admin_headers,
        )
        response = await client.post(
            f"/api/changes/{created.json()['id']}/submit", headers=admin_headers
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "approved"
        assert body["approved_at"] is not None

    async def test_the_required_environments_are_configurable(
        self, client, admin_headers, staging_id
    ):
        await client.put(
            "/api/settings",
            json={"updates": {"change_approval_environments": ["staging"]}},
            headers=admin_headers,
        )
        created = await client.post(
            "/api/changes",
            json=_payload(environment=staging_id),
            headers=admin_headers,
        )
        response = await client.post(
            f"/api/changes/{created.json()['id']}/submit", headers=admin_headers
        )
        assert response.json()["status"] == "pending_approval"

    async def test_the_requester_cannot_approve_their_own(
        self, client, admin_headers, production_id
    ):
        created = await client.post(
            "/api/changes",
            json=_payload(environment=production_id),
            headers=admin_headers,
        )
        change_id = created.json()["id"]
        await client.post(f"/api/changes/{change_id}/submit", headers=admin_headers)

        detail = await client.get(f"/api/changes/{change_id}", headers=admin_headers)
        assert detail.json()["can_approve"] is False

        response = await client.post(
            f"/api/changes/{change_id}/approve", headers=admin_headers
        )
        assert response.status_code == 400

    async def test_a_viewer_cannot_approve(
        self, client, admin_headers, viewer_headers, production_id
    ):
        created = await client.post(
            "/api/changes",
            json=_payload(environment=production_id),
            headers=admin_headers,
        )
        change_id = created.json()["id"]
        await client.post(f"/api/changes/{change_id}/submit", headers=admin_headers)

        response = await client.post(
            f"/api/changes/{change_id}/approve", headers=viewer_headers
        )
        assert response.status_code == 403

    async def test_an_approver_can(
        self, client, admin_headers, approver_headers, production_id
    ):
        created = await client.post(
            "/api/changes",
            json=_payload(environment=production_id),
            headers=admin_headers,
        )
        change_id = created.json()["id"]
        await client.post(f"/api/changes/{change_id}/submit", headers=admin_headers)

        response = await client.post(
            f"/api/changes/{change_id}/approve", headers=approver_headers
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "approved"
        assert body["approver_name"] == "approver1"

    async def test_rejection_needs_a_reason_and_is_visible(
        self, client, admin_headers, approver_headers, production_id
    ):
        created = await client.post(
            "/api/changes",
            json=_payload(environment=production_id),
            headers=admin_headers,
        )
        change_id = created.json()["id"]
        await client.post(f"/api/changes/{change_id}/submit", headers=admin_headers)

        empty = await client.post(
            f"/api/changes/{change_id}/reject",
            json={"reason": ""},
            headers=approver_headers,
        )
        assert empty.status_code == 422

        response = await client.post(
            f"/api/changes/{change_id}/reject",
            json={"reason": "Clashes with the database migration window."},
            headers=approver_headers,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "rejected"
        assert "migration window" in body["rejection_reason"]

    async def test_an_approver_cannot_deploy(
        self, client, admin_headers, approver_headers, staging_id
    ):
        created = await client.post(
            "/api/changes",
            json=_payload(environment=staging_id),
            headers=admin_headers,
        )
        change_id = created.json()["id"]
        await client.post(f"/api/changes/{change_id}/submit", headers=admin_headers)

        response = await client.post(
            f"/api/changes/{change_id}/start-deployment", headers=approver_headers
        )
        assert response.status_code == 403


class TestDeploymentPausesMonitoring:
    """The reason the feature exists."""

    async def _approved(self, client, admin_headers, staging_id, endpoint_ids):
        created = await client.post(
            "/api/changes",
            json=_payload(environment=staging_id, endpoint_ids=endpoint_ids),
            headers=admin_headers,
        )
        change_id = created.json()["id"]
        submitted = await client.post(
            f"/api/changes/{change_id}/submit", headers=admin_headers
        )
        assert submitted.json()["status"] == "approved"
        return change_id

    async def test_starting_pauses_the_affected_endpoints(
        self, client, admin_headers, staging_id, session
    ):
        endpoint = await _endpoint(client, admin_headers)
        change_id = await self._approved(
            client, admin_headers, staging_id, [endpoint["id"]]
        )

        response = await client.post(
            f"/api/changes/{change_id}/start-deployment", headers=admin_headers
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["change"]["status"] == "deployment_in_progress"
        assert result["change"]["deployer_name"] == "admin"
        assert len(result["monitoring_paused"]) == 1

        row = (
            await session.execute(
                select(Endpoint).where(Endpoint.id == endpoint["id"])
            )
        ).scalar_one()
        assert row.is_paused is True
        assert row.current_status == "paused"
        assert row.pause_reason == f"Deployment {result['change']['reference']}"
        assert row.paused_by_change_id == change_id
        # Nothing is due, so the worker's claim query cannot pick it up.
        assert row.next_check_at is None

    async def test_a_paused_endpoint_is_skipped_by_the_claim_query(
        self, client, admin_headers, staging_id, session
    ):
        """The pause is not a filter applied after the fact - the check is
        never scheduled, so there is nothing to suppress downstream."""
        endpoint = await _endpoint(client, admin_headers)
        change_id = await self._approved(
            client, admin_headers, staging_id, [endpoint["id"]]
        )
        await client.post(
            f"/api/changes/{change_id}/start-deployment", headers=admin_headers
        )

        due = (
            (
                await session.execute(
                    select(Endpoint).where(
                        Endpoint.monitoring_enabled.is_(True),
                        Endpoint.is_paused.is_(False),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert endpoint["id"] not in {str(row.id) for row in due}

    async def test_completing_resumes_and_reschedules(
        self, client, admin_headers, staging_id, session
    ):
        endpoint = await _endpoint(client, admin_headers)
        change_id = await self._approved(
            client, admin_headers, staging_id, [endpoint["id"]]
        )
        await client.post(
            f"/api/changes/{change_id}/start-deployment", headers=admin_headers
        )

        response = await client.post(
            f"/api/changes/{change_id}/complete",
            json={"deployment_notes": "Deployed 2.4.0 to staging."},
            headers=admin_headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["change"]["status"] == "completed"
        assert body["change"]["completed_at"] is not None
        assert body["change"]["actual_duration_minutes"] is not None
        assert len(body["monitoring_resumed"]) == 1

        session.expire_all()
        row = (
            await session.execute(
                select(Endpoint).where(Endpoint.id == endpoint["id"])
            )
        ).scalar_one()
        assert row.is_paused is False
        assert row.pause_reason is None
        assert row.paused_by_change_id is None
        assert row.next_check_at is not None

    async def test_failing_also_resumes_monitoring(
        self, client, admin_headers, staging_id, session
    ):
        """A failed deployment is exactly when you most want monitoring back."""
        endpoint = await _endpoint(client, admin_headers)
        change_id = await self._approved(
            client, admin_headers, staging_id, [endpoint["id"]]
        )
        await client.post(
            f"/api/changes/{change_id}/start-deployment", headers=admin_headers
        )

        response = await client.post(
            f"/api/changes/{change_id}/fail",
            json={"reason": "Migration failed; rolled back."},
            headers=admin_headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["change"]["status"] == "failed"

        session.expire_all()
        row = (
            await session.execute(
                select(Endpoint).where(Endpoint.id == endpoint["id"])
            )
        ).scalar_one()
        assert row.is_paused is False

    async def test_an_already_paused_endpoint_stays_paused(
        self, client, admin_headers, staging_id, session
    ):
        """Someone paused it by hand for their own reasons; finishing a
        deployment must not silently switch their monitoring back on."""
        endpoint = await _endpoint(client, admin_headers, is_paused=True)
        assert endpoint["is_paused"] is True

        change_id = await self._approved(
            client, admin_headers, staging_id, [endpoint["id"]]
        )
        start = await client.post(
            f"/api/changes/{change_id}/start-deployment", headers=admin_headers
        )
        assert start.json()["monitoring_paused"][0]["was_paused_before"] is True

        finish = await client.post(
            f"/api/changes/{change_id}/complete", json={}, headers=admin_headers
        )
        assert finish.json()["monitoring_resumed"] == []

        session.expire_all()
        row = (
            await session.execute(
                select(Endpoint).where(Endpoint.id == endpoint["id"])
            )
        ).scalar_one()
        assert row.is_paused is True

    async def test_a_manual_resume_clears_the_deployment_note(
        self, client, admin_headers, staging_id, session
    ):
        endpoint = await _endpoint(client, admin_headers)
        change_id = await self._approved(
            client, admin_headers, staging_id, [endpoint["id"]]
        )
        await client.post(
            f"/api/changes/{change_id}/start-deployment", headers=admin_headers
        )

        response = await client.put(
            f"/api/endpoints/{endpoint['id']}",
            json={"is_paused": False},
            headers=admin_headers,
        )
        assert response.status_code == 200
        assert response.json()["pause_reason"] is None
        assert response.json()["paused_by_change_id"] is None


class TestDeploymentGuards:
    async def test_an_unapproved_change_cannot_be_deployed(
        self, client, admin_headers, production_id
    ):
        created = await client.post(
            "/api/changes",
            json=_payload(environment=production_id),
            headers=admin_headers,
        )
        change_id = created.json()["id"]
        await client.post(f"/api/changes/{change_id}/submit", headers=admin_headers)

        response = await client.post(
            f"/api/changes/{change_id}/start-deployment", headers=admin_headers
        )
        assert response.status_code == 400
        assert "awaiting approval" in response.json()["detail"]

    async def test_a_draft_cannot_be_deployed(self, client, admin_headers):
        created = await client.post(
            "/api/changes", json=_payload(), headers=admin_headers
        )
        response = await client.post(
            f"/api/changes/{created.json()['id']}/start-deployment",
            headers=admin_headers,
        )
        assert response.status_code == 400

    async def test_two_deployments_of_one_application_conflict(
        self, client, admin_headers, staging_id
    ):
        first = await client.post(
            "/api/changes",
            json=_payload(environment=staging_id),
            headers=admin_headers,
        )
        second = await client.post(
            "/api/changes",
            json=_payload(environment=staging_id, title="Hotfix"),
            headers=admin_headers,
        )
        for created in (first, second):
            await client.post(
                f"/api/changes/{created.json()['id']}/submit", headers=admin_headers
            )

        started = await client.post(
            f"/api/changes/{first.json()['id']}/start-deployment",
            headers=admin_headers,
        )
        assert started.status_code == 200

        clash = await client.post(
            f"/api/changes/{second.json()['id']}/start-deployment",
            headers=admin_headers,
        )
        assert clash.status_code == 409
        assert first.json()["reference"] in clash.json()["detail"]

    async def test_the_same_application_in_another_environment_is_fine(
        self, client, admin_headers, staging_id, production_id, approver_headers
    ):
        staging = await client.post(
            "/api/changes",
            json=_payload(environment=staging_id),
            headers=admin_headers,
        )
        await client.post(
            f"/api/changes/{staging.json()['id']}/submit", headers=admin_headers
        )
        await client.post(
            f"/api/changes/{staging.json()['id']}/start-deployment",
            headers=admin_headers,
        )

        production = await client.post(
            "/api/changes",
            json=_payload(environment=production_id),
            headers=admin_headers,
        )
        change_id = production.json()["id"]
        await client.post(f"/api/changes/{change_id}/submit", headers=admin_headers)
        await client.post(f"/api/changes/{change_id}/approve", headers=approver_headers)

        response = await client.post(
            f"/api/changes/{change_id}/start-deployment", headers=admin_headers
        )
        assert response.status_code == 200

    async def test_failing_requires_a_reason(
        self, client, admin_headers, staging_id
    ):
        created = await client.post(
            "/api/changes",
            json=_payload(environment=staging_id),
            headers=admin_headers,
        )
        change_id = created.json()["id"]
        await client.post(f"/api/changes/{change_id}/submit", headers=admin_headers)
        await client.post(
            f"/api/changes/{change_id}/start-deployment", headers=admin_headers
        )

        response = await client.post(
            f"/api/changes/{change_id}/fail", json={"reason": ""}, headers=admin_headers
        )
        assert response.status_code == 422

    async def test_a_completed_change_cannot_be_edited(
        self, client, admin_headers, staging_id
    ):
        created = await client.post(
            "/api/changes",
            json=_payload(environment=staging_id),
            headers=admin_headers,
        )
        change_id = created.json()["id"]
        await client.post(f"/api/changes/{change_id}/submit", headers=admin_headers)
        await client.post(
            f"/api/changes/{change_id}/start-deployment", headers=admin_headers
        )
        await client.post(
            f"/api/changes/{change_id}/complete", json={}, headers=admin_headers
        )

        response = await client.put(
            f"/api/changes/{change_id}",
            json={"title": "Rewritten after the fact"},
            headers=admin_headers,
        )
        assert response.status_code == 400

    async def test_a_deploying_change_cannot_be_cancelled(
        self, client, admin_headers, staging_id
    ):
        created = await client.post(
            "/api/changes",
            json=_payload(environment=staging_id),
            headers=admin_headers,
        )
        change_id = created.json()["id"]
        await client.post(f"/api/changes/{change_id}/submit", headers=admin_headers)
        await client.post(
            f"/api/changes/{change_id}/start-deployment", headers=admin_headers
        )

        response = await client.post(
            f"/api/changes/{change_id}/cancel", json={}, headers=admin_headers
        )
        assert response.status_code == 400


class TestListingAndTimeline:
    async def test_activity_records_every_transition(
        self, client, admin_headers, staging_id
    ):
        endpoint = await _endpoint(client, admin_headers)
        created = await client.post(
            "/api/changes",
            json=_payload(environment=staging_id, endpoint_ids=[endpoint["id"]]),
            headers=admin_headers,
        )
        change_id = created.json()["id"]
        await client.post(f"/api/changes/{change_id}/submit", headers=admin_headers)
        await client.post(
            f"/api/changes/{change_id}/start-deployment", headers=admin_headers
        )
        await client.post(
            f"/api/changes/{change_id}/complete", json={}, headers=admin_headers
        )

        detail = await client.get(f"/api/changes/{change_id}", headers=admin_headers)
        actions = [entry["action"] for entry in detail.json()["activity"]]
        for expected in (
            "created",
            "approved",
            "deployment_started",
            "monitoring_paused",
            "deployment_completed",
            "monitoring_resumed",
        ):
            assert expected in actions, actions

    async def test_mine_filters_to_the_requester(
        self, client, admin_headers, viewer_headers
    ):
        await client.post("/api/changes", json=_payload(), headers=admin_headers)
        await client.post(
            "/api/changes", json=_payload(title="Viewer's own"), headers=viewer_headers
        )

        response = await client.get(
            "/api/changes", params={"mine": "true"}, headers=viewer_headers
        )
        items = response.json()["items"]
        assert len(items) == 1
        assert items[0]["requester_name"] == "viewer1"

    async def test_status_filter_backs_the_pending_tab(
        self, client, admin_headers, production_id
    ):
        created = await client.post(
            "/api/changes",
            json=_payload(environment=production_id),
            headers=admin_headers,
        )
        await client.post("/api/changes", json=_payload(title="Still a draft"), headers=admin_headers)
        await client.post(
            f"/api/changes/{created.json()['id']}/submit", headers=admin_headers
        )

        response = await client.get(
            "/api/changes",
            params={"status": "pending_approval"},
            headers=admin_headers,
        )
        items = response.json()["items"]
        assert len(items) == 1
        assert items[0]["status"] == "pending_approval"

    async def test_search_matches_reference_and_application(
        self, client, admin_headers
    ):
        created = await client.post(
            "/api/changes",
            json=_payload(application="Billing Service"),
            headers=admin_headers,
        )
        reference = created.json()["reference"]

        by_reference = await client.get(
            "/api/changes", params={"search": reference}, headers=admin_headers
        )
        assert by_reference.json()["meta"]["total"] == 1

        by_application = await client.get(
            "/api/changes", params={"search": "billing"}, headers=admin_headers
        )
        assert by_application.json()["meta"]["total"] == 1

    async def test_dashboard_counts_the_workflow(
        self, client, admin_headers, staging_id, production_id
    ):
        pending = await client.post(
            "/api/changes",
            json=_payload(environment=production_id),
            headers=admin_headers,
        )
        await client.post(
            f"/api/changes/{pending.json()['id']}/submit", headers=admin_headers
        )

        deploying = await client.post(
            "/api/changes",
            json=_payload(environment=staging_id, application="Other App"),
            headers=admin_headers,
        )
        await client.post(
            f"/api/changes/{deploying.json()['id']}/submit", headers=admin_headers
        )
        await client.post(
            f"/api/changes/{deploying.json()['id']}/start-deployment",
            headers=admin_headers,
        )

        response = await client.get("/api/changes/dashboard", headers=admin_headers)
        body = response.json()
        assert body["pending_approval"] == 1
        assert body["active_deployments"] == 1
        assert [item["id"] for item in body["active"]] == [deploying.json()["id"]]
        assert body["max_pause_minutes"] > 0

    async def test_comments_are_open_to_anyone_who_can_see_it(
        self, client, admin_headers, viewer_headers
    ):
        created = await client.post(
            "/api/changes", json=_payload(), headers=admin_headers
        )
        change_id = created.json()["id"]

        response = await client.post(
            f"/api/changes/{change_id}/comments",
            json={"body": "Please avoid the month-end window."},
            headers=viewer_headers,
        )
        assert response.status_code == 201, response.text
        comments = response.json()["comments"]
        assert comments[-1]["username"] == "viewer1"
        assert "month-end" in comments[-1]["body"]

    async def test_a_change_is_scoped_to_change_read(self, client):
        response = await client.get("/api/changes")
        assert response.status_code == 401
