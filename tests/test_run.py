"""Przebieg run_once z podmienionymi IMAP, Vulcanem i SMTP."""

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import SecretStr

from vulcan_forwarder import main
from vulcan_forwarder.config import Account, Settings
from vulcan_forwarder.imap_watcher import RawNotification
from vulcan_forwarder.store import SEEN, Store
from vulcan_forwarder.vulcan_client import Message, MessageSummary, VulcanError, VulcanLoginError

FIXTURES = Path(__file__).parent / "fixtures"
CET = timezone(timedelta(hours=1))
NOW = datetime.now(timezone.utc)

JAN_MAJA = "Nowak Jan - R - Nowak Maja - (SP1_Test)"
JAN_ZOFIA = "Nowak Jan - R - Nowak Zofia - (SP1_Test)"
ANNA_MAJA = "Nowak Anna - R - Nowak Maja - (SP1_Test)"
ANNA_ZOFIA = "Nowak Anna - R - Nowak Zofia - (SP1_Test)"
KOWALSKA = "Kowalska-Nowicka Ewa [Kn] - P - (SP1_Test)"
WARSZTATY_KEY = "22222222-bbbb-4bbb-8bbb-222222222222"


def summary(key: str, subject: str, sent_at: datetime, sender=KOWALSKA, mailbox=JAN_MAJA) -> MessageSummary:
    return MessageSummary(key=key, id=1, sender=sender, subject=subject, sent_at=sent_at, mailbox=mailbox,
                          has_attachments=False, read=False)  # fmt: skip


def warsztaty(mailbox=JAN_MAJA, key=WARSZTATY_KEY) -> MessageSummary:
    return summary(key, "Warsztaty muzyczno-taneczne", datetime(2026, 11, 2, 12, 7, 29, tzinfo=CET), mailbox=mailbox)


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


class FakeAccountVulcan:
    """Vulcan jednego konta."""

    messages_app_url = "https://wiadomosci.eduvulcan.pl/przyklad/App"

    def __init__(self, settings, account, mailboxes, summaries, error=None):
        self.settings, self.account = settings, account
        self._mailboxes, self.summaries, self.error = mailboxes, summaries, error

    def __enter__(self):
        if self.error:
            raise self.error
        return self

    def __exit__(self, *exc):
        pass

    def mailboxes(self):
        # Jak prawdziwy klient: zapamiętuje skrzynki konta w state.
        state = self.account.state_dir(self.settings)
        state.mkdir(parents=True, exist_ok=True)
        (state / "vulcan.json").write_text(json.dumps({"mailboxes": self._mailboxes}), encoding="utf-8")
        return self._mailboxes

    def list_received(self, stop_before=None, max_pages=20):
        return [s for s in self.summaries if stop_before is None or s.sent_at >= stop_before]

    def get_message(self, key, mailbox=None):
        s = next((s for s in self.summaries if s.key == key), None)
        if s is None:
            raise VulcanError(f"API WiadomoscSzczegoly: HTTP 404 ({key})")
        return Message(key=s.key, id=s.id, sender=s.sender, recipients=[s.mailbox], subject=s.subject,
                       sent_at=s.sent_at, html="<p>treść</p>", mailbox=mailbox or s.mailbox)  # fmt: skip


class FakeVulcan:
    """Podmiana klasy VulcanClient: per konto skrzynki, wiadomości i ewentualny błąd."""

    def __init__(self):
        self.accounts: dict[str, dict] = {}
        self.opened: list[str] = []

    def add(self, name, mailboxes, summaries=(), error=None):
        self.accounts[name] = dict(mailboxes=list(mailboxes), summaries=list(summaries), error=error)

    def __call__(self, settings, account, headed=False, debug=False):
        self.opened.append(account.name)
        return FakeAccountVulcan(settings, account, **self.accounts[account.name])


def account(name, mail_to):
    return Account(name=name, login=f"{name}@x.pl", password=SecretStr("haslo-" + name), mail_to=mail_to)


@pytest.fixture
def env(tmp_path, monkeypatch):
    settings = Settings(
        _env_file=None,
        db_path=tmp_path / "state" / "db.sqlite",
        state_dir=tmp_path / "state",
        mail_from="bot@x",
        full_check_interval_minutes=60,
        account_list=[account("mateusz", "mateusz@x.pl"), account("aga", "aga@x.pl, druga@x.pl")],
    )
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(main, "send", lambda msg, s, dry_run=False: sent.append((msg["X-Vulcan-Message-Key"], msg["To"])))
    watcher, vulcan = FakeWatcher([]), FakeVulcan()
    monkeypatch.setattr(main, "ImapWatcher", watcher)
    monkeypatch.setattr(main, "VulcanClient", vulcan)
    return settings, sent, watcher, vulcan


def args(**kw):
    base = dict(dry_run=False, headed=False, debug=False, sync=False, account=None, forward_last=0)
    base.update(kw)
    return argparse.Namespace(**base)


def notification(uid: int, name: str, parent: str | None = None) -> RawNotification:
    raw = (FIXTURES / name).read_bytes()
    if parent:  # ta sama wiadomość, ale powiadomienie z konta drugiego rodzica
        raw = raw.replace(b"Nowak Jan", parent.encode())
    return RawNotification(uid=uid, raw=raw)


def init(settings, name, key="old", sent_at=None):
    """Stan po --init konta: jedna znana wiadomość."""
    with Store(settings.db_path) as store:
        store.mark_initialized(name)
        store.record(account=name, vulcan_key=f"{key}-{name}", vulcan_id=1, sent_at=sent_at or NOW - timedelta(days=2),
                     subject="s", sender="x", mailbox="m", status=SEEN)  # fmt: skip


def remember_mailboxes(settings, name, mailboxes):
    acc = next(a for a in settings.accounts if a.name == name)
    acc.state_dir(settings).mkdir(parents=True, exist_ok=True)
    (acc.state_dir(settings) / "vulcan.json").write_text(json.dumps({"mailboxes": mailboxes}), encoding="utf-8")


def test_full_check_due():
    assert main.full_check_due(None, NOW, 60)
    assert not main.full_check_due(NOW - timedelta(minutes=30), NOW, 60)
    assert main.full_check_due(NOW - timedelta(minutes=61), NOW, 60)
    assert not main.full_check_due(None, NOW, 0)


def test_periodic_check_each_account_to_its_own_recipients(env):
    settings, sent, _, vulcan = env
    init(settings, "mateusz")
    init(settings, "aga")
    vulcan.add("mateusz", [JAN_MAJA], [summary("m-new", "Dla Jana", NOW - timedelta(hours=3))])
    vulcan.add("aga", [ANNA_MAJA], [summary("a-new", "Dla Anny", NOW - timedelta(hours=2), mailbox=ANNA_MAJA)])
    assert main.run_once(settings, args())
    assert sent == [("m-new", "mateusz@x.pl"), ("a-new", "aga@x.pl, druga@x.pl")]
    # Druga próba w tej samej godzinie: bez powiadomień nie zagląda do Vulcana.
    assert main.run_once(settings, args())
    assert vulcan.opened == ["mateusz", "aga"]
    # --sync --account aga: wymusza sprawdzenie tylko tego konta.
    assert main.run_once(settings, args(sync=True, account=["aga"]))
    assert vulcan.opened == ["mateusz", "aga", "aga"]


def test_notification_routed_to_owner_by_remembered_mailboxes(env):
    settings, sent, watcher, vulcan = env
    init(settings, "mateusz", sent_at=datetime(2026, 11, 1, 8, 0, tzinfo=CET))
    init(settings, "aga", sent_at=datetime(2026, 11, 1, 8, 0, tzinfo=CET))
    with Store(settings.db_path) as store:  # okresowe sprawdzenie niedawno było
        store.set_last_full_check("mateusz", NOW)
        store.set_last_full_check("aga", NOW)
    remember_mailboxes(settings, "mateusz", [JAN_MAJA, JAN_ZOFIA])
    remember_mailboxes(settings, "aga", [ANNA_MAJA, ANNA_ZOFIA])
    watcher.raws = [notification(7, "notif_kowalska_maja.eml", parent="Nowak Anna")]
    vulcan.add("mateusz", [JAN_MAJA, JAN_ZOFIA], [warsztaty(JAN_MAJA, key="kopia-jana")])
    vulcan.add("aga", [ANNA_MAJA, ANNA_ZOFIA], [warsztaty(ANNA_MAJA)])
    assert main.run_once(settings, args())
    assert vulcan.opened == ["aga"]  # konto Jana nie było potrzebne
    assert sent == [(WARSZTATY_KEY, "aga@x.pl, druga@x.pl")]
    assert watcher.processed == [7]


def test_unknown_owner_checks_all_accounts_and_same_message_goes_to_both_parents(env):
    settings, sent, watcher, vulcan = env
    # Bez zapamiętanych skrzynek i bez --init: właściciel nieznany → sprawdzane oba konta.
    watcher.raws = [
        notification(7, "notif_kowalska_maja.eml"),
        notification(8, "notif_kowalska_maja.eml", parent="Nowak Anna"),
    ]
    vulcan.add("mateusz", [JAN_MAJA], [warsztaty(JAN_MAJA)])
    anna_copy = warsztaty(ANNA_MAJA, key="55555555-eeee-4eee-8eee-555555555555")
    vulcan.add("aga", [ANNA_MAJA], [anna_copy])
    # Powiadomienie Anny ma ten sam UUID co kopia Jana (fixture), więc w jej koncie zadziała heurystyka
    # (nadawca/temat/skrzynka/czas) — a kopia Jana poszła tylko do niego.
    assert main.run_once(settings, args())
    assert sorted(vulcan.opened) == ["aga", "mateusz"]
    assert sorted(sent) == sorted([(WARSZTATY_KEY, "mateusz@x.pl"), (anna_copy.key, "aga@x.pl, druga@x.pl")])
    assert sorted(watcher.processed) == [7, 8]


def test_failing_account_does_not_block_other(env):
    settings, sent, watcher, vulcan = env
    init(settings, "mateusz")
    init(settings, "aga")
    remember_mailboxes(settings, "mateusz", [JAN_MAJA])
    watcher.raws = [notification(7, "notif_kowalska_maja.eml")]  # dla Jana
    vulcan.add("mateusz", [JAN_MAJA], error=VulcanLoginError("Wygląda na captchę"))
    vulcan.add("aga", [ANNA_MAJA], [summary("a-new", "Dla Anny", NOW - timedelta(hours=1), mailbox=ANNA_MAJA)])
    alerts = []
    alerter = type("A", (), {"alert": lambda self, kind, summary, details="", screenshot=None: alerts.append(kind)})()
    assert main.run_once(settings, args(), alerter) is False
    assert sent == [("a-new", "aga@x.pl, druga@x.pl")]
    assert alerts == ["vulcan-logowanie:mateusz"]
    assert watcher.processed == []  # powiadomienie Jana czeka na kolejną próbę


def test_without_init_does_not_send_history(env):
    settings, sent, watcher, vulcan = env
    watcher.raws = [notification(7, "notif_kowalska_maja.eml")]
    vulcan.add("mateusz", [JAN_MAJA],
               [warsztaty(), summary("earlier", "Wcześniejsza", datetime(2026, 11, 2, 9, 0, tzinfo=CET))])  # fmt: skip
    vulcan.add("aga", [ANNA_MAJA], [summary("aga-earlier", "x", datetime(2026, 11, 2, 9, 0, tzinfo=CET), mailbox=ANNA_MAJA)])
    assert main.run_once(settings, args())
    assert sent == [(WARSZTATY_KEY, "mateusz@x.pl")]
    # Bez powiadomień i bez --init okresowy check nic nie robi.
    vulcan.opened.clear()
    watcher.raws = []
    assert main.run_once(settings, args(sync=True))
    assert vulcan.opened == []


def test_dry_run_changes_nothing(env):
    settings, sent, watcher, vulcan = env
    init(settings, "mateusz")
    remember_mailboxes(settings, "mateusz", [JAN_MAJA])
    watcher.raws = [notification(7, "notif_kowalska_maja.eml")]
    vulcan.add("mateusz", [JAN_MAJA], [summary("new", "x", NOW - timedelta(hours=1))])
    vulcan.add("aga", [ANNA_MAJA])
    main.run_once(settings, args(dry_run=True))
    assert sent == [("new", "mateusz@x.pl")]  # send() dostaje dry_run=True i tylko wypisuje
    assert watcher.processed == []
    with Store(settings.db_path) as store:
        assert not store.is_known("new")
        assert store.last_full_check("mateusz") is None


def test_single_account_legacy_config(tmp_path):
    """Stara konfiguracja (VULCAN_LOGIN/MAIL_TO) → konto „default”; stan zostaje na miejscu."""
    settings = Settings(_env_file=None, db_path=tmp_path / "state" / "db.sqlite", state_dir=tmp_path / "state",
                        mail_from="bot@x", mail_to="ja@x.pl", vulcan_login="l", vulcan_password="p")  # fmt: skip
    assert [a.name for a in settings.accounts] == ["default"]
    assert settings.accounts[0].recipients == "ja@x.pl"
    assert settings.accounts[0].state_dir(settings) == tmp_path / "state"


def test_legacy_state_moves_to_first_named_account(env):
    settings, _, _, _ = env
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    (settings.state_dir / "session.json").write_text("{}", encoding="utf-8")
    with Store(settings.db_path) as store:
        store.set_meta("initialized", "2026-11-02T12:00:00+00:00")
        main.migrate_legacy_state(settings, store)
        assert store.is_initialized("mateusz") and not store.is_initialized("aga")
    assert (settings.state_dir / "accounts" / "mateusz" / "session.json").exists()
    assert not (settings.state_dir / "session.json").exists()


def test_reinit_with_forward_last_resends_newest_per_account(env):
    settings, sent, _, vulcan = env
    jan = [summary(f"j{i}", f"Jan {i}", NOW - timedelta(hours=10 - i)) for i in range(5)]  # j4 najnowsza
    anna = [summary(f"a{i}", f"Anna {i}", NOW - timedelta(hours=10 - i), mailbox=ANNA_MAJA) for i in range(3)]
    vulcan.add("mateusz", [JAN_MAJA], jan)
    vulcan.add("aga", [ANNA_MAJA], anna)
    # Zwykłe --init: nic nie idzie.
    assert main.init_store(settings, args())
    assert main.run_once(settings, args(sync=True)) and sent == []
    # Ponowny init z --forward-last 2: dwie najnowsze z każdego konta (także już widziane) idą od razu.
    assert main.init_store(settings, args(forward_last=2))
    assert main.run_once(settings, args())  # bez --sync: init wyzerował zegar pełnego sprawdzenia
    assert sorted(k for k, _ in sent) == ["a1", "a2", "j3", "j4"]
    assert ("j4", "mateusz@x.pl") in sent and ("a2", "aga@x.pl, druga@x.pl") in sent
    # I tylko raz.
    assert main.run_once(settings, args(sync=True))
    assert len(sent) == 4
