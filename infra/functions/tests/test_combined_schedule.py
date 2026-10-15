"""combined_schedule — the day's combined time schedule page — and its wiring
into processor.process_judging_papers.

The StartLists here are synthetic: reportlab draws them at the positions FSM
uses (banner image under a 0.2834646 scale, 'Nation' column header, the
Free Skating lists' CurrentRank/Points columns), so no real skater data lives
in the repo.
"""
import os
import zipfile
from datetime import datetime

import pytest
from pdfminer.high_level import extract_text
from pypdf import PdfReader
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

import combined_schedule as cs
import processor
from processor import combined_schedule_enabled

LOGO = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "logo.png")
FSM_SCALE = 0.2834646
PARAMS_SP = ("Resurfacing: 25:00    Warm-Up: 5:00    Duration of Skating: 2:40 +/-10 seconds    "
             "Introduction: 0:30    Judging First: 2:45    Judging Last: 2:45")
PARAMS_FS = ("Warm-Up: 6:00    Duration of Skating: 3:40 +/-10 seconds    "
             "Introduction: 0:30    Judging First: 2:50    Judging Last: 2:50")


def skater(start, end, number, name, club, rank=None, points=None):
    return ("skater", start, end, str(number), name, club, rank, points)


def label(start, end, text):
    return ("label", start, end, "", text, "", None, None)


def withdrawn(name, club):
    return ("wd", "", "", "WD", name, club, None, None)


SP_ROWS = [
    label("16.15.00", "16.20.00", "Warm-Up Group 1"),
    skater("16.20.30", "16.26.25", 1, "Aino ÄÄRELÄ", "TL"),
    skater("16.26.25", "16.32.20", 2, "Bea BÖRJESSON", "ETK"),
    label("16.32.20", "16.57.20", "Ice Resurfacing"),
    withdrawn("Cilla CÉDERLÖF", "ML"),
    skater("16.57.20", "17.03.15", 4, "Pihla KUULA / Axel KANERVA", "HL"),
]
FS_ROWS = [
    label("9.30.00", "9.35.00", "Warm-Up Group 1"),
    skater("9.35.30", "9.41.50", 1, "Dora DAHL", "LTL", rank="24", points="18.05"),
    skater("9.41.50", "9.48.10", 2, "Eeva ÉKMAN", "VarTa", rank="23", points="19.36"),
]


def make_start_list(path, title, rows_per_page, params, printed="18.09.2026 13.10",
                    date_line="SATURDAY, 19 SEPTEMBER 2026", banner=True, free_skating=False):
    """Draw an FSM-like StartListwithTimes; rows_per_page is a list of row lists."""
    nation_x = 435 if free_skating else 527
    c = canvas.Canvas(str(path), pagesize=A4)
    pages = len(rows_per_page)
    for page_no, rows in enumerate(rows_per_page, start=1):
        if banner:
            c.saveState()
            c.transform(FSM_SCALE, 0, 0, FSM_SCALE, 0, 0)
            c.drawImage(LOGO, 80, 2726.39, width=1940, height=194)
            c.restoreState()
        c.setFont("Helvetica-Bold", 11.8)
        c.drawString(23, 760.5, "TIME SCHEDULE")
        c.drawString(23, 742.6, title)
        c.drawString(26, 716.1, date_line)
        c.setFont("Helvetica-Bold", 9.8)
        c.drawString(nation_x, 716.6, "Nation")
        if free_skating:
            c.drawString(511, 721, "Current")
            c.drawString(494, 709, "Rank")
            c.drawString(539, 709, "Points")

        y = 694.8
        for kind, start, end, number, name, club, rank, points in rows:
            c.setFont("Helvetica", 9.8)
            if start:
                c.drawString(26, y, start)
                c.drawString(72, y, "-")
                c.drawString(82, y, end)
            if number:
                c.drawRightString(146, y, number)
            c.setFont("Helvetica-Bold" if kind == "label" else "Helvetica", 9.8)
            c.drawString(167, y, name)
            c.setFont("Helvetica", 9.8)
            if club:
                c.drawString(nation_x, y, club)
            if rank:
                c.drawRightString(520, y, rank)
                c.drawRightString(570, y, points)
            y -= 13.9
        if page_no == pages and params:
            c.drawString(26, y - 4, params)
        c.setFont("Helvetica-Bold", 7)
        c.drawString(26, 142, "Legend:")
        c.drawString(26, 134, "WD")
        c.setFont("Helvetica", 7)
        c.drawString(53, 134, "Withdrawn")
        c.drawString(23, 94, f"printed:   {printed}")
        c.drawString(546, 95, f"Page {page_no} / {pages}")
        c.showPage()
    c.save()
    return str(path)


@pytest.fixture
def sp_list(tmp_path):
    return make_start_list(tmp_path / "SP_StartListwithTimes.pdf", "JUNIORI NAISET  SHORT PROGRAM",
                           [SP_ROWS], PARAMS_SP)


@pytest.fixture
def fs_list(tmp_path):
    return make_start_list(tmp_path / "FS_StartListwithTimes.pdf", "SENIORI NAISET  FREE SKATING",
                           [FS_ROWS], PARAMS_FS, printed="18.09.2026 13.06", free_skating=True)


# --- parse_start_list ------------------------------------------------------

def test_parses_rows_params_date_and_printed(sp_list):
    parsed = cs.parse_start_list(sp_list)

    assert parsed["title_line"] == "JUNIORI NAISET  SHORT PROGRAM"
    assert parsed["date"] == datetime(2026, 9, 19)
    assert parsed["printed"] == datetime(2026, 9, 18, 13, 10)
    assert parsed["first_time"] == "16.15.00"
    assert parsed["params"] == {
        "Resurfacing": "25:00", "Warm-Up": "5:00", "Duration of Skating": "2:40 +/-10 seconds",
        "Introduction": "0:30", "Judging First": "2:45", "Judging Last": "2:45",
    }
    assert [(r["kind"], r["time"], r["number"], r["name"], r["club"]) for r in parsed["rows"]] == [
        ("warmup", "16.15.00 - 16.20.00", "", "Warm-Up Group 1", ""),
        ("skater", "16.20.30 - 16.26.25", "1", "Aino ÄÄRELÄ", "TL"),
        ("skater", "16.26.25 - 16.32.20", "2", "Bea BÖRJESSON", "ETK"),
        ("resurfacing", "16.32.20 - 16.57.20", "", "Ice Resurfacing", ""),
        ("withdrawn", "", "WD", "Cilla CÉDERLÖF", "ML"),
        ("skater", "16.57.20 - 17.03.15", "4", "Pihla KUULA / Axel KANERVA", "HL"),
    ]


def test_free_skating_drops_rank_and_points(fs_list):
    parsed = cs.parse_start_list(fs_list)

    skaters = [r for r in parsed["rows"] if r["kind"] == "skater"]
    assert [(r["name"], r["club"]) for r in skaters] == [("Dora DAHL", "LTL"), ("Eeva ÉKMAN", "VarTa")]
    assert "Resurfacing" not in parsed["params"]
    assert parsed["params"]["Warm-Up"] == "6:00"


def test_multi_page_list_skips_the_repeated_header(tmp_path):
    first, second = SP_ROWS[:3], SP_ROWS[3:]
    path = make_start_list(tmp_path / "L_StartListwithTimes.pdf", "JUNIORI NAISET  SHORT PROGRAM",
                           [first, second], PARAMS_SP)

    parsed = cs.parse_start_list(path)

    assert [r["name"] for r in parsed["rows"]] == [r[4] for r in SP_ROWS]
    assert parsed["params"]["Judging Last"] == "2:45"


def test_list_without_skaters(tmp_path):
    path = make_start_list(tmp_path / "E_StartListwithTimes.pdf", "SOLO SENIOR  RHYTHM DANCE",
                           [[]], "Duration of Skating: 2:50 +/-10 seconds")

    parsed = cs.parse_start_list(path)

    assert parsed["rows"] == []
    assert parsed["first_time"] is None
    assert parsed["params"] == {"Duration of Skating": "2:50 +/-10 seconds"}


def test_unreadable_file_is_none(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf")
    assert cs.parse_start_list(str(bad)) is None


@pytest.mark.parametrize("line, hint, expected", [
    ("JUNIORI NAISET  SHORT PROGRAM", "SHORT PROGRAM", ("JUNIORI NAISET", "SHORT PROGRAM")),
    ("SM-NOVIISI TYTÖT FREE SKATING", None, ("SM-NOVIISI TYTÖT", "FREE SKATING")),
    ("SM-NOVIISI TYTÖT FREE SKATING", "Free Skating", ("SM-NOVIISI TYTÖT", "FREE SKATING")),
    ("SOLO BEGINNER PD1 (DUTCH WALTZ)", None, ("SOLO BEGINNER", "PD1 (DUTCH WALTZ)")),
    ("SOMETHING  ELSE", None, ("SOMETHING", "ELSE")),
    ("JUST A TITLE", None, ("JUST A TITLE", None)),
])
def test_split_title(line, hint, expected):
    assert cs.split_title(line, hint) == expected


# --- banner ----------------------------------------------------------------

def test_banner_found_above_the_text(sp_list):
    banner = cs.find_banner(sp_list)

    assert len(banner["draws"]) == 1
    matrix, _ = banner["draws"][0]
    assert matrix[0] == pytest.approx(1940 * FSM_SCALE, abs=0.01)
    assert banner["bottom"] == pytest.approx(2726.39 * FSM_SCALE, abs=0.01)


def test_no_banner(tmp_path):
    path = make_start_list(tmp_path / "N_StartListwithTimes.pdf", "X  SHORT PROGRAM", [SP_ROWS],
                           PARAMS_SP, banner=False)
    assert cs.find_banner(path) is None


# --- rendering -------------------------------------------------------------

def segment(path, prefix, hint=None):
    parsed = cs.parse_start_list(path)
    parsed.update({"prefix": prefix, "path": path, "segment_hint": hint})
    return parsed


def test_page_text_banner_and_footer(tmp_path, sp_list, fs_list):
    out = str(tmp_path / "combined.pdf")
    segments = [segment(sp_list, "SP"), segment(fs_list, "FS")]

    pages = cs.create_combined_schedule_pdf(out, datetime(2026, 9, 19), segments, cs.find_banner(sp_list))

    assert pages == 1
    text = extract_text(out)
    assert "Time schedule and starting order | Saturday 19.09.2026" in text
    assert "JUNIORI NAISET | SHORT PROGRAM" in text
    assert "SENIORI NAISET | FREE SKATING" in text
    # Characters outside ASCII survive the banner step (merge_page lost them).
    assert "Aino ÄÄRELÄ" in text and "Cilla CÉDERLÖF" in text
    assert "Introduction: 0:30   Judging First: 2:45   Judging Last: 2:45" in text
    assert "Sources printed 18.09.2026 13.06–13.10." in text
    assert "1 / 1" in text
    # Rank/points never reach the page.
    assert "18.05" not in text

    page = PdfReader(out).pages[0]
    xobjects = page["/Resources"]["/XObject"]
    assert "/FstBanner0" in xobjects
    assert xobjects["/FstBanner0"]["/Subtype"] == "/Image"


def test_without_banner_still_renders(tmp_path, sp_list):
    out = str(tmp_path / "combined.pdf")
    cs.create_combined_schedule_pdf(out, datetime(2026, 9, 19), [segment(sp_list, "SP")], None)
    assert "Aino ÄÄRELÄ" in extract_text(out)


def test_empty_segment_says_so(tmp_path):
    path = make_start_list(tmp_path / "E_StartListwithTimes.pdf", "SOLO SENIOR  RHYTHM DANCE", [[]], "")
    out = str(tmp_path / "combined.pdf")
    cs.create_combined_schedule_pdf(out, datetime(2026, 9, 19), [segment(path, "E")], None)
    assert "No skaters or times in the source file." in extract_text(out)


def _fake_segment(prefix, n_rows):
    rows = [{"kind": "skater", "time": "10.00.00 - 10.05.00", "number": str(i + 1),
             "name": f"Skater {prefix}{i + 1}", "club": "TL"} for i in range(n_rows)]
    return {"prefix": prefix, "title_line": f"CAT {prefix}  FREE SKATING", "segment_hint": None,
            "rows": rows, "params": {"Warm-Up": "5:00", "Judging Last": "2:50"}, "printed": None}


def test_segments_move_whole_to_the_next_page():
    pages = cs._layout([_fake_segment("A", 30), _fake_segment("B", 25)], content_top=740)

    assert len(pages) == 2
    assert [[(p["segment"]["prefix"], len(p["rows"]), p["cont"]) for p in page] for page in pages] == [
        [("A", 30, False)], [("B", 25, False)]]


def test_long_segment_continues_on_the_next_page(tmp_path):
    seg = _fake_segment("A", 70)
    pages = cs._layout([seg], content_top=740)

    assert len(pages) == 2
    assert pages[0][0]["cont"] is False and pages[0][0]["params"] is False
    assert pages[1][0]["cont"] is True and pages[1][0]["params"] is True
    assert sum(len(p[0]["rows"]) for p in pages) == 70

    out = str(tmp_path / "long.pdf")
    assert cs.create_combined_schedule_pdf(out, datetime(2026, 9, 20), [seg], None) == 2
    text = extract_text(out)
    assert "CAT A | FREE SKATING (cont.)" in text
    assert "1 / 2" in text and "2 / 2" in text
    assert "Sunday 20.09.2026" in text


def test_struck_rows():
    wd_row = {"kind": "withdrawn", "name": "Cilla CÉDERLÖF"}
    sk_row = {"kind": "skater", "name": "Aino  ÄÄRELÄ"}
    label_row = {"kind": "warmup", "name": "Warm-Up Group 1"}

    assert cs.is_struck(wd_row, set())
    assert not cs.is_struck(sk_row, set())
    assert cs.is_struck(sk_row, {"aino äärelä"})
    assert not cs.is_struck(label_row, {"warm-up group 1"})


@pytest.mark.parametrize("stamps, expected", [
    ([], "Times as per the source documents."),
    ([datetime(2026, 9, 18, 9, 8)], "Times as per the source documents. Sources printed 18.09.2026 9.08."),
    ([datetime(2026, 9, 18, 9, 9), datetime(2026, 9, 18, 9, 8)],
     "Times as per the source documents. Sources printed 18.09.2026 9.08–9.09."),
    ([datetime(2026, 9, 18, 13, 6), datetime(2026, 9, 19, 19, 9)],
     "Times as per the source documents. Sources printed 18.09.2026 13.06 – 19.09.2026 19.09."),
])
def test_printed_note(stamps, expected):
    assert cs._printed_note(stamps) == expected


# --- processor -------------------------------------------------------------

@pytest.mark.parametrize("options, expected", [
    ({}, False),
    ({"globalCombinedSchedule": False}, False),
    ({"globalCombinedSchedule": "true"}, False),
    ({"globalCombinedSchedule": True}, True),
    ({"globalCombinedSchedule": True, "globalOmitSegmentCover": True}, True),
])
def test_combined_schedule_enabled(options, expected):
    assert combined_schedule_enabled(options) is expected


def make_panel(path, people):
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Helvetica", 10)
    y = 700
    for role, name in people:
        c.drawString(40, y, role)
        c.drawString(250, y, name)
        y -= 16
    c.save()


@pytest.fixture
def competition(tmp_path, monkeypatch):
    """Two segments on one day; a referee on the first, a judge on both."""
    monkeypatch.setattr(processor, "_get_categories_for_processor", lambda: [])
    src = tmp_path / "src"
    src.mkdir()
    make_start_list(src / "FSKWSINGLES-KJUNIW----QUAL000100--_StartListwithTimes.pdf",
                    "JUNIORI NAISET  SHORT PROGRAM", [SP_ROWS], PARAMS_SP)
    make_start_list(src / "FSKWSINGLES-KSENIW----QUAL000100--_StartListwithTimes.pdf",
                    "SENIORI NAISET  SHORT PROGRAM",
                    [[skater("18.50.30", "18.56.25", 1, "Fanni FORS", "MJT")]], PARAMS_FS)
    make_panel(src / "FSKWSINGLES-KJUNIW----QUAL000100--_ISUPanelofJudgesandTechnicalPanel.pdf",
               [("Referee", "Ms. Rea REFEREE"), ("Judge No.1", "Mr. Jon JUDGE")])
    make_panel(src / "FSKWSINGLES-KSENIW----QUAL000100--_ISUPanelofJudgesandTechnicalPanel.pdf",
               [("Judge No.1", "Mr. Jon JUDGE")])
    return src, tmp_path / "out"


def packet_pages(out_dir, slug):
    with zipfile.ZipFile(out_dir / "judgingPapers_20260919.zip") as zf:
        zf.extract(f"judgingpapers_20260919_{slug}.pdf", out_dir / "x")
    path = str(out_dir / "x" / f"judgingpapers_20260919_{slug}.pdf")
    return [page.extract_text() for page in PdfReader(path).pages]


def test_packet_gets_the_combined_schedule_after_the_cover(competition):
    src, out = competition

    stats = processor.process_judging_papers(str(src), str(out), {"globalCombinedSchedule": True})

    pages = packet_pages(out, "jon_judge")
    # cover, combined schedule, then the two segments' time schedules
    assert len(pages) == 4
    assert "Jon JUDGE" in pages[0]
    assert "Time schedule and starting order | Saturday 19.09.2026" in pages[1]
    assert "Fanni FORS" in pages[1] and "Pihla KUULA / Axel KANERVA" in pages[1]
    assert "TIME SCHEDULE" in pages[2]
    assert stats["combined_schedule"] is True
    assert (src / "combinedschedule_20260919.pdf").exists()


def test_packet_unchanged_without_the_option(competition):
    src, out = competition

    stats = processor.process_judging_papers(str(src), str(out), {})

    pages = packet_pages(out, "jon_judge")
    assert len(pages) == 3
    assert not any("Time schedule and starting order" in p for p in pages)
    assert stats["combined_schedule"] is False
    assert not (src / "combinedschedule_20260919.pdf").exists()


def test_combined_schedule_with_omitted_segment_pages(competition):
    src, out = competition

    processor.process_judging_papers(str(src), str(out), {"globalCombinedSchedule": True,
                                                          "globalOmitSegmentCover": True})

    pages = packet_pages(out, "jon_judge")
    assert len(pages) == 2
    assert "Time schedule and starting order" in pages[1]
