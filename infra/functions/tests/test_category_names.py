"""Category names as FSM printed them on line 2 of the uploaded JudgesSheetAll,
in the competition's own language: the categories table's names only locate
the category on that line (categories.names_from_sheet_line), and nothing
switches the papers or the site to the table's English names."""
import io

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

import function_app as fa
import processor
from categories import names_from_sheet_line
from conftest import PLATFORM_ID, call, make_request, payload, put_competition
from fakes import FakeContainerClient

JUN = {"abbreviation": "FSKWSINGLES-KJUNIW", "displayName": "Junior Women",
       "displayNameFi": "Juniorinaiset", "competitionType": "Figure skating",
       "judgingMethod": "ISU"}
ASILM = {"abbreviation": "FSKWSINGLES-ASILMW", "displayName": "A-Silmut, Girls",
         "displayNameFi": "A-Silmut, Tytöt", "competitionType": "Figure skating",
         "judgingMethod": "ISU"}
JUN_ISU = {"abbreviation": "FSKWSINGLES-JUNISU", "displayName": "Junior Women (ISU)",
           "displayNameFi": "Juniorinaiset (ISU)", "competitionType": "Figure skating",
           "judgingMethod": "ISU"}
NOV_JR = {"abbreviation": "FSKWSINGLES-NOVJR", "displayName": "Novice Girls Jr.",
          "displayNameFi": "Noviisitytöt Jr.", "competitionType": "Figure skating",
          "judgingMethod": "ISU"}
CATEGORIES = [JUN, ASILM]


@pytest.mark.parametrize("line, category, split, expected", [
    # Finnish table name, printed in capitals
    ("JUNIORINAISET SHORT PROGRAM", JUN, None, ("JUNIORINAISET", "SHORT PROGRAM")),
    # The English table name locates it just as well; the printed text is kept
    ("Junior Women Free Skating", JUN, None, ("Junior Women", "Free Skating")),
    # Punctuation and spacing differences
    ("A-SILMUT TYTÖT VAPAAOHJELMA", ASILM, None, ("A-SILMUT TYTÖT", "VAPAAOHJELMA")),
    ("A-Silmut, Tytöt, Free Skating", ASILM, None, ("A-Silmut, Tytöt", "Free Skating")),
    # A split group: '#N' kept when printed, added from the prefix otherwise
    ("JUNIORINAISET #2 SHORT PROGRAM", JUN, 2, ("JUNIORINAISET #2", "SHORT PROGRAM")),
    ("JUNIORINAISET SHORT PROGRAM", JUN, 1, ("JUNIORINAISET #1", "SHORT PROGRAM")),
    # Another language than the table's: split before a known segment name
    ("JUNIORDAMER SHORT PROGRAM", JUN, None, ("JUNIORDAMER", "SHORT PROGRAM")),
    # ... or at a wide gap, with no table row at all
    ("JUNIORDAMER   KORTPROGRAM", None, None, ("JUNIORDAMER", "KORTPROGRAM")),
    # Closing punctuation stays with the category, before a '#N' too
    ("JUNIOR WOMEN (ISU) #2 SHORT PROGRAM", JUN_ISU, 2, ("JUNIOR WOMEN (ISU) #2", "SHORT PROGRAM")),
    ("JUNIORINAISET (ISU) SHORT PROGRAM", JUN_ISU, None, ("JUNIORINAISET (ISU)", "SHORT PROGRAM")),
    ("JUNIORINAISET (ISU) SHORT PROGRAM", JUN_ISU, 1, ("JUNIORINAISET (ISU) #1", "SHORT PROGRAM")),
    ("NOVIISITYTÖT JR. VAPAAOHJELMA", NOV_JR, None, ("NOVIISITYTÖT JR.", "VAPAAOHJELMA")),
    ("A-SILMUT, TYTÖT, VAPAAOHJELMA", ASILM, None, ("A-SILMUT, TYTÖT", "VAPAAOHJELMA")),
    # The table name must end on a word boundary
    ("JUNIOR WOMENS  FREE SKATING", JUN, None, ("JUNIOR WOMENS", "FREE SKATING")),
    # Nothing to split on: no category, the whole line is the segment
    ("JUNIORDAMER", JUN, None, (None, "JUNIORDAMER")),
    ("", JUN, None, (None, None)),
])
def test_names_from_sheet_line(line, category, split, expected):
    assert names_from_sheet_line(line, category, split) == expected


# --- the generated papers ----------------------------------------------------

PREFIX = "FSKWSINGLES-KJUNIW----QUAL000100--"


@pytest.fixture
def generate(tmp_path, monkeypatch):
    """Runs process_judging_papers with one segment whose JudgesSheetAll's
    line 2 is `line`; returns (segment cover category name, stats)."""
    def _generate(line, options):
        src = tmp_path / "src"
        src.mkdir()
        for suffix in ("StartListwithTimes", "ISUPanelofJudgesandTechnicalPanel", "JudgesSheetAll"):
            (src / f"{PREFIX}_{suffix}.pdf").touch()
        covers = []
        monkeypatch.setattr(processor, "_get_categories_for_processor", lambda: CATEGORIES)
        monkeypatch.setattr(processor, "split_pdf", lambda path: [])
        monkeypatch.setattr(processor, "extract_segment_name_from_pdf", lambda path: line)
        monkeypatch.setattr(processor, "extract_title_from_pdf", lambda path: None)
        monkeypatch.setattr(processor, "create_cover_pdf", lambda *a, **k: None)
        monkeypatch.setattr(processor, "get_date_from_start_list", lambda path: "20261107")
        monkeypatch.setattr(processor, "get_first_start_time_from_start_list", lambda path: "10:00")
        monkeypatch.setattr(processor, "get_panel_info", lambda path: [("judge", "Jon JUDGE")])
        monkeypatch.setattr(processor, "create_segment_cover_pdf",
                            lambda path, title, dt, seg, cat, withdrawn: covers.append((title, seg, cat)))
        monkeypatch.setattr(processor, "merge_pdfs", lambda out, files: len(files))
        stats = processor.process_judging_papers(str(src), str(tmp_path / "out"), options)
        return covers, stats
    return _generate


def test_the_segment_cover_uses_the_printed_name(generate):
    covers, stats = generate("JUNIORINAISET  SHORT PROGRAM", {"globalSegmentCover": True})

    # No title on the sheet: the printed category stands in for it too
    assert covers == [("JUNIORINAISET", "SHORT PROGRAM", "JUNIORINAISET")]
    assert stats["categories"] == ["JUNIORINAISET"]
    assert "language" not in stats


def test_a_language_option_changes_nothing(generate):
    covers, _ = generate("JUNIORINAISET  SHORT PROGRAM", {"globalSegmentCover": True, "language": "en"})

    assert covers[0][2] == "JUNIORINAISET"


def test_unreadable_line_falls_back_to_the_finnish_table_name(generate):
    covers, _ = generate("JUNIORINAISET", {"globalSegmentCover": True})

    assert covers[0][2] == "Juniorinaiset"


# --- the site's file list (get_competition_details) --------------------------

def sheet_pdf(*lines):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for i, line in enumerate(lines):
        c.drawString(60, 780 - 20 * i, line)
    c.save()
    return buf.getvalue()


@pytest.fixture
def details(tables, blobs, monkeypatch):
    monkeypatch.setattr(fa, "_get_categories", lambda: CATEGORIES)
    monkeypatch.setattr(fa, "get_platform_container_client", lambda: FakeContainerClient("pool"))
    row = put_competition(tables, comp_id="abc12345", platform_id=PLATFORM_ID)
    folder = row["FolderPath"]
    blobs.upload_blob(f"{folder}/metadata.json", '{"id": "abc12345", "language": "en"}')

    def _details(files):
        for name, data in files.items():
            blobs.upload_blob(f"{folder}/{name}", data)
        return payload(call(fa.get_competition_details, make_request(params={"id": "abc12345"})))
    return _details


def test_file_list_uses_the_printed_category_name(details):
    data = details({
        f"{PREFIX}_JudgesSheetAll.pdf": sheet_pdf("Spring Cup 2026", "JUNIORINAISET  SHORT PROGRAM"),
        f"{PREFIX}_StartListwithTimes.pdf": b"%PDF-1.4",
        "FSKWSINGLES-KJUNIW----------------_CalculationSetupVerificationforReferee.pdf": b"%PDF-1.4",
        "FSKWSINGLES-ASILMW----FNL0000100--_StartListwithTimes.pdf": b"%PDF-1.4",
    })

    # Every file of the category under the printed name, the setup included
    assert set(data["structure"]["JUNIORINAISET"]) == {"SHORT PROGRAM", "Category General"}
    assert {f["category"] for f in data["files"] if f["categoryCode"] == JUN["abbreviation"]} == {"JUNIORINAISET"}
    # A category whose sheet isn't there yet: the table's Finnish name
    assert "A-Silmut, Tytöt" in data["structure"]
    assert not any("categoryFi" in f for f in data["files"])
    # The stored language setting is gone from the response
    assert "language" not in data


# --- usage statistics ----------------------------------------------------------

def category_stats(prefix_category_names):
    tasks = [(prefix, "judge") for prefix in prefix_category_names]
    stats = processor._compute_statistics(
        CATEGORIES, [], {"20261107": {"jon_judge": {"tasks": tasks}}}, {},
        prefix_category_names, total_pages=0)
    return stats["categories"], stats["category_count"]


def test_a_category_is_counted_once_with_its_printed_name():
    # The FNL's line couldn't be read: its table name doesn't count separately
    assert category_stats({
        PREFIX: "JUNIORINAISET",
        "FSKWSINGLES-KJUNIW----FNL0000100--": None,
    }) == (["JUNIORINAISET"], 1)


def test_split_groups_count_as_one_category():
    assert category_stats({
        "FSKWSINGLES-KJUNIW----QUAL000110--": "JUNIORINAISET #1",
        "FSKWSINGLES-KJUNIW----QUAL000120--": "JUNIORINAISET #2",
        "FSKWSINGLES-ASILMW----FNL0000100--": None,
    }) == (["A-Silmut, Tytöt", "JUNIORINAISET"], 2)
