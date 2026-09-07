"""Deployment branding, the uploaded logo, and the optional-module flags."""

from __future__ import annotations

import struct
import zlib

import pytest

from app.services import branding_service


def _png(width: int = 1, height: int = 1) -> bytes:
    """A real, minimal PNG.

    Built rather than fixtured because the upload path identifies formats from
    magic bytes - a file of zeroes named .png would (correctly) be refused, so
    the test needs bytes a browser would also accept.
    """

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class TestBrandingEndpoint:
    async def test_is_reachable_without_a_token(self, client):
        """The sign-in screen reads it before there is a session."""
        response = await client.get("/branding")
        assert response.status_code == 200
        body = response.json()
        assert body["app_name"] == "InfraSight"
        assert body["logo_url"] is None

    async def test_reflects_a_saved_name(self, client, admin_headers):
        saved = await client.put(
            "/api/settings",
            headers=admin_headers,
            json={"updates": {"branding_app_name": "Acme Monitoring"}},
        )
        assert saved.status_code == 200
        assert (await client.get("/branding")).json()["app_name"] == "Acme Monitoring"

    async def test_blank_name_falls_back_to_the_built_in(self, client, admin_headers):
        await client.put(
            "/api/settings",
            headers=admin_headers,
            json={"updates": {"branding_app_name": ""}},
        )
        assert (await client.get("/branding")).json()["app_name"] == "InfraSight"

    async def test_a_name_that_would_break_the_header_is_rejected(
        self, client, admin_headers
    ):
        response = await client.put(
            "/api/settings",
            headers=admin_headers,
            json={"updates": {"branding_app_name": "x" * 200}},
        )
        assert response.status_code == 422

    async def test_exposes_nothing_but_branding(self, client):
        """Regression guard: this response is public."""
        assert set((await client.get("/branding")).json()) == {"app_name", "logo_url"}


class TestLogoUpload:
    async def test_upload_then_serve_then_remove(self, client, admin_headers):
        png = _png()
        uploaded = await client.post(
            "/api/settings/branding/logo",
            headers=admin_headers,
            files={"file": ("logo.png", png, "image/png")},
        )
        assert uploaded.status_code == 200, uploaded.text
        logo_url = uploaded.json()["logo_url"]
        assert logo_url and logo_url.startswith("/branding/logo?v=")

        # Public, and byte-identical to what was uploaded.
        served = await client.get("/branding/logo")
        assert served.status_code == 200
        assert served.content == png
        assert served.headers["content-type"] == "image/png"
        assert served.headers["x-content-type-options"] == "nosniff"

        removed = await client.delete(
            "/api/settings/branding/logo", headers=admin_headers
        )
        assert removed.status_code == 200
        assert (await client.get("/branding/logo")).status_code == 404
        assert (await client.get("/branding")).json()["logo_url"] is None

    async def test_etag_allows_a_conditional_request(self, client, admin_headers):
        await client.post(
            "/api/settings/branding/logo",
            headers=admin_headers,
            files={"file": ("logo.png", _png(), "image/png")},
        )
        first = await client.get("/branding/logo")
        etag = first.headers["etag"]
        again = await client.get("/branding/logo", headers={"If-None-Match": etag})
        assert again.status_code == 304

    async def test_replacing_the_logo_changes_the_url(self, client, admin_headers):
        """The URL carries a content hash, so a cached copy cannot go stale."""
        first = await client.post(
            "/api/settings/branding/logo",
            headers=admin_headers,
            files={"file": ("a.png", _png(1, 1), "image/png")},
        )
        second = await client.post(
            "/api/settings/branding/logo",
            headers=admin_headers,
            files={"file": ("b.png", _png(2, 2), "image/png")},
        )
        assert first.json()["logo_url"] != second.json()["logo_url"]

    async def test_a_file_that_is_not_an_image_is_refused(self, client, admin_headers):
        """Declared content type is attacker-controlled; bytes decide."""
        response = await client.post(
            "/api/settings/branding/logo",
            headers=admin_headers,
            files={"file": ("logo.png", b"not really a png", "image/png")},
        )
        assert response.status_code == 400

    async def test_svg_is_refused(self, client, admin_headers):
        """SVG can carry script and is served from this origin."""
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        response = await client.post(
            "/api/settings/branding/logo",
            headers=admin_headers,
            files={"file": ("logo.svg", svg, "image/svg+xml")},
        )
        assert response.status_code == 400

    async def test_an_oversized_file_is_refused(self, client, admin_headers):
        oversized = _png() + b"\x00" * (branding_service.MAX_LOGO_BYTES + 1)
        response = await client.post(
            "/api/settings/branding/logo",
            headers=admin_headers,
            files={"file": ("logo.png", oversized, "image/png")},
        )
        assert response.status_code == 400

    async def test_a_viewer_cannot_upload(self, client, viewer_headers):
        response = await client.post(
            "/api/settings/branding/logo",
            headers=viewer_headers,
            files={"file": ("logo.png", _png(), "image/png")},
        )
        assert response.status_code == 403


class TestFeatureFlags:
    async def test_default_to_enabled(self, client, admin_headers):
        response = await client.get("/api/features", headers=admin_headers)
        assert response.status_code == 200
        assert response.json() == {"change_management": True, "rca": True}

    async def test_readable_by_a_viewer(self, client, viewer_headers):
        """The navigation needs these, and a viewer holds no settings:read."""
        response = await client.get("/api/features", headers=viewer_headers)
        assert response.status_code == 200

    async def test_require_authentication(self, client):
        assert (await client.get("/api/features")).status_code == 401

    @pytest.mark.parametrize(
        ("setting", "flag", "path"),
        [
            ("feature_change_management_enabled", "change_management", "/api/changes"),
            ("feature_rca_enabled", "rca", "/api/rca"),
        ],
    )
    async def test_disabling_a_module_refuses_its_api(
        self, client, admin_headers, setting, flag, path
    ):
        assert (await client.get(path, headers=admin_headers)).status_code == 200

        await client.put(
            "/api/settings", headers=admin_headers, json={"updates": {setting: False}}
        )

        assert (await client.get("/api/features", headers=admin_headers)).json()[
            flag
        ] is False
        blocked = await client.get(path, headers=admin_headers)
        assert blocked.status_code == 403
        assert "disabled" in blocked.json()["detail"].lower()

    async def test_disabling_rca_leaves_the_dashboard_intelligence_alone(
        self, client, admin_headers
    ):
        """The RCA router also carries /intelligence, which must stay up."""
        await client.put(
            "/api/settings",
            headers=admin_headers,
            json={"updates": {"feature_rca_enabled": False}},
        )
        response = await client.get("/api/intelligence/summary", headers=admin_headers)
        assert response.status_code == 200

    async def test_re_enabling_restores_access(self, client, admin_headers):
        await client.put(
            "/api/settings",
            headers=admin_headers,
            json={"updates": {"feature_change_management_enabled": False}},
        )
        await client.put(
            "/api/settings",
            headers=admin_headers,
            json={"updates": {"feature_change_management_enabled": True}},
        )
        assert (await client.get("/api/changes", headers=admin_headers)).status_code == 200


class TestEmojiFeaturesAreGone:
    """The emoji avatar and emoji logo were removed; nothing should remain."""

    async def test_no_avatar_field_on_the_user(self, client, admin_headers):
        body = (await client.get("/api/auth/me", headers=admin_headers)).json()
        assert "avatar_emoji" not in body

    async def test_the_profile_route_is_gone(self, client, admin_headers):
        response = await client.patch(
            "/api/auth/me", headers=admin_headers, json={"full_name": "Ada"}
        )
        assert response.status_code == 405

    async def test_the_emoji_logo_setting_is_gone(self, client, admin_headers):
        response = await client.put(
            "/api/settings",
            headers=admin_headers,
            json={"updates": {"branding_logo_text": "🛡"}},
        )
        assert response.status_code == 422
