"""Alerty mailowe o błędach, z throttlingiem (domyślnie 1 alert danego typu na godzinę)."""

from __future__ import annotations

import logging
import socket
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

from vulcan_forwarder import mailer
from vulcan_forwarder.config import Settings
from vulcan_forwarder.store import Store

log = logging.getLogger(__name__)

ALERT_THROTTLE = timedelta(hours=1)
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024


class Alerter:
    def __init__(self, settings: Settings, dry_run: bool = False, redact=lambda text: text):
        self.settings = settings
        self.dry_run = dry_run
        self.redact = redact

    def alert(self, kind: str, summary: str, details: str = "", screenshot: Path | None = None) -> bool:
        """Wysyła alert, jeśli w ostatniej godzinie nie poszedł alert tego samego typu. Zwraca True, gdy wysłano."""
        now = datetime.now(timezone.utc)
        key = f"alert:{kind}"
        with Store(self.settings.db_path) as store:
            last = store.get_meta(key)
            if last and now - datetime.fromisoformat(last) < ALERT_THROTTLE:
                log.info("Alert „%s” pominięty (throttling, ostatni: %s)", kind, last)
                return False
            if not self.settings.alert_recipient or not self.settings.mail_from:
                log.warning("Alert „%s” nie wysłany: brak ALERT_EMAIL_TO/MAIL_TO lub MAIL_FROM", kind)
                return False
            try:
                mailer.send(self._build(kind, summary, details, screenshot, now), self.settings, dry_run=self.dry_run)
            except Exception as exc:  # noqa: BLE001 - alert nie może przykryć pierwotnego błędu
                log.error("Nie udało się wysłać alertu „%s”: %s", kind, self.redact(str(exc)))
                return False
            if not self.dry_run:
                store.set_meta(key, now.isoformat(timespec="seconds"))
        log.info("%sWysłano alert „%s” do %s", "[dry-run] " if self.dry_run else "", kind, self.settings.alert_recipient)
        return True

    def _build(self, kind: str, summary: str, details: str, screenshot: Path | None, now: datetime) -> EmailMessage:
        summary, details = self.redact(summary), self.redact(details)
        msg = EmailMessage()
        msg["Subject"] = f"[Vulcan forwarder] Błąd: {kind} — {summary[:120]}"
        msg["From"] = self.settings.mail_from
        msg["To"] = self.settings.alert_recipient
        lines = [
            f"Typ: {kind}",
            f"Czas: {now.astimezone():%Y-%m-%d %H:%M:%S %z}",
            f"Host: {socket.gethostname()}",
            "",
            summary,
        ]
        if details:
            lines += ["", details]
        lines += [
            "",
            f"Kolejny alert tego typu najwcześniej za {int(ALERT_THROTTLE.total_seconds() // 60)} min.",
            "Szczegóły w logach serwisu (docker logs / journalctl).",
        ]
        if screenshot:
            lines.append(f"Zrzut ekranu w załączniku (kopia na serwerze: {screenshot}).")
        msg.set_content("\n".join(lines))
        if screenshot and screenshot.exists() and screenshot.stat().st_size <= MAX_ATTACHMENT_BYTES:
            msg.add_attachment(screenshot.read_bytes(), maintype="image", subtype="png", filename=screenshot.name)
        return msg
