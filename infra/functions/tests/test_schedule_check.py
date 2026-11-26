"""schedule_check — which segments of the CompetitionSchedule have papers among
the uploads (issue #84) — and get_competition_details' `scheduleCheck`."""
import io

import pytest

from competition_schedule import parse_competition_schedule
from schedule_check import check_schedule, start_list_title
from test_category_names import PREFIX, details, sheet_pdf  # noqa: F401
from test_combined_schedule import PARAMS_FS, FS_ROWS, make_start_list
from test_competition_schedule import make_schedule

FS_PREFIX = "FSKWSINGLES-KJUNIW----FNL0000100--"


def row(date, start, category, segment, entries=5):
    """A schedule entry as parse_competition_schedule returns it"""
    return {"date": date, "date_display": f"{date[6:]}.{date[4:6]}.{date[:4]}", "start_time": start,
            "end_time": "23:59", "entries": entries, "event_name": f"{category} {segment}".strip(),
            "category_name": category, "segment_name": segment}


def seg(prefix, *titles, date=None, card=None):
    return {"prefix": prefix, "categoryKey": card, "titles": list(titles), "date": date}


def statuses(result):
    return [(r["name"], r["status"], r["prefix"]) for r in result]


# --- start_list_title ----------------------------------------------------------

def test_the_title_is_the_line_after_the_heading():
    text = "\n   TIME SCHEDULE   \n\n  SM-JUNIORI MIEHET  SHORT PROGRAM\nprinted:   07.09.2026 21.01\n"
    assert start_list_title(text) == "SM-JUNIORI MIEHET  SHORT PROGRAM"


@pytest.mark.parametrize("text", [None, "", "STARTING ORDER\nJUNIORI NAISET", "TIME SCHEDULE"])
def test_no_heading_no_title(text):
    assert start_list_title(text) is None


# --- check_schedule --------------------------------------------------------------

def test_every_row_with_an_uploaded_segment_is_found():
    rows = [row("20260912", "10:40", "SM-JUNIORI Miehet", "Short Program"),
            row("20260912", "11:17", "SM-JUNIORI Naiset", "Short Program")]
    result = check_schedule(rows, [
        seg("M-QUAL", "SM-JUNIORI MIEHET  SHORT PROGRAM", "SM-JUNIORI MIEHET SHORT PROGRAM", date="20260912"),
        seg("W-QUAL", "SM-JUNIORI NAISET  SHORT PROGRAM", date="20260912"),
    ])

    assert statuses(result) == [("SM-JUNIORI Miehet Short Program", "found", "M-QUAL"),
                                ("SM-JUNIORI Naiset Short Program", "found", "W-QUAL")]
    assert result[0] == {
        "date": "2026-09-12", "startTime": "10:40", "entries": 5,
        "name": "SM-JUNIORI Miehet Short Program", "category": "SM-JUNIORI Miehet",
        "segment": "Short Program", "status": "found", "prefix": "M-QUAL", "categoryKey": None}


def test_a_row_without_papers_is_missing():
    rows = [row("20260912", "10:40", "SM-JUNIORI Miehet", "Short Program"),
            row("20260913", "11:29", "SM-JUNIORI Miehet", "Free Skating"),
            row("20260911", "16:00", "DEBYTANTTI Tytöt", "Free Skating")]
    result = check_schedule(rows, [seg("M-QUAL", "SM-JUNIORI MIEHET SHORT PROGRAM")])

    assert statuses(result) == [("SM-JUNIORI Miehet Short Program", "found", "M-QUAL"),
                                ("SM-JUNIORI Miehet Free Skating", "missing", None),
                                ("DEBYTANTTI Tytöt Free Skating", "missing", None)]


def test_a_row_with_no_entries_is_not_needed_unless_it_is_uploaded():
    rows = [row("20260912", "10:30", "SM-NOVIISI Pojat", "Short Program", entries=0),
            row("20260913", "11:17", "SM-NOVIISI Pojat", "Free Skating", entries=0)]
    result = check_schedule(rows, [seg("B-FNL", "SM-NOVIISI POJAT FREE SKATING")])

    assert statuses(result) == [("SM-NOVIISI Pojat Short Program", "notNeeded", None),
                                ("SM-NOVIISI Pojat Free Skating", "found", "B-FNL")]


def test_titles_differ_only_in_case_spacing_and_punctuation():
    rows = [row("20260912", "10:00", "A-Silmut, Tytöt", "Free Skating"),
            row("20260912", "11:00", "Advanced Novice Ice Dance", "PDK1 (Starlight Waltz)")]
    result = check_schedule(rows, [
        seg("A", "A-SILMUT TYTÖT   FREE SKATING"),
        seg("D", "ADVANCED NOVICE ICE DANCE PDK1 (STARLIGHT WALTZ)"),
    ])
    assert [r["status"] for r in result] == ["found", "found"]


def test_another_segment_of_the_same_category_does_not_cover_a_row():
    rows = [row("20260912", "10:00", "JUNIORI Naiset", "Free Skating")]
    result = check_schedule(rows, [seg("W-QUAL", "JUNIORI NAISET SHORT PROGRAM")])
    assert statuses(result) == [("JUNIORI Naiset Free Skating", "missing", None)]


def test_split_groups_are_matched_by_their_printed_names():
    rows = [row("20261031", "15:00", "SM-Noviisit L1", "Free Skating"),
            row("20261101", "15:00", "SM-Noviisit L2", "Free Skating")]
    result = check_schedule(rows, [
        seg("ADVNOV----FNL", "SM-NOVIISIT L2 FREE SKATING", date="20261101", card="SM-NOVIISIT L2"),
        seg("ADVNOV--01FNL", "SM-NOVIISIT L1 FREE SKATING", date="20261031", card="SM-NOVIISIT L1 #1"),
    ])

    assert statuses(result) == [("SM-Noviisit L1 Free Skating", "found", "ADVNOV--01FNL"),
                                ("SM-Noviisit L2 Free Skating", "found", "ADVNOV----FNL")]
    assert [r["categoryKey"] for r in result] == ["SM-NOVIISIT L1 #1", "SM-NOVIISIT L2"]


def test_one_segment_covers_one_row():
    rows = [row("20260912", "10:00", "JUNIORI Naiset", "Free Skating"),
            row("20260913", "10:00", "JUNIORI Naiset", "Free Skating")]

    # The same title on two days: the uploaded one goes to its own day
    result = check_schedule(rows, [seg("DAY2", "JUNIORI NAISET FREE SKATING", date="20260913")])
    assert statuses(result) == [("JUNIORI Naiset Free Skating", "missing", None),
                                ("JUNIORI Naiset Free Skating", "found", "DAY2")]

    # No date known (only its JudgesSheetAll is uploaded): the first row
    result = check_schedule(rows, [seg("ANY", "JUNIORI NAISET FREE SKATING")])
    assert statuses(result) == [("JUNIORI Naiset Free Skating", "found", "ANY"),
                                ("JUNIORI Naiset Free Skating", "missing", None)]

    # A segment whose date the schedule no longer has is still that row's
    result = check_schedule(rows[:1], [seg("MOVED", "JUNIORI NAISET FREE SKATING", date="20260920")])
    assert statuses(result) == [("JUNIORI Naiset Free Skating", "found", "MOVED")]


def test_a_missing_row_gets_the_card_of_its_category():
    rows = [row("20260912", "10:40", "SM-JUNIORI Naiset", "Short Program"),
            row("20260913", "12:10", "SM-JUNIORI Naiset", "Free Skating"),     # a found sibling's card
            row("20260911", "16:00", "A-SILMUT Tytöt", "Free Skating"),        # a card of the same name
            row("20260911", "17:00", "Juniorit", "Free Skating"),              # ... or without its '#N'
            row("20260911", "18:00", "SENIORI Naiset", "Short Program"),       # nothing uploaded
            {**row("20260911", "19:00", "", "Gaala Näytös"), "event_name": "Gaala Näytös"}]
    result = check_schedule(
        rows,
        [seg("W-QUAL", "SM-JUNIORI NAISET SHORT PROGRAM", card="SM-JUNIORIT Naiset")],
        ["SM-JUNIORIT Naiset", "A-Silmut, Tytöt", "JUNIORIT #2"])

    assert [r["categoryKey"] for r in result] == [
        "SM-JUNIORIT Naiset", "SM-JUNIORIT Naiset", "A-Silmut, Tytöt", "JUNIORIT #2", None, None]
    assert [r["status"] for r in result] == ["found"] + ["missing"] * 5


@pytest.mark.parametrize("keys", [["JUNIORIT #1", "JUNIORIT"], ["JUNIORIT", "JUNIORIT #1"]],
                         ids=["split card first", "exact card first"])
def test_a_card_of_exactly_that_name_wins_over_a_split_group(keys):
    result = check_schedule([row("20260911", "17:00", "Juniorit", "Free Skating")], [], keys)
    assert [r["categoryKey"] for r in result] == ["JUNIORIT"]


@pytest.mark.parametrize("keys, lowest", [
    (["JUNIORIT #2", "JUNIORIT #1"], "JUNIORIT #1"),
    (["JUNIORIT #1", "JUNIORIT #2"], "JUNIORIT #1"),
    # By number, not by spelling: group 2 comes before group 10
    (["JUNIORIT #10", "JUNIORIT #2"], "JUNIORIT #2"),
    (["JUNIORIT #2", "JUNIORIT #10", "JUNIORIT # 3"], "JUNIORIT #2"),
], ids=["2 then 1", "1 then 2", "10 then 2", "2, 10, 3"])
def test_a_bare_name_among_split_groups_goes_to_the_lowest_whatever_the_order(keys, lowest):
    result = check_schedule([row("20260911", "17:00", "Juniorit", "Free Skating")], [], keys)
    assert [r["categoryKey"] for r in result] == [lowest]


def test_a_numbered_segment_printed_as_one_string_still_finds_its_card():
    # No table row for the printed category: the split rests on the segment name alone
    data = make_schedule(
        [("12.09.2026", [("10:00:00", "10:30:00", 6, "JUNIORDAMER", "FREE SKATING 2")])], single_spaced=True)
    rows = parse_competition_schedule(io.BytesIO(data), [])
    assert [(r["category_name"], r["segment_name"]) for r in rows] == [("JUNIORDAMER", "FREE SKATING 2")]

    result = check_schedule(rows, [], ["Juniordamer"])
    assert [(r["status"], r["categoryKey"]) for r in result] == [("missing", "Juniordamer")]


def test_no_rows_no_result():
    assert check_schedule([], [seg("X", "JUNIORI NAISET FREE SKATING")]) == []


# --- get_competition_details ---------------------------------------------------

SCHEDULE_NAME = "FSK-------------------------------_CompetitionSchedule.pdf"
SETUP_ONLY = "FSKWSINGLES-ASILMW----------------_CalculationSetupVerificationforReferee.pdf"


def start_list(tmp_path, title, date_line):
    path = make_start_list(tmp_path / "list.pdf", title, [FS_ROWS], PARAMS_FS,
                           date_line=date_line, banner=False)
    with open(path, "rb") as f:
        return f.read()


def test_details_report_each_scheduled_segment(details, tmp_path):  # noqa: F811
    data = details({
        SCHEDULE_NAME: make_schedule([
            ("12.09.2026", [("10:40:00", "11:16:05", 5, "JUNIORINAISET", "Short Program")]),
            ("13.09.2026", [("11:29:00", "12:09:40", 5, "JUNIORINAISET", "Free Skating"),
                            ("12:10:00", "13:00:00", 9, "A-SILMUT Tytöt", "Free Skating"),
                            ("13:10:00", "14:00:00", 7, "SENIORI Naiset", "Free Skating"),
                            ("14:10:00", "14:20:00", 0, "SENIORI Miehet", "Free Skating")]),
        ], dotted=True),
        # Found by line 2 of its JudgesSheetAll alone ...
        f"{PREFIX}_JudgesSheetAll.pdf": sheet_pdf("Spring Cup 2026", "JUNIORINAISET  SHORT PROGRAM"),
        # ... and by the title of its start list alone
        f"{FS_PREFIX}_StartListwithTimes.pdf": start_list(
            tmp_path, "JUNIORINAISET  FREE SKATING", "SUNDAY, 13 SEPTEMBER 2026"),
        # Only the calculation setup: no papers for its segment
        SETUP_ONLY: b"%PDF-1.4",
    })

    assert [(r["date"], r["startTime"], r["entries"], r["name"], r["status"], r["prefix"], r["categoryKey"])
            for r in data["scheduleCheck"]["rows"]] == [
        ("2026-09-12", "10:40", 5, "JUNIORINAISET Short Program", "found", PREFIX, "JUNIORINAISET"),
        ("2026-09-13", "11:29", 5, "JUNIORINAISET Free Skating", "found", FS_PREFIX, "JUNIORINAISET"),
        ("2026-09-13", "12:10", 9, "A-SILMUT Tytöt Free Skating", "missing", None, "A-Silmut, Tytöt"),
        ("2026-09-13", "13:10", 7, "SENIORI Naiset Free Skating", "missing", None, None),
        ("2026-09-13", "14:10", 0, "SENIORI Miehet Free Skating", "notNeeded", None, None),
    ]
    assert [(r["category"], r["segment"]) for r in data["scheduleCheck"]["rows"][:1]] == [
        ("JUNIORINAISET", "Short Program")]
    # Every card the rows point at is in the file list
    assert {"JUNIORINAISET", "A-Silmut, Tytöt"} <= set(data["structure"])


def test_details_have_no_check_without_a_schedule(details):  # noqa: F811
    data = details({f"{PREFIX}_JudgesSheetAll.pdf": sheet_pdf("Spring Cup 2026", "JUNIORINAISET  SHORT PROGRAM")})
    assert data["scheduleCheck"] is None


@pytest.mark.parametrize("schedule", [b"%PDF-1.4", b"", sheet_pdf("Spring Cup 2026", "COMPETITION SCHEDULE")],
                         ids=["broken", "empty", "no rows"])
def test_an_unreadable_schedule_is_no_check_and_no_error(details, schedule):  # noqa: F811
    data = details({
        SCHEDULE_NAME: schedule,
        f"{PREFIX}_JudgesSheetAll.pdf": sheet_pdf("Spring Cup 2026", "JUNIORINAISET  SHORT PROGRAM"),
    })
    assert data["scheduleCheck"] is None
    assert "JUNIORINAISET" in data["structure"]


def test_a_failing_check_is_no_check_and_no_error(details, monkeypatch):  # noqa: F811
    import function_app as fa

    def boom(*_args, **_kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(fa, "check_schedule", boom)
    data = details({
        SCHEDULE_NAME: make_schedule([("12.09.2026", [("10:40:00", "11:16:05", 5, "JUNIORINAISET", "Short Program")])]),
        f"{PREFIX}_JudgesSheetAll.pdf": sheet_pdf("Spring Cup 2026", "JUNIORINAISET  SHORT PROGRAM"),
    })
    assert data["scheduleCheck"] is None
    assert "JUNIORINAISET" in data["structure"]
