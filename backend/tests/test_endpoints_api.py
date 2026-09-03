"""Endpoint management API."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.endpoint import Endpoint


BASE = {"name": "Translation API", "url": "https://api.example.com/health"}


class TestCreate:
    async def test_creates_with_parsed_components(self, client, admin_headers):
        response = await client.post("/api/endpoints", json=BASE, headers=admin_headers)
        assert response.status_code == 201, response.text

        body = response.json()
        assert body["name"] == "Translation API"
        assert body["protocol"] == "https"
        assert body["hostname"] == "api.example.com"
        assert body["port"] == 443
        assert body["path"] == "/health"
        assert body["current_status"] == "unknown"
        assert body["monitoring_enabled"] is True

    async def test_is_scheduled_immediately(self, client, admin_headers):
        """A new endpoint should show a real status within one worker poll."""
        response = await client.post("/api/endpoints", json=BASE, headers=admin_headers)
        assert response.json()["next_check_at"] is not None

    async def test_tags_are_created_on_the_fly(self, client, admin_headers):
        response = await client.post(
            "/api/endpoints",
            json={**BASE, "tags": ["production", "Backend", "critical", "backend"]},
            headers=admin_headers,
        )
        assert response.status_code == 201
        names = sorted(tag["name"] for tag in response.json()["tags"])
        # Lower-cased and de-duplicated.
        assert names == ["backend", "critical", "production"]

    async def test_environment_can_be_assigned_by_name(self, client, admin_headers):
        environments = await client.get("/api/environments", headers=admin_headers)
        production = next(
            item for item in environments.json() if item["name"] == "production"
        )

        response = await client.post(
            "/api/endpoints",
            json={**BASE, "environment": production["id"]},
            headers=admin_headers,
        )
        assert response.status_code == 201
        assert response.json()["environment"]["name"] == "production"

    async def test_defaults_come_from_settings(self, client, admin_headers):
        response = await client.post("/api/endpoints", json=BASE, headers=admin_headers)
        body = response.json()
        assert body["interval_seconds"] == 60
        assert body["timeout_seconds"] == 5
        assert body["expected_status_codes"] == "200"
        assert body["failure_threshold"] == 3

    async def test_aggressive_interval_is_clamped(self, client, admin_headers):
        """The floor is enforced server-side, not trusted from the client."""
        response = await client.post(
            "/api/endpoints",
            json={**BASE, "interval_seconds": 10},
            headers=admin_headers,
        )
        # Pydantic allows >= 10; the service clamps to MIN_MONITOR_INTERVAL.
        assert response.status_code == 201
        assert response.json()["interval_seconds"] == 30

    @pytest.mark.parametrize(
        "payload",
        [
            {"name": "", "url": "https://example.com"},
            {"name": "No URL", "url": ""},
            {"name": "Bad scheme", "url": "ftp://example.com"},
            {"name": "Creds", "url": "https://u:p@example.com"},
            {"name": "Bad method", "url": "https://example.com", "http_method": "FETCH"},
            {"name": "Bad status", "url": "https://example.com", "expected_status_codes": "abc"},
            {"name": "Bad type", "url": "https://example.com", "check_type": "icmp"},
        ],
    )
    async def test_invalid_payloads_are_rejected(
        self, client, admin_headers, payload
    ):
        response = await client.post(
            "/api/endpoints", json=payload, headers=admin_headers
        )
        assert response.status_code in (400, 422), response.text

    async def test_duplicate_url_is_a_conflict(self, client, admin_headers):
        await client.post("/api/endpoints", json=BASE, headers=admin_headers)
        response = await client.post(
            "/api/endpoints",
            json={"name": "Different name", "url": BASE["url"]},
            headers=admin_headers,
        )
        assert response.status_code == 409
        assert "already exists" in response.json()["detail"]

    async def test_duplicate_name_is_a_conflict(self, client, admin_headers):
        await client.post("/api/endpoints", json=BASE, headers=admin_headers)
        response = await client.post(
            "/api/endpoints",
            json={"name": BASE["name"], "url": "https://other.example.com/health"},
            headers=admin_headers,
        )
        assert response.status_code == 409

    async def test_authorization_header_cannot_be_set_via_custom_headers(
        self, client, admin_headers
    ):
        """It must go through the encrypted credential field instead."""
        response = await client.post(
            "/api/endpoints",
            json={**BASE, "custom_headers": {"Authorization": "Bearer leak"}},
            headers=admin_headers,
        )
        assert response.status_code == 400
        assert "authentication settings" in response.json()["detail"].lower()

    async def test_auth_type_requires_a_credential(self, client, admin_headers):
        response = await client.post(
            "/api/endpoints",
            json={**BASE, "auth_type": "bearer"},
            headers=admin_headers,
        )
        assert response.status_code == 400


class TestCredentialHandling:
    async def test_secret_is_never_returned(self, client, admin_headers):
        response = await client.post(
            "/api/endpoints",
            json={**BASE, "auth_type": "bearer", "auth_secret": "super-secret-token"},
            headers=admin_headers,
        )
        assert response.status_code == 201
        assert "super-secret-token" not in response.text

        body = response.json()
        assert body["has_auth_secret"] is True
        assert body["auth_secret_hint"].endswith("oken")
        assert "auth_secret" not in body or body.get("auth_secret") is None

    async def test_secret_is_encrypted_at_rest(
        self, client, admin_headers, session
    ):
        await client.post(
            "/api/endpoints",
            json={**BASE, "auth_type": "bearer", "auth_secret": "super-secret-token"},
            headers=admin_headers,
        )
        endpoint = (await session.execute(select(Endpoint))).scalar_one()
        assert endpoint.auth_secret_encrypted
        assert "super-secret-token" not in endpoint.auth_secret_encrypted

        from app.core.security import decrypt_secret

        assert decrypt_secret(endpoint.auth_secret_encrypted) == "super-secret-token"

    async def test_omitting_the_secret_on_update_keeps_it(
        self, client, admin_headers, session
    ):
        created = await client.post(
            "/api/endpoints",
            json={**BASE, "auth_type": "bearer", "auth_secret": "keep-me"},
            headers=admin_headers,
        )
        endpoint_id = created.json()["id"]

        await client.put(
            f"/api/endpoints/{endpoint_id}",
            json={"description": "updated"},
            headers=admin_headers,
        )

        from app.core.security import decrypt_secret

        endpoint = (await session.execute(select(Endpoint))).scalar_one()
        await session.refresh(endpoint)
        assert decrypt_secret(endpoint.auth_secret_encrypted) == "keep-me"

    async def test_setting_auth_type_none_clears_the_secret(
        self, client, admin_headers, session
    ):
        created = await client.post(
            "/api/endpoints",
            json={**BASE, "auth_type": "bearer", "auth_secret": "remove-me"},
            headers=admin_headers,
        )
        endpoint_id = created.json()["id"]

        response = await client.put(
            f"/api/endpoints/{endpoint_id}",
            json={"auth_type": "none"},
            headers=admin_headers,
        )
        assert response.status_code == 200
        assert response.json()["has_auth_secret"] is False


class TestListAndFilter:
    async def test_pagination_metadata(self, client, admin_headers):
        for index in range(5):
            await client.post(
                "/api/endpoints",
                json={
                    "name": f"Endpoint {index}",
                    "url": f"https://host{index}.example.com/health",
                },
                headers=admin_headers,
            )

        response = await client.get(
            "/api/endpoints", params={"page": 1, "page_size": 2}, headers=admin_headers
        )
        body = response.json()
        assert len(body["items"]) == 2
        assert body["meta"]["total"] == 5
        assert body["meta"]["pages"] == 3
        assert body["meta"]["has_next"] is True
        assert body["meta"]["has_previous"] is False

    async def test_search_matches_name_and_url(self, client, admin_headers):
        await client.post(
            "/api/endpoints",
            json={"name": "Translation API", "url": "https://translate.example.com/h"},
            headers=admin_headers,
        )
        await client.post(
            "/api/endpoints",
            json={"name": "Portal", "url": "https://portal.example.com"},
            headers=admin_headers,
        )

        by_name = await client.get(
            "/api/endpoints", params={"search": "translation"}, headers=admin_headers
        )
        assert by_name.json()["meta"]["total"] == 1

        by_url = await client.get(
            "/api/endpoints", params={"search": "portal.example"}, headers=admin_headers
        )
        assert by_url.json()["meta"]["total"] == 1

    async def test_status_filter(self, client, admin_headers, session):
        await client.post("/api/endpoints", json=BASE, headers=admin_headers)
        endpoint = (await session.execute(select(Endpoint))).scalar_one()
        endpoint.current_status = "down"
        await session.commit()

        down = await client.get(
            "/api/endpoints", params={"status": "down"}, headers=admin_headers
        )
        assert down.json()["meta"]["total"] == 1

        up = await client.get(
            "/api/endpoints", params={"status": "up"}, headers=admin_headers
        )
        assert up.json()["meta"]["total"] == 0

    async def test_combined_filters_are_an_and(self, client, admin_headers, session):
        environments = await client.get("/api/environments", headers=admin_headers)
        production = next(e for e in environments.json() if e["name"] == "production")

        await client.post(
            "/api/endpoints",
            json={**BASE, "environment": production["id"], "tags": ["backend"]},
            headers=admin_headers,
        )
        endpoint = (await session.execute(select(Endpoint))).scalar_one()
        endpoint.current_status = "down"
        await session.commit()

        tags = await client.get("/api/tags", headers=admin_headers)
        backend = next(t for t in tags.json() if t["name"] == "backend")

        matching = await client.get(
            "/api/endpoints",
            params={
                "status": "down",
                "environment": production["id"],
                "tag": backend["id"],
            },
            headers=admin_headers,
        )
        assert matching.json()["meta"]["total"] == 1

        # Changing one dimension must exclude the row.
        non_matching = await client.get(
            "/api/endpoints",
            params={"status": "up", "environment": production["id"]},
            headers=admin_headers,
        )
        assert non_matching.json()["meta"]["total"] == 0

    async def test_sorting(self, client, admin_headers):
        for name, host in (("Zulu", "z"), ("Alpha", "a")):
            await client.post(
                "/api/endpoints",
                json={"name": name, "url": f"https://{host}.example.com/h"},
                headers=admin_headers,
            )

        ascending = await client.get(
            "/api/endpoints",
            params={"sort_by": "name", "sort_dir": "asc"},
            headers=admin_headers,
        )
        assert [item["name"] for item in ascending.json()["items"]] == ["Alpha", "Zulu"]

        descending = await client.get(
            "/api/endpoints",
            params={"sort_by": "name", "sort_dir": "desc"},
            headers=admin_headers,
        )
        assert [item["name"] for item in descending.json()["items"]] == ["Zulu", "Alpha"]

    async def test_filters_endpoint_returns_options(self, client, admin_headers):
        response = await client.get("/api/endpoints/filters", headers=admin_headers)
        assert response.status_code == 200
        body = response.json()
        assert any(e["name"] == "production" for e in body["environments"])
        assert 30 in body["allowed_intervals"]


class TestUpdateAndDelete:
    async def test_update_changes_fields_and_reparses_the_url(
        self, client, admin_headers
    ):
        created = await client.post("/api/endpoints", json=BASE, headers=admin_headers)
        endpoint_id = created.json()["id"]

        response = await client.put(
            f"/api/endpoints/{endpoint_id}",
            json={
                "name": "Renamed API",
                "url": "http://10.0.0.5:8080/status",
                "description": "moved internal",
            },
            headers=admin_headers,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["name"] == "Renamed API"
        assert body["protocol"] == "http"
        assert body["hostname"] == "10.0.0.5"
        assert body["port"] == 8080
        assert body["path"] == "/status"
        # Switching away from https turns certificate monitoring off.
        assert body["ssl_monitoring_enabled"] is False

    async def test_update_is_partial(self, client, admin_headers):
        created = await client.post(
            "/api/endpoints",
            json={**BASE, "owner": "team@example.com", "tags": ["backend"]},
            headers=admin_headers,
        )
        endpoint_id = created.json()["id"]

        response = await client.put(
            f"/api/endpoints/{endpoint_id}",
            json={"description": "only this"},
            headers=admin_headers,
        )
        body = response.json()
        assert body["description"] == "only this"
        assert body["owner"] == "team@example.com"
        assert [tag["name"] for tag in body["tags"]] == ["backend"]

    async def test_pause_and_resume(self, client, admin_headers):
        created = await client.post("/api/endpoints", json=BASE, headers=admin_headers)
        endpoint_id = created.json()["id"]

        paused = await client.patch(
            f"/api/endpoints/{endpoint_id}/monitoring",
            json={"is_paused": True},
            headers=admin_headers,
        )
        assert paused.json()["is_paused"] is True
        assert paused.json()["current_status"] == "paused"
        assert paused.json()["next_check_at"] is None

        resumed = await client.patch(
            f"/api/endpoints/{endpoint_id}/monitoring",
            json={"is_paused": False},
            headers=admin_headers,
        )
        assert resumed.json()["is_paused"] is False
        assert resumed.json()["next_check_at"] is not None

    async def test_delete_removes_the_endpoint(self, client, admin_headers):
        created = await client.post("/api/endpoints", json=BASE, headers=admin_headers)
        endpoint_id = created.json()["id"]

        response = await client.delete(
            f"/api/endpoints/{endpoint_id}", headers=admin_headers
        )
        assert response.status_code == 200

        missing = await client.get(
            f"/api/endpoints/{endpoint_id}", headers=admin_headers
        )
        assert missing.status_code == 404

    async def test_delete_requires_the_delete_permission(
        self, client, viewer_headers, admin_headers
    ):
        created = await client.post("/api/endpoints", json=BASE, headers=admin_headers)
        response = await client.delete(
            f"/api/endpoints/{created.json()['id']}", headers=viewer_headers
        )
        assert response.status_code == 403

    async def test_missing_endpoint_is_404(self, client, admin_headers):
        response = await client.get(
            "/api/endpoints/00000000-0000-0000-0000-000000000000",
            headers=admin_headers,
        )
        assert response.status_code == 404

    async def test_malformed_id_is_422(self, client, admin_headers):
        response = await client.get("/api/endpoints/not-a-uuid", headers=admin_headers)
        assert response.status_code == 422


class TestBulkActions:
    async def test_pause_many(self, client, admin_headers):
        ids = []
        for index in range(3):
            created = await client.post(
                "/api/endpoints",
                json={
                    "name": f"Bulk {index}",
                    "url": f"https://bulk{index}.example.com/h",
                },
                headers=admin_headers,
            )
            ids.append(created.json()["id"])

        response = await client.post(
            "/api/endpoints/bulk",
            json={"endpoint_ids": ids, "action": "pause"},
            headers=admin_headers,
        )
        assert response.status_code == 200
        assert response.json()["succeeded"] == 3

        listing = await client.get(
            "/api/endpoints", params={"status": "paused"}, headers=admin_headers
        )
        assert listing.json()["meta"]["total"] == 3

    async def test_tagging_many(self, client, admin_headers):
        created = await client.post("/api/endpoints", json=BASE, headers=admin_headers)
        endpoint_id = created.json()["id"]

        response = await client.post(
            "/api/endpoints/bulk",
            json={
                "endpoint_ids": [endpoint_id],
                "action": "tag",
                "tags": ["urgent", "reviewed"],
            },
            headers=admin_headers,
        )
        assert response.json()["succeeded"] == 1

        detail = await client.get(
            f"/api/endpoints/{endpoint_id}", headers=admin_headers
        )
        assert sorted(t["name"] for t in detail.json()["tags"]) == [
            "reviewed",
            "urgent",
        ]

    async def test_unknown_ids_are_reported(self, client, admin_headers):
        response = await client.post(
            "/api/endpoints/bulk",
            json={
                "endpoint_ids": ["00000000-0000-0000-0000-000000000000"],
                "action": "pause",
            },
            headers=admin_headers,
        )
        body = response.json()
        assert body["failed"] == 1
        assert body["errors"][0]["error"] == "not found"

    async def test_bulk_delete_needs_the_delete_permission(
        self, client, viewer_headers
    ):
        response = await client.post(
            "/api/endpoints/bulk",
            json={
                "endpoint_ids": ["00000000-0000-0000-0000-000000000000"],
                "action": "delete",
            },
            headers=viewer_headers,
        )
        assert response.status_code == 403


class TestHistoryAndStats:
    async def test_history_is_empty_for_a_new_endpoint(self, client, admin_headers):
        created = await client.post("/api/endpoints", json=BASE, headers=admin_headers)
        response = await client.get(
            f"/api/endpoints/{created.json()['id']}/history", headers=admin_headers
        )
        assert response.status_code == 200
        assert response.json()["meta"]["total"] == 0

    async def test_stats_returns_every_window(self, client, admin_headers):
        created = await client.post("/api/endpoints", json=BASE, headers=admin_headers)
        response = await client.get(
            f"/api/endpoints/{created.json()['id']}/stats", headers=admin_headers
        )
        assert response.status_code == 200
        assert set(response.json()["windows"]) == {"24h", "7d", "30d", "90d"}

    async def test_ssl_is_404_before_a_handshake(self, client, admin_headers):
        created = await client.post("/api/endpoints", json=BASE, headers=admin_headers)
        response = await client.get(
            f"/api/endpoints/{created.json()['id']}/ssl", headers=admin_headers
        )
        assert response.status_code == 404


class TestDashboardApi:
    async def test_dashboard_is_coherent_when_empty(self, client, admin_headers):
        response = await client.get("/api/dashboard", headers=admin_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["summary"]["total_endpoints"] == 0
        assert body["response_time_series"] == []
        assert body["open_incidents"] == []

    async def test_summary_counts_endpoints(self, client, admin_headers):
        await client.post("/api/endpoints", json=BASE, headers=admin_headers)
        response = await client.get("/api/dashboard/summary", headers=admin_headers)
        body = response.json()
        assert body["total_endpoints"] == 1
        assert body["unknown"] == 1
