from datetime import datetime, timedelta, timezone

from vulcan_forwarder.config import Settings
from vulcan_forwarder.mailer import build_email, sanitize_html, short_name, student_of
from vulcan_forwarder.vulcan_client import Attachment, Message

SETTINGS = Settings(_env_file=None, mail_from="bot@example.com", mail_to="ja@example.com")


def make_message(**kw) -> Message:
    base = dict(
        key="22222222-bbbb-4bbb-8bbb-222222222222",
        id=941756,
        sender="Kowalska-Nowicka Ewa [Kn] - P - (SP1_Test)",
        recipients=["Nowak Jan - R - Nowak Maja - (SP1_Test)"],
        subject="Warsztaty muzyczno-taneczne",
        sent_at=datetime(2026, 11, 2, 12, 7, 29, tzinfo=timezone(timedelta(hours=1))),
        html="<p>Drodzy Rodzice,</p><ul><li>punkt <strong>pierwszy</strong></li><li>drugi</li></ul>",
        mailbox="Nowak Jan - R - Nowak Maja - (SP1_Test)",
    )
    base.update(kw)
    return Message(**base)


def test_sanitize_removes_scripts_handlers_and_trackers():
    dirty = (
        '<p style="color:red;position:fixed" onclick="x()">Tekst</p>'
        "<script>alert(1)</script>"
        '<img src="https://tracker.example.com/pixel.gif" width="1">'
        '<img src="data:image/png;base64,iVBORw0KGgo=" alt="obrazek">'
        '<a href="javascript:alert(1)">zły</a> <a href="https://szkola.pl/x">dobry</a>'
        "<style>body{display:none}</style><iframe src='https://x'></iframe>"
    )
    clean = sanitize_html(dirty)
    assert "script" not in clean and "alert" not in clean
    assert "onclick" not in clean
    assert "tracker.example.com" not in clean
    assert 'src="data:image/png;base64,iVBORw0KGgo="' in clean
    assert "javascript:" not in clean
    assert 'href="https://szkola.pl/x"' in clean
    assert "color:red" in clean and "position" not in clean
    assert "iframe" not in clean and "display:none" not in clean


def test_sanitize_keeps_formatting():
    html = '<p>📚 <strong>Drodzy</strong> <em>Rodzice</em></p><ul style="list-style-type:disc"><li>a</li></ul>'
    clean = sanitize_html(html)
    assert "<strong>Drodzy</strong>" in clean and "<em>Rodzice</em>" in clean
    assert "<li>a</li>" in clean and "list-style-type:disc" in clean


def test_names():
    assert short_name("Śliwa Agata [ŚA] - P - (SP1_Test)") == "Śliwa Agata"
    assert short_name("coś dziwnego") == "coś dziwnego"
    assert student_of("Nowak Jan - R - Nowak Zofia - (SP1_Test)") == "Nowak Zofia"
    assert student_of(None) is None


def test_build_email():
    msg = build_email(
        make_message(attachments=[Attachment("Regulamin.pdf", "https://1drv.ms/b/c/abc/XYZ")]),
        SETTINGS,
        link="https://wiadomosci.eduvulcan.pl/przyklad/App/odebrane",
    )
    assert msg["Subject"] == "[Vulcan] Warsztaty muzyczno-taneczne — Kowalska-Nowicka Ewa"
    assert msg["To"] == "ja@example.com" and msg["From"] == "bot@example.com"
    plain = msg.get_body(preferencelist=("plain",)).get_content()
    html = msg.get_body(preferencelist=("html",)).get_content()
    for part in (plain, html):
        assert "Kowalska-Nowicka Ewa [Kn] - P - (SP1_Test)" in part
        assert "Nowak Maja" in part
        assert "02.11.2026 12:07" in part
        assert "https://1drv.ms/b/c/abc/XYZ" in part
        assert "https://wiadomosci.eduvulcan.pl/przyklad/App/odebrane" in part
    assert "• punkt pierwszy" in plain
    assert "<strong>pierwszy</strong>" in html
    assert not [p for p in msg.walk() if p.get_content_disposition() == "attachment"]


def test_build_email_withdrawn_and_no_link():
    msg = build_email(make_message(withdrawn=True), SETTINGS)
    plain = msg.get_body(preferencelist=("plain",)).get_content()
    assert "wycofana" in plain
    assert "Wiadomość w dzienniku" not in plain
