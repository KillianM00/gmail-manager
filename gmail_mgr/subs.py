"""Subscription registry: SQLite-backed history of every sender we've seen.

Schema: one row per sender address. Tracks total observed message count, total
size in bytes, last-seen timestamp, and a status string.

Status values:
  - "active":       seen in a scan, no action taken
  - "unsubscribed": unsubscribe sweep succeeded
  - "trashed":      bulk-trashed (history of mass-deletes)
  - "blocked":      Gmail filter created to auto-trash future mail
  - "archived":     bulk-archived
"""
import sqlite3
import time
from contextlib import contextmanager
from typing import Iterable

from . import config as user_config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS senders (
    address TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    domain TEXT NOT NULL DEFAULT '',
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    total_bytes INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    status_changed REAL NOT NULL,
    notes TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_senders_status ON senders(status);
CREATE INDEX IF NOT EXISTS idx_senders_domain ON senders(domain);
"""


@contextmanager
def _connect():
    user_config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(user_config.SUBS_DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_seen(records: Iterable[dict]) -> int:
    """Each record: {address, name, count, bytes (optional)}.

    Inserts unseen senders. For seen senders, updates last_seen, name (if
    blank), and the latest scan's count/bytes (overwrites — counts are
    snapshots, not running totals).
    """
    now = time.time()
    written = 0
    with _connect() as conn:
        for r in records:
            addr = (r.get("address") or "").lower().strip()
            if not addr:
                continue
            domain = addr.split("@", 1)[1] if "@" in addr else ""
            name = r.get("name") or ""
            count = int(r.get("count") or 0)
            total_bytes = int(r.get("bytes") or 0)
            row = conn.execute("SELECT 1 FROM senders WHERE address=?", (addr,)).fetchone()
            if row:
                conn.execute(
                    """UPDATE senders
                          SET last_seen=?, message_count=?, total_bytes=?,
                              name=CASE WHEN name='' AND ? != '' THEN ? ELSE name END
                        WHERE address=?""",
                    (now, count, total_bytes, name, name, addr),
                )
            else:
                conn.execute(
                    """INSERT INTO senders
                       (address, name, domain, first_seen, last_seen, message_count,
                        total_bytes, status, status_changed)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)""",
                    (addr, name, domain, now, now, count, total_bytes, now),
                )
                written += 1
    return written


def set_status(addresses: Iterable[str], status: str, note: str = "") -> int:
    addresses = [a.lower().strip() for a in addresses if a]
    if not addresses:
        return 0
    now = time.time()
    with _connect() as conn:
        # Use executemany so each row's domain is computed correctly on insert
        for addr in addresses:
            domain = addr.split("@", 1)[1] if "@" in addr else ""
            row = conn.execute("SELECT 1 FROM senders WHERE address=?", (addr,)).fetchone()
            if row:
                conn.execute(
                    "UPDATE senders SET status=?, status_changed=?, notes=? WHERE address=?",
                    (status, now, note, addr),
                )
            else:
                conn.execute(
                    """INSERT INTO senders
                       (address, name, domain, first_seen, last_seen, message_count,
                        total_bytes, status, status_changed, notes)
                       VALUES (?, '', ?, ?, ?, 0, 0, ?, ?, ?)""",
                    (addr, domain, now, now, status, now, note),
                )
        return len(addresses)


def list_senders(status: str | None = None, domain: str | None = None, limit: int = 500) -> list[dict]:
    with _connect() as conn:
        sql = "SELECT * FROM senders"
        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if domain:
            clauses.append("domain = ?")
            params.append(domain)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY last_seen DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def stats() -> dict:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n, SUM(message_count) AS msgs FROM senders GROUP BY status"
        ).fetchall()
        return {r["status"]: {"senders": r["n"], "messages": r["msgs"] or 0} for r in rows}
