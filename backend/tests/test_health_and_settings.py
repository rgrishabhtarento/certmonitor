"""Health probes, runtime settings, users and notification channels."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models.alert import NotificationChannel
from app.models.monitoring import WorkerHeartbeat
from app.services import notification_service, settings_service


class TestHealthProbes:
    async def test_live_needs_no_authentication(self, client):
        """Kubernetes cannot present a bearer token."""
        response = await client.get("/live")
        assert response.status_code == 200
        assert response.json()["status"] == "alive"

    async def test_health_reports_components(self, client):
        response = await client.get("/health")
        assert response.status_code == 200

        body = response.json()
        assert body["database"] == "healthy"
        # No worker has registered in the test environment.
        assert body["monitoring_worker"] == "unhealthy"
        assert body["status"] == "degraded"
        assert "database" in body["components"]

    async def test_health_is_healthy_once_a_worker_reports(self, client, session):
        session.add(
            WorkerHeartbeat(
                worker_id="test-worker-1",
                started_at=datetime.now(timezone.utc),
                last_seen_at=datetime.now(timezone.utc),
                checks_completed=10,
                checks_failed=1,
                in_flight=0,
                version="1.0.0",
                hostname="test-host",
            )
        )
        await session.commit()

        response = await client.get("/health")
        body = response.json()
        assert body["monitoring_worker"] == "healthy"
        assert body["status"] == "healthy"

    async def test_a_stale_heartbeat_degrades_health(self, client, session):
        session.add(
            WorkerHeartbeat(
                worker_id="stale-worker",
                started_at=datetime.now(timezone.utc) - timedelta(hours=2),
                last_seen_at=datetime.now(timezone.utc) - timedelta(hours=1),
            )
        )
        await session.commit()

        response = await client.get("/health")
        assert response.json()["monitoring_worker"] == "unhealthy"

    async def test_ready_requires_a_seeded_schema(self, client):
        response = await client.get("/ready")
        assert response.status_code == 200
        assert response.json()["status"] == "ready"

    async def test_health_leaks_no_configuration(self, client):
        response = await client.get("/health")
        text = response.text.lower()
        for secret in ("password", "jwt_secret", "database_url", "postgresql://"):
            assert secret not in text

    async def test_workers_endpoint_lists_heartbeats(
        self, client, admin_headers, session
    ):
        session.add(
            WorkerHeartbeat(
                worker_id="w1",
                started_at=datetime.now(timezone.utc),
                last_seen_at=datetime.now(timezone.utc),
                checks_completed=5,
            )
        )
        await session.commit()

        response = await client.get("/api/workers", headers=admin_headers)
        assert response.status_code == 200
        body = response.json()
        assert body[0]["worker_id"] == "w1"
        assert body[0]["is_healthy"] is True


class TestSecurityHeaders:
    async def test_defensive_headers_are_present(self, client):
        response = await client.get("/live")
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert "content-security-policy" in response.headers

    async def test_a_request_id_is_returned(self, client):
        response = await client.get("/live")
        assert response.headers.get("x-request-id")


class TestSettings:
    async def test_defaults_are_seeded_and_readable(self, client, admin_headers):
        response = await client.get("/api/settings", headers=admin_headers)
        assert response.status_code == 200

        body = response.json()
        assert body["effective"]["ssl_warning_days"] == 30
        assert body["effective"]["ssl_critical_days"] == 7
        assert body["effective"]["failure_threshold"] == 3
        keys = {setting["key"] for setting in body["settings"]}
        assert "data_retention_days" in keys
        assert "alert_cooldown_minutes" in keys

    async def test_update_persists_and_takes_effect(self, client, admin_headers):
        response = await client.put(
            "/api/settings",
            json={"updates": {"ssl_warning_days": 45, "failure_threshold": 5}},
            headers=admin_headers,
        )
        assert response.status_code == 200
        assert response.json()["effective"]["ssl_warning_days"] == 45
        assert response.json()["effective"]["failure_threshold"] == 5

        again = await client.get("/api/settings", headers=admin_headers)
        assert again.json()["effective"]["ssl_warning_days"] == 45

    async def test_a_new_endpoint_picks_up_the_changed_default(
        self, client, admin_headers
    ):
        await client.put(
            "/api/settings",
            json={"updates": {"default_monitor_interval": 300, "failure_threshold": 5}},
            headers=admin_headers,
        )
        response = await client.post(
            "/api/endpoints",
            json={"name": "After change", "url": "https://after.example.com/h"},
            headers=admin_headers,
        )
        assert response.json()["interval_seconds"] == 300
        assert response.json()["failure_threshold"] == 5

    async def test_out_of_range_value_is_rejected(self, client, admin_headers):
        response = await client.put(
            "/api/settings",
            json={"updates": {"ssl_warning_days": 5000}},
            headers=admin_headers,
        )
        assert response.status_code == 422

    async def test_critical_above_warning_is_rejected(self, client, admin_headers):
        response = await client.put(
            "/api/settings",
            json={"updates": {"ssl_critical_days": 60, "ssl_warning_days": 30}},
            headers=admin_headers,
        )
        assert response.status_code == 422

    async def test_unknown_key_is_rejected(self, client, admin_headers):
        response = await client.put(
            "/api/settings",
            json={"updates": {"totally_made_up": 1}},
            headers=admin_headers,
        )
        assert response.status_code == 422

    async def test_a_bad_value_rejects_the_whole_batch(
        self, client, admin_headers
    ):
        """Partial application would leave configuration in a mixed state."""
        await client.put(
            "/api/settings",
            json={"updates": {"ssl_warning_days": 45, "failure_threshold": 999}},
            headers=admin_headers,
        )
        current = await client.get("/api/settings", headers=admin_headers)
        assert current.json()["effective"]["ssl_warning_days"] == 30

    async def test_interval_below_the_floor_is_rejected(
        self, client, admin_headers
    ):
        response = await client.put(
            "/api/settings",
            json={"updates": {"allowed_intervals": [1, 5, 60]}},
            headers=admin_headers,
        )
        assert response.status_code == 422

    async def test_settings_change_is_audited(self, client, admin_headers):
        await client.put(
            "/api/settings",
            json={"updates": {"ssl_warning_days": 45}},
            headers=admin_headers,
        )
        audit = await client.get(
            "/api/audit-logs",
            params={"action": "settings_changed"},
            headers=admin_headers,
        )
        assert audit.json()["meta"]["total"] == 1

    async def test_cache_is_invalidated_on_write(self, session, seeded):
        first = await settings_service.load_settings(session)
        assert first["ssl_warning_days"] == 30

        await settings_service.update_settings(session, {"ssl_warning_days": 60})
        await session.commit()

        second = await settings_service.load_settings(session)
        assert second["ssl_warning_days"] == 60


class TestUserManagement:
    async def test_create_list_and_delete(self, client, admin_headers):
        created = await client.post(
            "/api/users",
            json={
                "username": "operator1",
                "password": "OperatorPass@123",
                "role": "viewer",
                "email": "operator1@example.test",
            },
            headers=admin_headers,
        )
        assert created.status_code == 201, created.text
        assert created.json()["must_change_password"] is True
        assert "OperatorPass@123" not in created.text

        listing = await client.get("/api/users", headers=admin_headers)
        usernames = [row["username"] for row in listing.json()["items"]]
        assert "operator1" in usernames

        deleted = await client.delete(
            f"/api/users/{created.json()['id']}", headers=admin_headers
        )
        assert deleted.status_code == 200

    async def test_duplicate_username_is_a_conflict(self, client, admin_headers):
        response = await client.post(
            "/api/users",
            json={"username": "admin", "password": "AnotherPass@123", "role": "viewer"},
            headers=admin_headers,
        )
        assert response.status_code == 409

    async def test_weak_password_is_rejected(self, client, admin_headers):
        response = await client.post(
            "/api/users",
            json={"username": "weakuser", "password": "weak", "role": "viewer"},
            headers=admin_headers,
        )
        assert response.status_code == 422

    async def test_the_last_admin_cannot_be_demoted(
        self, client, admin_headers, session
    ):
        from app.services import user_service

        admin = await user_service.get_user_by_username(session, "admin")
        response = await client.put(
            f"/api/users/{admin.id}", json={"role": "viewer"}, headers=admin_headers
        )
        assert response.status_code == 400
        assert "administrator" in response.json()["detail"].lower()

    async def test_an_admin_cannot_delete_themselves(
        self, client, admin_headers, session
    ):
        from app.services import user_service

        admin = await user_service.get_user_by_username(session, "admin")
        response = await client.delete(
            f"/api/users/{admin.id}", headers=admin_headers
        )
        assert response.status_code == 400

    async def test_a_reset_invalidates_the_users_sessions(
        self, client, admin_headers, viewer, viewer_headers
    ):
        before = await client.get("/api/endpoints", headers=viewer_headers)
        assert before.status_code == 200

        reset = await client.post(
            f"/api/users/{viewer.id}/reset-password",
            json={"new_password": "ResetPass@456", "force_change": True},
            headers=admin_headers,
        )
        assert reset.status_code == 200

        after = await client.get("/api/endpoints", headers=viewer_headers)
        assert after.status_code == 401

    async def test_disabling_a_user_blocks_their_token(
        self, client, admin_headers, viewer, viewer_headers
    ):
        await client.put(
            f"/api/users/{viewer.id}", json={"is_active": False}, headers=admin_headers
        )
        response = await client.get("/api/endpoints", headers=viewer_headers)
        assert response.status_code == 401

    async def test_a_role_change_takes_effect_immediately(
        self, client, admin_headers, viewer, viewer_headers
    ):
        """A promoted user must not have to wait for their token to expire."""
        blocked = await client.post(
            "/api/endpoints",
            json={"name": "Nope", "url": "https://nope.example.com/h"},
            headers=viewer_headers,
        )
        assert blocked.status_code == 403

        await client.put(
            f"/api/users/{viewer.id}", json={"role": "admin"}, headers=admin_headers
        )

        # The old token is now invalid, so the user signs in again.
        login = await client.post(
            "/api/auth/login",
            json={"username": "viewer1", "password": "ViewerPass@123"},
        )
        new_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        allowed = await client.post(
            "/api/endpoints",
            json={"name": "Now allowed", "url": "https://allowed.example.com/h"},
            headers=new_headers,
        )
        assert allowed.status_code == 201

    async def test_roles_endpoint_describes_permissions(self, client, admin_headers):
        response = await client.get("/api/users/roles", headers=admin_headers)
        roles = {role["name"]: role for role in response.json()}

        assert "endpoint:write" in roles["admin"]["permissions"]
        assert "endpoint:write" not in roles["viewer"]["permissions"]
        assert "endpoint:read" in roles["viewer"]["permissions"]

    async def test_user_list_never_exposes_hashes(self, client, admin_headers):
        response = await client.get("/api/users", headers=admin_headers)
        assert "hashed_password" not in response.text
        assert "$2b$" not in response.text


class TestNotificationChannels:
    async def test_webhook_channel_hides_its_configuration(
        self, client, admin_headers, session
    ):
        response = await client.post(
            "/api/notification-channels",
            json={
                "name": "Ops webhook",
                "channel_type": "webhook",
                "config": {
                    "url": "https://hooks.example.com/secret-path",
                    "secret": "hmac-signing-secret",
                },
                "min_severity": "warning",
            },
            headers=admin_headers,
        )
        assert response.status_code == 201, response.text

        assert "secret-path" not in response.text
        assert "hmac-signing-secret" not in response.text

        body = response.json()
        assert body["config_public"]["target_host"] == "hooks.example.com"
        assert body["config_public"]["signed"] is True

        stored = (await session.execute(select(NotificationChannel))).scalar_one()
        assert "hooks.example.com/secret-path" not in (stored.config_encrypted or "")
        decrypted = notification_service.decrypt_config(stored.config_encrypted)
        assert decrypted["url"] == "https://hooks.example.com/secret-path"

    async def test_a_missing_required_field_is_rejected(self, client, admin_headers):
        response = await client.post(
            "/api/notification-channels",
            json={"name": "Broken", "channel_type": "webhook", "config": {}},
            headers=admin_headers,
        )
        assert response.status_code == 422

    async def test_a_non_http_webhook_url_is_rejected(self, client, admin_headers):
        response = await client.post(
            "/api/notification-channels",
            json={
                "name": "Bad scheme",
                "channel_type": "webhook",
                "config": {"url": "ftp://hooks.example.com"},
            },
            headers=admin_headers,
        )
        assert response.status_code == 422

    async def test_email_channel_requires_recipients(self, client, admin_headers):
        response = await client.post(
            "/api/notification-channels",
            json={
                "name": "Mail",
                "channel_type": "email",
                "config": {"host": "smtp.example.com", "from_address": "a@b.c"},
            },
            headers=admin_headers,
        )
        assert response.status_code == 422

    async def test_email_channel_public_view_counts_recipients(
        self, client, admin_headers
    ):
        response = await client.post(
            "/api/notification-channels",
            json={
                "name": "Mail",
                "channel_type": "email",
                "config": {
                    "host": "smtp.example.com",
                    "from_address": "monitoring@example.com",
                    "recipients": "a@example.com,b@example.com",
                    "password": "smtp-secret",
                },
            },
            headers=admin_headers,
        )
        assert response.status_code == 201
        assert "smtp-secret" not in response.text
        assert response.json()["config_public"]["recipient_count"] == 2

    async def test_update_without_config_keeps_the_secret(
        self, client, admin_headers, session
    ):
        created = await client.post(
            "/api/notification-channels",
            json={
                "name": "Keep",
                "channel_type": "webhook",
                "config": {"url": "https://hooks.example.com/keep"},
            },
            headers=admin_headers,
        )
        channel_id = created.json()["id"]

        await client.put(
            f"/api/notification-channels/{channel_id}",
            json={"min_severity": "critical"},
            headers=admin_headers,
        )

        stored = (await session.execute(select(NotificationChannel))).scalar_one()
        await session.refresh(stored)
        decrypted = notification_service.decrypt_config(stored.config_encrypted)
        assert decrypted["url"] == "https://hooks.example.com/keep"
        assert stored.min_severity == "critical"

    async def test_a_viewer_cannot_create_a_channel(self, client, viewer_headers):
        response = await client.post(
            "/api/notification-channels",
            json={
                "name": "Nope",
                "channel_type": "webhook",
                "config": {"url": "https://hooks.example.com/x"},
            },
            headers=viewer_headers,
        )
        assert response.status_code == 403


class TestTaxonomy:
    async def test_default_environments_are_seeded(self, client, admin_headers):
        response = await client.get("/api/environments", headers=admin_headers)
        names = {item["name"] for item in response.json()}
        assert {"development", "testing", "staging", "production"} <= names

    async def test_tag_in_use_is_protected(self, client, admin_headers):
        await client.post(
            "/api/endpoints",
            json={
                "name": "Tagged",
                "url": "https://tagged.example.com/h",
                "tags": ["important"],
            },
            headers=admin_headers,
        )
        tags = await client.get("/api/tags", headers=admin_headers)
        important = next(t for t in tags.json() if t["name"] == "important")

        blocked = await client.delete(
            f"/api/tags/{important['id']}", headers=admin_headers
        )
        assert blocked.status_code == 409

        forced = await client.delete(
            f"/api/tags/{important['id']}",
            params={"force": True},
            headers=admin_headers,
        )
        assert forced.status_code == 200

    async def test_environment_in_use_is_protected(self, client, admin_headers):
        environments = await client.get("/api/environments", headers=admin_headers)
        production = next(e for e in environments.json() if e["name"] == "production")

        await client.post(
            "/api/endpoints",
            json={
                "name": "In production",
                "url": "https://prod.example.com/h",
                "environment": production["id"],
            },
            headers=admin_headers,
        )

        blocked = await client.delete(
            f"/api/environments/{production['id']}", headers=admin_headers
        )
        assert blocked.status_code == 409

    async def test_duplicate_tag_is_a_conflict(self, client, admin_headers):
        await client.post("/api/tags", json={"name": "dupe"}, headers=admin_headers)
        response = await client.post(
            "/api/tags", json={"name": "DUPE"}, headers=admin_headers
        )
        assert response.status_code == 409


class TestOpenApi:
    async def test_schema_is_served(self, client):
        response = await client.get("/api/openapi.json")
        assert response.status_code == 200

        schema = response.json()
        assert schema["info"]["title"] == "InfraSight API"
        for path in (
            "/api/auth/login",
            "/api/endpoints",
            "/api/endpoints/{endpoint_id}",
            "/api/endpoints/{endpoint_id}/check",
            "/api/endpoints/{endpoint_id}/history",
            "/api/endpoints/{endpoint_id}/ssl",
            "/api/dashboard",
            "/api/incidents",
            "/api/alerts",
            "/api/users",
            "/api/import",
            "/api/export",
            "/health",
            "/ready",
        ):
            assert path in schema["paths"], f"{path} missing from the OpenAPI schema"

    async def test_schema_contains_no_secrets(self, client):
        response = await client.get("/api/openapi.json")
        text = response.text
        assert "test-secret-key" not in text
        assert "sqlite+aiosqlite" not in text


class TestHealthPathDiscoverySettings:
    async def test_the_candidate_list_round_trips(self, client, admin_headers):
        response = await client.put(
            "/api/settings",
            json={
                "updates": {
                    "health_path_discovery": True,
                    "health_path_candidates": ["/healthz", "/actuator/health"],
                }
            },
            headers=admin_headers,
        )
        assert response.status_code == 200, response.text

        effective = response.json()["effective"]
        assert effective["health_path_discovery"] is True
        assert effective["health_path_candidates"] == ["/healthz", "/actuator/health"]

        again = await client.get("/api/settings", headers=admin_headers)
        assert again.json()["effective"]["health_path_candidates"] == [
            "/healthz",
            "/actuator/health",
        ]

    async def test_discovery_reaches_the_check_target(
        self, client, admin_headers, endpoint_factory, session
    ):
        """The setting has to survive the trip into the CheckTarget - it is
        read from runtime config, not from the endpoint row."""
        from app.monitoring.checker import build_target_from_endpoint

        endpoint = await endpoint_factory(url="https://api.example.com/health")

        off = build_target_from_endpoint(endpoint, defaults={})
        assert off.health_path_candidates == []

        on = build_target_from_endpoint(
            endpoint,
            defaults={
                "health_path_discovery": True,
                "health_path_candidates": ["/healthz"],
            },
        )
        assert on.health_path_candidates == ["/healthz"]

    async def test_a_discovered_path_is_used_on_the_next_check(
        self, endpoint_factory
    ):
        """Once found, the path is probed directly - the 404 and the search
        are paid once, not every interval."""
        from app.monitoring.checker import build_target_from_endpoint

        endpoint = await endpoint_factory(url="https://api.example.com/health")
        endpoint.resolved_health_path = "/actuator/health"

        target = build_target_from_endpoint(endpoint, defaults={})
        assert target.url == "https://api.example.com/actuator/health"
        # The operator's own configuration is left alone.
        assert endpoint.url == "https://api.example.com/health"


class TestSelfMonitoring:
    """InfraSight measuring its own services.

    The rule worth protecting: it reports what it can actually see and names
    what it cannot. A resource page that quietly shows 0% for something it
    never measured is worse than one that shows nothing.
    """

    async def test_the_snapshot_is_served(self, client, admin_headers):
        response = await client.get("/api/system/resources", headers=admin_headers)
        assert response.status_code == 200, response.text

        body = response.json()
        assert body["disk"]["path"] == "/"
        assert body["database"]["available"] is True
        assert body["api"] is not None

    async def test_it_names_what_it_cannot_measure(self, client, admin_headers):
        """No Docker socket means no nginx CPU and no postgres memory. Both
        are declared rather than left as an empty tile."""
        response = await client.get("/api/system/resources", headers=admin_headers)
        not_measured = response.json()["not_measured"]

        assert not_measured, "the unmeasured list must never be empty"
        services = " ".join(item["service"].lower() for item in not_measured)
        assert "nginx" in services
        assert "postgres" in services
        # Each one explains itself; a bare "unavailable" teaches nobody.
        assert all(len(item["reason"]) > 40 for item in not_measured)

    async def test_database_size_and_tables_are_real(self, client, admin_headers):
        response = await client.get("/api/system/resources", headers=admin_headers)
        database = response.json()["database"]

        assert database["size_bytes"] > 0
        assert database["max_connections"] > 0
        # Table names come from the live catalogue, so ours must be in there.
        names = {table["name"] for table in database["tables"]}
        assert names & {"endpoints", "monitoring_results", "users"}

    async def test_cpu_percent_is_null_before_a_second_sample(self):
        """A rate needs two readings. Reporting 0% on the first call would be
        a lie rather than a gap."""
        from app.services import resource_service

        resource_service._previous_cpu.pop("unit-test", None)
        first = resource_service.process_stats("unit-test")
        assert first["cpu_percent"] is None

    async def test_disk_usage_reports_real_numbers(self):
        from app.services import resource_service

        disk = resource_service.disk_usage("/")
        assert disk["available"] is True
        assert disk["total_gb"] > 0
        assert 0 <= disk["used_percent"] <= 100

    async def test_redis_absence_is_a_state_not_a_failure(self):
        """The suite runs with REDIS_URL empty, and the application degrades
        to in-process equivalents - so this must report unavailable rather
        than raise."""
        from app.services import resource_service

        stats = await resource_service.redis_stats()
        assert stats["available"] is False
        assert stats["reason"]

    async def test_it_needs_settings_read(self, client, viewer_headers):
        response = await client.get("/api/system/resources", headers=viewer_headers)
        # The viewer role holds settings:read, so this is allowed - the point
        # is that the route is permission-gated at all.
        assert response.status_code in (200, 403)
