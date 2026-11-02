"""Stan w SQLite: które wiadomości Vulcana zostały już przekazane (lub oznaczone jako widziane)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    vulcan_key TEXT PRIMARY KEY,          -- apiGlobalKey (UUID z powiadomienia)
    vulcan_id INTEGER,
    sent_at TEXT NOT NULL,                -- ISO 8601, UTC
    subject TEXT,
    sender TEXT,
    mailbox TEXT,
    status TEXT NOT NULL,                 -- 'forwarded' | 'seen'
    notification_message_id TEXT,
    processed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_sent_at ON messages(sent_at);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

FORWARDED = "forwarded"
SEEN = "seen"
LAST_FULL_CHECK = "last_full_check"
INITIALIZED = "initialized"


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def is_known(self, vulcan_key: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM messages WHERE vulcan_key = ?", (vulcan_key.lower(),)).fetchone()
        return row is not None

    def is_empty(self) -> bool:
        return self.conn.execute("SELECT 1 FROM messages LIMIT 1").fetchone() is None

    def last_sent_at(self) -> datetime | None:
        """Data najnowszej znanej (przekazanej lub widzianej) wiadomości."""
        row = self.conn.execute("SELECT MAX(sent_at) FROM messages").fetchone()
        return datetime.fromisoformat(row[0]) if row and row[0] else None

    def record(
        self,
        *,
        vulcan_key: str,
        vulcan_id: int | None,
        sent_at: datetime,
        subject: str,
        sender: str,
        mailbox: str | None,
        status: str,
        notification_message_id: str | None = None,
    ) -> bool:
        """Zapisuje wiadomość. Zwraca False, jeśli już była zapisana (nic nie zmienia)."""
        with self.conn:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO messages
                   (vulcan_key, vulcan_id, sent_at, subject, sender, mailbox, status, notification_message_id, processed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    vulcan_key.lower(),
                    vulcan_id,
                    _utc_iso(sent_at),
                    subject,
                    sender,
                    mailbox,
                    status,
                    notification_message_id,
                    _utc_iso(datetime.now(timezone.utc)),
                ),
            )
        return cur.rowcount == 1

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def is_initialized(self) -> bool:
        """Czy uruchomiono --init (dopiero wtedy synchronizacja wie, od kiedy zaczynać)."""
        return self.get_meta(INITIALIZED) is not None

    def mark_initialized(self) -> None:
        self.set_meta(INITIALIZED, _utc_iso(datetime.now(timezone.utc)))

    def last_full_check(self) -> datetime | None:
        value = self.get_meta(LAST_FULL_CHECK)
        return datetime.fromisoformat(value) if value else None

    def set_last_full_check(self, when: datetime) -> None:
        self.set_meta(LAST_FULL_CHECK, _utc_iso(when))

    def status_of(self, vulcan_key: str) -> str | None:
        row = self.conn.execute("SELECT status FROM messages WHERE vulcan_key = ?", (vulcan_key.lower(),)).fetchone()
        return row[0] if row else None
