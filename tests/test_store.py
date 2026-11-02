import sqlite3
from datetime import datetime, timedelta, timezone

from vulcan_forwarder.store import FORWARDED, SEEN, Store

CET = timezone(timedelta(hours=1))


def record(store, key, sent_at, status=FORWARDED, account="mateusz"):
    return store.record(
        account=account, vulcan_key=key, vulcan_id=1, sent_at=sent_at, subject="s", sender="x", mailbox="m",
        status=status,
    )  # fmt: skip


def test_dedup_is_global_across_accounts(tmp_path):
    with Store(tmp_path / "db.sqlite") as store:
        assert store.is_empty()
        assert record(store, "ABC-1", datetime(2026, 11, 2, 12, 0, tzinfo=CET)) is True
        assert record(store, "abc-1", datetime(2026, 11, 2, 12, 0, tzinfo=CET)) is False  # klucz bez rozróżniania wielkości
        assert record(store, "abc-1", datetime(2026, 11, 2, 12, 0, tzinfo=CET), account="aga") is False
        assert store.is_known("abc-1") and store.is_known("ABC-1")
        assert not store.is_known("other")


def test_persists_between_connections(tmp_path):
    path = tmp_path / "state" / "db.sqlite"
    with Store(path) as store:
        record(store, "k1", datetime(2026, 11, 2, 12, 0, tzinfo=CET), status=SEEN)
    with Store(path) as store:
        assert store.is_known("k1")
        assert store.status_of("k1") == SEEN


def test_last_sent_at_per_account_across_timezones(tmp_path):
    with Store(tmp_path / "db.sqlite") as store:
        assert store.last_sent_at("mateusz") is None
        record(store, "a", datetime(2026, 11, 2, 12, 0, tzinfo=CET))
        record(store, "b", datetime(2026, 11, 2, 11, 30, tzinfo=timezone.utc))  # 12:30 CET — najnowsza
        record(store, "c", datetime(2026, 10, 1, 8, 0, tzinfo=CET))
        record(store, "d", datetime(2026, 12, 1, 8, 0, tzinfo=CET), account="aga")
        assert store.last_sent_at("mateusz") == datetime(2026, 11, 2, 11, 30, tzinfo=timezone.utc)
        assert store.last_sent_at("aga") == datetime(2026, 12, 1, 7, 0, tzinfo=timezone.utc)


def test_meta_per_account(tmp_path):
    now = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    with Store(tmp_path / "db.sqlite") as store:
        store.mark_initialized("mateusz")
        store.set_last_full_check("mateusz", now)
        assert store.is_initialized("mateusz") and not store.is_initialized("aga")
        assert store.last_full_check("mateusz") == now and store.last_full_check("aga") is None


def test_migration_from_single_account_database(tmp_path):
    """Baza z wersji jednokontowej: brak kolumny account, meta bez prefiksu konta."""
    path = tmp_path / "db.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE messages (vulcan_key TEXT PRIMARY KEY, vulcan_id INTEGER, sent_at TEXT NOT NULL, subject TEXT,
            sender TEXT, mailbox TEXT, status TEXT NOT NULL, notification_message_id TEXT, processed_at TEXT NOT NULL);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO messages VALUES ('old-1', 1, '2026-11-02T11:07:29+00:00', 's', 'x', 'm', 'forwarded', NULL,
            '2026-11-02T12:00:00+00:00');
        INSERT INTO meta VALUES ('initialized', '2026-11-02T12:00:00+00:00');
        INSERT INTO meta VALUES ('last_full_check', '2026-11-02T12:15:00+00:00');
    """)
    conn.commit()
    conn.close()

    with Store(path) as store:
        store.adopt_legacy("mateusz")
        store.adopt_legacy("mateusz")  # idempotentne
        assert store.is_known("old-1")
        assert store.last_sent_at("mateusz") == datetime(2026, 11, 2, 11, 7, 29, tzinfo=timezone.utc)
        assert store.is_initialized("mateusz") and not store.is_initialized("aga")
        assert store.last_full_check("mateusz") == datetime(2026, 11, 2, 12, 15, tzinfo=timezone.utc)
        assert store.get_meta("initialized") is None


def test_migration_from_default_account_to_named(tmp_path):
    """Najpierw jedno konto (VULCAN_LOGIN → „default”), potem VULCAN_ACCOUNTS=mateusz,aga."""
    with Store(tmp_path / "db.sqlite") as store:
        record(store, "k1", datetime(2026, 11, 2, 12, 0, tzinfo=CET), account="default")
        store.mark_initialized("default")
        store.adopt_legacy("default")  # wersja jednokontowa: nic się nie zmienia
        assert store.is_initialized("default")

        store.adopt_legacy("mateusz")
        assert store.is_initialized("mateusz") and not store.is_initialized("default")
        assert store.last_sent_at("mateusz") == datetime(2026, 11, 2, 11, 0, tzinfo=timezone.utc)
        assert store.last_sent_at("default") is None
