"""Dziennik ucznia (uczen.eduvulcan.pl): oceny, sprawdziany i zadania domowe — modele, parsowanie API
i harmonogram powiadomień.

API (zbadane 2026-11, wszystko GET z parametrem `key` ucznia):
  - api/Context → {"uczniowie": [{key, uczen, oddzial, idDziennik, ...}]} — wszyscy uczniowie konta;
  - api/OkresyKlasyfikacyjne?key=&idDziennik= → [{id, numerOkresu, dataOd, dataDo}];
  - api/Oceny?key=&idOkresKlasyfikacyjny= → {"ocenyPrzedmioty": [{przedmiotNazwa,
    kolumnyOcenyCzastkowe: [{oceny: [{wpis, dataOceny "dd.mm.rrrr", kategoriaKolumny, nazwaKolumny,
    waga, nauczyciel, idOcenaPoprawiona}]}]}]} — oceny nie mają własnego identyfikatora;
  - api/SprawdzianyZadaniaDomowe?key=&dataOd=&dataDo= → [{typ, przedmiotNazwa, data, hasAttachment, id}],
    typ: 1 sprawdzian, 2 kartkówka, 3 praca klasowa (np. diagnoza), 4 zadanie domowe;
  - api/SprawdzianSzczegoly?key=&id= (typ 1–3) i api/ZadanieDomoweSzczegoly?key=&id= (typ 4) →
    {opis, nauczycielImieNazwisko, ...}. Identyfikatory sprawdzianów i zadań to osobne przestrzenie.

Żadne z tych API nie podaje, kiedy wpis dodano, więc „nowe” = nieobecne w bazie przy poprzednim
sprawdzeniu (patrz digest.py).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

WARSAW = ZoneInfo("Europe/Warsaw")

HOMEWORK_TYPE = 4
CALENDAR_TYPES = {1: "Sprawdzian", 2: "Kartkówka", 3: "Praca klasowa", HOMEWORK_TYPE: "Zadanie domowe"}

SATURDAY = 5
# Dni (date.weekday(): pon=0 … nd=6), w które o DAILY_CHECK_HOUR sprawdzamy kalendarz: niedziela–czwartek.
DAILY_CHECK_DAYS = frozenset({6, 0, 1, 2, 3})
# Podsumowanie, którego nie udało się wysłać w sobotę (np. serwer wyłączony), dosyłamy najwyżej tyle później.
WEEKLY_CATCH_UP = timedelta(days=2)

WEEKDAYS_SHORT = ("pon", "wt", "śr", "czw", "pt", "sob", "nd")


@dataclass(frozen=True)
class Student:
    key: str  # identyfikator ucznia w URL-ach i parametrach API
    name: str  # „Alicja Kowalska”
    class_name: str  # oddział, np. „4f”
    diary_id: int  # idDziennik

    @classmethod
    def from_api(cls, item: dict[str, Any]) -> Student:
        return cls(key=item["key"], name=(item.get("uczen") or "").strip(), class_name=item.get("oddzial") or "",
                   diary_id=int(item["idDziennik"]))  # fmt: skip

    @property
    def label(self) -> str:
        return f"{self.name} ({self.class_name})" if self.class_name else self.name


@dataclass(frozen=True)
class Period:
    id: int
    number: int
    start: date
    end: date

    @classmethod
    def from_api(cls, item: dict[str, Any]) -> Period:
        return cls(id=int(item["id"]), number=int(item.get("numerOkresu") or 0),
                   start=datetime.fromisoformat(item["dataOd"]).date(),
                   end=datetime.fromisoformat(item["dataDo"]).date())  # fmt: skip


@dataclass(frozen=True)
class Grade:
    key: str  # złożony z ucznia, przedmiotu, kolumny, daty i wpisu (API nie ma ID oceny)
    student: str  # klucz ucznia
    subject: str
    value: str  # „5+”, „bz”, „A A B”
    date: date | None
    category: str
    column: str
    weight: float | None
    teacher: str
    corrected: bool  # ocena poprawiona (idOcenaPoprawiona)

    def to_dict(self) -> dict[str, Any]:
        return {"subject": self.subject, "value": self.value, "date": self.date.isoformat() if self.date else None,
                "category": self.category, "column": self.column, "weight": self.weight, "teacher": self.teacher,
                "corrected": self.corrected}  # fmt: skip


@dataclass(frozen=True)
class CalendarItem:
    """Sprawdzian, kartkówka, praca klasowa albo zadanie domowe (lista bez szczegółów)."""

    id: int
    type: int
    student: str  # klucz ucznia
    subject: str
    date: date
    has_attachment: bool

    @classmethod
    def from_api(cls, item: dict[str, Any], student: str) -> CalendarItem:
        return cls(id=int(item["id"]), type=int(item.get("typ") or 0), student=student,
                   subject=(item.get("przedmiotNazwa") or "").strip(),
                   date=datetime.fromisoformat(item["data"]).date(), has_attachment=bool(item.get("hasAttachment")))  # fmt: skip

    @property
    def is_homework(self) -> bool:
        return self.type == HOMEWORK_TYPE

    @property
    def kind(self) -> str:
        return "homework" if self.is_homework else "test"

    @property
    def key(self) -> str:
        return f"{self.kind}|{self.student}|{self.id}"

    @property
    def type_name(self) -> str:
        return CALENDAR_TYPES.get(self.type, f"Wpis (typ {self.type})")


def parse_grades(data: dict[str, Any], student: str) -> list[Grade]:
    """Oceny cząstkowe z api/Oceny. Identyczne wpisy w tej samej kolumnie i dniu dostają kolejne numery."""
    grades: list[Grade] = []
    seen: Counter[str] = Counter()
    for subject in data.get("ocenyPrzedmioty") or []:
        subject_name = (subject.get("przedmiotNazwa") or "").strip()
        for column in subject.get("kolumnyOcenyCzastkowe") or []:
            for g in column.get("oceny") or []:
                value = (g.get("wpis") or "").strip()
                if not value:
                    continue
                raw_date = (g.get("dataOceny") or "").strip()
                base = f"grade|{student}|{subject_name}|{g.get('idKolumny', column.get('idKolumny'))}|{raw_date}|{value}"
                seen[base] += 1
                grades.append(Grade(
                    key=f"{base}|{seen[base]}",
                    student=student,
                    subject=subject_name,
                    value=value,
                    date=_parse_pl_date(raw_date),
                    category=(g.get("kategoriaKolumny") or column.get("kategoriaKolumny") or "").strip(),
                    column=(g.get("nazwaKolumny") or column.get("nazwaKolumny") or "").strip(),
                    weight=g.get("waga"),
                    teacher=(g.get("nauczyciel") or "").strip(),
                    corrected=g.get("idOcenaPoprawiona") is not None,
                ))  # fmt: skip
    return grades


def _parse_pl_date(text: str) -> date | None:
    try:
        return datetime.strptime(text, "%d.%m.%Y").date()
    except ValueError:
        return None


def periods_to_check(periods: list[Period], today: date, lookback: timedelta = timedelta(days=14)) -> list[Period]:
    """Okresy klasyfikacyjne, w których mogły pojawić się nowe oceny: bieżący i — tuż po zmianie
    semestru — poprzedni."""
    return [p for p in periods if p.start <= today and p.end >= today - lookback]


# --- harmonogram ---------------------------------------------------------------------------------


def weekly_slot(now: datetime, hour: int) -> datetime:
    """Ostatnia sobota o `hour` (czasu polskiego), nie później niż `now`."""
    local = now.astimezone(WARSAW)
    day = local.date() - timedelta(days=(local.weekday() - SATURDAY) % 7)
    slot = datetime.combine(day, time(hour), WARSAW)
    if slot > local:
        slot -= timedelta(days=7)
    return slot


def weekly_due(now: datetime, hour: int, last: datetime | None) -> bool:
    """Czy wysłać podsumowanie tygodnia: minęła sobotnia godzina, a od niej nic nie wysłano. Pominiętą sobotę
    (np. serwer wyłączony) dosyłamy do WEEKLY_CATCH_UP później — ale nie przy pierwszym uruchomieniu, wtedy
    pierwsze podsumowanie idzie w najbliższą sobotę."""
    slot = weekly_slot(now, hour)
    if last is None:
        return now.astimezone(WARSAW).date() == slot.date()
    return last < slot and now - slot < WEEKLY_CATCH_UP


def daily_slot(now: datetime, hour: int) -> datetime | None:
    """Dzisiejsza godzina sprawdzenia kalendarza (nd–czw), jeśli już minęła; inaczej None."""
    local = now.astimezone(WARSAW)
    if local.weekday() not in DAILY_CHECK_DAYS:
        return None
    slot = datetime.combine(local.date(), time(hour), WARSAW)
    return slot if local >= slot else None


def daily_due(now: datetime, hour: int, last: datetime | None) -> bool:
    slot = daily_slot(now, hour)
    return slot is not None and (last is None or last < slot)


def urgent_until(today: date) -> date:
    """Pierwszy dzień, o którym zdąży jeszcze powiedzieć najbliższe sobotnie podsumowanie (poniedziałek po
    nim). Wpisy z terminem wcześniej wysyłamy od razu."""
    saturday = today + timedelta(days=(SATURDAY - today.weekday()) % 7)
    return saturday + timedelta(days=2)


def local_today(now: datetime) -> date:
    return now.astimezone(WARSAW).date()


def format_day(d: date) -> str:
    return f"{WEEKDAYS_SHORT[d.weekday()]} {d:%d.%m}"
