"""Read-only OpenCode SQLite reply reader (verified fallback from v0.1.0)."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

DEFAULT_DB = Path(os.path.expanduser("~")) / ".local" / "share" / "opencode" / "opencode.db"


def _connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DEFAULT_DB
    uri = f"file:{path.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=5.0)
    con.execute("PRAGMA query_only=1")
    return con


def extract_reply_text(data: dict[str, Any]) -> str:
    """Extract assistant visible text (excluding reasoning blocks)."""
    content = data.get("content")
    if isinstance(content, list):
        parts = [
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text" and item.get("text")
        ]
        return "\n".join(parts).strip()
    if isinstance(data.get("text"), str):
        return data["text"].strip()
    return ""


def is_completed(data: dict[str, Any]) -> bool:
    t = data.get("time")
    return isinstance(t, dict) and bool(t.get("completed"))


def read_latest_reply(
    *,
    title: str | None = None,
    after_seq: int = 0,
    db_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Return latest completed assistant reply from OpenCode's local SQLite DB."""
    path = Path(db_path) if db_path else DEFAULT_DB
    if not path.is_file():
        return None
    con = _connect(path)
    try:
        if title:
            sess = con.execute(
                "SELECT id, title FROM session_v2 WHERE title = ? ORDER BY time_updated DESC LIMIT 1",
                (title,),
            ).fetchone()
        else:
            sess = con.execute(
                "SELECT id, title FROM session_v2 ORDER BY time_updated DESC LIMIT 1"
            ).fetchone()
        if not sess:
            return None
        session_id, session_title = sess[0], sess[1]
        rows = con.execute(
            "SELECT seq, data FROM session_message WHERE session_id = ? AND type = 'assistant' AND seq > ? ORDER BY seq DESC LIMIT 5",
            (session_id, int(after_seq)),
        ).fetchall()
        for seq, raw in rows:
            try:
                data = json.loads(raw)
            except Exception:
                continue
            if not is_completed(data):
                continue
            text = extract_reply_text(data)
            if text:
                return {
                    "session_id": session_id,
                    "title": session_title,
                    "seq": int(seq),
                    "reply": text,
                }
        return None
    finally:
        con.close()


def current_max_seq(*, title: str | None = None, db_path: str | Path | None = None) -> int:
    path = Path(db_path) if db_path else DEFAULT_DB
    if not path.is_file():
        return 0
    con = _connect(path)
    try:
        if title:
            sess = con.execute(
                "SELECT id FROM session_v2 WHERE title = ? ORDER BY time_updated DESC LIMIT 1",
                (title,),
            ).fetchone()
        else:
            sess = con.execute(
                "SELECT id FROM session_v2 ORDER BY time_updated DESC LIMIT 1"
            ).fetchone()
        if not sess:
            return 0
        row = con.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM session_message WHERE session_id = ?",
            (sess[0],),
        ).fetchone()
        return int(row[0] or 0) if row else 0
    finally:
        con.close()
