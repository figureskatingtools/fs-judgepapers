"""The referee's CalculationSetupVerificationforReferee in the personal packets:
once per referee per category (their first segment of it, across days) by
default, in every segment with globalCalculationSetupEverySegment."""
import pytest

import processor
from processor import calculation_setup_every_segment, calculation_setup_prefix

CATEGORIES = [
    {"abbreviation": "FSKWSINGLES-KJUNIW", "displayName": "Junior Women",
     "displayNameFi": "Juniorinaiset", "competitionType": "Singles"},
    {"abbreviation": "FSKWSINGLES-KSENIW", "displayName": "Senior Women",
     "displayNameFi": "Seniorinaiset", "competitionType": "Singles"},
]
JUN_QUAL = "FSKWSINGLES-KJUNIW----QUAL000100--"
JUN_FNL = "FSKWSINGLES-KJUNIW----FNL0000100--"
SEN_QUAL = "FSKWSINGLES-KSENIW----QUAL000100--"
SEN_FNL = "FSKWSINGLES-KSENIW----FNL0000100--"
JUN_SETUP = "FSKWSINGLES-KJUNIW----------------_CalculationSetupVerificationforReferee.pdf"
SEN_SETUP = "FSKWSINGLES-KSENIW----------------_CalculationSetupVerificationforReferee.pdf"


@pytest.mark.parametrize("options, expected", [
    ({}, False),
    ({"globalCalculationSetupEverySegment": False}, False),
    ({"globalCalculationSetupEverySegment": True}, True),
    ({"globalCalculationSetupEverySegment": "true"}, False),
    ({"globalCalculationSetupEverySegment": 1}, False),
])
def test_every_segment_needs_a_real_true(options, expected):
    assert calculation_setup_every_segment(options) is expected


@pytest.mark.parametrize("prefix, expected", [
    (JUN_QUAL, "FSKWSINGLES-KJUNIW----------------"),
    (JUN_FNL, "FSKWSINGLES-KJUNIW----------------"),
    # A split group keeps its two digits: one setup per group
    ("FSKWSINGLES-KJUNIW--01QUAL000100", "FSKWSINGLES-KJUNIW--01----------"),
])
def test_setup_prefix(prefix, expected):
    assert calculation_setup_prefix(prefix, CATEGORIES) == expected


@pytest.fixture
def run(tmp_path, monkeypatch):
    """
    Runs process_judging_papers over empty placeholder files and returns
    {packet file name: [merged file names]}. segments maps prefix ->
    (date YYYYMMDD, start time, [(role, name)]).
    """
    def _run(segments, options):
        src = tmp_path / "src"
        src.mkdir()
        for prefix in segments:
            (src / f"{prefix}_StartListwithTimes.pdf").touch()
            (src / f"{prefix}_ISUPanelofJudgesandTechnicalPanel.pdf").touch()

        def prefix_of(path):
            return path.rsplit("/", 1)[-1].split("_", 1)[0]

        packets = {}

        def merge(output_path, file_list):
            packets[output_path.rsplit("/", 1)[-1]] = [p.rsplit("/", 1)[-1] for p in file_list]
            return len(file_list)

        monkeypatch.setattr(processor, "_get_categories_for_processor", lambda: CATEGORIES)
        monkeypatch.setattr(processor, "split_pdf", lambda path: [])
        monkeypatch.setattr(processor, "extract_segment_name_from_pdf", lambda path: None)
        monkeypatch.setattr(processor, "extract_title_from_pdf", lambda path: "Competition")
        monkeypatch.setattr(processor, "create_cover_pdf", lambda *a, **k: None)
        monkeypatch.setattr(processor, "get_date_from_start_list",
                            lambda path: segments[prefix_of(path)][0])
        monkeypatch.setattr(processor, "get_first_start_time_from_start_list",
                            lambda path: segments[prefix_of(path)][1])
        monkeypatch.setattr(processor, "get_panel_info",
                            lambda path: segments[prefix_of(path)][2])
        monkeypatch.setattr(processor, "merge_pdfs", merge)

        processor.process_judging_papers(str(src), str(tmp_path / "out"), options)
        return packets

    return _run


def setups(files):
    return [f for f in files if f.endswith("_CalculationSetupVerificationforReferee.pdf")]


def test_same_referee_gets_the_setup_once_per_category(run):
    packets = run({
        JUN_QUAL: ("20261107", "10:00", [("referee", "Rea REFEREE")]),
        JUN_FNL: ("20261107", "15:00", [("referee", "Rea REFEREE")]),
    }, {})

    files = packets["judgingpapers_20261107_rea_referee.pdf"]
    assert setups(files) == [JUN_SETUP]
    # In the first segment, right before its referee sheet
    assert files.index(JUN_SETUP) == files.index(f"{JUN_QUAL}_RefereeSheet.pdf") - 1
    # The later segment still has its referee sheet and judges' sheet
    assert f"{JUN_FNL}_RefereeSheet.pdf" in files
    assert f"{JUN_FNL}_JudgesSheetAll_referee_rea_referee.pdf" in files


def test_first_segment_is_the_earliest_by_start_time(run):
    packets = run({
        JUN_QUAL: ("20261107", "15:00", [("referee", "Rea REFEREE")]),
        JUN_FNL: ("20261107", "10:00", [("referee", "Rea REFEREE")]),
    }, {})

    files = packets["judgingpapers_20261107_rea_referee.pdf"]
    assert setups(files) == [JUN_SETUP]
    assert files.index(JUN_SETUP) < files.index(f"{JUN_FNL}_RefereeSheet.pdf")
    assert files.index(f"{JUN_FNL}_RefereeSheet.pdf") < files.index(f"{JUN_QUAL}_RefereeSheet.pdf")


def test_different_referees_each_get_the_setup(run):
    packets = run({
        JUN_QUAL: ("20261107", "10:00", [("referee", "Rea REFEREE")]),
        JUN_FNL: ("20261107", "15:00", [("referee", "Otto OTHER")]),
    }, {})

    assert setups(packets["judgingpapers_20261107_rea_referee.pdf"]) == [JUN_SETUP]
    assert setups(packets["judgingpapers_20261107_otto_other.pdf"]) == [JUN_SETUP]


def test_same_referee_in_two_categories_gets_both_setups(run):
    packets = run({
        JUN_QUAL: ("20261107", "10:00", [("referee", "Rea REFEREE")]),
        SEN_QUAL: ("20261107", "12:00", [("referee", "Rea REFEREE")]),
        JUN_FNL: ("20261107", "15:00", [("referee", "Rea REFEREE")]),
        SEN_FNL: ("20261107", "17:00", [("referee", "Rea REFEREE")]),
    }, {})

    assert setups(packets["judgingpapers_20261107_rea_referee.pdf"]) == [JUN_SETUP, SEN_SETUP]


def test_once_per_category_across_days(run):
    packets = run({
        JUN_FNL: ("20261108", "09:00", [("referee", "Rea REFEREE")]),
        JUN_QUAL: ("20261107", "18:00", [("referee", "Rea REFEREE")]),
    }, {})

    assert setups(packets["judgingpapers_20261107_rea_referee.pdf"]) == [JUN_SETUP]
    assert setups(packets["judgingpapers_20261108_rea_referee.pdf"]) == []


def test_every_segment_option_keeps_the_old_behaviour(run):
    packets = run({
        JUN_QUAL: ("20261107", "10:00", [("referee", "Rea REFEREE")]),
        JUN_FNL: ("20261108", "15:00", [("referee", "Rea REFEREE")]),
        SEN_QUAL: ("20261107", "12:00", [("referee", "Rea REFEREE")]),
        SEN_FNL: ("20261107", "17:00", [("referee", "Rea REFEREE")]),
    }, {"globalCalculationSetupEverySegment": True})

    assert setups(packets["judgingpapers_20261107_rea_referee.pdf"]) == [JUN_SETUP, SEN_SETUP, SEN_SETUP]
    assert setups(packets["judgingpapers_20261108_rea_referee.pdf"]) == [JUN_SETUP]


def test_a_judge_never_gets_the_setup(run):
    packets = run({
        JUN_QUAL: ("20261107", "10:00", [("referee", "Rea REFEREE"), ("judge", "Jon JUDGE")]),
        JUN_FNL: ("20261107", "15:00", [("judge", "Rea REFEREE"), ("referee", "Jon JUDGE")]),
    }, {})

    # Jon referees only the final: his first segment of the category as referee
    assert setups(packets["judgingpapers_20261107_jon_judge.pdf"]) == [JUN_SETUP]
    assert setups(packets["judgingpapers_20261107_rea_referee.pdf"]) == [JUN_SETUP]
