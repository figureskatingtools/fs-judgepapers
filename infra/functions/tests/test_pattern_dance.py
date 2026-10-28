"""Pattern Dance segments leave out the Data / Replay Operators' planned program
content and the Technical Specialist 1 sheet by default (issue #72);
globalPatternDanceAllPapers keeps them. The time schedule stays either way."""
import pytest

import processor
from combined_schedule import split_title
from processor import is_pattern_dance, pattern_dance_all_papers

ICE_DANCE = {"abbreviation": "FSKXICEDANCE-ADVNOV", "displayName": "Advanced Novice Ice Dance",
             "displayNameFi": "Jäätanssi, Advanced Novice", "competitionType": "Figure skating",
             "judgingMethod": "ISU"}
PD = "FSKXICEDANCE-ADVNOV-QUAL0001PK"
FD = "FSKXICEDANCE-ADVNOV-FNL000100--"
LINES = {
    PD: "JÄÄTANSSI, ADVANCED NOVICE PDK1 (STARLIGHT WALTZ)",
    FD: "JÄÄTANSSI, ADVANCED NOVICE FREE DANCE",
}
PANEL = [("referee", "Rea REFEREE"), ("judge", "Jon JUDGE"),
         ("technical_controller", "Tia CONTROLLER"),
         ("technical_specialist_1", "Taru SPECIALIST"), ("technical_specialist_2", "Toni SECOND"),
         ("data_operator", "Dan OPERATOR"), ("replay_operator", "Rasmus REPLAY")]


@pytest.mark.parametrize("name, expected", [
    ("PDK1 (Starlight Waltz)", True),
    ("PDK2 (TANGO)", True),
    ("PD1", True),
    ("Pattern Dance 1", True),
    ("PATTERN DANCE (Tango Romantica)", True),
    ("JÄÄTANSSI PDK1 (STARLIGHT WALTZ)", True),  # an unsplit line
    ("FREE DANCE", False),
    ("RHYTHM DANCE", False),
    ("SHORT PROGRAM", False),
    ("UPDATE 1", False),
    ("", False),
    (None, False),
])
def test_is_pattern_dance(name, expected):
    assert is_pattern_dance(name) is expected


@pytest.mark.parametrize("options, expected", [
    ({}, False),
    ({"globalPatternDanceAllPapers": True}, True),
    ({"globalPatternDanceAllPapers": "true"}, False),
])
def test_all_papers_needs_a_real_true(options, expected):
    assert pattern_dance_all_papers(options) is expected


def test_a_pdk_segment_splits_from_an_unknown_category():
    assert split_title("ISTANSS PDK1 (STARLIGHT WALTZ)") == ("ISTANSS", "PDK1 (STARLIGHT WALTZ)")


@pytest.fixture
def run(tmp_path, monkeypatch):
    """Both segments on one day with the full PANEL; returns
    {packet file name: [merged file names]}."""
    def _run(options):
        src = tmp_path / "src"
        src.mkdir()
        for prefix in LINES:
            for suffix in ("StartListwithTimes", "ISUPanelofJudgesandTechnicalPanel", "JudgesSheetAll",
                           "TechnicalControllerSheet", "TechnicalSpecialistSheet1",
                           "TechnicalSpecialistSheet2"):
                (src / f"{prefix}_{suffix}.pdf").touch()

        def prefix_of(path):
            return path.rsplit("/", 1)[-1].split("_", 1)[0]

        packets = {}

        def merge(output_path, file_list):
            packets[output_path.rsplit("/", 1)[-1]] = [p.rsplit("/", 1)[-1] for p in file_list]
            return len(file_list)

        monkeypatch.setattr(processor, "_get_categories_for_processor", lambda: [ICE_DANCE])
        monkeypatch.setattr(processor, "split_pdf", lambda path: [])
        monkeypatch.setattr(processor, "extract_segment_name_from_pdf", lambda path: LINES[prefix_of(path)])
        monkeypatch.setattr(processor, "extract_title_from_pdf", lambda path: "Competition")
        monkeypatch.setattr(processor, "create_cover_pdf", lambda *a, **k: None)
        monkeypatch.setattr(processor, "get_date_from_start_list", lambda path: "20261107")
        monkeypatch.setattr(processor, "get_first_start_time_from_start_list",
                            lambda path: "10:00" if prefix_of(path) == PD else "15:00")
        monkeypatch.setattr(processor, "get_panel_info", lambda path: PANEL)
        monkeypatch.setattr(processor, "filter_withdrawn_pages", lambda src, dst: 0)
        monkeypatch.setattr(processor, "merge_pdfs", merge)

        processor.process_judging_papers(str(src), str(tmp_path / "out"), options)
        return packets

    return _run


def packet(packets, slug):
    return packets[f"judgingpapers_20261107_{slug}.pdf"]


@pytest.mark.parametrize("slug, paper", [
    ("dan_operator", "PlannedProgramContent.pdf"),
    ("rasmus_replay", "PlannedProgramContent.pdf"),
    ("taru_specialist", "TechnicalSpecialistSheet1_nowd.pdf"),
])
def test_pattern_dance_leaves_out_do_ro_ts1_papers(run, slug, paper):
    files = packet(run({}), slug)

    assert f"{PD}_{paper}" not in files
    assert f"{FD}_{paper}" in files
    # The PD's time schedule stays, in running order before the FD's
    assert files.index(f"{PD}_StartListwithTimes.pdf") < files.index(f"{FD}_StartListwithTimes.pdf")


def test_other_officials_keep_their_pattern_dance_papers(run):
    packets = run({})

    assert f"{PD}_TechnicalSpecialistSheet2_nowd.pdf" in packet(packets, "toni_second")
    assert f"{PD}_TechnicalControllerSheet_nowd.pdf" in packet(packets, "tia_controller")
    assert f"{PD}_JudgesSheetAll_judge_jon_judge.pdf" in packet(packets, "jon_judge")
    assert f"{PD}_RefereeSheet.pdf" in packet(packets, "rea_referee")


@pytest.mark.parametrize("slug, paper", [
    ("dan_operator", "PlannedProgramContent.pdf"),
    ("rasmus_replay", "PlannedProgramContent.pdf"),
    ("taru_specialist", "TechnicalSpecialistSheet1_nowd.pdf"),
])
def test_all_papers_option_keeps_them(run, slug, paper):
    files = packet(run({"globalPatternDanceAllPapers": True}), slug)

    assert f"{PD}_{paper}" in files
    assert f"{FD}_{paper}" in files
