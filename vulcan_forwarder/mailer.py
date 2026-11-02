"""Budowa i wysyłka maila z pełną treścią wiadomości Vulcana."""

from __future__ import annotations

import html
import logging
import smtplib
import ssl
import sys
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid

import nh3

from vulcan_forwarder.config import SMTP_FIELDS, Settings
from vulcan_forwarder.notification_parser import html_to_text, parse_mailbox, parse_sender
from vulcan_forwarder.resilience import retry
from vulcan_forwarder.vulcan_client import Message

log = logging.getLogger(__name__)

SMTP_TIMEOUT_S = 60

ALLOWED_TAGS = set(nh3.ALLOWED_TAGS) | {"font"}
ALLOWED_ATTRIBUTES = {
    "*": {"style", "align", "title", "dir", "lang"},
    "a": {"href"},
    "img": {"src", "alt", "width", "height"},
    "font": {"color", "size", "face"},
    "td": {"colspan", "rowspan", "valign", "width"},
    "th": {"colspan", "rowspan", "valign", "width"},
    "table": {"border", "cellpadding", "cellspacing", "width"},
    "ol": {"start", "type"},
}
ALLOWED_STYLE_PROPERTIES = {
    "color", "background-color", "font-weight", "font-style", "font-size", "font-family", "text-decoration",
    "text-align", "text-indent", "line-height", "list-style-type", "vertical-align", "white-space",
    "margin", "margin-left", "margin-right", "margin-top", "margin-bottom",
    "padding", "padding-left", "padding-right", "padding-top", "padding-bottom",
    "border", "border-collapse", "width",
}  # fmt: skip


def _attribute_filter(tag: str, attr: str, value: str) -> str | None:
    lowered = value.strip().lower()
    if tag == "img" and attr == "src":
        # Zewnętrzne obrazki to typowe trackery — zostawiamy tylko osadzone (data:image/...).
        return value if lowered.startswith("data:image/") else None
    if attr == "href" and not lowered.startswith(("http://", "https://", "mailto:")):
        return None
    return value


def sanitize_html(content: str) -> str:
    """Usuwa skrypty, handlery zdarzeń, zewnętrzne obrazki/trackery; zachowuje formatowanie."""
    return nh3.clean(
        content,
        tags=ALLOWED_TAGS,
        clean_content_tags={"script", "style"},
        attributes=ALLOWED_ATTRIBUTES,
        attribute_filter=_attribute_filter,
        filter_style_properties=ALLOWED_STYLE_PROPERTIES,
        url_schemes={"http", "https", "mailto", "data"},
        link_rel="noopener noreferrer",
    )


def short_name(party: str) -> str:
    """„Nowak Anna [AN] - P - (SP1)” → „Nowak Anna”."""
    parsed = parse_sender(party)
    return parsed.name if parsed else party


def student_of(mailbox: str | None) -> str | None:
    parsed = parse_mailbox(mailbox) if mailbox else None
    return parsed.student if parsed else None


def build_email(message: Message, settings: Settings, link: str | None = None) -> EmailMessage:
    sender_name = short_name(message.sender)
    student = student_of(message.mailbox)
    sent = message.sent_at.strftime("%d.%m.%Y %H:%M")
    body_html = sanitize_html(message.html)

    msg = EmailMessage()
    msg["Subject"] = f"[Vulcan] {message.subject} — {sender_name}"
    msg["From"] = settings.mail_from
    msg["To"] = settings.mail_recipient
    msg["Date"] = format_datetime(message.sent_at)
    msg["Message-ID"] = make_msgid(idstring=message.key, domain="vulcan-forwarder.local")
    msg["X-Vulcan-Message-Key"] = message.key

    # --- plain text ---
    lines = [
        f"Od: {message.sender}",
        f"Do: {', '.join(message.recipients)}",
        f"Data: {sent}",
    ]
    if student:
        lines.append(f"Uczeń: {student}")
    if message.withdrawn:
        lines.append("UWAGA: wiadomość została wycofana przez nadawcę.")
    lines += ["", f"Temat: {message.subject}", "", html_to_text(body_html).strip(), ""]
    if message.attachments:
        lines.append("Załączniki (linki):")
        lines += [f"- {a.name}: {a.url}" for a in message.attachments]
        lines.append("")
    if link:
        lines.append(f"Wiadomość w dzienniku: {link}")
    msg.set_content("\n".join(lines))

    # --- HTML ---
    e = html.escape

    def row(label: str, value: str) -> str:
        return (
            f'<tr><td style="padding:2px 12px 2px 0;color:#666;vertical-align:top;white-space:nowrap">{label}</td>'
            f'<td style="padding:2px 0">{value}</td></tr>'
        )

    meta = [row("Od:", e(message.sender)), row("Do:", "<br>".join(e(r) for r in message.recipients)), row("Data:", e(sent))]
    if student:
        meta.append(row("Uczeń:", f"<strong>{e(student)}</strong>"))
    parts = [
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#222;max-width:800px">',
        f'<h2 style="font-size:18px;margin:0 0 8px">{e(message.subject)}</h2>',
        f'<table style="border-collapse:collapse;font-size:13px;margin-bottom:12px">{"".join(meta)}</table>',
    ]
    if message.withdrawn:
        parts.append('<p style="color:#b00"><strong>Wiadomość została wycofana przez nadawcę.</strong></p>')
    parts.append(f'<div style="border-top:1px solid #ddd;padding-top:12px">{body_html}</div>')
    if message.attachments:
        items = "".join(f'<li><a href="{e(a.url, quote=True)}">{e(a.name)}</a></li>' for a in message.attachments)
        parts.append(
            '<div style="border-top:1px solid #ddd;margin-top:16px;padding-top:8px">'
            f"<strong>Załączniki</strong> (linki do OneDrive nadawcy):<ul>{items}</ul></div>"
        )
    if link:
        parts.append(
            f'<p style="border-top:1px solid #ddd;margin-top:16px;padding-top:8px;font-size:12px;color:#666">'
            f'<a href="{e(link, quote=True)}">Otwórz wiadomości w dzienniku VULCAN</a></p>'
        )
    parts.append("</div>")
    msg.add_alternative("".join(parts), subtype="html")
    return msg


def print_email(msg: EmailMessage) -> None:
    """Czytelny (odkodowany) podgląd maila na stdout — dla --dry-run."""
    out = sys.stdout
    out.write("=" * 78 + "\n[dry-run] mail, który zostałby wysłany:\n")
    for header in ("Subject", "From", "To", "Date", "X-Vulcan-Message-Key"):
        if msg[header] is not None:
            out.write(f"{header}: {msg[header]}\n")
    for part in msg.walk():
        if part.get_content_maintype() == "text":
            out.write(f"\n--- {part.get_content_type()} ---\n{part.get_content()}\n")
    out.write("=" * 78 + "\n")
    out.flush()


def is_transient_smtp_error(exc: BaseException) -> bool:
    """Sieć, rozłączenie, odpowiedzi 4xx — ponawiamy. Logowanie, odrzucony adres, 5xx — nie."""
    if isinstance(exc, (smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused)):
        return False
    if isinstance(exc, smtplib.SMTPResponseException):
        return 400 <= exc.smtp_code < 500
    return isinstance(exc, OSError)  # w tym SMTPServerDisconnected, SMTPConnectError, timeouty


def send(msg: EmailMessage, settings: Settings, dry_run: bool = False) -> None:
    if dry_run:
        print_email(msg)
        return
    settings.require(*SMTP_FIELDS)
    retry(lambda: _send_smtp(msg, settings), what="SMTP: wysyłka", should_retry=is_transient_smtp_error)
    log.info("SMTP: wysłano „%s” do %s", msg["Subject"], msg["To"])


def _send_smtp(msg: EmailMessage, settings: Settings) -> None:
    context = ssl.create_default_context()
    if settings.smtp_port == 465:
        smtp: smtplib.SMTP = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=SMTP_TIMEOUT_S, context=context)
    else:
        smtp = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=SMTP_TIMEOUT_S)
    with smtp:
        if settings.smtp_port != 465:
            smtp.ehlo()
            if smtp.has_extn("starttls"):
                smtp.starttls(context=context)
                smtp.ehlo()
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password.get_secret_value())
        smtp.send_message(msg)
