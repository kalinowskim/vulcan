from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from vulcan_forwarder.matcher import match
from vulcan_forwarder.notification_parser import parse_bytes
from vulcan_forwarder.vulcan_client import MessageSummary

FIXTURES = Path(__file__).parent / "fixtures"

# Fragment prawdziwej odpowiedzi api/Odebrane (2026-11-02).
API_ITEMS = [
    {
        "apiGlobalKey": "22222222-bbbb-4bbb-8bbb-222222222222",
        "korespondenci": "Kowalska-Nowicka Ewa [Kn] - P - (SP1_Test)",
        "temat": "Warsztaty muzyczno-taneczne",
        "data": "2026-11-02T12:07:29.883+01:00",
        "skrzynka": "Nowak Jan - R - Nowak Maja - (SP1_Test)",
        "hasZalaczniki": False,
        "przeczytana": False,
        "id": 941756,
    },
    {
        "apiGlobalKey": "44444444-dddd-4ddd-8ddd-444444444444",
        "korespondenci": "Śliwa Agata [ŚA] - P - (SP1_Test)",
        "temat": 'Zbiórka darów dla Schroniska "Psie Pole"',
        "data": "2026-11-02T10:13:07.223+01:00",
        "skrzynka": "Nowak Jan - R - Nowak Maja - (SP1_Test)",
        "hasZalaczniki": False,
        "przeczytana": False,
        "id": 941462,
    },
    {
        "apiGlobalKey": "33333333-cccc-4ccc-8ccc-333333333333",
        "korespondenci": "Śliwa Agata [ŚA] - P - (SP1_Test)",
        "temat": 'Zbiórka darów dla Schroniska "Psie Pole"',
        "data": "2026-11-02T10:13:07.223+01:00",
        "skrzynka": "Nowak Jan - R - Nowak Zofia - (SP1_Test)",
        "hasZalaczniki": False,
        "przeczytana": False,
        "id": 941462,
    },
]
MESSAGES = [MessageSummary.from_api(i) for i in API_ITEMS]


def load(name: str):
    return parse_bytes((FIXTURES / name).read_bytes())


def test_match_by_uuid():
    n = load("notif_sliwa_zofia.eml")
    assert match(n, MESSAGES).key == "33333333-cccc-4ccc-8ccc-333333333333"


def test_same_message_two_students_matched_separately_by_uuid():
    zofia = match(load("notif_sliwa_zofia.eml"), MESSAGES)
    maja = match(load("notif_sliwa_maja.eml"), MESSAGES)
    assert zofia.key != maja.key
    assert "Zofia" in zofia.mailbox and "Maja" in maja.mailbox


def test_heuristic_match_without_uuid_uses_mailbox_to_disambiguate():
    n = replace(load("notif_sliwa_maja.eml"), vulcan_id=None)
    assert match(n, MESSAGES).key == "44444444-dddd-4ddd-8ddd-444444444444"


def test_heuristic_match_rejects_time_outside_tolerance():
    n = replace(load("notif_kowalska_maja.eml"), vulcan_id=None)
    assert match(n, MESSAGES) is not None
    late = replace(n, sent_at=n.sent_at + timedelta(hours=1))
    assert match(late, MESSAGES) is None


def test_heuristic_match_ignores_whitespace_and_case_in_subject():
    n = replace(load("notif_kowalska_maja.eml"), vulcan_id=None, subject="  warsztaty   MUZYCZNO-taneczne ")
    assert match(n, MESSAGES).key == "22222222-bbbb-4bbb-8bbb-222222222222"


def test_ambiguous_heuristic_returns_none():
    n = replace(load("notif_kowalska_maja.eml"), vulcan_id=None)
    duplicate = replace(MESSAGES[0], key="00000000-0000-0000-0000-000000000000")
    assert match(n, [MESSAGES[0], duplicate]) is None


def test_approximate_date_from_forward_matches_earlier_message():
    n = replace(load("fwd_gmail_inline_kowalska_maja.eml"), vulcan_id=None, date_approximate=True)
    n = replace(n, sent_at=n.sent_at + timedelta(hours=3))  # data przekazania, nie oryginału
    assert match(n, MESSAGES).key == "22222222-bbbb-4bbb-8bbb-222222222222"


def test_unknown_uuid_falls_back_to_heuristic():
    n = replace(load("notif_kowalska_maja.eml"), vulcan_id="11111111-1111-1111-1111-111111111111")
    assert match(n, MESSAGES).key == "22222222-bbbb-4bbb-8bbb-222222222222"


def test_no_match():
    n = load("notif_lis_zofia.eml")
    assert match(n, MESSAGES) is None


def test_summary_parses_api_date():
    assert MESSAGES[0].sent_at.utcoffset() == timedelta(hours=1)
    assert MESSAGES[0].sent_at.replace(tzinfo=None) == datetime(2026, 11, 2, 12, 7, 29, 883000)
