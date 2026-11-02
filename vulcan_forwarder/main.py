"""Punkt wejścia: pojedynczy przebieg (--once) lub pętla (--loop)."""

from __future__ import annotations

import argparse
import email
import email.policy
import imaplib
import logging
import re
import shutil
import smtplib
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError

from vulcan_forwarder.alerts import Alerter
from vulcan_forwarder.config import DEFAULT_ACCOUNT, Account, ConfigError, Settings
from vulcan_forwarder.imap_watcher import ImapWatcher
from vulcan_forwarder.mailer import build_email, send
from vulcan_forwarder.matcher import match
from vulcan_forwarder.notification_parser import (
    Notification,
    NotificationParseError,
    is_vulcan_notification,
    parse_message,
)
from vulcan_forwarder.resilience import FileLock, SecretFilter
from vulcan_forwarder.store import FORWARDED, SEEN, Store
from vulcan_forwarder.vulcan_client import (
    Message,
    MessageSummary,
    VulcanClient,
    VulcanError,
    VulcanLoginError,
    known_mailboxes,
)

# Ile wstecz od najstarszego powiadomienia sięgamy po listę odebranych.
LIST_WINDOW = timedelta(days=1)
# Synchronizacja sięga tyle przed ostatnią znaną wiadomość (na wypadek opóźnionego pojawienia się w API).
SYNC_OVERLAP = timedelta(days=1)
# Niedopasowane powiadomienie zostaje w skrzynce do kolejnych prób, ale nie dłużej niż tyle.
UNMATCHED_GIVE_UP_AFTER = timedelta(hours=24)
# Ile stron (po 50) ostatnich wiadomości --init oznacza jako widziane.
INIT_PAGES = 4

log = logging.getLogger("vulcan_forwarder")


def setup_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    # imapclient w trybie DEBUG loguje całą sesję IMAP, łącznie z LOGIN — wyciszamy.
    logging.getLogger("imapclient").setLevel(logging.WARNING)


def install_secret_filter(settings: Settings) -> SecretFilter:
    """Maskuje hasła z konfiguracji (IMAP, SMTP, wszystkich kont Vulcana) we wszystkich logach."""
    secret_filter = SecretFilter(settings.secrets)
    for handler in logging.getLogger().handlers:
        handler.addFilter(secret_filter)
    return secret_filter


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="vulcan-forwarder", description=__doc__)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="jeden przebieg (cron / systemd timer)")
    mode.add_argument("--loop", action="store_true", help="ciągła pętla co POLL_INTERVAL_MINUTES")
    mode.add_argument("--init", action="store_true", help="oznacz bieżące wiadomości jako widziane, nic nie wysyłaj")
    mode.add_argument(
        "--fetch-fixtures",
        type=int,
        metavar="N",
        help="zapisz N ostatnich powiadomień ze skrzynki do debug/eml/ (do testów parsera)",
    )
    mode.add_argument(
        "--probe",
        type=int,
        nargs="?",
        const=5,
        metavar="N",
        help="diagnostyka Vulcana: zaloguj się i wypisz N ostatnich odebranych wiadomości (bez oznaczania)",
    )
    p.add_argument("--account", action="append", metavar="NAZWA", help="tylko wskazane konto (można powtórzyć)")
    p.add_argument("--dry-run", action="store_true", help="zamiast wysyłać mail, wypisz go na stdout")
    p.add_argument("--sync", action="store_true", help="z --once/--loop: wymuś pełne sprawdzenie Vulcana teraz")
    p.add_argument("--headed", action="store_true", help="uruchom przeglądarkę z oknem")
    p.add_argument("--debug", action="store_true", help="screenshoty + HTML każdego kroku do debug/, logi DEBUG")
    return p.parse_args(argv)


def selected_accounts(settings: Settings, args: argparse.Namespace) -> list[Account]:
    accounts = settings.require_accounts()
    wanted = [name.lower() for name in getattr(args, "account", None) or []]
    if not wanted:
        return accounts
    unknown = sorted(set(wanted) - {a.name for a in accounts})
    if unknown:
        raise ConfigError(f"Nieznane konto: {', '.join(unknown)} (skonfigurowane: {', '.join(a.name for a in accounts)})")
    return [a for a in accounts if a.name in wanted]


def migrate_legacy_state(settings: Settings, store: Store) -> None:
    """Stan z wersji jednokontowej przechodzi na pierwsze skonfigurowane konto."""
    first = settings.require_accounts()[0]
    store.adopt_legacy(first.name)
    if first.name == DEFAULT_ACCOUNT:
        return
    target = first.state_dir(settings)
    for name in ("session.json", "vulcan.json"):
        legacy, new = settings.state_dir / name, target / name
        if legacy.exists() and not new.exists():
            target.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy), str(new))
            log.info("Przeniesiono %s do %s (konto %s)", legacy, new, first.name)


def describe(n: Notification) -> str:
    fwd = (" | przekazane" + (", data przybliżona" if n.date_approximate else "")) if n.forwarded else ""
    return f"{n.sent_at:%Y-%m-%d %H:%M} | {n.sender.name} → {n.student or n.recipient.name} | {n.subject} | id={n.vulcan_id}{fwd}"


def _key(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def owns(mailboxes: list[str], n: Notification) -> bool:
    """Czy powiadomienie dotyczy którejś ze skrzynek konta („Rodzic - R - Uczeń - (Szkoła)”)."""
    return _key(n.recipient.raw) in {_key(m) for m in mailboxes}


def read_notifications(
    watcher: ImapWatcher, include_processed: bool = False, mark_ignored: bool = True, alerter: Alerter | None = None
) -> list[tuple[int, Notification]]:
    result = []
    for raw in watcher.fetch_new(include_processed=include_processed):
        msg = email.message_from_bytes(raw.raw, policy=email.policy.default)
        if not is_vulcan_notification(msg):
            log.info("UID %s: mail nie jest powiadomieniem o wiadomości (%r), pomijam", raw.uid, msg.get("Subject"))
            if mark_ignored and not include_processed:
                watcher.mark_ignored(raw.uid)
            continue
        try:
            result.append((raw.uid, parse_message(msg)))
        except NotificationParseError as exc:
            log.error("UID %s: nie udało się sparsować powiadomienia: %s", raw.uid, exc)
            if alerter:
                alerter.alert(
                    "parser",
                    "Nie udało się sparsować powiadomienia Vulcana — możliwa zmiana formatu maila",
                    f"Temat: {msg.get('Subject')}\nBłąd: {exc}\n\nWiadomość i tak zostanie złapana "
                    "przez okresową synchronizację. Powiadomienie zostaje w skrzynce z flagą.",
                )
            if mark_ignored and not include_processed:
                watcher.mark_ignored(raw.uid)
    return result


def fetch_fixtures(settings: Settings, count: int) -> None:
    out = settings.debug_dir / "eml"
    out.mkdir(parents=True, exist_ok=True)
    with ImapWatcher(settings) as watcher:
        for raw in watcher.fetch_new(include_processed=True, limit=count):
            path = out / f"uid_{raw.uid}.eml"
            path.write_bytes(raw.raw)
            log.info("Zapisano %s", path)


def probe_vulcan(settings: Settings, args: argparse.Namespace, count: int) -> None:
    with Store(settings.db_path) as store:
        migrate_legacy_state(settings, store)
    for account in selected_accounts(settings, args):
        log.info("=== Konto %s (%s) → %s", account.name, account.login, account.recipients)
        with VulcanClient(settings, account, headed=args.headed, debug=args.debug) as vulcan:
            for box in vulcan.mailboxes():
                log.info("Skrzynka: %s", box)
            messages = vulcan.list_received(max_pages=1)[:count]
            for m in messages:
                log.info("%s | %s | %s | %s | %s", m.sent_at.strftime("%Y-%m-%d %H:%M"), m.key, m.sender, m.subject,
                         m.mailbox)  # fmt: skip
            if messages:
                full = vulcan.get_message(messages[0].key, mailbox=messages[0].mailbox)
                log.info(
                    "Najnowsza: od %s do %s, %d znaków HTML, załączniki: %s",
                    full.sender,
                    full.recipients,
                    len(full.html),
                    [a.name for a in full.attachments] or "brak",
                )


@dataclass
class Run:
    """Przekazywanie wiadomości jednego konta; `sent_keys` jest wspólne dla całego przebiegu."""

    settings: Settings
    store: Store
    vulcan: VulcanClient
    account: Account
    dry_run: bool
    sent_keys: set[str] = field(default_factory=set)

    def already_handled(self, key: str) -> bool:
        return key in self.sent_keys or self.store.is_known(key)

    def forward(self, message: Message, notification: Notification | None = None) -> bool:
        """Wysyła wiadomość na adresy konta, jeśli jeszcze jej nie wysłano. Zwraca True, gdy wysłano."""
        if self.already_handled(message.key):
            log.info("Pomijam %s („%s”) — już przekazana lub oznaczona jako widziana", message.key, message.subject)
            return False
        link = f"{self.vulcan.messages_app_url}/odebrane"
        send(build_email(message, self.settings, self.account.recipients, link=link), self.settings, self.dry_run)
        self.sent_keys.add(message.key)
        if not self.dry_run:
            self.store.record(
                account=self.account.name,
                vulcan_key=message.key,
                vulcan_id=message.id,
                sent_at=message.sent_at,
                subject=message.subject,
                sender=message.sender,
                mailbox=message.mailbox,
                status=FORWARDED,
                notification_message_id=notification.email_message_id if notification else None,
            )
        log.info("%s[%s] Przekazano %s: „%s” od %s → %s", "[dry-run] " if self.dry_run else "", self.account.name,
                 message.key, message.subject, message.sender, self.account.recipients)  # fmt: skip
        return True

    def resolve(self, n: Notification, summaries: list[MessageSummary]) -> str | Message | None:
        """Wiadomość dla powiadomienia: Message do wysłania, klucz (str) gdy już obsłużona, None gdy brak dopasowania."""
        summary = match(n, summaries)
        if summary is not None:
            if self.already_handled(summary.key):
                return summary.key
            return self.vulcan.get_message(summary.key, mailbox=summary.mailbox)
        if n.vulcan_id:
            if self.already_handled(n.vulcan_id):
                return n.vulcan_id
            # Starsza niż pobrany fragment listy — spróbuj bezpośrednio po kluczu.
            try:
                return self.vulcan.get_message(n.vulcan_id, mailbox=n.recipient.raw)
            except VulcanError as exc:
                log.warning("Nie znaleziono wiadomości %s w Vulcanie: %s", n.vulcan_id, exc)
        return None


def full_check_due(last: datetime | None, now: datetime, interval_minutes: int) -> bool:
    """Czy minął czas na okresowe sprawdzenie Vulcana (niezależne od powiadomień)."""
    if interval_minutes <= 0:
        return False
    return last is None or now - last >= timedelta(minutes=interval_minutes)


@dataclass
class Pass:
    """Wspólny stan przebiegu przez wszystkie konta."""

    settings: Settings
    args: argparse.Namespace
    store: Store
    watcher: ImapWatcher
    now: datetime
    notifications: list[tuple[int, Notification]]
    sent_keys: set[str] = field(default_factory=set)
    owner: dict[int, str] = field(default_factory=dict)  # UID powiadomienia → konto, do którego należy
    done: set[int] = field(default_factory=set)  # UID powiadomień już przeniesionych do Processed
    synced: dict[str, int] = field(default_factory=dict)  # konto → ile wiadomości wysłała synchronizacja
    failed: set[str] = field(default_factory=set)

    def mark_processed(self, uid: int) -> None:
        if not self.args.dry_run:
            self.watcher.mark_processed(uid)
        self.done.add(uid)


def process_account(p: Pass, account: Account) -> None:
    """Powiadomienia konta + synchronizacja jego skrzynek."""
    dry_run, store, now = p.args.dry_run, p.store, p.now
    with VulcanClient(p.settings, account, headed=p.args.headed, debug=p.args.debug) as vulcan:
        mailboxes = vulcan.mailboxes()
        mine = [(uid, n) for uid, n in p.notifications if uid not in p.owner and owns(mailboxes, n)]
        for uid, _ in mine:
            p.owner[uid] = account.name
        run = Run(p.settings, store, vulcan, account, dry_run, p.sent_keys)

        # Synchronizacja od ostatniej znanej wiadomości konta (z zapasem). Bez --init
        # tylko okno wokół powiadomień, żeby nie wysłać całej historii.
        last_known = store.last_sent_at(account.name) if store.is_initialized(account.name) else None
        oldest_notification = min((n.sent_at for _, n in mine), default=now)
        sync_since = (last_known - SYNC_OVERLAP) if last_known else (oldest_notification - LIST_WINDOW)
        summaries = vulcan.list_received(stop_before=min(sync_since, oldest_notification - LIST_WINDOW))

        # 1. Powiadomienia konta: dopasuj i przekaż (także te spoza okna synchronizacji).
        unmatched = []
        for uid, n in mine:
            resolved = run.resolve(n, summaries)
            if resolved is None:
                log.warning("UID %s: brak jednoznacznego dopasowania w koncie %s", uid, account.name)
                unmatched.append(uid)
                continue
            if isinstance(resolved, str):
                log.info("UID %s: wiadomość %s już obsłużona", uid, resolved)
            else:
                run.forward(resolved, n)
            p.mark_processed(uid)

        # 2. Synchronizacja: wszystko nowsze niż ostatnia znana wiadomość konta, czego jeszcze nie wysłano.
        #    Bez --init tylko jako fallback dla niedopasowanych powiadomień.
        pending = sorted(
            (m for m in summaries if m.sent_at >= sync_since and not run.already_handled(m.key)),
            key=lambda m: m.sent_at,
        )
        if last_known is None and not unmatched:
            pending = []
        if pending:
            log.info("[%s] Synchronizacja: %d nieprzekazanych wiadomości od %s", account.name, len(pending),
                     sync_since.isoformat())  # fmt: skip
        p.synced[account.name] = sum(run.forward(vulcan.get_message(m.key, mailbox=m.mailbox)) for m in pending)
        if not dry_run and last_known is not None:
            store.set_last_full_check(account.name, now)


def run_once(settings: Settings, args: argparse.Namespace, alerter: Alerter | None = None) -> bool:
    """Jeden przebieg przez wszystkie konta. Zwraca False, jeśli któreś konto zakończyło się błędem.

    Vulcan danego konta jest sprawdzany, gdy w skrzynce IMAP jest powiadomienie dla niego (wg zapamiętanych
    skrzynek; nieznany adresat → wszystkie konta) albo minął FULL_CHECK_INTERVAL_MINUTES. Za każdym razem
    przekazywane są wszystkie wiadomości konta nowsze niż ostatnia znana, których jeszcze nie wysłano —
    na adresy z jego MAIL_TO.
    """
    accounts = selected_accounts(settings, args)
    now = datetime.now(timezone.utc)
    with Store(settings.db_path) as store, ImapWatcher(settings) as watcher:
        migrate_legacy_state(settings, store)
        notifications = read_notifications(watcher, mark_ignored=not args.dry_run, alerter=alerter)
        for uid, n in notifications:
            log.info("UID %s: %s", uid, describe(n))

        # Właściciel powiadomienia wg skrzynek zapamiętanych przy poprzednich połączeniach.
        cached_owner: dict[int, str | None] = {}
        for uid, n in notifications:
            cached_owner[uid] = next((a.name for a in accounts if owns(known_mailboxes(settings, a), n)), None)

        to_check: list[Account] = []
        for account in accounts:
            initialized = store.is_initialized(account.name)
            due = initialized and (
                args.sync or full_check_due(store.last_full_check(account.name), now, settings.full_check_interval_minutes)
            )
            notified = any(owner in (account.name, None) for owner in cached_owner.values())
            if not initialized and not notified:
                log.warning("[%s] Nie uruchomiono --init — okresowe sprawdzanie wyłączone, działają tylko powiadomienia",
                            account.name)  # fmt: skip
            if due or notified:
                to_check.append(account)
        if not to_check:
            log.info("Brak nowych powiadomień; następne pełne sprawdzenie za mniej niż %d min",
                     settings.full_check_interval_minutes)  # fmt: skip
            return True

        p = Pass(settings, args, store, watcher, now, notifications)
        checked: set[str] = set()
        while to_check:
            for account in to_check:
                checked.add(account.name)
                log.info("[%s] Sprawdzam Vulcana", account.name)
                try:
                    process_account(p, account)
                except Exception as exc:  # noqa: BLE001 - błąd jednego konta nie blokuje pozostałych
                    p.failed.add(account.name)
                    report_failure(alerter, exc, account=account.name)
            # Powiadomienia, których nikt nie przygarnął (np. nieaktualna lista skrzynek) — sprawdź resztę kont.
            unclaimed = [uid for uid, _ in notifications if uid not in p.owner]
            to_check = [a for a in accounts if a.name not in checked] if unclaimed else []

        # Niedopasowane powiadomienia: po synchronizacji pewnie są już obsłużone.
        for uid, n in notifications:
            if uid in p.done:
                continue
            owner = p.owner.get(uid)
            if owner in p.failed or (owner is None and p.failed):
                log.info("UID %s: zostawiam w skrzynce (błąd konta), spróbuję ponownie", uid)
                continue
            handled = n.vulcan_id is not None and (n.vulcan_id in p.sent_keys or store.is_known(n.vulcan_id))
            synced = p.synced.get(owner, 0) if owner else sum(p.synced.values())
            if handled or synced or now - n.sent_at > UNMATCHED_GIVE_UP_AFTER:
                if not (handled or synced):
                    who = f"konto {owner}" if owner else "żadne skonfigurowane konto nie ma tej skrzynki"
                    log.warning("UID %s: poddaję się po %s bez dopasowania (%s)", uid, UNMATCHED_GIVE_UP_AFTER, who)
                    if alerter:
                        alerter.alert("dopasowanie", f"Nie znaleziono w Vulcanie wiadomości z powiadomienia: {n.subject}",
                                      f"{describe(n)}\nAdresat: {n.recipient.raw}\n{who}")  # fmt: skip
                p.mark_processed(uid)
            else:
                log.info("UID %s: zostawiam w skrzynce do kolejnej próby", uid)

        suffix = " (dry-run, nic nie zapisano)" if args.dry_run else ""
        log.info("Przebieg zakończony: przekazano %d wiadomości%s", len(p.sent_keys), suffix)
        return not p.failed


def init_store(settings: Settings, args: argparse.Namespace, alerter: Alerter | None = None) -> bool:
    """Pierwsze uruchomienie (lub nowe konto): zapisuje bieżące wiadomości jako widziane, nic nie wysyła."""
    ok = True
    with Store(settings.db_path) as store:
        migrate_legacy_state(settings, store)
        for account in selected_accounts(settings, args):
            try:
                with VulcanClient(settings, account, headed=args.headed, debug=args.debug) as vulcan:
                    vulcan.mailboxes()  # zapamiętuje skrzynki do przypisywania powiadomień
                    summaries = vulcan.list_received(max_pages=INIT_PAGES)
            except Exception as exc:  # noqa: BLE001 - błąd jednego konta nie blokuje pozostałych
                report_failure(alerter, exc, account=account.name)
                ok = False
                continue
            added = 0
            for s in summaries:
                added += store.record(
                    account=account.name,
                    vulcan_key=s.key,
                    vulcan_id=s.id,
                    sent_at=s.sent_at,
                    subject=s.subject,
                    sender=s.sender,
                    mailbox=s.mailbox,
                    status=SEEN,
                )
            store.mark_initialized(account.name)
            log.info("[%s] --init: oznaczono %d wiadomości jako widziane (pobrano %d)", account.name, added,
                     len(summaries))  # fmt: skip
    return ok


def classify(exc: BaseException) -> tuple[str, str, Path | None]:
    """(typ alertu, opis, zrzut ekranu) dla błędu przebiegu. Typ służy też do throttlingu alertów."""
    if isinstance(exc, VulcanLoginError):
        return "vulcan-logowanie", f"Logowanie do eduVULCAN nie powiodło się: {exc}", exc.screenshot
    if isinstance(exc, VulcanError):
        return "vulcan", f"Błąd Vulcana: {exc}", exc.screenshot
    if isinstance(exc, PlaywrightError):
        return "vulcan", f"Błąd przeglądarki (Playwright): {str(exc).splitlines()[0]}", None
    if isinstance(exc, smtplib.SMTPException):
        return "smtp", f"Błąd SMTP: {exc}", None
    if isinstance(exc, imaplib.IMAP4.error):
        return "imap", f"Błąd IMAP: {exc}", None
    if isinstance(exc, OSError):
        return "siec", f"Błąd sieci: {type(exc).__name__}: {exc}", None
    return "nieoczekiwany", f"Nieoczekiwany błąd: {type(exc).__name__}: {exc}", None


def report_failure(alerter: Alerter | None, exc: BaseException, account: str | None = None) -> None:
    """Loguje błąd i wysyła alert; błędy konta mają osobny throttling (typ „vulcan:aga”)."""
    if isinstance(exc, ConfigError):
        raise exc
    kind, summary, screenshot = classify(exc)
    if account:
        kind, summary = f"{kind}:{account}", f"[konto {account}] {summary}"
    if kind.startswith("nieoczekiwany"):
        log.error("%s", summary, exc_info=exc)
    else:
        log.error("%s%s", summary, f" (zrzut: {screenshot})" if screenshot else "")
    if alerter:
        alerter.alert(kind, summary, "".join(traceback.format_exception(exc, limit=-8)), screenshot)


def guarded(settings: Settings, args: argparse.Namespace, alerter: Alerter, action) -> bool:
    """Uruchamia akcję pod blokadą pliku; błąd loguje i zgłasza alertem. Zwraca True przy sukcesie."""
    lock = FileLock(settings.state_dir / "forwarder.lock")
    if not lock.acquire():
        log.warning("Inny przebieg jeszcze trwa (blokada %s) — pomijam", lock.path)
        return True
    try:
        return action(settings, args, alerter) is not False
    except ConfigError:
        raise
    except Exception as exc:
        report_failure(alerter, exc)
        return False
    finally:
        lock.release()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.debug)
    try:
        settings = Settings.load()
        secrets = install_secret_filter(settings)
        alerter = Alerter(settings, dry_run=args.dry_run, redact=secrets.redact)
        if args.fetch_fixtures:
            fetch_fixtures(settings, args.fetch_fixtures)
        elif args.probe is not None:
            probe_vulcan(settings, args, args.probe)
        elif args.init:
            return 0 if guarded(settings, args, alerter, init_store) else 1
        elif args.once:
            return 0 if guarded(settings, args, alerter, run_once) else 1
        else:
            interval = settings.poll_interval_minutes * 60
            names = ", ".join(a.name for a in selected_accounts(settings, args))
            log.info("Tryb pętli: konta %s; co %d min, pełne sprawdzenie co %d min", names,
                     settings.poll_interval_minutes, settings.full_check_interval_minutes)  # fmt: skip
            while True:
                guarded(settings, args, alerter, run_once)
                args.sync = False  # --sync dotyczy tylko pierwszego przebiegu
                time.sleep(interval)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2
    except VulcanError as exc:
        log.error("Vulcan: %s%s", exc, f" (zrzut: {exc.screenshot})" if exc.screenshot else "")
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
