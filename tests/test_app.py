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

    def test_index_identifies_the_service(self, client):
        body = client.get("/").get_json()
        assert body["service"] == "undercurrent"

    def test_there_is_no_read_api(self, client):
        """This is headless by design -- no digest/data routes exist."""
        for path in ("/digest", "/digests", "/api", "/admin", "/signals"):
            assert client.get(path).status_code == 404


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
