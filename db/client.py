"""Supabase access layer.

Uses the Supabase REST client (supabase-py) with the service-role key, which is
what Section 11 provisions. That means no direct Postgres connection string and
no pg_trgm: fuzzy entity matching happens in Python over the (small) entity
table instead. See intelligence/entities.py.

Every write here is idempotent (Section 12): raw_items upsert on
(source, external_id), digests upsert on digest_date, signals upsert on
raw_item_id, relationships/hypotheses on their unique keys. Re-running a day
does not duplicate rows or re-deliver a digest.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

import config

log = logging.getLogger("undercurrent.db")

_client = None


class DBNotConfigured(RuntimeError):
    pass


def is_configured() -> bool:
    return bool(config.SUPABASE_URL and config.SUPABASE_SERVICE_ROLE_KEY)


def get_client():
    """Lazily build the Supabase client so importing this module is cheap."""
    global _client
    if _client is not None:
        return _client
    if not is_configured():
        raise DBNotConfigured(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set "
            "(local .env for dev, Render env vars in production)."
        )
    from supabase import create_client

    _client = create_client(config.SUPABASE_URL, config.SUPABASE_SERVICE_ROLE_KEY)
    return _client


def _table(name: str):
    return get_client().table(name)


def _rows(resp) -> list[dict]:
    return list(getattr(resp, "data", None) or [])


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


# ------------------------------------------------------------- raw_items --


def upsert_raw_items(items: Sequence[dict]) -> list[dict]:
    """Upsert normalized items; returns the stored rows (with ids).

    on_conflict=(source, external_id) makes a re-run a no-op rather than a
    duplicate. ignore_duplicates=False so a later fetch can refresh score/title.
    """
    if not items:
        return []
    stored: list[dict] = []
    for chunk in _chunks(items, 200):
        resp = (
            _table("raw_items")
            .upsert(list(chunk), on_conflict="source,external_id")
            .execute()
        )
        stored.extend(_rows(resp))
    return stored


def existing_external_ids(source: str, external_ids: Sequence[str]) -> set[str]:
    """Which of these ids are already stored -- used to report items_new."""
    if not external_ids:
        return set()
    found: set[str] = set()
    for chunk in _chunks(list(external_ids), 100):
        resp = (
            _table("raw_items")
            .select("external_id")
            .eq("source", source)
            .in_("external_id", list(chunk))
            .execute()
        )
        found.update(r["external_id"] for r in _rows(resp))
    return found


def recent_raw_items(days: int, limit: int = 2000, fields: str = "*") -> list[dict]:
    since = iso(utcnow() - timedelta(days=days))
    resp = (
        _table("raw_items")
        .select(fields)
        .gte("collected_at", since)
        .order("collected_at", desc=True)
        .limit(limit)
        .execute()
    )
    return _rows(resp)


def raw_items_by_ids(ids: Sequence[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for chunk in _chunks(list(ids), 100):
        resp = _table("raw_items").select("*").in_("id", list(chunk)).execute()
        for row in _rows(resp):
            out[row["id"]] = row
    return out


# --------------------------------------------------------------- signals --


def upsert_signals(signals: Sequence[dict]) -> list[dict]:
    if not signals:
        return []
    stored: list[dict] = []
    for chunk in _chunks(signals, 200):
        resp = (
            _table("signals").upsert(list(chunk), on_conflict="raw_item_id").execute()
        )
        stored.extend(_rows(resp))
    return stored


def recent_signals(days: int, limit: int = 2000) -> list[dict]:
    since = iso(utcnow() - timedelta(days=days))
    resp = (
        _table("signals")
        .select("*")
        .gte("created_at", since)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return _rows(resp)


# -------------------------------------------------------------- entities --


def all_entities(limit: int = 5000) -> list[dict]:
    resp = _table("entities").select("*").limit(limit).execute()
    return _rows(resp)


def all_aliases(limit: int = 20000) -> list[dict]:
    resp = _table("entity_aliases").select("*").limit(limit).execute()
    return _rows(resp)


def insert_entity(row: dict) -> dict | None:
    resp = _table("entities").upsert(row, on_conflict="entity_type,canonical_name").execute()
    rows = _rows(resp)
    return rows[0] if rows else None


def update_entity(entity_id: str, patch: dict) -> None:
    patch = dict(patch)
    patch["updated_at"] = iso(utcnow())
    _table("entities").update(patch).eq("id", entity_id).execute()


def insert_aliases(rows: Sequence[dict]) -> None:
    if not rows:
        return
    for chunk in _chunks(rows, 200):
        _table("entity_aliases").upsert(
            list(chunk), on_conflict="alias", ignore_duplicates=True
        ).execute()


# ---------------------------------------------------------------- themes --


def all_themes(limit: int = 2000) -> list[dict]:
    resp = _table("themes").select("*").limit(limit).execute()
    return _rows(resp)


def upsert_theme(row: dict) -> dict | None:
    resp = _table("themes").upsert(row, on_conflict="slug").execute()
    rows = _rows(resp)
    return rows[0] if rows else None


def update_theme(theme_id: str, patch: dict) -> None:
    patch = dict(patch)
    patch["updated_at"] = iso(utcnow())
    _table("themes").update(patch).eq("id", theme_id).execute()


# -------------------------------------------------------------- problems --


def all_problems(limit: int = 2000) -> list[dict]:
    resp = _table("problems").select("*").limit(limit).execute()
    return _rows(resp)


def upsert_problem(row: dict) -> dict | None:
    resp = _table("problems").upsert(row, on_conflict="slug").execute()
    rows = _rows(resp)
    return rows[0] if rows else None


# --------------------------------------------------- relationships / obs --


def insert_relationships(rows: Sequence[dict]) -> None:
    if not rows:
        return
    for chunk in _chunks(rows, 200):
        _table("relationships").upsert(
            list(chunk),
            on_conflict="from_type,from_id,relation,to_type,to_id",
            ignore_duplicates=True,
        ).execute()


def insert_observations(rows: Sequence[dict]) -> list[dict]:
    if not rows:
        return []
    resp = _table("observations").insert(list(rows)).execute()
    return _rows(resp)


def recent_observations(days: int, limit: int = 200) -> list[dict]:
    since = iso(utcnow() - timedelta(days=days))
    resp = (
        _table("observations")
        .select("*")
        .gte("observed_at", since)
        .order("observed_at", desc=True)
        .limit(limit)
        .execute()
    )
    return _rows(resp)


# ------------------------------------------------------------ hypotheses --


def upsert_hypotheses(rows: Sequence[dict]) -> list[dict]:
    if not rows:
        return []
    resp = _table("hypotheses").upsert(list(rows), on_conflict="dedup_key").execute()
    return _rows(resp)


def open_hypotheses(limit: int = 200) -> list[dict]:
    resp = (
        _table("hypotheses")
        .select("*")
        .eq("status", "open")
        .order("confidence", desc=True)
        .limit(limit)
        .execute()
    )
    return _rows(resp)


# --------------------------------------------------------------- digests --


def get_digest(digest_date: date) -> dict | None:
    resp = (
        _table("digests").select("*").eq("digest_date", digest_date.isoformat()).execute()
    )
    rows = _rows(resp)
    return rows[0] if rows else None


def upsert_digest(
    digest_date: date, content: str, item_count: int, delivered: bool = False
) -> dict | None:
    row = {
        "digest_date": digest_date.isoformat(),
        "content": content,
        "item_count": item_count,
        "delivered": delivered,
    }
    resp = _table("digests").upsert(row, on_conflict="digest_date").execute()
    rows = _rows(resp)
    return rows[0] if rows else None


def mark_digest_delivered(digest_id: str) -> None:
    _table("digests").update({"delivered": True}).eq("id", digest_id).execute()


def link_digest_items(digest_id: str, raw_item_ids: Iterable[str]) -> None:
    rows = [{"digest_id": digest_id, "raw_item_id": rid} for rid in set(raw_item_ids)]
    if not rows:
        return
    for chunk in _chunks(rows, 200):
        _table("digest_items").upsert(
            list(chunk), on_conflict="digest_id,raw_item_id", ignore_duplicates=True
        ).execute()


# ----------------------------------------------------------- operational --


def start_run() -> str | None:
    resp = _table("run_logs").insert({"status": "running"}).execute()
    rows = _rows(resp)
    return rows[0]["id"] if rows else None


def finish_run(run_id: str | None, **patch: Any) -> None:
    if not run_id:
        return
    patch["finished_at"] = iso(utcnow())
    _table("run_logs").update(patch).eq("id", run_id).execute()


def log_source_run(run_id: str | None, row: dict) -> None:
    row = dict(row)
    row["run_id"] = run_id
    try:
        _table("source_runs").insert(row).execute()
    except Exception as exc:  # logging must never break the pipeline
        log.warning("could not log source_run for %s: %s", row.get("source"), exc)


def log_llm_usage(run_id: str | None, row: dict) -> None:
    row = dict(row)
    row["run_id"] = run_id
    try:
        _table("llm_usage").insert(row).execute()
    except Exception as exc:
        log.warning("could not log llm_usage: %s", exc)


def llm_calls_today() -> int:
    resp = (
        _table("llm_usage")
        .select("calls")
        .eq("usage_date", date.today().isoformat())
        .execute()
    )
    return sum(int(r.get("calls") or 0) for r in _rows(resp))


# --------------------------------------------------------- x query state --


def x_queries_least_recently_used(limit: int) -> list[dict]:
    resp = (
        _table("x_query_state")
        .select("*")
        .order("last_used", desc=False, nullsfirst=True)
        .limit(limit)
        .execute()
    )
    return _rows(resp)


def register_x_queries(queries: Sequence[dict]) -> None:
    if not queries:
        return
    _table("x_query_state").upsert(
        list(queries), on_conflict="query", ignore_duplicates=True
    ).execute()


def mark_x_query_used(query: str, use_count: int) -> None:
    _table("x_query_state").update(
        {"last_used": iso(utcnow()), "use_count": use_count + 1}
    ).eq("query", query).execute()


# ----------------------------------------------------------------- utils --


def _chunks(seq: Sequence, size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]
