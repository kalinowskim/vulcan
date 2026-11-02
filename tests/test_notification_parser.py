import email
import email.policy
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from vulcan_forwarder.notification_parser import (
    NotificationParseError,
    is_vulcan_notification,
    parse_bytes,
    parse_message,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    return parse_bytes((FIXTURES / name).read_bytes())


def test_lis_zofia():
    n = load("notif_lis_zofia.eml")
    assert n.sender.name == "Lis Anna"
    assert n.sender.initials == "LA"
    assert n.sender.role == "P"
    assert n.sender.unit == "SP1_Test"
    assert n.recipient.name == "Nowak Jan"
    assert n.recipient.role == "R"
    assert n.student == "Nowak Zofia"
    assert n.subject == "OPATRUNEK NA RATUNEK!"
    assert n.vulcan_id == "11111111-aaaa-4aaa-8aaa-111111111111"
    assert n.sent_at == datetime(2026, 10, 22, 13, 56, 22, tzinfo=timezone(timedelta(hours=2)))
    assert n.email_message_id == "<SJHIN6KSMUU4.6KDWSPI0W4V01@ap-60-75c>"
    assert n.link is None


def test_hyphenated_sender_and_folded_subject():
    n = load("notif_kowalska_maja.eml")
    assert n.sender.name == "Kowalska-Nowicka Ewa"
    assert n.sender.initials == "Kn"
    assert n.student == "Nowak Maja"
    assert n.subject == "Warsztaty muzyczno-taneczne"
    assert n.vulcan_id == "22222222-bbbb-4bbb-8bbb-222222222222"


def test_polish_initials_and_quotes_in_subject():
    n = load("notif_sliwa_zofia.eml")
    assert n.sender.name == "Śliwa Agata"
    assert n.sender.initials == "ŚA"
    assert n.subject == 'Zbiórka darów dla Schroniska "Psie Pole"'


def test_same_message_to_two_students_has_distinct_ids():
    a = load("notif_sliwa_zofia.eml")
    e = load("notif_sliwa_maja.eml")
    assert a.subject == e.subject
    assert a.sender == e.sender
    assert {a.student, e.student} == {"Nowak Zofia", "Nowak Maja"}
    assert a.vulcan_id != e.vulcan_id


def _msg(subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["Date"] = "Mon, 02 Nov 2026 10:13:14 +0100"
    msg["Message-ID"] = "<x@y>"
    msg.set_content(body, subtype="html")
    return msg


def test_sender_without_initials_and_missing_id():
    msg = _msg(
        "Wiadomość od użytkownika Nowak Jan - P - (SP1) do Kowalski Adam - R - Kowalska Ola - (SP1)",
        "Użytkownik: Nowak Jan - P - (SP1) przesłał do Ciebie wiadomość<br>Temat: Test<br>",
    )
    n = parse_message(msg)
    assert n.sender.name == "Nowak Jan"
    assert n.sender.initials is None
    assert n.student == "Kowalska Ola"
    assert n.vulcan_id is None


def test_not_a_notification():
    with pytest.raises(NotificationParseError):
        parse_message(_msg("Re: obiad", "cześć"))


# --- powiadomienia przekazane ręcznie ---

WARSAW = ZoneInfo("Europe/Warsaw")


def _forward_inline(date_line: str, subject_prefix: str = "Fwd: ") -> EmailMessage:
    original = (FIXTURES / "notif_sliwa_zofia.eml").read_bytes()
    inner = email.message_from_bytes(original, policy=email.policy.default)
    inner_html = inner.get_body(preferencelist=("html",)).get_content()
    msg = EmailMessage()
    msg["From"] = "Jan Nowak <rodzic@example.com>"
    msg["Subject"] = subject_prefix + str(inner["Subject"])
    msg["Date"] = "Mon, 02 Nov 2026 14:00:00 +0100"
    msg["Message-ID"] = "<fwd@example.com>"
    msg.set_content(
        '<div dir="ltr"><br><div class="gmail_quote">---------- Forwarded message ---------<br>'
        "Od: <strong>Dziennik VULCAN</strong> &lt;noreply@vulcan.net.pl&gt;<br>"
        f"{date_line}<br>Subject: {inner['Subject']}<br>To: &lt;x@example.com&gt;<br><br><br>"
        f"{inner_html}</div></div>",
        subtype="html",
    )
    return msg


@pytest.mark.parametrize(
    "date_line",
    [
        "Date: pon., 2 lis 2026 o 10:13",
        "Date: Mon, Nov 2, 2026 at 10:13 AM",
        "Wysłano: poniedziałek, 2 listopada 2026 10:13",
    ],
)
def test_inline_forward_uses_original_date(date_line):
    n = parse_message(_forward_inline(date_line))
    assert n.forwarded
    assert not n.date_approximate
    assert n.sent_at == datetime(2026, 11, 2, 10, 13, tzinfo=WARSAW)
    assert n.sender.name == "Śliwa Agata"
    assert n.student == "Nowak Zofia"
    assert n.subject == 'Zbiórka darów dla Schroniska "Psie Pole"'
    assert n.vulcan_id == "33333333-cccc-4ccc-8ccc-333333333333"


def test_inline_forward_without_date_is_approximate():
    n = parse_message(_forward_inline("", subject_prefix="PD: "))
    assert n.forwarded
    assert n.date_approximate
    assert n.sent_at == datetime(2026, 11, 2, 14, 0, tzinfo=timezone(timedelta(hours=1)))


def test_forward_as_attachment():
    original = (FIXTURES / "notif_lis_zofia.eml").read_bytes()
    msg = EmailMessage()
    msg["From"] = "rodzic@example.com"
    msg["Subject"] = "Fwd: zobacz"
    msg["Date"] = "Mon, 02 Nov 2026 14:00:00 +0100"
    msg.set_content("w załączniku")
    msg.add_attachment(email.message_from_bytes(original, policy=email.policy.default))
    assert is_vulcan_notification(msg)
    n = parse_message(msg)
    assert n.forwarded
    assert n.subject == "OPATRUNEK NA RATUNEK!"
    assert n.sent_at == datetime(2026, 10, 22, 13, 56, 22, tzinfo=timezone(timedelta(hours=2)))


def test_real_gmail_inline_forward():
    n = load("fwd_gmail_inline_kowalska_maja.eml")
    assert n.forwarded
    assert not n.date_approximate
    assert n.sent_at == datetime(2026, 11, 2, 12, 7, tzinfo=WARSAW)
    assert n.sender.name == "Kowalska-Nowicka Ewa"
    assert n.student == "Nowak Maja"
    assert n.subject == "Warsztaty muzyczno-taneczne"
    assert n.vulcan_id == "22222222-bbbb-4bbb-8bbb-222222222222"
