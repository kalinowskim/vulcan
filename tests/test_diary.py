"""Harmonogram i parsowanie dziennika ucznia."""

from datetime import date, datetime, timedelta

from vulcan_forwarder.diary import (
    WARSAW,
    CalendarItem,
    Student,
    daily_due,
    parse_grades,
    urgent_until,
    weekly_due,
    weekly_slot,
)


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """Listopad 2026: 7 = sobota, 8 = niedziela, 9 = poniedziałek, 12 = czwartek, 13 = piątek."""
    return datetime(2026, 11, day, hour, minute, tzinfo=WARSAW)


def test_weekly_slot_is_last_saturday_hour():
    assert weekly_slot(at(7, 9), 9) == at(7, 9)
    assert weekly_slot(at(7, 8, 59), 9) == at(7, 9) - timedelta(days=7)
    assert weekly_slot(at(12, 17), 9) == at(7, 9)


def test_weekly_due_on_saturday_once():
    assert not weekly_due(at(7, 8, 59), 9, None)
    assert weekly_due(at(7, 9), 9, None)
    assert not weekly_due(at(7, 12), 9, at(7, 9, 5))
    assert weekly_due(at(14, 9), 9, at(7, 9, 5))


def test_weekly_catches_up_but_not_on_first_start_midweek():
    assert weekly_due(at(8, 20), 9, at(1, 0))  # serwer leżał w sobotę — dosyłamy w niedzielę
    assert not weekly_due(at(10, 12), 9, None)  # pierwsze uruchomienie we wtorek: czekamy do soboty
    assert not weekly_due(at(8, 8), 9, None)  # ani w niedzielę
    assert not weekly_due(at(10, 12), 9, at(1, 9))  # za dawno — nie dosyłamy


def test_daily_due_sunday_to_thursday_after_hour():
    assert daily_due(at(8, 17), 17, None)  # niedziela
    assert daily_due(at(12, 23), 17, at(11, 17))  # czwartek
    assert not daily_due(at(12, 16, 59), 17, None)
    assert not daily_due(at(13, 18), 17, None)  # piątek
    assert not daily_due(at(7, 18), 17, None)  # sobota
    assert not daily_due(at(9, 18), 17, at(9, 17, 1))  # już dziś sprawdzone


def test_urgent_until_is_monday_after_next_saturday():
    assert urgent_until(date(2026, 11, 8)) == date(2026, 11, 16)  # niedziela → cały nadchodzący tydzień
    assert urgent_until(date(2026, 11, 12)) == date(2026, 11, 16)  # czwartek → do niedzieli
    assert urgent_until(date(2026, 11, 7)) == date(2026, 11, 9)


def test_parse_grades_numbers_identical_entries_and_skips_empty():
    data = {"ocenyPrzedmioty": [{
        "przedmiotNazwa": "Matematyka",
        "kolumnyOcenyCzastkowe": [{"idKolumny": 7, "oceny": [
            {"wpis": "+", "dataOceny": "05.11.2026", "idKolumny": 7, "kategoriaKolumny": "Aktywność",
             "nazwaKolumny": "", "waga": 1.0, "nauczyciel": "Puto Justyna [JU]", "idOcenaPoprawiona": None},
            {"wpis": "+", "dataOceny": "05.11.2026", "idKolumny": 7, "waga": 1.0, "nauczyciel": "Puto Justyna [JU]"},
            {"wpis": " ", "dataOceny": "06.11.2026", "idKolumny": 7},
            {"wpis": "4", "dataOceny": "06.11.2026", "idKolumny": 7, "idOcenaPoprawiona": 123},
        ]}],
    }]}  # fmt: skip
    grades = parse_grades(data, "UCZ")
    assert [g.value for g in grades] == ["+", "+", "4"]
    assert len({g.key for g in grades}) == 3
    assert grades[0].date == date(2026, 11, 5) and grades[0].category == "Aktywność"
    assert grades[2].corrected


def test_calendar_item_keys_separate_tests_from_homework():
    test = CalendarItem.from_api({"typ": 1, "przedmiotNazwa": "Matematyka", "data": "2026-11-12T00:00:00+01:00",
                                  "hasAttachment": False, "id": 5}, "UCZ")  # fmt: skip
    homework = CalendarItem.from_api({"typ": 4, "przedmiotNazwa": "Przyroda", "data": "2026-11-12T00:00:00+01:00",
                                      "hasAttachment": True, "id": 5}, "UCZ")  # fmt: skip
    assert test.key != homework.key
    assert (test.type_name, homework.type_name) == ("Sprawdzian", "Zadanie domowe")
    assert test.date == date(2026, 11, 12)


def test_student_label():
    s = Student.from_api({"key": "K", "uczen": "Alicja Nowak", "oddzial": "4f", "idDziennik": 1})
    assert s.label == "Alicja Nowak (4f)"
