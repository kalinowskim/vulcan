"""Dopasowanie powiadomienia z maila do wiadomości w Vulcanie."""

from __future__ import annotations

import re
from datetime import timedelta

from vulcan_forwarder.notification_parser import Notification
from vulcan_forwarder.vulcan_client import MessageSummary

# Powiadomienie wychodzi kilka sekund po wysłaniu wiadomości; zostawiamy zapas na opóźnienia kolejki.
TIME_TOLERANCE = timedelta(minutes=10)
# Dla przekazań bez daty oryginału (date_approximate) znamy tylko górną granicę.
APPROXIMATE_WINDOW = timedelta(days=7)


def _key(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def match(notification: Notification, messages: list[MessageSummary]) -> MessageSummary | None:
    """Zwraca wiadomość odpowiadającą powiadomieniu albo None, gdy brak jednoznacznego dopasowania.

    1. Po UUID z powiadomienia (= apiGlobalKey) — dokładnie.
    2. Inaczej: nadawca + temat + skrzynka (uczeń) + czas z tolerancją; musi wyjść dokładnie jeden kandydat.
    """
    if notification.vulcan_id:
        for m in messages:
            if m.key == notification.vulcan_id:
                return m

    sender = _key(notification.sender.raw)
    subject = _key(notification.subject)
    mailbox = _key(notification.recipient.raw)

    def time_ok(m: MessageSummary) -> bool:
        if notification.date_approximate:
            return notification.sent_at - APPROXIMATE_WINDOW <= m.sent_at <= notification.sent_at + TIME_TOLERANCE
        return abs(m.sent_at - notification.sent_at) <= TIME_TOLERANCE

    candidates = [
        m
        for m in messages
        if _key(m.sender) == sender and _key(m.subject) == subject and _key(m.mailbox) == mailbox and time_ok(m)
    ]
    return candidates[0] if len(candidates) == 1 else None
