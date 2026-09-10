"""HTTP wrapper for Render: exactly one internal trigger endpoint.

This is not a user-facing app. It exists because Render's free tier hosts web
services, not cron jobs (Section 9 rules out the paid Cron product), so the
daily pipeline has to sit behind an HTTP request that GitHub Actions can send.
There is no UI, no admin panel, and no read API -- the only output of this
system is the Discord message.

Three routes, and each earns its place:

  POST /trigger   runs the pipeline. Requires RENDER_TRIGGER_TOKEN, so a
                  stranger who finds the URL cannot burn the day's LLM budget.
  GET  /healthz   liveness, unauthenticated. Render pings this; it touches
                  nothing and reveals nothing.
  GET  /          the one public page: what Undercurrent is, and a link to
                  join the Discord where the digest is posted.

The public page exists only to explain the project and hand over the invite.
There is still no dashboard, no login, no signup and no way to read the archive
over HTTP -- the digest is delivered to a channel, not browsed here.

The pipeline takes several minutes, which is longer than most HTTP clients will
wait. /trigger therefore runs it on a background thread and returns 202
immediately; the caller's job is to start the run, not to watch it. Results land
in Discord and in run_logs either way.
"""

from __future__ import annotations

import hmac
import logging
import os
import threading
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template, request

import config
import main as pipeline

log = logging.getLogger("undercurrent.app")

app = Flask(__name__)

# One run at a time. Two concurrent runs would double-spend the LLM budget and
# race each other's upserts; the digest is daily, so serialising is free.
_run_lock = threading.Lock()
_state: dict = {"running": False, "last_started": None, "last_result": None}


def _authorized(req) -> bool:
    """Constant-time comparison against the shared secret.

    Accepts either `Authorization: Bearer <token>` or `?token=` so the workflow
    can use whichever is convenient. If no token is configured the endpoint is
    closed rather than open -- an unset secret must not mean "no auth".
    """
    expected = config.RENDER_TRIGGER_TOKEN
    if not expected:
        return False
    header = req.headers.get("Authorization", "")
    supplied = ""
    if header.startswith("Bearer "):
        supplied = header[7:].strip()
    elif req.headers.get("X-Trigger-Token"):
        supplied = req.headers["X-Trigger-Token"].strip()
    elif req.args.get("token"):
        supplied = req.args["token"].strip()
    return bool(supplied) and hmac.compare_digest(supplied, expected)


def _run_in_background(force: bool, deliver_digest: bool) -> None:
    def worker() -> None:
        try:
            result = pipeline.run(deliver_digest=deliver_digest, force=force)
            _state["last_result"] = result
        except Exception as exc:  # already logged and reported by pipeline.run
            _state["last_result"] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        finally:
            _state["running"] = False
            _run_lock.release()

    threading.Thread(target=worker, name="undercurrent-run", daemon=True).start()


@app.get("/")
def index():
    return render_template("index.html", invite_url=config.DISCORD_INVITE_URL or "")


@app.get("/healthz")
def healthz():
    """Liveness only. Deliberately does not touch the DB or any provider --
    a health check that depends on Supabase would report the service dead
    whenever Supabase is merely slow."""
    return jsonify(
        {
            "ok": True,
            "running": _state["running"],
            "time": datetime.now(timezone.utc).isoformat(),
        }
    )


@app.post("/trigger")
def trigger():
    if not _authorized(request):
        # Same response whether the token is absent, wrong, or unconfigured:
        # distinguishing them tells an attacker which state we are in.
        log.warning("rejected unauthorized trigger from %s", request.remote_addr)
        return jsonify({"error": "unauthorized"}), 401

    force = request.args.get("force", "").lower() in ("1", "true", "yes")
    deliver_digest = request.args.get("deliver", "1").lower() not in ("0", "false", "no")

    if not _run_lock.acquire(blocking=False):
        return (
            jsonify(
                {
                    "status": "already_running",
                    "started_at": _state["last_started"],
                }
            ),
            409,
        )

    _state.update(running=True, last_started=datetime.now(timezone.utc).isoformat())
    _run_in_background(force=force, deliver_digest=deliver_digest)
    log.info("run accepted (force=%s deliver=%s)", force, deliver_digest)

    # 202: the run has started, not finished. The pipeline takes minutes and
    # the caller should not hold a connection open for it.
    return (
        jsonify(
            {
                "status": "accepted",
                "started_at": _state["last_started"],
                "last_result": _state["last_result"],
            }
        ),
        202,
    )


if __name__ == "__main__":
    pipeline.configure_logging()
    # Render provides PORT. Bind all interfaces so the platform can reach it.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
