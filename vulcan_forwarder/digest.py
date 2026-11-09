"""Oceny, sprawdziany i zadania domowe z dziennika ucznia: sobotnie podsumowanie tygodnia i pilne
powiadomienia o nowych wpisach w kalendarzu.

- Sobota, WEEKLY_DIGEST_HOUR: jeden mail na konto (wszyscy uczniowie konta) — oceny, które pojawiły się
  od poprzedniego podsumowania, i nowe sprawdziany/kartkówki/zadania domowe, o których jeszcze nie było
  powiadomienia (z terminem od dziś).
- Niedziela–czwartek, DAILY_CHECK_HOUR: sprawdzenie kalendarza. Nowy wpis z terminem przed poniedziałkiem
  po najbliższej sobocie (sobotnie podsumowanie byłoby spóźnione) idzie od razu, w jednym mailu na konto.
  Pozostałe nowe wpisy czekają na podsumowanie.

API nie podaje daty dodania wpisu, więc „nowe” = nieznane bazie (tabela diary_items). Przy pierwszym
sprawdzeniu ucznia wszystko, co już jest w dzienniku, zapisujemy jako zastane: oceny liczą się wg daty
wystawienia, a sprawdziany i zadania trafią do najbliższego podsumowania (bez pilnych maili).
"""

from __future__ import annotations

import html
import logging
import re
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from pathlib import Path
from typing import Any

from vulcan_forwarder.config import Account, Settings
from vulcan_forwarder.diary import (
    WARSAW,
    CalendarItem,
    Grade,
    Student,
    daily_due,
    format_day,
    local_today,
    urgent_until,
    weekly_due,
)
from vulcan_forwarder.mailer import send
from vulcan_forwarder.store import DiaryItem, Store
from vulcan_forwarder.vulcan_client import VulcanClient, VulcanError

log = logging.getLogger(__name__)

WEEKLY, DAILY = "weekly", "daily"
# Jak daleko w przód pobieramy kalendarz (dalsze wpisy wpadną jako nowe, gdy okno się przesunie).
CALENDAR_DAYS_AHEAD = 35
# Po nieudanej próbie (np. błąd Vulcana) kolejna najwcześniej po tylu minutach, nie co przebieg pętli.
RETRY_AFTER = timedelta(minutes=30)

LAST_WEEKLY = "diary_last_weekly"
LAST_DAILY = "diary_last_daily"
LAST_ATTEMPT = "diary_last_attempt"
BASELINE = "diary_baseline"


@dataclass
class Snapshot:
    """To, co jest teraz w dzienniku ucznia konta."""

    students: list[Student]
    grades: list[Grade]
    calendar: list[CalendarItem]
    links: dict[str, tuple[str, str]]  # klucz ucznia → (oceny, sprawdziany i zadania)


def _meta_dt(store: Store, key: str) -> datetime | None:
    value = store.get_meta(key)
    return datetime.fromisoformat(value) if value else None


def _set_meta_dt(store: Store, key: str, when: datetime) -> None:
    store.set_meta(key, when.astimezone(timezone.utc).isoformat(timespec="seconds"))


def due_mode(store: Store, settings: Settings, account: str, now: datetime) -> str | None:
    """Co należy się teraz kontu: podsumowanie tygodnia, sprawdzenie dzienne albo nic."""
    if not settings.diary_enabled:
        return None
    attempt = _meta_dt(store, f"{LAST_ATTEMPT}:{account}")
    if attempt is not None and now - attempt < RETRY_AFTER:
        return None
    if weekly_due(now, settings.weekly_digest_hour, _meta_dt(store, f"{LAST_WEEKLY}:{account}")):
        return WEEKLY
    if daily_due(now, settings.daily_check_hour, _meta_dt(store, f"{LAST_DAILY}:{account}")):
        return DAILY
    return None


# --- pobranie i synchronizacja ----------------------------------------------------------------------


def fetch(vulcan: VulcanClient, today: date) -> Snapshot:
    vulcan.enter_diary()
    students = vulcan.students()
    grades: list[Grade] = []
    calendar: list[CalendarItem] = []
    links = {}
    for student in students:
        grades.extend(vulcan.grades(student, today))
        calendar.extend(vulcan.calendar(student, today, today + timedelta(days=CALENDAR_DAYS_AHEAD)))
        links[student.key] = (vulcan.diary_url(student.key, "oceny"), vulcan.diary_url(student.key, "sprawdzianyZadaniaDomowe"))
    log.info("Dziennik: %d uczniów, %d ocen, %d sprawdzianów/zadań w kalendarzu", len(students), len(grades), len(calendar))
    return Snapshot(students, grades, calendar, links)


def sync(store: Store, account: str, snapshot: Snapshot, details: Callable[[CalendarItem], dict[str, Any]],
         now: datetime) -> None:  # fmt: skip
    """Zapisuje w bazie nowe oceny i wpisy kalendarza; dla nowych wpisów pobiera opis (`details`)."""
    names = {s.key: s.label for s in snapshot.students}
    for student in snapshot.students:
        baseline = store.get_meta(f"{BASELINE}:{account}:{student.key}") is None
        added = 0
        for g in (g for g in snapshot.grades if g.student == student.key):
            if store.diary_item(account, g.key) is not None:
                continue
            # Zastane oceny liczymy wg daty wystawienia, żeby pierwsze podsumowanie miało oceny z tygodnia.
            first_seen = datetime.combine(g.date, time(12), WARSAW) if baseline and g.date else now
            store.add_diary_item(account, g.key, "grade", student.key, g.date,
                                 {**g.to_dict(), "student_name": names[student.key]}, min(first_seen, now), baseline)  # fmt: skip
            added += 1
        for item in (i for i in snapshot.calendar if i.student == student.key):
            existing = store.diary_item(account, item.key)
            if existing is None:
                store.add_diary_item(account, item.key, item.kind, student.key, item.date,
                                     _calendar_data(item, details, names[student.key]), now, baseline)  # fmt: skip
                added += 1
            elif existing.date != item.date:
                prev = existing.date if existing.notified_at else existing.prev_date
                log.info("[%s] Zmiana terminu: %s %s %s → %s", account, item.type_name, item.subject,
                         existing.date, item.date)  # fmt: skip
                store.reschedule_diary_item(account, item.key, item.date, prev)
        if baseline:
            store.set_meta(f"{BASELINE}:{account}:{student.key}", now.isoformat(timespec="seconds"))
            log.info("[%s] Dziennik %s: pierwsze sprawdzenie, zapisano %d zastanych wpisów", account, student.label, added)
        elif added:
            log.info("[%s] Dziennik %s: %d nowych wpisów", account, student.label, added)


def _calendar_data(item: CalendarItem, details: Callable[[CalendarItem], dict[str, Any]], student_name: str) -> dict:
    try:
        info = details(item) or {}
    except VulcanError as exc:
        log.warning("Nie udało się pobrać szczegółów %s %s: %s", item.type_name, item.id, exc)
        info = {}
    return {
        "type": item.type,
        "type_name": item.type_name,
        "subject": item.subject,
        "description": (info.get("opis") or "").strip(),
        "teacher": (info.get("nauczycielImieNazwisko") or "").strip(),
        "has_attachment": item.has_attachment or bool(info.get("zalaczniki")),
        "student_name": student_name,
    }


# --- przebieg -------------------------------------------------------------------------------------


def run_diary(
    settings: Settings,
    accounts: list[Account],
    *,
    dry_run: bool = False,
    force: str | None = None,
    headed: bool = False,
    debug: bool = False,
    on_error: Callable[[BaseException, str], None] | None = None,
    now: datetime | None = None,
) -> bool:
    """Podsumowanie tygodnia / sprawdzenie dzienne dla kont, którym się teraz należy (albo `force`).
    W --dry-run działa na kopii bazy (nic nie zapisuje) i wypisuje maile na stdout. Zwraca False przy błędzie."""
    now = now or datetime.now(timezone.utc)
    if not settings.diary_enabled and force is None:
        return True
    with _store(settings, dry_run) as store:
        ok = True
        for account in accounts:
            mode = force or due_mode(store, settings, account.name, now)
            if mode is None:
                continue
            log.info("[%s] Dziennik: %s", account.name, "podsumowanie tygodnia" if mode == WEEKLY else "sprawdzenie dzienne")
            _set_meta_dt(store, f"{LAST_ATTEMPT}:{account.name}", now)
            try:
                process_account(store, settings, account, mode, now, dry_run=dry_run, headed=headed, debug=debug)
            except Exception as exc:  # noqa: BLE001 - błąd jednego konta nie blokuje pozostałych
                ok = False
                if on_error is None:
                    raise
                on_error(exc, account.name)
        return ok


class _store:
    """Baza z ustawień albo — w --dry-run — jej tymczasowa kopia."""

    def __init__(self, settings: Settings, dry_run: bool):
        self.settings, self.dry_run = settings, dry_run
        self.tmp: tempfile.TemporaryDirectory | None = None

    def __enter__(self) -> Store:
        path = self.settings.db_path
        if self.dry_run:
            self.tmp = tempfile.TemporaryDirectory(prefix="vulcan-dry-run-")
            copy = Path(self.tmp.name) / "db.sqlite"
            if path.exists():
                shutil.copy(path, copy)
            path = copy
        self.store = Store(path)
        return self.store

    def __exit__(self, *exc) -> None:
        self.store.close()
        if self.tmp is not None:
            self.tmp.cleanup()


def process_account(store: Store, settings: Settings, account: Account, mode: str, now: datetime, *,
                    dry_run: bool = False, headed: bool = False, debug: bool = False) -> None:  # fmt: skip
    today = local_today(now)
    with VulcanClient(settings, account, headed=headed, debug=debug) as vulcan:
        snapshot = fetch(vulcan, today)
        sync(store, account.name, snapshot, vulcan.calendar_details, now)
    name = account.name
    pending = store.pending_calendar_items(name, today)

    if mode == WEEKLY:
        last = _meta_dt(store, f"{LAST_WEEKLY}:{name}")
        since = last or now - timedelta(days=7)
        grades = store.grades_seen_since(name, since)
        msg = build_weekly_email(settings, account, snapshot, grades, pending, since, now)
        send(msg, settings, dry_run)
        store.mark_diary_notified(name, [i.key for i in grades + pending], now)
        _set_meta_dt(store, f"{LAST_WEEKLY}:{name}", now)
        log.info("%s[%s] Wysłano podsumowanie tygodnia: %d ocen, %d sprawdzianów/zadań", _dry(dry_run), name,
                 len(grades), len(pending))  # fmt: skip
        return

    horizon = urgent_until(today)
    urgent = [i for i in pending if not i.baseline and i.date is not None and i.date < horizon]
    if urgent:
        send(build_urgent_email(settings, account, snapshot, urgent, now), settings, dry_run)
        store.mark_diary_notified(name, [i.key for i in urgent], now)
        log.info("%s[%s] Wysłano powiadomienie o %d pilnych wpisach w kalendarzu", _dry(dry_run), name, len(urgent))
    else:
        later = sum(1 for i in pending if not i.baseline)
        log.info("[%s] Brak pilnych nowych wpisów w kalendarzu%s", name,
                 f" ({later} nowych poczeka na sobotnie podsumowanie)" if later else "")  # fmt: skip
    _set_meta_dt(store, f"{LAST_DAILY}:{name}", now)


def _dry(dry_run: bool) -> str:
    return "[dry-run] " if dry_run else ""


# --- maile ----------------------------------------------------------------------------------------

e = html.escape
STYLE_TABLE = "border-collapse:collapse;font-size:13px;margin:4px 0 12px;width:100%"
STYLE_TH = "text-align:left;padding:4px 8px;border-bottom:2px solid #ddd;color:#555;font-weight:normal"
STYLE_TD = "padding:4px 8px;border-bottom:1px solid #eee;vertical-align:top"


def _entries(n: int) -> str:
    """Odmiana: 1 wpis, 2–4 wpisy, 5+ wpisów (12–14 wpisów)."""
    if n == 1:
        return "wpis"
    return "wpisy" if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else "wpisów"


def _teacher(name: str) -> str:
    """„Puto Justyna [JU]” → „Puto Justyna”."""
    return re.sub(r"\s*\[[^\]]*\]\s*$", "", name).strip()


def _grade_desc(d: dict) -> str:
    parts = [p for p in (d.get("category"), d.get("column")) if p]
    desc = " — ".join(dict.fromkeys(parts))
    weight = d.get("weight")
    if weight not in (None, 0, 0.0, 1, 1.0):
        desc += f" (waga {weight:g})"
    if d.get("corrected"):
        desc += " (poprawa)"
    return desc


def _calendar_term(i: DiaryItem) -> str:
    term = format_day(i.date) if i.date else "?"
    if i.prev_date:
        term += f" (zmiana terminu, było {format_day(i.prev_date)})"
    return term


def _calendar_desc(d: dict) -> str:
    desc = d.get("description") or ""
    if d.get("has_attachment"):
        desc += (" " if desc else "") + "[załącznik w dzienniku]"
    return desc


def _grade_rows(grades: list[DiaryItem]) -> tuple[list[str], list[str]]:
    text, rows = [], []
    for g in sorted(grades, key=lambda g: (g.data.get("subject") or "", g.date or date.min)):
        d = g.data
        when = f"{g.date:%d.%m}" if g.date else ""
        desc, teacher = _grade_desc(d), _teacher(d.get("teacher") or "")
        text.append(f"  {d.get('subject')}: {d.get('value')}  {desc}  [{when}{', ' + teacher if teacher else ''}]")
        rows.append(f'<tr><td style="{STYLE_TD}">{e(d.get("subject") or "")}</td>'
                    f'<td style="{STYLE_TD};font-weight:bold;font-size:15px">{e(d.get("value") or "")}</td>'
                    f'<td style="{STYLE_TD}">{e(desc)}</td><td style="{STYLE_TD};white-space:nowrap">{e(when)}</td>'
                    f'<td style="{STYLE_TD};color:#666">{e(teacher)}</td></tr>')  # fmt: skip
    return text, rows


def _calendar_rows(items: list[DiaryItem]) -> tuple[list[str], list[str]]:
    text, rows = [], []
    for i in sorted(items, key=lambda i: (i.date or date.max, i.data.get("subject") or "")):
        d = i.data
        term, desc, teacher = _calendar_term(i), _calendar_desc(d), _teacher(d.get("teacher") or "")
        text.append(f"  {term}: {d.get('type_name')} — {d.get('subject')}" + (f": {desc}" if desc else "")
                    + (f" [{teacher}]" if teacher else ""))  # fmt: skip
        color = "#b00" if d.get("type") in (1, 3) else "#222"
        rows.append(f'<tr><td style="{STYLE_TD};white-space:nowrap;font-weight:bold">{e(term)}</td>'
                    f'<td style="{STYLE_TD};color:{color}">{e(d.get("type_name") or "")}</td>'
                    f'<td style="{STYLE_TD}">{e(d.get("subject") or "")}</td><td style="{STYLE_TD}">{e(desc)}</td>'
                    f'<td style="{STYLE_TD};color:#666">{e(teacher)}</td></tr>')  # fmt: skip
    return text, rows


def _table(headers: list[str], rows: list[str]) -> str:
    head = "".join(f'<th style="{STYLE_TH}">{e(h)}</th>' for h in headers)
    return f'<table style="{STYLE_TABLE}"><tr>{head}</tr>{"".join(rows)}</table>'


def _students_in_order(snapshot: Snapshot, items: list[DiaryItem]) -> list[tuple[str, str]]:
    """(klucz, etykieta) uczniów: najpierw kolejność z dziennika, potem ewentualni nieobecni w nim."""
    result = [(s.key, s.label) for s in snapshot.students]
    known = {k for k, _ in result}
    for i in items:
        if i.student not in known:
            known.add(i.student)
            result.append((i.student, i.data.get("student_name") or i.student))
    return result


def _new_email(settings: Settings, account: Account, subject: str, now: datetime, kind: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.mail_from
    msg["To"] = account.recipients
    msg["Date"] = format_datetime(now)
    msg["Message-ID"] = make_msgid(idstring=f"{kind}-{account.name}", domain="vulcan-forwarder.local")
    return msg


def _wrap(parts: list[str]) -> str:
    return ('<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#222;max-width:900px">'
            + "".join(parts) + "</div>")  # fmt: skip


def build_weekly_email(settings: Settings, account: Account, snapshot: Snapshot, grades: list[DiaryItem],
                       calendar: list[DiaryItem], since: datetime, now: datetime) -> EmailMessage:  # fmt: skip
    """Jeden mail na konto: dla każdego ucznia nowe oceny i nowe sprawdziany/zadania."""
    period = f"{since.astimezone(WARSAW):%d.%m}–{now.astimezone(WARSAW):%d.%m.%Y}"
    msg = _new_email(settings, account, f"[Vulcan] Podsumowanie tygodnia {period}", now, "weekly")
    text = [f"Podsumowanie tygodnia {period}", ""]
    parts = [f'<h2 style="font-size:18px;margin:0 0 12px">Podsumowanie tygodnia {e(period)}</h2>']
    for key, label in _students_in_order(snapshot, grades + calendar):
        g = [i for i in grades if i.student == key]
        c = [i for i in calendar if i.student == key]
        grades_link, calendar_link = snapshot.links.get(key, (None, None))
        text += [f"=== {label} ===", "", f"Nowe oceny ({len(g)}):"]
        parts.append(f'<h3 style="font-size:16px;margin:20px 0 4px;border-bottom:1px solid #ccc">{e(label)}</h3>')
        parts.append(f'<h4 style="font-size:14px;margin:8px 0 2px">Nowe oceny ({len(g)})</h4>')
        if g:
            t, rows = _grade_rows(g)
            text += t
            parts.append(_table(["Przedmiot", "Ocena", "Za co", "Data", "Nauczyciel"], rows))
        else:
            text.append("  brak")
            parts.append('<p style="color:#666;margin:2px 0 8px">Brak nowych ocen.</p>')
        text += ["", f"Nowe sprawdziany, kartkówki i zadania domowe ({len(c)}):"]
        parts.append(f'<h4 style="font-size:14px;margin:8px 0 2px">Nowe sprawdziany, kartkówki i zadania domowe ({len(c)})</h4>')
        if c:
            t, rows = _calendar_rows(c)
            text += t
            parts.append(_table(["Termin", "Rodzaj", "Przedmiot", "Opis", "Nauczyciel"], rows))
        else:
            text.append("  brak")
            parts.append('<p style="color:#666;margin:2px 0 8px">Brak nowych wpisów.</p>')
        links = [(n, u) for n, u in (("oceny", grades_link), ("sprawdziany i zadania", calendar_link)) if u]
        if links:
            text += [""] + [f"Dziennik — {n}: {u}" for n, u in links]
            parts.append('<p style="font-size:12px;color:#666">Dziennik: '
                         + " · ".join(f'<a href="{e(u, quote=True)}">{e(n)}</a>' for n, u in links) + "</p>")  # fmt: skip
        text.append("")
    msg.set_content("\n".join(text))
    msg.add_alternative(_wrap(parts), subtype="html")
    return msg


def build_urgent_email(settings: Settings, account: Account, snapshot: Snapshot, items: list[DiaryItem],
                       now: datetime) -> EmailMessage:  # fmt: skip
    """Nowe wpisy kalendarza z terminem przed najbliższym podsumowaniem — jeden mail na konto."""
    if len(items) == 1:
        i = items[0]
        who = (i.data.get("student_name") or "").split(" ")[0]
        subject = f"[Vulcan] {i.data.get('type_name')}: {i.data.get('subject')} — {_calendar_term(i)}" + (f" ({who})" if who else "")
    else:
        subject = f"[Vulcan] Nowe w kalendarzu na ten tydzień: {len(items)} {_entries(len(items))}"
    msg = _new_email(settings, account, subject, now, "urgent")
    text = ["Nowe sprawdziany / zadania domowe z terminem w tym tygodniu:", ""]
    parts = ['<h2 style="font-size:18px;margin:0 0 12px">Nowe w kalendarzu na ten tydzień</h2>']
    for key, label in _students_in_order(snapshot, items):
        mine = [i for i in items if i.student == key]
        if not mine:
            continue
        t, rows = _calendar_rows(mine)
        text += [f"=== {label} ===", *t, ""]
        parts.append(f'<h3 style="font-size:16px;margin:16px 0 4px">{e(label)}</h3>')
        parts.append(_table(["Termin", "Rodzaj", "Przedmiot", "Opis", "Nauczyciel"], rows))
        link = snapshot.links.get(key, (None, None))[1]
        if link:
            text.append(f"Dziennik: {link}")
            parts.append(f'<p style="font-size:12px;color:#666"><a href="{e(link, quote=True)}">Sprawdziany i zadania w dzienniku</a></p>')
    msg.set_content("\n".join(text))
    msg.add_alternative(_wrap(parts), subtype="html")
    return msg
