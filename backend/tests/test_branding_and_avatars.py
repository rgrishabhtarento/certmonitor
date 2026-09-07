"""Per-deployment branding and per-user avatar emoji."""

from __future__ import annotations

import pytest

from app.schemas.user import validate_avatar_emoji


class TestBrandingEndpoint:
    async def test_is_reachable_without_a_token(self, client):
        """The sign-in screen reads it before there is a session."""
        response = await client.get("/branding")
        assert response.status_code == 200
        assert response.json()["app_name"] == "InfraSight"

    async def test_reflects_a_saved_name_and_logo(self, client, admin_headers):
        saved = await client.put(
            "/api/settings",
            headers=admin_headers,
            json={
                "updates": {
                    "branding_app_name": "Acme Monitoring",
                    "branding_logo_text": "🛡",
                }
            },
        )
        assert saved.status_code == 200

        response = await client.get("/branding")
        assert response.json() == {"app_name": "Acme Monitoring", "logo_text": "🛡"}

    async def test_blank_name_falls_back_to_the_built_in(self, client, admin_headers):
        await client.put(
            "/api/settings",
            headers=admin_headers,
            json={"updates": {"branding_app_name": ""}},
        )
        response = await client.get("/branding")
        assert response.json()["app_name"] == "InfraSight"

    async def test_a_name_that_would_break_the_header_is_rejected(
        self, client, admin_headers
    ):
        response = await client.put(
            "/api/settings",
            headers=admin_headers,
            json={"updates": {"branding_app_name": "x" * 200}},
        )
        assert response.status_code == 422

    async def test_exposes_nothing_but_branding(self, client, admin_headers):
        """Regression guard: this response is public."""
        response = await client.get("/branding")
        assert set(response.json()) == {"app_name", "logo_text"}


class TestAvatarValidation:
    @pytest.mark.parametrize(
        "value",
        ["🙂", "🛡️", "👨‍💻", "🇮🇳", "👍🏽"],
    )
    def test_accepts_emoji_including_joined_and_modified(self, value):
        assert validate_avatar_emoji(value) == value

    @pytest.mark.parametrize("value", ["", "   ", None])
    def test_blank_clears_the_avatar(self, value):
        assert validate_avatar_emoji(value) is None

    @pytest.mark.parametrize(
        "value",
        ["ab", "<script>x</script>", "hello", "1", "🙂🙂🙂🙂🙂"],
    )
    def test_rejects_text_markup_and_overlong_input(self, value):
        with pytest.raises(ValueError):
            validate_avatar_emoji(value)


class TestProfileUpdate:
    async def test_a_user_can_set_and_clear_their_own_emoji(
        self, client, admin_headers
    ):
        response = await client.patch(
            "/api/auth/me", headers=admin_headers, json={"avatar_emoji": "🚀"}
        )
        assert response.status_code == 200
        assert response.json()["avatar_emoji"] == "🚀"

        # Confirm it persisted rather than only being echoed back.
        assert (await client.get("/api/auth/me", headers=admin_headers)).json()[
            "avatar_emoji"
        ] == "🚀"

        cleared = await client.patch(
            "/api/auth/me", headers=admin_headers, json={"avatar_emoji": ""}
        )
        assert cleared.json()["avatar_emoji"] is None

    async def test_a_viewer_can_set_their_own_emoji(self, client, viewer_headers):
        """No extra permission required - it is the caller's own account."""
        response = await client.patch(
            "/api/auth/me", headers=viewer_headers, json={"avatar_emoji": "🦊"}
        )
        assert response.status_code == 200
        assert response.json()["avatar_emoji"] == "🦊"

    async def test_omitted_fields_are_left_alone(self, client, admin_headers):
        await client.patch(
            "/api/auth/me", headers=admin_headers, json={"avatar_emoji": "🐧"}
        )
        response = await client.patch(
            "/api/auth/me", headers=admin_headers, json={"full_name": "Ada"}
        )
        body = response.json()
        assert body["full_name"] == "Ada"
        assert body["avatar_emoji"] == "🐧"

    async def test_cannot_escalate_through_the_profile_route(
        self, client, viewer_headers
    ):
        """Role is not a profile field, so an extra key must not take effect."""
        response = await client.patch(
            "/api/auth/me", headers=viewer_headers, json={"role": "admin"}
        )
        assert response.json()["role"] == "viewer"

    async def test_text_is_refused(self, client, admin_headers):
        response = await client.patch(
            "/api/auth/me", headers=admin_headers, json={"avatar_emoji": "admin"}
        )
        assert response.status_code == 422
