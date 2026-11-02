"""Parser maili z powiadomieniami VULCAN („Wiadomość od użytkownika ...”).

Format (stan na 2026-11):

    Subject: Wiadomość od użytkownika <Nadawca> [<inicjały>] - P - (<jednostka>)
             do <Adresat> - R - <Uczeń> - (<jednostka>)

    Treść (HTML, linie rozdzielone <br>):
        Użytkownik: <nadawca jak w temacie> przesłał do Ciebie ... wiadomość
        Temat: <temat wiadomości>
        ...
        ID: <uuid>

Powiadomienie nie zawiera linku do wiadomości.

Obsługiwane są też powiadomienia przekazane ręcznie („Fwd:”/„PD:”): jako załącznik
message/rfc822 (parsowany jak oryginał) albo wklejone w treść — wtedy datę oryginału
próbujemy odczytać z nagłówka przekazania, a gdy się nie da, bierzemy datę przekazania
i ustawiamy `date_approximate`.
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import html
import re
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from zoneinfo import ZoneInfo


class NotificationParseError(ValueError):
    pass


@dataclass(frozen=True)
class Party:
    """Uczestnik korespondencji w formacie Vulcana, np. „Nowak Anna [AN] - P - (SP1)”."""

    raw: str
    name: str
    role: str  # P = pracownik, R = rodzic, U = uczeń
    unit: str  # skrót jednostki/szkoły
    initials: str | None = None
    student: str | None = None  # dla rodzica: uczeń, w kontekście którego przyszła wiadomość


@dataclass(frozen=True)
class Notification:
    email_message_id: str
    sent_at: datetime  # nagłówek Date (z tz)
    sender: Party
    recipient: Party
    subject: str  # temat wiadomości w dzienniku
    vulcan_id: str | None  # UUID z treści powiadomienia
    link: str | None = None
    forwarded: bool = False  # powiadomienie przekazane ręcznie
    date_approximate: bool = False  # sent_at to data przekazania, nie oryginału

    @property
    def student(self) -> str | None:
        return self.recipient.student


SUBJECT_RE = re.compile(r"^Wiadomość od użytkownika (?P<sender>.+ - \w+ - \([^)]*\)) do (?P<recipient>.+)$")
# „Nazwisko Imię [IN] - P - (Jednostka)”; inicjały opcjonalne
SENDER_RE = re.compile(r"^(?P<name>.+?)(?:\s+\[(?P<initials>[^\]]*)\])? - (?P<role>\w+) - \((?P<unit>[^)]*)\)$")
# „Nazwisko Imię - R - Uczeń Imię - (Jednostka)”; uczeń opcjonalny
RECIPIENT_RE = re.compile(
    r"^(?P<name>.+?)(?:\s+\[(?P<initials>[^\]]*)\])? - (?P<role>\w+) - (?:(?P<student>.+?) - )?\((?P<unit>[^)]*)\)$"
)
BODY_SENDER_RE = re.compile(r"^Użytkownik:\s*(?P<sender>.+?)\s+przesłał", re.MULTILINE)
BODY_SUBJECT_RE = re.compile(r"^Temat:\s*(?P<subject>.*)$", re.MULTILINE)
BODY_ID_RE = re.compile(r"^ID:\s*(?P<id>[0-9a-fA-F-]{36})\s*$", re.MULTILINE)
LINK_RE = re.compile(r"https?://[^\s\"'<>]+")
FORWARD_PREFIX_RE = re.compile(r"^(?:(?:fwd?|fw|pd|tr|wg)\s*:\s*)+", re.IGNORECASE)
NOTIFICATION_SUBJECT_PREFIX = "Wiadomość od użytkownika"

LOCAL_TZ = ZoneInfo("Europe/Warsaw")
# Nagłówek daty w bloku przekazania (Gmail/Outlook, PL/EN), np.
#   „Date: pon., 2 lis 2026 o 10:13”, „Data: 2 listopada 2026 10:13”,
#   „Date: Mon, Nov 2, 2026 at 10:13 AM”, „Sent: Monday, November 2, 2026 10:13 AM”
FWD_DATE_LINE_RE = re.compile(r"^\s*\*?(?:Date|Data|Sent|Wysłano|Wysłane)\s*:\*?\s*(?P<value>.+)$", re.MULTILINE | re.IGNORECASE)
_TIME = r"(?P<h>\d{1,2}):(?P<mi>\d{2})(?::\d{2})?\s*(?P<ampm>AM|PM)?"
FWD_DATE_DMY_RE = re.compile(r"(?P<d>\d{1,2})\.?\s+(?P<mon>[^\W\d_]+)\.?,?\s+(?P<y>\d{4})\D*?" + _TIME, re.IGNORECASE)
FWD_DATE_MDY_RE = re.compile(r"(?P<mon>[^\W\d_]+)\.?\s+(?P<d>\d{1,2}),?\s+(?P<y>\d{4})\D*?" + _TIME, re.IGNORECASE)
MONTHS = {
    "sty": 1, "lut": 2, "mar": 3, "kwi": 4, "maj": 5, "cze": 6, "lip": 7, "sie": 8, "wrz": 9, "paź": 10, "paz": 10,
    "lis": 11, "gru": 12,
    "jan": 1, "feb": 2, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _parse_party(raw: str, pattern: re.Pattern[str]) -> Party:
    raw = _norm(raw)
    m = pattern.match(raw)
    if not m:
        raise NotificationParseError(f"Nieznany format uczestnika: {raw!r}")
    groups = m.groupdict()
    return Party(
        raw=raw,
        name=groups["name"].strip(),
        role=groups["role"],
        unit=groups["unit"],
        initials=groups.get("initials"),
        student=(groups.get("student") or None),
    )


def parse_sender(raw: str) -> Party | None:
    """„Nazwisko Imię [IN] - P - (Jednostka)” → Party; None, gdy format nieznany."""
    try:
        return _parse_party(raw, SENDER_RE)
    except NotificationParseError:
        return None


def parse_mailbox(raw: str) -> Party | None:
    """„Rodzic - R - Uczeń - (Jednostka)” → Party (z polem student); None, gdy format nieznany."""
    try:
        return _parse_party(raw, RECIPIENT_RE)
    except NotificationParseError:
        return None


def strip_forward_prefix(subject: str) -> str:
    return FORWARD_PREFIX_RE.sub("", _norm(subject))


def _forwarded_date(text: str) -> datetime | None:
    """Data oryginału z bloku „---------- Forwarded message ----------” (czas lokalny PL)."""
    for line in FWD_DATE_LINE_RE.finditer(text):
        value = line["value"]
        for pattern in (FWD_DATE_DMY_RE, FWD_DATE_MDY_RE):
            m = pattern.search(value)
            if not m or (month := MONTHS.get(m["mon"][:3].lower())) is None:
                continue
            hour = int(m["h"])
            if m["ampm"]:
                hour = hour % 12 + (12 if m["ampm"].upper() == "PM" else 0)
            return datetime(int(m["y"]), month, int(m["d"]), hour, int(m["mi"]), tzinfo=LOCAL_TZ)
    return None


def _attached_notification(msg: EmailMessage) -> EmailMessage | None:
    """Powiadomienie przekazane jako załącznik (message/rfc822)."""
    for part in msg.walk():
        if part.get_content_type() == "message/rfc822":
            inner = part.get_content()
            if isinstance(inner, EmailMessage) and is_vulcan_notification(inner):
                return inner
    return None


def html_to_text(body: str) -> str:
    text = re.sub(r"(?i)<li[^>]*>", "\n• ", body)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</h\d>|</tr>|</ul>|</ol>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return html.unescape(text)


def _body_text(msg: EmailMessage) -> tuple[str, str | None]:
    """Zwraca (tekst, html) z treści maila.

    HTML ma pierwszeństwo: klienci pocztowi zawijają długie linie w części text/plain
    przy przekazywaniu, co ucinałoby np. linię „Temat:”.
    """
    html_part = msg.get_body(preferencelist=("html",))
    plain_part = msg.get_body(preferencelist=("plain",))
    html_body = html_part.get_content() if html_part is not None else None
    if html_body is not None:
        return html_to_text(html_body), html_body
    if plain_part is not None:
        return plain_part.get_content(), None
    raise NotificationParseError("Mail nie ma treści tekstowej ani HTML")


def is_vulcan_notification(msg: EmailMessage) -> bool:
    if strip_forward_prefix(str(msg.get("Subject", ""))).startswith(NOTIFICATION_SUBJECT_PREFIX):
        return True
    return _attached_notification(msg) is not None


def parse_message(msg: EmailMessage) -> Notification:
    if not strip_forward_prefix(str(msg.get("Subject", ""))).startswith(NOTIFICATION_SUBJECT_PREFIX):
        if (inner := _attached_notification(msg)) is not None:
            n = parse_message(inner)
            return Notification(**{**n.__dict__, "forwarded": True})

    raw_subject = _norm(str(msg.get("Subject", "")))
    subject_header = strip_forward_prefix(raw_subject)
    forwarded = subject_header != raw_subject
    text, html_body = _body_text(msg)

    m = SUBJECT_RE.match(subject_header)
    if not m:
        raise NotificationParseError(f"Temat nie wygląda na powiadomienie Vulcana: {subject_header!r}")
    sender = _parse_party(m["sender"], SENDER_RE)
    recipient = _parse_party(m["recipient"], RECIPIENT_RE)

    # Treść ma pełną nazwę nadawcy — jeśli się różni, ufamy treści.
    if (bm := BODY_SENDER_RE.search(text)) and _norm(bm["sender"]) != sender.raw:
        sender = _parse_party(bm["sender"], SENDER_RE)

    sm = BODY_SUBJECT_RE.search(text)
    if not sm:
        raise NotificationParseError("Brak linii „Temat:” w treści powiadomienia")

    id_match = BODY_ID_RE.search(text)
    link_match = LINK_RE.search(html_body or text)

    date_header = msg.get("Date")
    if not date_header:
        raise NotificationParseError("Brak nagłówka Date")
    sent_at = email.utils.parsedate_to_datetime(str(date_header))
    date_approximate = False
    if forwarded:
        original = _forwarded_date(text)
        if original is not None:
            sent_at = original
        else:
            date_approximate = True

    return Notification(
        email_message_id=_norm(str(msg.get("Message-ID", ""))),
        sent_at=sent_at,
        sender=sender,
        recipient=recipient,
        subject=_norm(sm["subject"]),
        vulcan_id=id_match["id"].lower() if id_match else None,
        link=link_match.group(0) if link_match else None,
        forwarded=forwarded,
        date_approximate=date_approximate,
    )


def parse_bytes(raw: bytes) -> Notification:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    assert isinstance(msg, EmailMessage)
    return parse_message(msg)
