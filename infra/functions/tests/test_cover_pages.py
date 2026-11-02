"""The personal cover's segment list and the day document's check block for
the Results Service (create_cover_pages.create_cover_pdf, processor.day_summary
and its wiring into processor.process_judging_papers).

Uses the synthetic StartLists and panels of test_combined_schedule.
"""
from datetime import datetime

import pytest
from pypdf import PdfReader
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

import processor
from combine_judging_papers import get_last_end_time_from_start_list, get_panel_info
from create_cover_pages import create_cover_pdf
from test_combined_schedule import competition, day_pages, make_panel, packet_pages, sp_list  # noqa: F401

DATE = datetime(2026, 9, 19)
JUN = "FSKWSINGLES-KJUNIW----QUAL000100--"
SEN = "FSKWSINGLES-KSENIW----QUAL000100--"
CHECK = "RESULTS SERVICE CHECK"


def page_texts(path):
    return [page.extract_text() for page in PdfReader(str(path)).pages]


# --- create_cover_pdf --------------------------------------------------------

def test_cover_lists_the_segments_under_the_name(tmp_path):
    path = tmp_path / "cover.pdf"
    create_cover_pdf(str(path), "Kansallinen kilpailu", DATE, "Jon JUDGE", [
        ("09:30", "SM-JUNIORIT Naiset, Short Program", "Judge 3"),
        (None, "SM-SENIORIT Naiset, Free Skating", "Referee"),
    ])

    [text] = page_texts(path)
    assert "Jon JUDGE" in text and "19.09.2026" in text
    assert "09:30" in text and "SM-JUNIORIT Naiset, Short Program" in text and "Judge 3" in text
    assert "–" in text and "SM-SENIORIT Naiset, Free Skating" in text and "Referee" in text
    assert CHECK not in text


def test_many_segments_stay_on_one_page(tmp_path):
    path = tmp_path / "cover.pdf"
    segments = [(f"{8 + i:02d}:00", f"Category {i}, Free Skating", "Judge 1") for i in range(20)]

    create_cover_pdf(str(path), "Competition", DATE, "Jon JUDGE", segments)

    [text] = page_texts(path)
    assert "+ 5 more" in text
    assert "Category 14, Free Skating" in text and "Category 15," not in text


def test_a_long_label_is_shortened(tmp_path):
    path = tmp_path / "cover.pdf"
    label = "Aluekilpailu Minioret Tytöt #12, " + "Free Skating " * 10

    create_cover_pdf(str(path), "Competition", DATE, "Jon JUDGE", [("10:00", label, "Technical Specialist")])

    [text] = page_texts(path)
    assert "…" in text and label.strip() not in text
    assert "Technical Specialist" in text


def test_cover_with_the_check_block(tmp_path):
    path = tmp_path / "cover.pdf"
    create_cover_pdf(str(path), "Competition", DATE, "Jon JUDGE", [("10:00", "A, Short Program", "Judge 1")], {
        "date": DATE, "packets": 16,
        "roles": [("judge", 9), ("referee", 1), ("technical_specialist", 3)],
        "categories": 5, "segments": 8, "start": "08:30", "end": "21:40",
        "pages": 312, "file_name": "judgingpapers_20260919.pdf",
    })

    [text] = page_texts(path)
    assert f"{CHECK} · Saturday 19.09.2026" in text
    assert "Packets 16: 9 judges · 1 referee · 3 technical specialists" in text
    assert "Categories 5 · Segments 8 · Schedule 08:30–21:40" in text
    assert "Pages 312 · judgingpapers_20260919.pdf" in text


# --- parsing helpers ---------------------------------------------------------

def test_panel_info_keeps_the_printed_role(tmp_path):
    path = tmp_path / "panel.pdf"
    make_panel(path, [("Referee", "Ms. Rea REFEREE"), ("Judge No.3", "Mr. Jon JUDGE"),
                      ("Technical Controller", "Tia CONTROLLER")])

    assert get_panel_info(str(path)) == [
        ("referee", "Rea REFEREE", "Referee"),
        ("judge", "Jon JUDGE", "Judge 3"),
        ("technical_controller", "Tia CONTROLLER", "Technical Controller"),
    ]


def test_last_end_time_of_a_start_list(sp_list):
    assert get_last_end_time_from_start_list(sp_list) == "17:03"


def test_last_end_time_of_an_unreadable_file(tmp_path):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"not a pdf")
    assert get_last_end_time_from_start_list(str(path)) is None


# --- processor.day_summary ---------------------------------------------------

def test_day_summary_counts():
    persons = {
        "jon_judge": {'tasks': [("JUN01", "judge"), ("JUN02", "referee")]},
        "rea_referee": {'tasks': [("JUN01", "referee")]},
        "tia": {'tasks': [("SEN", "technical_specialist_1")]},
        "toni": {'tasks': [("SEN", "technical_specialist_2")]},
    }
    summary = processor.day_summary(
        "20260919", persons, 42, "judgingpapers_20260919.pdf",
        {"JUN01": "9:05", "JUN02": "13:00", "SEN": "10:30"},
        {"JUN01": "11:40", "JUN02": "16:10", "SEN": "12:00"},
        {"JUN01": "SM-NOORET Tytöt #1", "JUN02": "SM-Nooret tytöt #2", "SEN": "SM-SENIORIT Naiset"})

    assert summary['date'] == DATE
    assert summary['packets'] == 4
    # Someone with two roles counts in both groups
    assert summary['roles'] == [("judge", 1), ("referee", 2), ("technical_specialist", 2)]
    assert summary['categories'] == 2  # split groups count once
    assert summary['segments'] == 3
    assert (summary['start'], summary['end']) == ("9:05", "16:10")
    assert summary['pages'] == 42


def test_day_summary_without_printed_names_or_times():
    persons = {"jon": {'tasks': [("A1", "judge"), ("B1", "judge")]}}

    summary = processor.day_summary("20260919", persons, 3, "x.pdf", {}, {}, {})

    assert summary['categories'] == 2
    assert (summary['start'], summary['end']) == (None, None)


# --- process_judging_papers --------------------------------------------------

def make_judges_sheet(path, line):
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Helvetica", 10)
    c.drawString(40, 800, "Kansallinen kilpailu")
    c.drawString(40, 780, line)
    c.save()


@pytest.fixture
def printed(competition, monkeypatch):
    """The competition with JudgesSheetAlls printing the names; no categories table."""
    src, out = competition
    make_judges_sheet(src / f"{JUN}_JudgesSheetAll.pdf", "SM-JUNIORIT Naiset  Short Program")
    make_judges_sheet(src / f"{SEN}_JudgesSheetAll.pdf", "SM-SENIORIT Naiset  Short Program")
    monkeypatch.setattr(processor, "split_pdf", lambda path: [])
    return src, out


def test_packet_covers_list_the_segments(printed):
    src, out = printed

    processor.process_judging_papers(str(src), str(out), {})

    cover = packet_pages(out, "jon_judge")[0]
    assert "Jon JUDGE" in cover
    # In running order, names as the JudgesSheetAll prints them
    order = [cover.index(text) for text in ("16:15", "SM-JUNIORIT Naiset, Short Program", "Judge 1",
                                            "18:50", "SM-SENIORIT Naiset, Short Program")]
    assert order == sorted(order)
    assert "Referee" in packet_pages(out, "rea_referee")[0]
    # The check block is only in the day document
    assert CHECK not in cover
    assert not any(CHECK in page for page in packet_pages(out, "rea_referee"))


def test_day_document_starts_with_the_check_block(printed):
    src, out = printed

    processor.process_judging_papers(str(src), str(out), {})

    day = day_pages(out)
    first = day[0]
    assert "Jon JUDGE" in first  # first packet alphabetically
    assert f"{CHECK} · Saturday 19.09.2026" in first
    assert "Packets 2: 1 judge · 1 referee" in first
    assert "Categories 2 · Segments 2 · Schedule 16:15–18:56" in first
    assert f"Pages {len(day)} · judgingpapers_20260919.pdf" in first
    assert sum(CHECK in page for page in day) == 1
    # The rest of the first packet follows its check cover unchanged
    assert len(day) == len(packet_pages(out, "jon_judge")) + len(packet_pages(out, "rea_referee"))


def test_page_count_includes_the_combined_schedule_copies(printed):
    src, out = printed

    processor.process_judging_papers(str(src), str(out), {"globalCombinedSchedule": True,
                                                          "combinedScheduleCopies": 3})

    day = day_pages(out)
    assert f"Pages {len(day)} ·" in day[0]
    assert sum("Time schedule and starting order" in page for page in day) == 3


def test_a_failing_check_cover_leaves_the_day_document_whole(printed, monkeypatch):
    src, out = printed
    monkeypatch.setattr(processor, "day_summary", lambda *a, **k: 1 / 0)

    processor.process_judging_papers(str(src), str(out), {})

    day = day_pages(out)
    assert not any(CHECK in page for page in day)
    assert "Jon JUDGE" in day[0] and "SM-JUNIORIT Naiset, Short Program" in day[0]
