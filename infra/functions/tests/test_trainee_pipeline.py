"""Trainees end to end (issue #93): the real pipeline over real PDFs, from the
JudgesSheetAll split to the stamped copies in the personal packet and the day
document's check block.

The sources are synthetic: the StartLists and panels of test_combined_schedule
and the FSM-like judge and specialist pages of test_trainee_sheets. The stamp
lives in a Form XObject, so the stamped pages are read with pdfminer (pypdf's
layout mode does not see it).
"""
import zipfile

import pytest
from pdfminer.high_level import extract_text
from pypdf import PdfReader
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

import processor
from test_combined_schedule import PARAMS_FS, PARAMS_SP, SP_ROWS, make_panel, make_start_list, skater
from test_trainee_sheets import NAME_X, ROLE_X, ROLE_Y, RULE_DY, RULE_X0, RULE_X1, SIZE, _body, _rule, _titles
from test_trainee_sheets import judge_page, specialist_page

DATE = "20260919"
JUN = "FSKWSINGLES-KJUNIW----QUAL000100--"
SEN = "FSKWSINGLES-KSENIW----QUAL000100--"
TRAINEE = "Åsa Ärölä"
SLUG = "asa_arola"
CHECK = "RESULTS SERVICE CHECK"


def referee_page(c):
    """As test_trainee_sheets.referee_page, with the panel's spelling of the referee."""
    _titles(c)
    c.setFont("Helvetica-Bold", SIZE)
    c.drawString(ROLE_X, ROLE_Y, "Referee")
    c.drawString(NAME_X, ROLE_Y, "Ms. Rea REFEREE")
    _rule(c, RULE_X0, RULE_X1, ROLE_Y - RULE_DY)
    _body(c)


def make_pdf(path, *pages):
    c = canvas.Canvas(str(path), pagesize=A4)
    for draw in pages:
        draw(c)
        c.showPage()
    c.save()


@pytest.fixture
def competition(tmp_path, monkeypatch):
    """
    One day, two segments. JUN: a referee and two judges, with a real
    JudgesSheetAll (the referee's pages, then two pages per judge). SEN: one
    judge and a Technical Specialist with a two-page sheet.
    """
    monkeypatch.setattr(processor, "_get_categories_for_processor", lambda: [])
    src = tmp_path / "src"
    src.mkdir()
    make_start_list(src / f"{JUN}_StartListwithTimes.pdf", "JUNIORI NAISET  SHORT PROGRAM", [SP_ROWS], PARAMS_SP)
    make_start_list(src / f"{SEN}_StartListwithTimes.pdf", "SENIORI NAISET  SHORT PROGRAM",
                    [[skater("18.50.30", "18.56.25", 1, "Fanni FORS", "MJT")]], PARAMS_FS)
    make_panel(src / f"{JUN}_ISUPanelofJudgesandTechnicalPanel.pdf",
               [("Referee", "Ms. Rea REFEREE"), ("Judge No.1", "Mr. Jon JUDGE"), ("Judge No.2", "Ms. Jane SECOND")])
    make_panel(src / f"{SEN}_ISUPanelofJudgesandTechnicalPanel.pdf",
               [("Judge No.1", "Mr. Jon JUDGE"), ("Technical Specialist", "Ms. Tia SPECIALIST")])
    make_pdf(src / f"{JUN}_JudgesSheetAll.pdf", referee_page, referee_page,
             judge_page, judge_page,
             lambda c: judge_page(c, "Judge No. 2", "Ms. Jane SECOND"),
             lambda c: judge_page(c, "Judge No. 2", "Ms. Jane SECOND"))
    make_pdf(src / f"{SEN}_TechnicalSpecialistSheet1.pdf", specialist_page, specialist_page)
    return src, tmp_path / "out"


def packet(out_dir, slug):
    with zipfile.ZipFile(out_dir / f"judgingPapers_{DATE}.zip") as zf:
        zf.extract(f"judgingpapers_{DATE}_{slug}.pdf", out_dir / "x")
    return out_dir / "x" / f"judgingpapers_{DATE}_{slug}.pdf"


def pages_text(path):
    """Every page's text by pdfminer, which also reads the stamp's Form XObject."""
    return [extract_text(str(path), page_numbers=[i]) for i in range(len(PdfReader(str(path)).pages))]


def collapse(text):
    return " ".join(text.split())


TRAINEES = [{"name": TRAINEE, "role": "judge", "segments": [JUN]},
            {"name": TRAINEE, "role": "technical", "segments": [SEN, "FSKWSINGLES-KSENIW----FNL0000100--"]}]


def test_the_trainee_gets_a_packet_of_stamped_copies(competition, capsys):
    src, out = competition

    stats = processor.process_judging_papers(str(src), str(out), {"trainees": TRAINEES})

    pages = [collapse(t) for t in pages_text(packet(out, SLUG))]
    # cover, JUN time schedule, Judge No. 1's two pages, SEN time schedule, the TS's two pages
    assert len(pages) == 7
    cover = pages[0]
    assert TRAINEE in cover and "Judge Trainee" in cover and "Technical Trainee" in cover
    assert cover.index("Judge Trainee") < cover.index("Technical Trainee")  # running order
    assert "TIME SCHEDULE" in pages[1] and "JUNIORI NAISET" in pages[1]
    for page in pages[2:4]:
        # pdfminer runs the label and the name cell together
        assert "Judge Trainee" in page and TRAINEE in page
        assert "Aino AAVIKKO" in page          # Jon's sheet underneath
    assert "TIME SCHEDULE" in pages[4] and "SENIORI NAISET" in pages[4]
    for page in pages[5:7]:
        assert f"Technical Trainee: {TRAINEE}" in page
    assert CHECK not in " ".join(pages)

    # The judges keep their own, unstamped sheets
    jon = [collapse(t) for t in pages_text(packet(out, "jon_judge"))]
    assert not any("Trainee" in page for page in jon)
    assert sum("Jon JUDGE" in page for page in jon) >= 3   # cover + two sheet pages

    assert stats["trainee_count"] == 1
    assert stats["trainee_assignment_count"] == 2
    assert stats["trainee_sheets_unstamped"] == 0
    assert stats["unique_official_count"] == 4
    logs = capsys.readouterr().out
    assert "check block says" not in logs and "could not be stamped" not in logs
    assert TRAINEE not in logs and SLUG not in logs
    # The trainee copies stay clear of the JudgesSheetAll / StartList walkers
    assert not any(p.name.endswith(("_JudgesSheetAll.pdf", "_StartListwithTimes.pdf"))
                   for p in src.iterdir() if "_trainee_" in p.name)


def test_the_day_document_counts_the_trainee(competition, capsys):
    src, out = competition

    processor.process_judging_papers(str(src), str(out), {"trainees": TRAINEES})

    day = [collapse(t) for t in pages_text(out / f"judgingpapers_{DATE}.pdf")]
    # In name order (by slug): Åsa's packet first, so her cover carries the check block
    first = day[0]
    assert TRAINEE in first and f"{CHECK} · Saturday 19.09.2026" in first
    assert "Packets 5: 2 judges · 1 referee · 1 technical specialist · 1 trainee" in first
    assert f"Pages {len(day)} · judgingpapers_{DATE}.pdf" in first
    assert len(day) == sum(len(PdfReader(str(packet(out, slug))).pages)
                           for slug in (SLUG, "jane_second", "jon_judge", "rea_referee", "tia_specialist"))
    # Every packet's cover, in slug order
    covers = [i for i, page in enumerate(day) if "19.09.2026" in page]
    names = [next(n for n in (TRAINEE, "Jane SECOND", "Jon JUDGE", "Rea REFEREE", "Tia SPECIALIST")
                  if n in day[i]) for i in covers]
    assert names == [TRAINEE, "Jane SECOND", "Jon JUDGE", "Rea REFEREE", "Tia SPECIALIST"]
    assert sum("Judge Trainee" in page for page in day) == 3       # cover + two stamped pages
    assert "check block says" not in capsys.readouterr().out


def test_an_unreadable_ts_sheet_costs_the_trainee_that_segment_only(competition, capsys):
    """JUN has no Technical Specialist on its panel, so no official's packet reads its
    TS sheet: a broken one used to fail the whole run in the withdrawn-page filter."""
    src, out = competition
    (src / f"{JUN}_TechnicalSpecialistSheet1.pdf").write_bytes(b"%PDF-1.4 this is not a pdf at all")
    trainees = [{"name": TRAINEE, "role": "judge", "segments": [JUN]},
                {"name": TRAINEE, "role": "technical", "segments": [JUN]}]

    stats = processor.process_judging_papers(str(src), str(out), {"trainees": trainees})

    assert stats["trainee_segments_without_sheets"] == 1
    assert stats["trainee_sheets_unstamped"] == 0
    with zipfile.ZipFile(out / f"judgingPapers_{DATE}.zip") as zf:
        assert sorted(zf.namelist()) == [f"judgingpapers_{DATE}_{slug}.pdf" for slug in
                                         (SLUG, "jane_second", "jon_judge", "rea_referee", "tia_specialist")]
    pages = [collapse(t) for t in pages_text(packet(out, SLUG))]
    # cover, time schedule + Judge No. 1's two stamped pages, time schedule (no TS sheet)
    assert len(pages) == 5
    assert sum("Judge Trainee" in page and TRAINEE in page for page in pages[1:]) == 2
    assert not any("Technical Trainee:" in page for page in pages)
    # The specialist on SEN still gets her own sheets
    assert any("Technical Specialist:" in collapse(t) for t in pages_text(packet(out, "tia_specialist")))
    logs = capsys.readouterr().out
    assert "can't be read" in logs and TRAINEE not in logs and SLUG not in logs


def test_without_trainees_nothing_changes(competition):
    src, out = competition

    stats = processor.process_judging_papers(str(src), str(out), {})

    with zipfile.ZipFile(out / f"judgingPapers_{DATE}.zip") as zf:
        assert sorted(zf.namelist()) == [f"judgingpapers_{DATE}_{slug}.pdf" for slug in
                                         ("jane_second", "jon_judge", "rea_referee", "tia_specialist")]
    day = [collapse(t) for t in pages_text(out / f"judgingpapers_{DATE}.pdf")]
    assert not any("Trainee" in page for page in day)
    assert "Packets 4: 2 judges · 1 referee · 1 technical specialist" in day[0]
    assert stats["trainee_count"] == 0 and stats["trainee_assignment_count"] == 0
    assert not any("_trainee_" in p.name for p in src.iterdir())
