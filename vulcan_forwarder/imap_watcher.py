"""Odczyt powiadomień Vulcana ze skrzynki IMAP i oznaczanie ich jako przetworzonych."""

from __future__ import annotations

import imaplib
import logging
import ssl
from dataclasses import dataclass

from imapclient import IMAPClient

from vulcan_forwarder.config import IMAP_FIELDS, Settings
from vulcan_forwarder.resilience import retry

log = logging.getLogger(__name__)

PROCESSED_KEYWORD = "$VulcanForwarded"
IMAP_TIMEOUT_S = 60


@dataclass(frozen=True)
class RawNotification:
    uid: int
    raw: bytes


def is_transient_imap_error(exc: BaseException) -> bool:
    """Sieć / zerwane połączenie — warto ponowić. Błędy logowania i protokołu — nie."""
    return isinstance(exc, (OSError, imaplib.IMAP4.abort))


def from_criteria(senders: list[str]) -> list:
    """Kryterium SEARCH dla kilku nadawców: OR (FROM a) (OR (FROM b) (FROM c))."""
    if not senders:
        return []
    if len(senders) == 1:
        return ["FROM", senders[0]]
    return ["OR", ["FROM", senders[0]], from_criteria(senders[1:])]


class ImapWatcher:
    """Użycie: `with ImapWatcher(settings) as w: for n in w.fetch_new(): ...`."""

    def __init__(self, settings: Settings):
        settings.require(*IMAP_FIELDS)
        self.settings = settings
        self.client: IMAPClient | None = None
        self._keywords_supported = False

    def __enter__(self) -> ImapWatcher:
        self.client = retry(self._connect, what="IMAP: połączenie", should_retry=is_transient_imap_error)
        return self

    def _connect(self) -> IMAPClient:
        s = self.settings
        log.info("IMAP: łączenie z %s:%s jako %s", s.imap_host, s.imap_port, s.imap_user)
        client = IMAPClient(
            s.imap_host,
            port=s.imap_port,
            ssl=s.imap_port == 993,
            ssl_context=ssl.create_default_context(),
            timeout=IMAP_TIMEOUT_S,
        )
        if s.imap_port != 993:
            client.starttls(ssl.create_default_context())
        client.login(s.imap_user, s.imap_password.get_secret_value())
        info = client.select_folder(s.imap_folder)
        permanent = info.get(b"PERMANENTFLAGS", ())
        self._keywords_supported = b"\\*" in permanent
        if not self._keywords_supported:
            log.warning("IMAP: serwer nie pozwala na własne flagi; polegam na przenoszeniu do %s", s.imap_processed_folder)
        return client

    def __exit__(self, *exc) -> None:
        if self.client is not None:
            try:
                self.client.logout()
            except Exception:  # noqa: BLE001 - zamykanie połączenia nie może maskować właściwego błędu
                log.debug("IMAP: błąd przy wylogowaniu", exc_info=True)
            self.client = None

    def _search_criteria(self, include_processed: bool) -> list:
        # Temat filtrujemy po stronie klienta: wyszukiwanie po zakodowanym (RFC 2047) temacie
        # z polskimi znakami nie działa jednakowo na wszystkich serwerach.
        criteria: list = from_criteria(self.settings.sender_filters)
        if not include_processed and self._keywords_supported:
            criteria += ["UNKEYWORD", PROCESSED_KEYWORD]
        return criteria or ["ALL"]

    def fetch_new(self, include_processed: bool = False, limit: int | None = None) -> list[RawNotification]:
        assert self.client is not None
        uids = sorted(self.client.search(self._search_criteria(include_processed)))
        if limit is not None:
            uids = uids[-limit:]
        log.info("IMAP: %d kandydatów w %s", len(uids), self.settings.imap_folder)
        if not uids:
            return []
        data = self.client.fetch(uids, ["BODY.PEEK[]"])
        return [RawNotification(uid=uid, raw=data[uid][b"BODY[]"]) for uid in uids if uid in data]

    def mark_ignored(self, uid: int) -> None:
        """Mail od Vulcana, który nie jest powiadomieniem o wiadomości: tylko flaga, bez przenoszenia."""
        assert self.client is not None
        if self._keywords_supported:
            self.client.add_flags([uid], [PROCESSED_KEYWORD])

    def mark_processed(self, uid: int) -> None:
        assert self.client is not None
        self.mark_ignored(uid)
        folder = self.settings.imap_processed_folder
        if not folder:
            return
        if not self.client.folder_exists(folder):
            log.info("IMAP: tworzę folder %s", folder)
            self.client.create_folder(folder)
        if self.client.has_capability("MOVE"):
            self.client.move([uid], folder)
        else:
            self.client.copy([uid], folder)
            self.client.delete_messages([uid])
            if self.client.has_capability("UIDPLUS"):
                self.client.uid_expunge([uid])
            else:
                self.client.expunge()
