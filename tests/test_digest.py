"""Podsumowanie tygodnia i pilne powiadomienia — z podmienionym Vulcanem i SMTP."""

from datetime import date, datetime

import pytest
from pydantic import SecretStr

from vulcan_forwarder import digest
from vulcan_forwarder.config import Account, Settings
from vulcan_forwarder.diary import WARSAW, CalendarItem, Grade, Student
from vulcan_forwarder.store import Store
from vulcan_forwarder.vulcan_client import VulcanLoginError

ALA = Student(key="ALA", name="Alicja Nowak", class_name="4f", diary_id=1)
EMI = Student(key="EMI", name="Emilia Nowak", class_name="1d", diary_id=2)


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """Listopad 2026: 7 = sobota, 8 = niedziela, 9 = poniedziałek, 14 = sobota."""
    return datetime(2026, 11, day, hour, minute, tzinfo=WARSAW)


def grade(student: Student, subject: str, value: str, day: int, n: int = 1) -> Grade:
    return Grade(key=f"grade|{student.key}|{subject}|{day}|{value}|{n}", student=student.key, subject=subject,
                 value=value, date=date(2026, 11, day), category="Bieżące", column="Kartkówka", weight=1.0,
                 teacher="Puto Justyna [JU]", corrected=False)  # fmt: skip


def item(student: Student, id: int, type: int, subject: str, d: date) -> CalendarItem:
    return CalendarItem(id=id, type=type, student=student.key, subject=subject, date=d, has_attachment=False)


class FakeDiary:
    """Dziennik jednego konta; testy zmieniają `grades_` i `calendar_` między przebiegami."""

    def __init__(self, students, error=None):
        self.students_, self.error = students, error
        self.grades_: list[Grade] = []
        self.calendar_: list[CalendarItem] = []
        self.details_calls = 0

    def __call__(self, settings, account, headed=False, debug=False):
        return self

    def __enter__(self):
        if self.error:
            raise self.error
        return self

    def __exit__(self, *exc):
        pass

    def enter_diary(self):
        pass

    def students(self):
        return self.students_

    def grades(self, student, today):
        return [g for g in self.grades_ if g.student == student.key]

    def calendar(self, student, start, end):
        return [i for i in self.calendar_ if i.student == student.key and start <= i.date <= end]

    def calendar_details(self, it):
        self.details_calls += 1
        return {"opis": f"opis {it.subject}", "nauczycielImieNazwisko": "Justyna Puto"}

    def diary_url(self, key, route):
        return f"https://uczen.eduvulcan.pl/test/App/{key}/{route}"


class Accounts:
    """Podmiana VulcanClient w digest: konto → FakeDiary."""

    def __init__(self, **diaries):
        self.diaries = diaries

    def __call__(self, settings, account, headed=False, debug=False):
        return self.diaries[account.name]


def account(name):
    return Account(name=name, login=f"{name}@x.pl", password=SecretStr("h"), mail_to=f"{name}@x.pl")


@pytest.fixture
def env(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, db_path=tmp_path / "db.sqlite", state_dir=tmp_path, mail_from="bot@x",
                        account_list=[account("mateusz"), account("aga")])  # fmt: skip
    sent = []
    monkeypatch.setattr(digest, "send", lambda msg, s, dry_run=False: sent.append(msg))
    mateusz, aga = FakeDiary([ALA, EMI]), FakeDiary([ALA])
    monkeypatch.setattr(digest, "VulcanClient", Accounts(mateusz=mateusz, aga=aga))
    return settings, sent, mateusz, aga


def run(settings, now, **kw):
    return digest.run_diary(settings, settings.accounts, now=now, **kw)


def body(msg) -> str:
    return msg.get_body(("plain",)).get_content()


def test_first_check_is_baseline_then_new_test_this_week_goes_out_immediately(env):
    settings, sent, mateusz, aga = env
    mateusz.calendar_ = [item(ALA, 1, 1, "Matematyka", date(2026, 11, 10))]
    assert run(settings, at(8, 17, 5))  # niedziela: pierwsze sprawdzenie — zastane wpisy bez maila
    assert sent == []

    mateusz.calendar_ += [
        item(ALA, 2, 2, "Przyroda", date(2026, 11, 12)),  # czwartek tego tygodnia → od razu
        item(EMI, 3, 4, "Edukacja", date(2026, 11, 13)),  # piątek → od razu, w tym samym mailu
        item(ALA, 4, 1, "Historia", date(2026, 11, 18)),  # przyszły tydzień → w sobotę
    ]
    assert run(settings, at(9, 17, 1))
    assert len(sent) == 1
    msg = sent[0]
    assert msg["To"] == "mateusz@x.pl"
    assert msg["Subject"] == "[Vulcan] Nowe w kalendarzu na ten tydzień: 2 wpisy"
    text = body(msg)
    assert "Kartkówka — Przyroda: opis Przyroda" in text and "Zadanie domowe — Edukacja" in text
    assert "Alicja Nowak (4f)" in text and "Emilia Nowak (1d)" in text
    assert "Historia" not in text and "Matematyka" not in text

    sent.clear()
    assert run(settings, at(9, 20))  # dziś już sprawdzone
    assert sent == []


def test_saturday_digest_has_week_grades_and_pending_calendar_per_account(env):
    settings, sent, mateusz, aga = env
    mateusz.grades_ = [grade(ALA, "Matematyka", "5", 8), grade(ALA, "Matematyka", "3", 4)]
    mateusz.calendar_ = [item(ALA, 1, 1, "Matematyka", date(2026, 11, 20))]
    aga.grades_ = [grade(ALA, "Matematyka", "5", 8)]
    assert run(settings, at(8, 17))  # niedziela 8.11: zastane oceny liczą się wg daty wystawienia
    sent.clear()

    mateusz.grades_.append(grade(EMI, "Plastyka", "6", 2))  # wystawiona z datą wsteczną, ale nowa w tym tygodniu
    mateusz.calendar_.append(item(ALA, 2, 4, "Przyroda", date(2026, 11, 25)))
    assert run(settings, at(14, 9, 3))
    assert [m["To"] for m in sent] == ["mateusz@x.pl", "aga@x.pl"]
    text = body(sent[0])
    assert sent[0]["Subject"] == "[Vulcan] Podsumowanie tygodnia 07.11–14.11.2026"
    assert "Matematyka: 5  Bieżące — Kartkówka  [08.11, Puto Justyna]" in text
    assert "Matematyka: 3" not in text  # z 4.11 — poprzedni tydzień
    assert "Plastyka: 6" in text
    assert "Sprawdzian — Matematyka" in text and "Zadanie domowe — Przyroda" in text
    assert "Nowe oceny (1):" in text and "Nowe oceny (1):" in body(sent[1])

    sent.clear()
    assert run(settings, at(14, 12))  # w tę sobotę już było
    assert run(settings, at(15, 17))  # niedziela: nic nowego
    assert sent == []

    assert run(settings, at(21, 9))  # następna sobota: pusto, ale mail i tak idzie
    text = body(sent[0])
    assert "Nowe oceny (0):" in text and "Sprawdzian" not in text


def test_rescheduled_test_is_notified_again(env):
    settings, sent, mateusz, _ = env
    mateusz.calendar_ = []
    assert run(settings, at(8, 17))
    mateusz.calendar_ = [item(ALA, 1, 1, "Matematyka", date(2026, 11, 12))]
    assert run(settings, at(9, 17))
    assert len(sent) == 1
    mateusz.calendar_ = [item(ALA, 1, 1, "Matematyka", date(2026, 11, 11))]
    assert run(settings, at(10, 17))
    assert len(sent) == 2
    assert "zmiana terminu, było czw 12.11" in body(sent[1])
    assert mateusz.details_calls == 1  # szczegóły pobierane tylko dla nowych wpisów


def test_dry_run_does_not_touch_database(env):
    settings, sent, mateusz, _ = env
    mateusz.grades_ = [grade(ALA, "Matematyka", "5", 6)]
    assert digest.run_diary(settings, settings.accounts[:1], now=at(14, 9), dry_run=True, force=digest.WEEKLY)
    assert len(sent) == 1
    with Store(settings.db_path) as store:
        assert store.diary_item("mateusz", mateusz.grades_[0].key) is None
        assert store.get_meta("diary_last_weekly:mateusz") is None


def test_account_error_does_not_block_others_and_backs_off(env):
    settings, sent, mateusz, aga = env
    mateusz.error = VulcanLoginError("złe hasło")
    errors = []
    assert not run(settings, at(14, 9), on_error=lambda exc, name: errors.append(name))
    assert errors == ["mateusz"]
    assert [m["To"] for m in sent] == ["aga@x.pl"]

    mateusz.error = None
    assert run(settings, at(14, 9, 10))  # za wcześnie na ponowną próbę
    assert len(sent) == 1
    assert run(settings, at(14, 9, 31))
    assert [m["To"] for m in sent] == ["aga@x.pl", "mateusz@x.pl"]


def test_disabled_does_nothing_unless_forced(env):
    settings, sent, *_ = env
    settings.diary_enabled = False
    assert run(settings, at(14, 9))
    assert sent == []
    assert run(settings, at(14, 9), force=digest.DAILY)
