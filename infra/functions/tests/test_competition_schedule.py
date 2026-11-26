"""competition_schedule.parse_competition_schedule: FSM's CompetitionSchedule PDF.

The schedules here are synthetic: reportlab draws them in FSM's column layout
(date line, then start / finish / entries / category / segment and the four
duration columns), with times in either locale's notation.
"""
import io

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from competition_schedule import get_schedule_start_time, parse_competition_schedule

DURATIONS = ("00:15:00", "00:02:40", "00:02:45", "00:02:45")
HEADERS = ((70, "Time"), (112, "Time"), (150, "Entries"), (180, "Category"), (340, "Segment"),
           (430, "Resurfacing"), (476, "mance"), (514, "First"), (552, "Last"))


def make_schedule(days, dotted=False, printed="07.09.2026 21.03", single_spaced=False):
    """
    A CompetitionSchedule as bytes. days: [(dd.mm.yyyy or None, [(start, end,
    entries, category, segment), ...])]; None draws the rows with no date line
    above them. dotted prints the clock times as a Finnish-locale export does
    (16.00.00; the duration columns keep their colons). single_spaced draws
    each row as one string, as pypdf's plain extraction returns it.
    """
    def clock(value):
        return value.replace(":", ".") if dotted else value

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Helvetica-Bold", 11.8)
    c.drawString(23, 780, "Spring Cup 2026")
    c.drawString(23, 762, "COMPETITION SCHEDULE")
    c.setFont("Helvetica", 7)
    c.drawString(23, 742, f"printed:   {clock(printed)}")
    c.setFont("Helvetica-Bold", 8)
    for x, text in HEADERS:
        c.drawString(x, 722, text)
    y = 706
    for date, rows in days:
        if date:
            c.setFont("Helvetica-Bold", 8)
            c.drawString(24, y, date)
            y -= 14
        c.setFont("Helvetica", 8)
        for start, end, entries, category, segment in rows:
            if single_spaced:
                c.drawString(70, y, " ".join(
                    [clock(start), clock(end), str(entries), category, segment, *DURATIONS]))
            else:
                c.drawString(70, y, clock(start))
                c.drawString(112, y, clock(end))
                c.drawRightString(172, y, str(entries))
                c.drawString(180, y, category)
                c.drawString(340, y, segment)
                for x, duration in zip((430, 476, 514, 552), DURATIONS):
                    c.drawString(x, y, duration)
            y -= 14
    c.setFont("Helvetica", 7)
    c.drawString(546, 95, "Page 1 / 1")
    c.save()
    return buf.getvalue()


DAYS = [
    ("11.09.2026", [
        ("16:00:00", "17:19:45", 11, "DEBYTANTTI Tytöt", "Free Skating"),
        ("17:35:00", "19:53:00", 18, "JUNIORI Naiset", "Short Program"),
    ]),
    ("12.09.2026", [
        ("8:30:00", "10:14:10", 17, "SM-NOVIISI Tytöt", "Short Program"),
        ("10:30:00", "10:39:40", 0, "SM-NOVIISI Pojat", "Short Program"),
        ("14:15:00", "14:32:30", 4, "Advanced Novice Ice Dance", "PDK1 (Starlight Waltz)"),
    ]),
]


def parse(data, categories=None):
    return parse_competition_schedule(io.BytesIO(data), categories)


@pytest.mark.parametrize("dotted", [False, True], ids=["14:00:00", "16.00.00"])
def test_rows_are_read_in_either_time_notation(dotted):
    rows = parse(make_schedule(DAYS, dotted=dotted))

    assert [(r["date"], r["date_display"], r["start_time"], r["end_time"], r["entries"]) for r in rows] == [
        ("20260911", "11.09.2026", "16:00", "17:19", 11),
        ("20260911", "11.09.2026", "17:35", "19:53", 18),
        ("20260912", "12.09.2026", "08:30", "10:14", 17),
        ("20260912", "12.09.2026", "10:30", "10:39", 0),
        ("20260912", "12.09.2026", "14:15", "14:32", 4),
    ]


@pytest.mark.parametrize("dotted", [False, True], ids=["14:00:00", "16.00.00"])
def test_category_and_segment_are_as_printed_without_the_duration_columns(dotted):
    rows = parse(make_schedule(DAYS, dotted=dotted))

    assert [(r["category_name"], r["segment_name"]) for r in rows] == [
        ("DEBYTANTTI Tytöt", "Free Skating"),
        ("JUNIORI Naiset", "Short Program"),
        ("SM-NOVIISI Tytöt", "Short Program"),
        ("SM-NOVIISI Pojat", "Short Program"),
        ("Advanced Novice Ice Dance", "PDK1 (Starlight Waltz)"),
    ]
    assert rows[0]["event_name"] == "DEBYTANTTI Tytöt Free Skating"
    assert rows[4]["event_name"] == "Advanced Novice Ice Dance PDK1 (Starlight Waltz)"


def test_a_row_with_no_entries_is_kept():
    rows = parse(make_schedule(DAYS))
    assert [r["entries"] for r in rows if r["category_name"] == "SM-NOVIISI Pojat"] == [0]


def test_the_printed_stamp_is_not_a_competition_day():
    # The stamp line carries a date, but a row above the first date line still has no day
    rows = parse(make_schedule([
        (None, [("9:00:00", "9:30:00", 5, "JUNIORI Naiset", "Short Program")]),
        ("12.09.2026", [("10:00:00", "10:30:00", 6, "SENIORI Naiset", "Short Program")]),
    ]))
    assert [(r["date"], r["category_name"]) for r in rows] == [("20260912", "SENIORI Naiset")]


def test_a_row_extracted_as_one_string_is_split_before_the_segment_name():
    rows = parse(make_schedule(DAYS, dotted=True, single_spaced=True))

    assert [(r["category_name"], r["segment_name"]) for r in rows] == [
        ("DEBYTANTTI Tytöt", "Free Skating"),
        ("JUNIORI Naiset", "Short Program"),
        ("SM-NOVIISI Tytöt", "Short Program"),
        ("SM-NOVIISI Pojat", "Short Program"),
        ("Advanced Novice Ice Dance", "PDK1 (Starlight Waltz)"),
    ]
    assert rows[1]["event_name"] == "JUNIORI Naiset Short Program"


def test_a_row_that_cannot_be_split_keeps_its_whole_text():
    rows = parse(make_schedule(
        [("12.09.2026", [("10:00:00", "10:30:00", 6, "Gaala", "Näytös")])], single_spaced=True))

    assert [(r["category_name"], r["segment_name"], r["event_name"]) for r in rows] == [
        ("", "Gaala Näytös", "Gaala Näytös")]


def test_the_categories_table_is_the_last_resort_for_a_split():
    table = [{"displayName": "Gaala", "displayNameFi": "Gaala"}]
    rows = parse(make_schedule(
        [("12.09.2026", [("10:00:00", "10:30:00", 6, "Gaala", "Näytös")])], single_spaced=True), table)

    assert [(r["category_name"], r["segment_name"]) for r in rows] == [("Gaala", "Näytös")]


def one_row(category, segment, table):
    """(category, segment) of a single row drawn as one string, split with `table`."""
    rows = parse(make_schedule(
        [("12.09.2026", [("10:00:00", "10:30:00", 6, category, segment)])], single_spaced=True), table)
    return [(r["category_name"], r["segment_name"]) for r in rows]


def test_the_table_split_knows_the_finnish_name_and_keeps_the_printed_text():
    # The schedule prints the competition's own language, in its own spelling
    table = [{"displayName": "Chicks, Girls", "displayNameFi": "A-Silmut, Tytöt"}]
    assert one_row("A-SILMUT Tytöt", "Näytös", table) == [("A-SILMUT Tytöt", "Näytös")]
    assert one_row("CHICKS GIRLS", "Näytös", table) == [("CHICKS GIRLS", "Näytös")]


def test_the_table_split_takes_the_longest_name_on_a_word_boundary():
    table = [{"displayName": "Adults", "displayNameFi": "Aikuiset"},
             {"displayName": "Adult Women", "displayNameFi": "Aikuiset Naiset"}]
    assert one_row("AIKUISET NAISET", "Näytös", table) == [("AIKUISET NAISET", "Näytös")]
    assert one_row("Aikuiset", "Näytös", table) == [("Aikuiset", "Näytös")]
    # "Aikuiset" is not the start of another word
    assert one_row("Aikuisetkin", "Näytös", table) == [("", "Aikuisetkin Näytös")]


def test_the_table_split_keeps_a_split_group_mark_with_the_category():
    table = [{"displayName": "Gala", "displayNameFi": "Gaala"}]
    assert one_row("Gaala #2", "Näytös", table) == [("Gaala #2", "Näytös")]


def test_a_category_name_with_nothing_after_it_is_not_a_split():
    table = [{"displayName": "Gala", "displayNameFi": "Gaala"}]
    rows = parse(make_schedule(
        [("12.09.2026", [("10:00:00", "10:30:00", 6, "Gaala", "")])]), table)
    assert [(r["category_name"], r["segment_name"]) for r in rows] == [("", "Gaala")]


@pytest.mark.parametrize("data", [b"", b"not a pdf", b"%PDF-1.4"])
def test_an_unreadable_schedule_gives_no_rows(data):
    assert parse(data) == []


def test_start_time_lookup_works_on_a_dotted_schedule():
    rows = parse(make_schedule(DAYS, dotted=True))
    assert get_schedule_start_time(rows, "SM-NOVIISI Tytöt", "Short Program", "20260912") == "08:30"
