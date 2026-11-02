import argparse
import imaplib
import logging
import smtplib

import pytest

from vulcan_forwarder import alerts, main
from vulcan_forwarder.alerts import Alerter
from vulcan_forwarder.config import Settings
from vulcan_forwarder.imap_watcher import is_transient_imap_error
from vulcan_forwarder.mailer import is_transient_smtp_error
from vulcan_forwarder.resilience import FileLock, SecretFilter, retry
from vulcan_forwarder.vulcan_client import VulcanApiError, VulcanLoginError, is_transient_vulcan_error


# --- retry ---


def test_retry_succeeds_after_transient_errors():
    calls, sleeps = [], []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionResetError("reset")
        return "ok"

    assert retry(flaky, what="t", should_retry=lambda e: True, attempts=3, delay=2, backoff=3, sleep=sleeps.append) == "ok"
    assert len(calls) == 3
    assert sleeps == [2, 6]


def test_retry_does_not_retry_permanent_errors():
    calls = []

    def bad_login():
        calls.append(1)
        raise PermissionError("złe hasło")

    with pytest.raises(PermissionError):
        retry(bad_login, what="t", should_retry=lambda e: False, sleep=lambda s: None)
    assert len(calls) == 1


def test_retry_gives_up_after_attempts():
    calls = []

    def always():
        calls.append(1)
        raise TimeoutError()

    with pytest.raises(TimeoutError):
        retry(always, what="t", should_retry=lambda e: True, attempts=3, sleep=lambda s: None)
    assert len(calls) == 3


def test_transient_classifiers():
    assert is_transient_imap_error(TimeoutError())
    assert is_transient_imap_error(imaplib.IMAP4.abort("socket error"))
    assert not is_transient_imap_error(imaplib.IMAP4.error("LOGIN failed"))

    assert is_transient_smtp_error(smtplib.SMTPServerDisconnected())
    assert is_transient_smtp_error(smtplib.SMTPResponseException(451, b"try later"))
    assert not is_transient_smtp_error(smtplib.SMTPResponseException(550, b"no such user"))
    assert not is_transient_smtp_error(smtplib.SMTPAuthenticationError(535, b"bad auth"))

    assert is_transient_vulcan_error(VulcanApiError("x", 503))
    assert not is_transient_vulcan_error(VulcanApiError("x", 404))
    assert not is_transient_vulcan_error(VulcanLoginError("złe hasło"))


# --- lock ---


def test_file_lock_excludes_second_run(tmp_path):
    path = tmp_path / "state" / "forwarder.lock"
    first, second = FileLock(path), FileLock(path)
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


# --- maskowanie sekretów ---


def test_secret_filter_masks_message_args_and_traceback(caplog):
    logger = logging.getLogger("test.secret")
    secret_filter = SecretFilter(["Tajne#Haslo123", "", "abc"])
    caplog.handler.addFilter(secret_filter)
    try:
        logger.error("login nieudany: %s", "user / Tajne#Haslo123")
        try:
            raise RuntimeError("AUTH Tajne#Haslo123 failed")
        except RuntimeError:
            logger.exception("błąd")
    finally:
        caplog.handler.removeFilter(secret_filter)
    assert "Tajne#Haslo123" not in caplog.text
    assert "user / ***" in caplog.text
    assert "AUTH *** failed" in caplog.text


# --- alerty ---


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, db_path=tmp_path / "db.sqlite", state_dir=tmp_path / "state",
                    mail_from="bot@x", mail_to="ja@x")  # fmt: skip


def test_alerts_throttled_per_kind(settings, monkeypatch):
    sent = []
    monkeypatch.setattr(alerts.mailer, "send", lambda msg, s, dry_run=False: sent.append(msg))
    alerter = Alerter(settings)
    assert alerter.alert("imap", "pierwszy")
    assert not alerter.alert("imap", "drugi w tej samej godzinie")
    assert alerter.alert("smtp", "inny typ")
    assert [m["To"] for m in sent] == ["ja@x", "ja@x"]
    assert "imap" in sent[0]["Subject"] and "pierwszy" in sent[0]["Subject"]


def test_alert_redacts_and_attaches_screenshot(settings, monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(alerts.mailer, "send", lambda msg, s, dry_run=False: sent.append(msg))
    shot = tmp_path / "login-failure.png"
    shot.write_bytes(b"\x89PNG fake")
    Alerter(settings, redact=lambda t: t.replace("sekret", "***")).alert("vulcan-logowanie", "hasło sekret złe", screenshot=shot)
    msg = sent[0]
    assert "sekret" not in msg["Subject"] and "sekret" not in msg.get_body(("plain",)).get_content()
    attachments = [p for p in msg.walk() if p.get_content_disposition() == "attachment"]
    assert [a.get_filename() for a in attachments] == ["login-failure.png"]


def test_dry_run_alert_does_not_consume_throttle(settings, monkeypatch):
    calls = []
    monkeypatch.setattr(alerts.mailer, "send", lambda msg, s, dry_run=False: calls.append(dry_run))
    assert Alerter(settings, dry_run=True).alert("imap", "a")
    assert Alerter(settings).alert("imap", "b")
    assert calls == [True, False]


def test_failed_alert_send_is_swallowed(settings, monkeypatch):
    def boom(msg, s, dry_run=False):
        raise smtplib.SMTPServerDisconnected("down")

    monkeypatch.setattr(alerts.mailer, "send", boom)
    assert not Alerter(settings).alert("smtp", "SMTP leży")


# --- klasyfikacja i guarded ---


def test_classify():
    assert main.classify(VulcanLoginError("captcha"))[0] == "vulcan-logowanie"
    assert main.classify(smtplib.SMTPAuthenticationError(535, b"x"))[0] == "smtp"
    assert main.classify(imaplib.IMAP4.error("x"))[0] == "imap"
    assert main.classify(ConnectionRefusedError())[0] == "siec"
    assert main.classify(KeyError("x"))[0] == "nieoczekiwany"


class RecordingAlerter:
    def __init__(self):
        self.alerts = []

    def alert(self, kind, summary, details="", screenshot=None):
        self.alerts.append(kind)
        return True


def test_guarded_alerts_on_failure_and_releases_lock(settings):
    alerter = RecordingAlerter()

    def failing(s, a, al):
        raise VulcanLoginError("Wygląda na captchę")

    assert not main.guarded(settings, argparse.Namespace(), alerter, failing)
    assert alerter.alerts == ["vulcan-logowanie"]
    assert main.guarded(settings, argparse.Namespace(), alerter, lambda s, a, al: None)


def test_guarded_skips_when_locked(settings):
    ran = []
    holder = FileLock(settings.state_dir / "forwarder.lock")
    assert holder.acquire()
    try:
        assert main.guarded(settings, argparse.Namespace(), RecordingAlerter(), lambda s, a, al: ran.append(1))
    finally:
        holder.release()
    assert ran == []


def test_multiple_recipients(tmp_path, monkeypatch):
    from email.utils import getaddresses

    s = Settings(_env_file=None, db_path=tmp_path / "db.sqlite", mail_from="bot@x",
                 mail_to="ja@x.pl; zona@y.pl", alert_email_to=" a@x.pl ,b@y.pl; ")  # fmt: skip
    assert s.mail_recipient == "ja@x.pl, zona@y.pl"
    assert s.alert_recipient == "a@x.pl, b@y.pl"
    assert Settings(_env_file=None, mail_to="ja@x.pl;zona@y.pl").alert_recipient == "ja@x.pl, zona@y.pl"

    sent = []
    monkeypatch.setattr(alerts.mailer, "send", lambda msg, st, dry_run=False: sent.append(msg))
    Alerter(s).alert("imap", "test")
    # smtplib.send_message wyznacza kopertę z nagłówka To tą samą metodą
    assert [addr for _, addr in getaddresses([str(sent[0]["To"])])] == ["a@x.pl", "b@y.pl"]
