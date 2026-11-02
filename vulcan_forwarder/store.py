"""Stan w SQLite: które wiadomości Vulcana zostały już przekazane (lub oznaczone jako widziane).

Klucz wiadomości (apiGlobalKey) jest unikalny globalnie — każda skrzynka ma własną kopię z własnym
kluczem — więc deduplikacja nie zależy od konta. Per konto liczone są: data ostatniej znanej
wiadomości, znacznik --init i czas ostatniego pełnego sprawdzenia.
"""

from __future__ import annotations

import logging
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

log = logging.getLogger(__name__)


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(messages)")}
        if "account" not in columns:
            with self.conn:
                self.conn.execute("ALTER TABLE messages ADD COLUMN account TEXT")
        self.conn.execute("CREATE INDEX IF NOT EXISTS messages_account_sent_at ON messages(account, sent_at)")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def adopt_legacy(self, account: str, legacy_account: str = "default") -> None:
        """Przypisuje stan z wersji jednokontowej do `account`: wiersze bez konta i meta bez prefiksu
        (sprzed obsługi wielu kont) oraz stan konta `legacy_account` (konfiguracja VULCAN_LOGIN),
        gdy przechodzimy na nazwane konta z VULCAN_ACCOUNTS."""
        sources = [None] if account == legacy_account else [None, legacy_account]
        with self.conn:
            moved = self.conn.execute("UPDATE messages SET account = ? WHERE account IS NULL", (account,)).rowcount
            if account != legacy_account:
                moved += self.conn.execute(
                    "UPDATE messages SET account = ? WHERE account = ?", (account, legacy_account)
                ).rowcount
            for key in (INITIALIZED, LAST_FULL_CHECK):
                for source in sources:
                    old_key = key if source is None else f"{key}:{source}"
                    value = self.get_meta(old_key)
                    if value is not None:
                        if self.get_meta(f"{key}:{account}") is None:
                            self.set_meta(f"{key}:{account}", value)
                        self.conn.execute("DELETE FROM meta WHERE key = ?", (old_key,))
        if moved:
            log.info("Baza: %d wcześniejszych wiadomości przypisano do konta %s", moved, account)

    def is_known(self, vulcan_key: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM messages WHERE vulcan_key = ?", (vulcan_key.lower(),)).fetchone()
        return row is not None

    def is_empty(self) -> bool:
        return self.conn.execute("SELECT 1 FROM messages LIMIT 1").fetchone() is None

    def last_sent_at(self, account: str) -> datetime | None:
        """Data najnowszej znanej (przekazanej lub widzianej) wiadomości konta."""
        row = self.conn.execute("SELECT MAX(sent_at) FROM messages WHERE account = ?", (account,)).fetchone()
        return datetime.fromisoformat(row[0]) if row and row[0] else None

    def record(
        self,
        *,
        account: str,
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
                   (vulcan_key, vulcan_id, sent_at, subject, sender, mailbox, status, notification_message_id,
                    processed_at, account)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    account,
                ),
            )
        return cur.rowcount == 1

    def forget(self, vulcan_key: str) -> bool:
        """Usuwa wiadomość z bazy (zostanie potraktowana jak nowa). Zwraca True, jeśli była zapisana."""
        with self.conn:
            return self.conn.execute("DELETE FROM messages WHERE vulcan_key = ?", (vulcan_key.lower(),)).rowcount == 1

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def is_initialized(self, account: str) -> bool:
        """Czy dla konta uruchomiono --init (dopiero wtedy synchronizacja wie, od kiedy zaczynać)."""
        return self.get_meta(f"{INITIALIZED}:{account}") is not None

    def mark_initialized(self, account: str) -> None:
        self.set_meta(f"{INITIALIZED}:{account}", _utc_iso(datetime.now(timezone.utc)))

    def last_full_check(self, account: str) -> datetime | None:
        value = self.get_meta(f"{LAST_FULL_CHECK}:{account}")
        return datetime.fromisoformat(value) if value else None

    def set_last_full_check(self, account: str, when: datetime) -> None:
        self.set_meta(f"{LAST_FULL_CHECK}:{account}", _utc_iso(when))

    def reset_last_full_check(self, account: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM meta WHERE key = ?", (f"{LAST_FULL_CHECK}:{account}",))

    def status_of(self, vulcan_key: str) -> str | None:
        row = self.conn.execute("SELECT status FROM messages WHERE vulcan_key = ?", (vulcan_key.lower(),)).fetchone()
        return row[0] if row else None
