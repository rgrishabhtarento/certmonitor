"""Authentication, password policy and role authorisation."""

from __future__ import annotations

import pytest

from app.core.security import decode_token, verify_password
from app.services import user_service


class TestLogin:
    async def test_admin_can_sign_in(self, client):
        response = await client.post(
            "/api/auth/login", json={"username": "admin", "password": "Passwd@123"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["token_type"] == "bearer"
        assert body["access_token"]
        assert body["refresh_token"]
        assert body["user"]["username"] == "admin"
        assert body["user"]["role"] == "admin"

    async def test_response_never_contains_a_password(self, client):
        response = await client.post(
            "/api/auth/login", json={"username": "admin", "password": "Passwd@123"}
        )
        raw = response.text.lower()
        assert "passwd@123" not in raw
        assert "hashed_password" not in raw
        assert "$2b$" not in raw

    async def test_wrong_password_is_rejected(self, client):
        response = await client.post(
            "/api/auth/login", json={"username": "admin", "password": "wrong"}
        )
        assert response.status_code == 401

    async def test_unknown_user_and_wrong_password_are_indistinguishable(self, client):
        """The error must not let an attacker enumerate accounts."""
        unknown = await client.post(
            "/api/auth/login", json={"username": "nobody", "password": "whatever"}
        )
        wrong = await client.post(
            "/api/auth/login", json={"username": "admin", "password": "whatever"}
        )
        assert unknown.status_code == wrong.status_code == 401
        assert unknown.json()["detail"] == wrong.json()["detail"]

    async def test_token_carries_the_role_and_version(self, client):
        response = await client.post(
            "/api/auth/login", json={"username": "admin", "password": "Passwd@123"}
        )
        claims = decode_token(response.json()["access_token"], expected_type="access")
        assert claims["role"] == "admin"
        assert claims["type"] == "access"
        assert "tv" in claims

    async def test_disabled_account_cannot_sign_in(self, client, session, viewer):
        viewer.is_active = False
        await session.commit()
        response = await client.post(
            "/api/auth/login",
            json={"username": "viewer1", "password": "ViewerPass@123"},
        )
        assert response.status_code == 401


class TestLockout:
    async def test_repeated_failures_lock_the_account(self, client, viewer):
        """ACCOUNT_LOCKOUT_ATTEMPTS is 5 in the test environment."""
        for _ in range(5):
            await client.post(
                "/api/auth/login",
                json={"username": "viewer1", "password": "wrong-password"},
            )

        response = await client.post(
            "/api/auth/login",
            json={"username": "viewer1", "password": "ViewerPass@123"},
        )
        assert response.status_code == 423
        assert "locked" in response.json()["detail"].lower()

    async def test_an_admin_can_clear_a_lockout(
        self, client, admin_headers, viewer, session
    ):
        for _ in range(5):
            await client.post(
                "/api/auth/login",
                json={"username": "viewer1", "password": "wrong-password"},
            )

        response = await client.put(
            f"/api/users/{viewer.id}", json={"unlock": True}, headers=admin_headers
        )
        assert response.status_code == 200

        login = await client.post(
            "/api/auth/login",
            json={"username": "viewer1", "password": "ViewerPass@123"},
        )
        assert login.status_code == 200


class TestTokens:
    async def test_protected_route_requires_a_token(self, client):
        response = await client.get("/api/endpoints")
        assert response.status_code == 401

    async def test_garbage_token_is_rejected(self, client):
        response = await client.get(
            "/api/endpoints", headers={"Authorization": "Bearer not-a-jwt"}
        )
        assert response.status_code == 401

    async def test_refresh_returns_a_new_access_token(self, client):
        login = await client.post(
            "/api/auth/login", json={"username": "admin", "password": "Passwd@123"}
        )
        refresh_token = login.json()["refresh_token"]

        response = await client.post(
            "/api/auth/refresh", json={"refresh_token": refresh_token}
        )
        assert response.status_code == 200
        assert response.json()["access_token"]

    async def test_an_access_token_cannot_be_used_to_refresh(self, client, admin_token):
        response = await client.post(
            "/api/auth/refresh", json={"refresh_token": admin_token}
        )
        assert response.status_code == 401

    async def test_me_returns_the_caller(self, client, admin_headers):
        response = await client.get("/api/auth/me", headers=admin_headers)
        assert response.status_code == 200
        assert response.json()["username"] == "admin"
        assert "endpoint:write" in response.json()["permissions"]


class TestPasswordChange:
    async def test_change_password_succeeds_and_reissues_tokens(
        self, client, admin_headers
    ):
        response = await client.post(
            "/api/auth/change-password",
            json={
                "current_password": "Passwd@123",
                "new_password": "BrandNewPass@456",
            },
            headers=admin_headers,
        )
        assert response.status_code == 200
        assert response.json()["access_token"]

        # The new password works.
        login = await client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "BrandNewPass@456"},
        )
        assert login.status_code == 200

        # The old one does not.
        old = await client.post(
            "/api/auth/login", json={"username": "admin", "password": "Passwd@123"}
        )
        assert old.status_code == 401

    async def test_old_tokens_stop_working_after_a_change(self, client, admin_headers):
        """A password change bumps token_version, invalidating prior tokens."""
        await client.post(
            "/api/auth/change-password",
            json={
                "current_password": "Passwd@123",
                "new_password": "BrandNewPass@456",
            },
            headers=admin_headers,
        )
        response = await client.get("/api/endpoints", headers=admin_headers)
        assert response.status_code == 401

    async def test_wrong_current_password_is_rejected(self, client, admin_headers):
        response = await client.post(
            "/api/auth/change-password",
            json={"current_password": "nope", "new_password": "BrandNewPass@456"},
            headers=admin_headers,
        )
        assert response.status_code == 400

    @pytest.mark.parametrize(
        "weak",
        [
            "short",           # too short
            "alllowercase1!",  # no upper case
            "ALLUPPERCASE1!",  # no lower case
            "NoDigitsHere!!",  # no digit
            "NoSymbols12345",  # no special character
        ],
    )
    async def test_policy_rejects_weak_passwords(self, client, admin_headers, weak):
        response = await client.post(
            "/api/auth/change-password",
            json={"current_password": "Passwd@123", "new_password": weak},
            headers=admin_headers,
        )
        assert response.status_code == 422

    async def test_reusing_the_current_password_is_rejected(
        self, client, admin_headers
    ):
        response = await client.post(
            "/api/auth/change-password",
            json={"current_password": "Passwd@123", "new_password": "Passwd@123"},
            headers=admin_headers,
        )
        assert response.status_code == 422

    async def test_password_is_stored_only_as_a_hash(self, session, seeded):
        admin = await user_service.get_user_by_username(session, "admin")
        assert admin.hashed_password != "Passwd@123"
        assert admin.hashed_password.startswith("$2")
        assert verify_password("Passwd@123", admin.hashed_password)


class TestRoleAuthorisation:
    async def test_viewer_can_read_endpoints(self, client, viewer_headers):
        response = await client.get("/api/endpoints", headers=viewer_headers)
        assert response.status_code == 200

    async def test_viewer_cannot_create_an_endpoint(self, client, viewer_headers):
        response = await client.post(
            "/api/endpoints",
            json={"name": "Blocked", "url": "https://example.com/health"},
            headers=viewer_headers,
        )
        assert response.status_code == 403

    async def test_viewer_cannot_list_users(self, client, viewer_headers):
        response = await client.get("/api/users", headers=viewer_headers)
        assert response.status_code == 403

    async def test_viewer_cannot_change_settings(self, client, viewer_headers):
        response = await client.put(
            "/api/settings",
            json={"updates": {"ssl_warning_days": 45}},
            headers=viewer_headers,
        )
        assert response.status_code == 403

    async def test_viewer_cannot_read_the_audit_log(self, client, viewer_headers):
        response = await client.get("/api/audit-logs", headers=viewer_headers)
        assert response.status_code == 403

    async def test_viewer_can_read_settings(self, client, viewer_headers):
        """A viewer needs the thresholds to interpret what they are shown."""
        response = await client.get("/api/settings", headers=viewer_headers)
        assert response.status_code == 200

    async def test_admin_holds_every_permission(self, client, admin_headers):
        response = await client.get("/api/auth/me", headers=admin_headers)
        permissions = response.json()["permissions"]
        for code in (
            "endpoint:write",
            "endpoint:delete",
            "user:write",
            "settings:write",
            "audit:read",
        ):
            assert code in permissions


class TestForcedPasswordChange:
    async def test_must_change_password_blocks_other_routes(
        self, client, session, seeded
    ):
        admin = await user_service.get_user_by_username(session, "admin")
        admin.must_change_password = True
        await session.commit()

        login = await client.post(
            "/api/auth/login", json={"username": "admin", "password": "Passwd@123"}
        )
        assert login.status_code == 200
        assert login.json()["must_change_password"] is True
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        blocked = await client.get("/api/endpoints", headers=headers)
        assert blocked.status_code == 403
        assert blocked.headers.get("x-password-change-required") == "true"

        # The password-change route itself stays reachable.
        allowed = await client.post(
            "/api/auth/change-password",
            json={
                "current_password": "Passwd@123",
                "new_password": "FreshStart@2026",
            },
            headers=headers,
        )
        assert allowed.status_code == 200

        new_headers = {
            "Authorization": f"Bearer {allowed.json()['access_token']}"
        }
        unblocked = await client.get("/api/endpoints", headers=new_headers)
        assert unblocked.status_code == 200


class TestAuditTrail:
    async def test_login_is_audited(self, client, admin_headers):
        response = await client.get(
            "/api/audit-logs", params={"action": "login"}, headers=admin_headers
        )
        assert response.status_code == 200
        actions = [row["action"] for row in response.json()["items"]]
        assert "login" in actions

    async def test_failed_login_is_audited(self, client, admin_headers):
        await client.post(
            "/api/auth/login", json={"username": "admin", "password": "wrong"}
        )
        response = await client.get(
            "/api/audit-logs", params={"action": "login_failed"}, headers=admin_headers
        )
        assert response.json()["meta"]["total"] >= 1

    async def test_audit_details_never_contain_a_password(
        self, client, admin_headers
    ):
        await client.post(
            "/api/auth/login", json={"username": "admin", "password": "hunter2secret"}
        )
        response = await client.get("/api/audit-logs", headers=admin_headers)
        assert "hunter2secret" not in response.text
