"""Trigger endpoint auth and concurrency (app.py).

The pipeline itself is stubbed here: what needs testing is that a stranger
cannot start a run, and that two callers cannot start two.
"""

from __future__ import annotations

import pytest

import app as app_module
import config


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(config, "RENDER_TRIGGER_TOKEN", "test-token-123")
    # Never let a test actually run the pipeline.
    monkeypatch.setattr(app_module, "_run_in_background", lambda **kw: None)
    app_module._state.update(running=False, last_started=None, last_result=None)
    # Ensure the lock starts free even if a prior test left it held.
    try:
        app_module._run_lock.release()
    except RuntimeError:
        pass
    app_module.app.config.update(TESTING=True)
    return app_module.app.test_client()


class TestAuth:
    def test_no_token_is_rejected(self, client):
        assert client.post("/trigger").status_code == 401

    def test_wrong_token_is_rejected(self, client):
        assert client.post(
            "/trigger", headers={"Authorization": "Bearer nope"}
        ).status_code == 401

    def test_bearer_token_is_accepted(self, client):
        resp = client.post("/trigger", headers={"Authorization": "Bearer test-token-123"})
        assert resp.status_code == 202
        assert resp.get_json()["status"] == "accepted"

    def test_header_token_is_accepted(self, client):
        assert client.post(
            "/trigger", headers={"X-Trigger-Token": "test-token-123"}
        ).status_code == 202

    def test_query_token_is_accepted(self, client):
        assert client.post("/trigger?token=test-token-123").status_code == 202

    def test_unconfigured_secret_closes_the_endpoint(self, client, monkeypatch):
        """An unset secret must mean 'closed', never 'open to everyone'."""
        monkeypatch.setattr(config, "RENDER_TRIGGER_TOKEN", "")
        assert client.post("/trigger?token=anything").status_code == 401
        assert client.post("/trigger").status_code == 401

    def test_rejection_does_not_reveal_which_failure_it_was(self, client, monkeypatch):
        wrong = client.post("/trigger?token=wrong").get_json()
        monkeypatch.setattr(config, "RENDER_TRIGGER_TOKEN", "")
        unset = client.post("/trigger?token=wrong").get_json()
        assert wrong == unset == {"error": "unauthorized"}


class TestConcurrency:
    def test_second_concurrent_trigger_is_refused(self, client):
        first = client.post("/trigger?token=test-token-123")
        assert first.status_code == 202
        second = client.post("/trigger?token=test-token-123")
        assert second.status_code == 409
        assert second.get_json()["status"] == "already_running"

    def test_lock_is_released_for_the_next_run(self, client):
        client.post("/trigger?token=test-token-123")
        app_module._state["running"] = False
        app_module._run_lock.release()
        assert client.post("/trigger?token=test-token-123").status_code == 202


class TestPublicRoutes:
    def test_healthz_needs_no_auth(self, client):
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_index_serves_the_landing_page(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["Content-Type"]
        body = resp.get_data(as_text=True)
        assert "UNDERCURRENT" in body
        assert "A daily research radar" in body

    def test_there_is_no_read_api(self, client):
        """Still headless by design -- the page explains, it does not expose data."""
        for path in ("/digest", "/digests", "/api", "/admin", "/signals", "/subscribe"):
            assert client.get(path).status_code in (404, 405)


class TestJoinButton:
    """The only interactive element on the page is the Discord invite."""

    def test_shows_the_invite_when_configured(self, client, monkeypatch):
        monkeypatch.setattr(config, "DISCORD_INVITE_URL", "https://discord.gg/abc123")
        body = client.get("/").get_data(as_text=True)
        assert "https://discord.gg/abc123" in body
        # Assert the link is live rather than its wording, which is copy.
        assert 'class="join"' in body
        assert "disabled" not in body.split('class="join"')[1][:40]

    def test_degrades_to_a_placeholder_when_unset(self, client, monkeypatch):
        """An unset invite must not render a dead link."""
        monkeypatch.setattr(config, "DISCORD_INVITE_URL", "")
        body = client.get("/").get_data(as_text=True)
        assert "Invite link coming soon" in body
        assert 'href="https://discord.gg' not in body

    def test_page_collects_nothing(self, client, monkeypatch):
        """No form, no input, no address collection anywhere on the page."""
        monkeypatch.setattr(config, "DISCORD_INVITE_URL", "https://discord.gg/abc123")
        body = client.get("/").get_data(as_text=True).lower()
        assert "<form" not in body
        assert "<input" not in body
        assert "password" not in body

    def test_external_link_is_safely_targeted(self, client, monkeypatch):
        monkeypatch.setattr(config, "DISCORD_INVITE_URL", "https://discord.gg/abc123")
        body = client.get("/").get_data(as_text=True)
        assert 'rel="noopener noreferrer"' in body


class TestPageIsUsableWithoutScript:
    """The page collapses and reveals via a runtime `.js` class.

    If any of that were done in plain CSS, a visitor with JavaScript disabled
    would get a blank page instead of a readable one. These assertions exist
    because that failure is invisible in a normal browser.
    """

    def test_collapse_and_reveal_rules_are_js_gated(self, client):
        css = client.get("/").get_data(as_text=True)
        assert ".js .panel" in css
        assert ".js [data-reveal]" in css

    def test_the_js_class_is_only_added_at_runtime(self, client):
        body = client.get("/").get_data(as_text=True)
        # The served markup must not carry the class already.
        assert '<html lang="en">' in body
        assert 'class="js"' not in body
        assert "classList.add('js')" in body

    def test_panels_default_to_open_in_css(self, client):
        """Base rule is grid-template-rows:1fr; script collapses it, not CSS."""
        css = client.get("/").get_data(as_text=True)
        panel_rule = css.split(".panel {")[1].split("}")[0]
        assert "grid-template-rows: 1fr" in panel_rule

    def test_reduced_motion_disables_the_animations(self, client):
        css = client.get("/").get_data(as_text=True)
        block = css.split("prefers-reduced-motion: reduce")[1].split("}\n\n")[0]
        for selector in ("[data-reveal]", ".join", ".ground::after"):
            assert selector in block


class TestLogo:
    def test_logo_is_referenced_and_dimensioned(self, client):
        """Explicit width/height prevent layout shift while the image loads."""
        body = client.get("/").get_data(as_text=True)
        assert "/static/undercurrent.png" in body
        assert 'width="900"' in body and 'height="141"' in body
        assert 'alt="Undercurrent"' in body

    def test_logo_file_is_served(self, client):
        resp = client.get("/static/undercurrent.png")
        assert resp.status_code == 200
        assert resp.headers["Content-Type"].startswith("image/")


class TestAccordion:
    def test_three_sections_are_present(self, client):
        body = client.get("/").get_data(as_text=True)
        assert body.count('class="acc"') == 3
        for title in ("What is Undercurrent", "What it is about", "What it provides"):
            assert title in body

    def test_headers_are_buttons_with_aria(self, client):
        """Accordion headers must be focusable controls, not styled divs."""
        body = client.get("/").get_data(as_text=True)
        assert body.count('class="acc-head" type="button"') == 3
        assert body.count("aria-expanded") >= 3
        assert body.count("aria-controls") == 3

    def test_each_header_controls_an_existing_panel(self, client):
        import re

        body = client.get("/").get_data(as_text=True)
        controlled = re.findall(r'aria-controls="([^"]+)"', body)
        assert controlled
        for panel_id in controlled:
            assert f'id="{panel_id}"' in body


class TestFlags:
    def test_force_and_deliver_flags_are_parsed(self, client, monkeypatch):
        seen = {}
        monkeypatch.setattr(app_module, "_run_in_background", lambda **kw: seen.update(kw))
        client.post("/trigger?token=test-token-123&force=1&deliver=0")
        assert seen == {"force": True, "deliver_digest": False}

    def test_defaults_deliver_and_do_not_force(self, client, monkeypatch):
        seen = {}
        monkeypatch.setattr(app_module, "_run_in_background", lambda **kw: seen.update(kw))
        client.post("/trigger?token=test-token-123")
        assert seen == {"force": False, "deliver_digest": True}
