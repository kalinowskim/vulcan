from datetime import datetime, timedelta, timezone

from vulcan_forwarder.store import FORWARDED, SEEN, Store

CET = timezone(timedelta(hours=1))


def record(store, key, sent_at, status=FORWARDED):
    return store.record(
        vulcan_key=key, vulcan_id=1, sent_at=sent_at, subject="s", sender="x", mailbox="m", status=status
    )


def test_dedup(tmp_path):
    with Store(tmp_path / "db.sqlite") as store:
        assert store.is_empty()
        assert record(store, "ABC-1", datetime(2026, 11, 2, 12, 0, tzinfo=CET)) is True
        assert record(store, "abc-1", datetime(2026, 11, 2, 12, 0, tzinfo=CET)) is False  # klucz bez rozróżniania wielkości
        assert store.is_known("abc-1") and store.is_known("ABC-1")
        assert not store.is_known("other")


def test_persists_between_connections(tmp_path):
    path = tmp_path / "state" / "db.sqlite"
    with Store(path) as store:
        record(store, "k1", datetime(2026, 11, 2, 12, 0, tzinfo=CET), status=SEEN)
    with Store(path) as store:
        assert store.is_known("k1")
        assert store.status_of("k1") == SEEN


def test_last_sent_at_is_max_across_timezones(tmp_path):
    with Store(tmp_path / "db.sqlite") as store:
        assert store.last_sent_at() is None
        record(store, "a", datetime(2026, 11, 2, 12, 0, tzinfo=CET))
        record(store, "b", datetime(2026, 11, 2, 11, 30, tzinfo=timezone.utc))  # 12:30 CET — najnowsza
        record(store, "c", datetime(2026, 10, 1, 8, 0, tzinfo=CET))
        assert store.last_sent_at() == datetime(2026, 11, 2, 11, 30, tzinfo=timezone.utc)
