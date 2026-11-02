"""Przebieg run_once z podmienionymi IMAP, Vulcanem i SMTP."""

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from vulcan_forwarder import main
from vulcan_forwarder.config import Settings
from vulcan_forwarder.imap_watcher import RawNotification
from vulcan_forwarder.store import SEEN, Store
from vulcan_forwarder.vulcan_client import Message, MessageSummary, VulcanError

FIXTURES = Path(__file__).parent / "fixtures"
CET = timezone(timedelta(hours=1))
NOW = datetime.now(timezone.utc)


def summary(key: str, subject: str, sent_at: datetime, sender="Kowalska-Nowicka Ewa [Kn] - P - (SP1_Test)",
            mailbox="Nowak Jan - R - Nowak Maja - (SP1_Test)") -> MessageSummary:  # fmt: skip
    return MessageSummary(key=key, id=1, sender=sender, subject=subject, sent_at=sent_at, mailbox=mailbox,
                          has_attachments=False, read=False)  # fmt: skip


class FakeWatcher:
    def __init__(self, raws):
        self.raws = raws
        self.processed: list[int] = []

    def __call__(self, settings):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def fetch_new(self, include_processed=False, limit=None):
        return [r for r in self.raws if r.uid not in self.processed]

    def mark_ignored(self, uid):
        pass

    def mark_processed(self, uid):
        self.processed.append(uid)


class FakeVulcan:
    messages_app_url = "https://wiadomosci.eduvulcan.pl/przyklad/App"

    def __init__(self, summaries):
        self.summaries = summaries
        self.opened = 0

    def __call__(self, settings, headed=False, debug=False):
        self.opened += 1
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def list_received(self, stop_before=None, max_pages=20):
        return [s for s in self.summaries if stop_before is None or s.sent_at >= stop_before]

    def get_message(self, key, mailbox=None):
        s = next((s for s in self.summaries if s.key == key), None)
        if s is None:
            raise VulcanError(f"API WiadomoscSzczegoly: HTTP 404 ({key})")
        return Message(key=s.key, id=s.id, sender=s.sender, recipients=[s.mailbox], subject=s.subject,
                       sent_at=s.sent_at, html="<p>treść</p>", mailbox=mailbox or s.mailbox)  # fmt: skip


@pytest.fixture
def env(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, db_path=tmp_path / "db.sqlite", mail_from="a@x", mail_to="b@x",
                        full_check_interval_minutes=60)  # fmt: skip
    sent: list[str] = []
    monkeypatch.setattr(main, "send", lambda msg, s, dry_run=False: sent.append(msg["X-Vulcan-Message-Key"]))

    def setup(raws, summaries):
        watcher, vulcan = FakeWatcher(raws), FakeVulcan(summaries)
        monkeypatch.setattr(main, "ImapWatcher", watcher)
        monkeypatch.setattr(main, "VulcanClient", vulcan)
        return watcher, vulcan

    return settings, sent, setup


def args(**kw):
    base = dict(dry_run=False, headed=False, debug=False, sync=False)
    base.update(kw)
    return argparse.Namespace(**base)


def notification(uid: int, name: str) -> RawNotification:
    return RawNotification(uid=uid, raw=(FIXTURES / name).read_bytes())


def seed(settings, key, sent_at):
    """Stan po --init: jedna znana wiadomość."""
    with Store(settings.db_path) as store:
        store.mark_initialized()
        store.record(vulcan_key=key, vulcan_id=1, sent_at=sent_at, subject="s", sender="x", mailbox="m", status=SEEN)


def test_full_check_due():
    assert main.full_check_due(None, NOW, 60)
    assert not main.full_check_due(NOW - timedelta(minutes=30), NOW, 60)
    assert main.full_check_due(NOW - timedelta(minutes=61), NOW, 60)
    assert not main.full_check_due(None, NOW, 0)


def test_periodic_check_forwards_messages_without_notification(env):
    settings, sent, setup = env
    seed(settings, "old", NOW - timedelta(days=2))
    _, vulcan = setup([], [
        summary("new-1", "Nowa bez powiadomienia", NOW - timedelta(hours=3)),
        summary("old", "Stara", NOW - timedelta(days=2)),
    ])  # fmt: skip
    main.run_once(settings, args())
    assert sent == ["new-1"]
    # Drugi przebieg w tej samej godzinie: bez powiadomień nie zagląda do Vulcana.
    main.run_once(settings, args())
    assert vulcan.opened == 1 and sent == ["new-1"]
    # --sync wymusza sprawdzenie, ale nic nowego nie ma.
    main.run_once(settings, args(sync=True))
    assert vulcan.opened == 2 and sent == ["new-1"]


def test_notification_triggers_check_and_sync_catches_missed_ones(env):
    settings, sent, setup = env
    with Store(settings.db_path) as store:
        store.set_last_full_check(NOW)  # okresowy check niedawno był
    seed(settings, "old", datetime(2026, 11, 1, 8, 0, tzinfo=CET))
    watcher, vulcan = setup(
        [notification(7, "notif_kowalska_maja.eml")],
        [
            summary("22222222-bbbb-4bbb-8bbb-222222222222", "Warsztaty muzyczno-taneczne",
                    datetime(2026, 11, 2, 12, 7, 29, tzinfo=CET)),
            summary("missed", "Ta nie miała powiadomienia", datetime(2026, 11, 2, 9, 0, tzinfo=CET)),
        ],
    )  # fmt: skip
    main.run_once(settings, args())
    assert vulcan.opened == 1
    assert sorted(sent) == ["22222222-bbbb-4bbb-8bbb-222222222222", "missed"]
    assert watcher.processed == [7]


def test_without_init_does_not_send_history(env):
    settings, sent, setup = env
    watcher, vulcan = setup(
        [notification(7, "notif_kowalska_maja.eml")],
        [
            summary("22222222-bbbb-4bbb-8bbb-222222222222", "Warsztaty muzyczno-taneczne",
                    datetime(2026, 11, 2, 12, 7, 29, tzinfo=CET)),
            summary("earlier", "Wcześniejsza", datetime(2026, 11, 2, 9, 0, tzinfo=CET)),
        ],
    )  # fmt: skip
    main.run_once(settings, args())
    assert sent == ["22222222-bbbb-4bbb-8bbb-222222222222"]
    # Bez --init okresowy check nic nie robi — także gdy w bazie jest już przekazana wiadomość.
    _, vulcan = setup([], [summary("x", "y", NOW)])
    main.run_once(settings, args(sync=True))
    assert vulcan.opened == 0
    assert sent == ["22222222-bbbb-4bbb-8bbb-222222222222"]


def test_dry_run_changes_nothing(env):
    settings, sent, setup = env
    seed(settings, "old", NOW - timedelta(days=2))
    watcher, _ = setup([notification(7, "notif_kowalska_maja.eml")], [summary("new", "x", NOW - timedelta(hours=1))])
    main.run_once(settings, args(dry_run=True))
    assert sent == ["new"]  # send() dostaje dry_run=True i tylko wypisuje
    assert watcher.processed == []
    with Store(settings.db_path) as store:
        assert not store.is_known("new")
        assert store.last_full_check() is None
