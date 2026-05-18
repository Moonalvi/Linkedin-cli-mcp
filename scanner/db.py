from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import DB_PATH, ensure_app_dirs


SNAPSHOT_OFFSETS = [
    ("1h", timedelta(hours=1)),
    ("6h", timedelta(hours=6)),
    ("24h", timedelta(hours=24)),
    ("72h", timedelta(hours=72)),
    ("7d", timedelta(days=7)),
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime | None = None) -> str:
    return (dt or utc_now()).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def connect() -> sqlite3.Connection:
    ensure_app_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"pragma table_info({table})")}


def _add_column(conn: sqlite3.Connection, table: str, name: str, definition: str) -> None:
    if name not in _columns(conn, table):
        conn.execute(f"alter table {table} add column {name} {definition}")


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            create table if not exists posts (
              canonical_urn text primary key,
              canonical_url text,
              author text,
              post_title text,
              post_text text,
              body_text text,
              published_at_raw text,
              published_at text,
              discovered_at text not null,
              hook_text text,
              hook_type text,
              cta_text text,
              cta_type text,
              topic text,
              format text,
              body_structure text,
              media_type text,
              last_scanned_at text,
              completed_at text
            );

            create table if not exists scan_due (
              id integer primary key autoincrement,
              canonical_urn text not null,
              snapshot_window text not null,
              due_at text not null,
              status text not null default 'pending',
              attempts integer not null default 0,
              last_error text,
              unique(canonical_urn, snapshot_window)
            );

            create table if not exists snapshots (
              id integer primary key autoincrement,
              canonical_urn text not null,
              snapshot_window text,
              captured_at text not null,
              capture_timestamp text,
              payload_json text not null,
              profile_clicks integer,
              follower_count_at_capture integer,
              missing_fields_json text,
              warnings_json text,
              sync_status text not null default 'queued',
              sync_error text,
              schema_version text
            );

            create table if not exists logs (
              id integer primary key autoincrement,
              created_at text not null,
              level text not null,
              message text not null
            );
            """
        )
        for name, definition in (
            ("post_title", "text"),
            ("body_text", "text"),
            ("published_at_raw", "text"),
            ("hook_text", "text"),
            ("hook_type", "text"),
            ("cta_text", "text"),
            ("cta_type", "text"),
            ("topic", "text"),
            ("format", "text"),
            ("body_structure", "text"),
            ("media_type", "text"),
        ):
            _add_column(conn, "posts", name, definition)
        for name, definition in (
            ("capture_timestamp", "text"),
            ("profile_clicks", "integer"),
            ("follower_count_at_capture", "integer"),
            ("missing_fields_json", "text"),
            ("warnings_json", "text"),
            ("schema_version", "text"),
        ):
            _add_column(conn, "snapshots", name, definition)
        conn.execute(
            """
            update posts
            set hook_text = null,
                hook_type = null,
                cta_text = null,
                cta_type = null,
                topic = null,
                format = null,
                body_structure = null
            """
        )


def log(level: str, message: str) -> None:
    with connect() as conn:
        conn.execute(
            "insert into logs(created_at, level, message) values (?, ?, ?)",
            (iso(), level, message[:2000]),
        )


def upsert_post(post: dict[str, Any]) -> None:
    canonical_urn = str(post.get("canonical_urn") or "").strip()
    if not canonical_urn:
        return
    discovered_at = post.get("discovered_at") or iso()
    published_at = post.get("published_at") or discovered_at
    with connect() as conn:
        conn.execute(
            """
            insert into posts(
              canonical_urn, canonical_url, author, post_title, post_text, body_text,
              published_at_raw, published_at, discovered_at, hook_text, hook_type,
              cta_text, cta_type, topic, format, body_structure, media_type
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(canonical_urn) do update set
              canonical_url=excluded.canonical_url,
              author=coalesce(nullif(excluded.author, ''), posts.author),
              post_title=coalesce(nullif(excluded.post_title, ''), posts.post_title),
              post_text=coalesce(nullif(excluded.post_text, ''), posts.post_text),
              body_text=coalesce(nullif(excluded.body_text, ''), posts.body_text),
              published_at_raw=coalesce(nullif(excluded.published_at_raw, ''), posts.published_at_raw),
              published_at=coalesce(excluded.published_at, posts.published_at),
              hook_text=null,
              hook_type=null,
              cta_text=null,
              cta_type=null,
              topic=null,
              format=null,
              body_structure=null,
              media_type=coalesce(nullif(excluded.media_type, ''), posts.media_type)
            """,
            (
                canonical_urn,
                str(post.get("canonical_url") or ""),
                str(post.get("author") or ""),
                str(post.get("post_title") or ""),
                str(post.get("post_text") or ""),
                str(post.get("body_text") or post.get("post_text") or ""),
                str(post.get("published_at_raw") or ""),
                published_at,
                discovered_at,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                str(post.get("media_type") or ""),
            ),
        )
        anchor = parse_iso(published_at) or parse_iso(discovered_at) or utc_now()
        age = utc_now() - anchor
        schedule = [("historical", timedelta())] if age > timedelta(days=7) else SNAPSHOT_OFFSETS
        if age > timedelta(days=7):
            conn.execute(
                """
                delete from scan_due
                where canonical_urn = ?
                  and snapshot_window != 'historical'
                  and status in ('pending', 'retry')
                """,
                (canonical_urn,),
            )
        for window, offset in schedule:
            due_at = utc_now() if window == "historical" else anchor + offset
            conn.execute(
                """
                insert into scan_due(canonical_urn, snapshot_window, due_at)
                values (?, ?, ?)
                on conflict(canonical_urn, snapshot_window) do update set
                  status = 'pending',
                  due_at = excluded.due_at,
                  last_error = null,
                  attempts = 0
                """,
                (canonical_urn, window, iso(due_at)),
            )


def due_scans(limit: int = 5, include_not_due: bool = False) -> list[sqlite3.Row]:
    with connect() as conn:
        due_filter = "" if include_not_due else "and scan_due.due_at <= ?"
        params: tuple[Any, ...] = (limit,) if include_not_due else (iso(), limit)
        return list(
            conn.execute(
                f"""
                select
                  scan_due.*,
                  posts.canonical_url, posts.author, posts.post_title, posts.post_text,
                  posts.body_text, posts.published_at_raw, posts.published_at,
                  posts.hook_text, posts.hook_type, posts.cta_text, posts.cta_type,
                  posts.topic, posts.format, posts.body_structure, posts.media_type
                from scan_due
                join posts on posts.canonical_urn = scan_due.canonical_urn
                where scan_due.status in ('pending', 'retry') {due_filter}
                order by scan_due.due_at asc
                limit ?
                """,
                params,
            )
        )


def record_snapshot(canonical_urn: str, snapshot_window: str, payload: dict[str, Any]) -> int:
    capture = payload.get("capture") if isinstance(payload.get("capture"), dict) else {}
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    capture_timestamp = str(capture.get("capture_timestamp") or iso())
    missing_fields = capture.get("missing_fields") if isinstance(capture.get("missing_fields"), list) else []
    warnings = capture.get("warnings") if isinstance(capture.get("warnings"), list) else []
    schema_version = str(payload.get("schema_version") or "v1")
    with connect() as conn:
        cursor = conn.execute(
            """
            insert into snapshots(
              canonical_urn, snapshot_window, captured_at, capture_timestamp,
              payload_json, profile_clicks, follower_count_at_capture,
              missing_fields_json, warnings_json, schema_version
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                canonical_urn,
                snapshot_window,
                capture_timestamp,
                capture_timestamp,
                json.dumps(payload),
                metrics.get("profile_clicks"),
                metrics.get("follower_count_at_capture"),
                json.dumps(missing_fields),
                json.dumps(warnings),
                schema_version,
            ),
        )
        conn.execute(
            "update posts set last_scanned_at = ? where canonical_urn = ?",
            (iso(), canonical_urn),
        )
        return int(cursor.lastrowid)


def mark_due_done(scan_id: int) -> None:
    with connect() as conn:
        conn.execute("update scan_due set status = 'done', last_error = null where id = ?", (scan_id,))


def mark_due_failed(scan_id: int, error: str) -> None:
    with connect() as conn:
        conn.execute(
            "update scan_due set status = 'retry', attempts = attempts + 1, last_error = ? where id = ?",
            (error[:1000], scan_id),
        )


def queued_snapshots(limit: int = 10) -> list[sqlite3.Row]:
    with connect() as conn:
        return list(conn.execute("select * from snapshots where sync_status = 'queued' order by id asc limit ?", (limit,)))


def mark_snapshot_synced(snapshot_id: int) -> None:
    with connect() as conn:
        conn.execute("update snapshots set sync_status = 'synced', sync_error = null where id = ?", (snapshot_id,))


def mark_snapshot_sync_failed(snapshot_id: int, error: str) -> None:
    with connect() as conn:
        conn.execute("update snapshots set sync_status = 'queued', sync_error = ? where id = ?", (error[:1000], snapshot_id))


def status_summary() -> dict[str, Any]:
    with connect() as conn:
        pending = conn.execute("select count(*) as c from scan_due where status in ('pending', 'retry')").fetchone()["c"]
        queued = conn.execute("select count(*) as c from snapshots where sync_status = 'queued'").fetchone()["c"]
        next_due = conn.execute("select min(due_at) as v from scan_due where status in ('pending', 'retry')").fetchone()["v"]
        last_scan = conn.execute("select max(captured_at) as v from snapshots").fetchone()["v"]
    return {"pending_scans": pending, "queued_snapshots": queued, "next_due_at": next_due, "last_scan_at": last_scan}