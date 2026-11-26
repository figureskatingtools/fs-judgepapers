"""Custom generate (issue #85): the papers index a generate run writes
(processor.write_papers_index), part by part.

The placeholder harness of test_trainee_packets shows what becomes a part
(every merged file counts as one page there); the synthetic competition of
test_trainee_pipeline runs the real pipeline, where the index must agree with
the PDFs it describes and a cut by it must give exactly the packets' pages.
"""
import json
import re
import zipfile

import pytest
from pypdf import PdfReader

import custom_papers
import processor
from test_calculation_setup import CATEGORIES, JUN_FNL, JUN_QUAL
from test_trainee_packets import DATE, FD, PANEL, PD, SLUG, TRAINEE, judge, run  # noqa: F401 (run: fixture)
from test_trainee_pipeline import DATE as REAL_DATE, JUN, SEN, TRAINEES, collapse, competition, pages_text  # noqa: F401


def people_of(index, date=DATE):
    day = next(day for day in index["days"] if day["date"] == date)
    return {person["slug"]: person for person in day["people"]}


def parts_of(person):
    return [(part["prefix"], part["role"], part["pages"], part["flags"]) for part in person["parts"]]


# --- what becomes a part (placeholder files: one page each) --------------------

def test_every_packet_is_listed_part_by_part(run):
    index = run({}).index

    assert index["version"] == custom_papers.INDEX_VERSION
    day, = index["days"]
    assert day["date"] == DATE and day["file"] == f"judgingpapers_{DATE}.pdf" and day["schedule"] is None
    assert day["zip"]["file"] == f"judgingPapers_{DATE}.zip" and day["zip"]["size"] > 0
    assert re.fullmatch(r"[0-9a-f]{64}", day["zip"]["sha256"])
    # the day document's order
    assert [person["slug"] for person in day["people"]] == sorted(processor.slugify(name) for _, name, _ in PANEL)

    people = people_of(index)
    jon = people["jon_judge"]
    assert (jon["name"], jon["file"], jon["cover"]) == ("Jon JUDGE", f"judgingpapers_{DATE}_jon_judge.pdf", 1)
    # running order; each part = the time schedule before the segment + the sheets
    assert [(part["prefix"], part["role"], part["label"], part["pages"], part["intro"]) for part in jon["parts"]] == [
        (PD, "judge", "Judge 1", 2, 1), (FD, "judge", "Judge 1", 2, 1)]
    # the referee: calculation setup, referee sheet and their judges' sheet after the intro page
    assert parts_of(people["rea_referee"])[0] == (PD, "referee", 4, ["calculationSetup"])


def test_the_pages_add_up_to_the_packets_and_the_day_document(run):
    day, = run({}).index["days"]

    for person in day["people"]:
        assert person["cover"] + sum(part["pages"] for part in person["parts"]) == person["pages"]
    assert sum(person["pages"] for person in day["people"]) == day["pages"]


def test_the_segments_are_named_as_the_papers_print_them(run):
    index = run({}).index

    assert index["segments"] == {
        PD: {"date": DATE, "start": "10:00", "end": None,
             "category": "JÄÄTANSSI, ADVANCED NOVICE", "segment": "PDK1 (STARLIGHT WALTZ)"},
        FD: {"date": DATE, "start": "15:00", "end": None,
             "category": "JÄÄTANSSI, ADVANCED NOVICE", "segment": "FREE DANCE"},
    }


def test_a_pattern_dance_part_without_papers_is_the_intro_page_only(run):
    people = people_of(run({}).index)

    for slug, role in (("dan_operator", "data_operator"), ("rasmus_replay", "replay_operator"),
                       ("taru_specialist", "technical_specialist_1")):
        assert parts_of(people[slug]) == [(PD, role, 1, ["patternDanceSkipped"]), (FD, role, 2, [])]


def test_without_an_intro_page_such_a_part_has_no_pages(run):
    people = people_of(run({"globalOmitSegmentCover": True}).index)

    assert parts_of(people["dan_operator"]) == [
        (PD, "data_operator", 0, ["patternDanceSkipped"]), (FD, "data_operator", 1, [])]
    assert people["dan_operator"]["pages"] == 2   # the cover and the FD papers
    assert people["dan_operator"]["parts"][1]["intro"] == 0


@pytest.fixture
def referee_index(tmp_path, monkeypatch):
    """The index of a run over segments {prefix: (date, start time, panel)} of
    test_calculation_setup's categories, placeholder files as above."""
    def _index(segments, options):
        src = tmp_path / "src"
        src.mkdir()
        for prefix in segments:
            (src / f"{prefix}_StartListwithTimes.pdf").touch()
            (src / f"{prefix}_ISUPanelofJudgesandTechnicalPanel.pdf").touch()

        def prefix_of(path):
            return path.rsplit("/", 1)[-1].split("_", 1)[0]

        pages_of = {}

        def merge(output_path, file_list, page_counts=None):
            open(output_path, "wb").close()
            counts = [pages_of.get(path, 1) for path in file_list]
            if page_counts is not None:
                page_counts.extend(counts)
            pages_of[output_path] = sum(counts)
            return sum(counts)

        monkeypatch.setattr(processor, "_get_categories_for_processor", lambda: CATEGORIES)
        monkeypatch.setattr(processor, "split_pdf", lambda path: [])
        monkeypatch.setattr(processor, "extract_segment_name_from_pdf", lambda path: None)
        monkeypatch.setattr(processor, "extract_title_from_pdf", lambda path: "Competition")
        monkeypatch.setattr(processor, "create_cover_pdf", lambda path, *a, **k: open(path, "wb").close())
        monkeypatch.setattr(processor, "get_date_from_start_list", lambda path: segments[prefix_of(path)][0])
        monkeypatch.setattr(processor, "get_first_start_time_from_start_list",
                            lambda path: segments[prefix_of(path)][1])
        monkeypatch.setattr(processor, "get_last_end_time_from_start_list", lambda path: None)
        monkeypatch.setattr(processor, "get_panel_info", lambda path: segments[prefix_of(path)][2])
        monkeypatch.setattr(processor, "merge_pdfs", merge)
        internal = tmp_path / "internal"
        processor.process_judging_papers(str(src), str(tmp_path / "out"), options, index_dir=str(internal))
        return json.loads((internal / custom_papers.INDEX_FILE).read_text(encoding="utf-8"))

    return _index


REFEREE = [("referee", "Rea REFEREE", "Referee")]


def test_the_calculation_setup_is_flagged_where_it_was_put(referee_index):
    # the same referee in both segments of a category, on two days: the setup
    # goes into the first one only, and that is what a cut by the index gives
    index = referee_index({JUN_QUAL: ("20261107", "10:00", REFEREE),
                           JUN_FNL: ("20261108", "15:00", REFEREE)}, {})

    first, second = (people_of(index, date)["rea_referee"] for date in ("20261107", "20261108"))
    assert parts_of(first) == [(JUN_QUAL, "referee", 4, ["calculationSetup"])]
    assert parts_of(second) == [(JUN_FNL, "referee", 3, [])]
    assert [day["date"] for day in index["days"]] == ["20261107", "20261108"]
    # no JudgesSheetAll line here: the categories table names the segments
    assert index["segments"][JUN_QUAL]["date"] == "20261107" and index["segments"][JUN_FNL]["date"] == "20261108"
    assert index["segments"][JUN_QUAL]["category"] == "Juniorinaiset"


def test_the_setup_in_every_segment_when_asked(referee_index):
    index = referee_index({JUN_QUAL: ("20261107", "10:00", REFEREE),
                           JUN_FNL: ("20261107", "15:00", REFEREE)},
                          {"globalCalculationSetupEverySegment": True})

    assert parts_of(people_of(index)["rea_referee"]) == [
        (JUN_QUAL, "referee", 4, ["calculationSetup"]), (JUN_FNL, "referee", 4, ["calculationSetup"])]


def test_two_roles_in_one_segment_are_two_parts(run):
    panel = [entry for entry in PANEL if entry[0] != "replay_operator"]
    panel.append(("replay_operator", "Dan OPERATOR", "Replay Operator"))

    people = people_of(run({"globalPatternDanceAllPapers": True}, panels={PD: panel, FD: PANEL}).index)

    assert parts_of(people["dan_operator"]) == [
        (PD, "data_operator", 2, []), (PD, "replay_operator", 2, []), (FD, "data_operator", 2, [])]
    assert people["dan_operator"]["pages"] == 7


def test_a_trainee_has_parts_of_their_own(run):
    people = people_of(run({"trainees": [judge(TRAINEE, [PD, FD])]}).index)

    trainee = people[SLUG]
    assert trainee["name"] == TRAINEE
    assert [(part["role"], part["label"], part["pages"]) for part in trainee["parts"]] == [
        ("judge_trainee", "Judge Trainee", 2), ("judge_trainee", "Judge Trainee", 2)]


def test_a_trainee_segment_without_sheets_is_flagged(run):
    panel = [entry for entry in PANEL if entry[0] != "judge"]

    people = people_of(run({"trainees": [judge(TRAINEE, [PD])]}, panels={PD: panel, FD: PANEL}).index)

    assert parts_of(people[SLUG]) == [(PD, "judge_trainee", 1, ["noTraineeSheet"])]


def test_the_effective_options_are_in_the_index(run):
    index = run({"globalSegmentCover": True, "segmentCovers": {FD: False}, "globalCombinedSchedule": True,
                 "combinedScheduleCopies": 3, "trainees": [judge(TRAINEE, [PD])], "unknown": "ignored"}).index

    assert index["options"] == {
        "intro": {FD: "schedule", PD: "cover"},
        "setupEverySegment": False,
        "patternDanceAll": False,
        "scheduleCopies": 3,
        "trainees": [{"name": TRAINEE, "role": "judge", "segments": [PD]}],
    }


@pytest.mark.parametrize("same", [
    {},
    {"globalSegmentCover": False},
    {"segmentCovers": {PD: False}},
    {"omitSegmentCovers": {FD: False}},
    {"combinedScheduleCopies": 7},                    # copies without the schedule: no schedule
    {"judgingMethodOverrides": {"X": "MUPI"}},        # statistics only
    {"globalCalculationSetupEverySegment": "yes"},    # only a JSON true turns it on
])
def test_options_that_give_the_same_papers_are_the_same(same):
    assert processor.effective_options(same, [PD, FD]) == processor.effective_options({}, [FD, PD])


@pytest.mark.parametrize("other", [
    {"globalSegmentCover": True},
    {"segmentCovers": {PD: True}},
    {"globalOmitSegmentCover": True},
    {"omitSegmentCovers": {FD: True}},
    {"globalCalculationSetupEverySegment": True},
    {"globalPatternDanceAllPapers": True},
    {"globalCombinedSchedule": True},
    {"globalCombinedSchedule": True, "combinedScheduleCopies": 2},
    {"trainees": [judge(TRAINEE, [PD])]},
])
def test_options_that_change_the_papers_differ(other):
    plain = processor.effective_options({}, [PD, FD])
    with_schedule = processor.effective_options({"globalCombinedSchedule": True}, [PD, FD])

    assert processor.effective_options(other, [PD, FD]) != plain
    if other.get("combinedScheduleCopies"):
        assert processor.effective_options(other, [PD, FD]) != with_schedule


def test_no_index_when_a_packet_has_no_page_counts():
    assert processor.packet_parts([], 2, 1, [(PD, "judge", 1, 2, 2, [])], {}) is None
    assert processor.packet_parts([1, 1, 3], 3, 1, [(PD, "judge", 1, 2, 3, [])], {(PD, "judge"): "Judge 4"}) == (
        1, [{"prefix": PD, "role": "judge", "label": "Judge 4", "pages": 4, "intro": 1, "flags": []}])


def test_an_index_that_would_not_add_up_is_not_written(tmp_path):
    (tmp_path / "out").mkdir()
    with zipfile.ZipFile(tmp_path / "out" / "day.zip", "w"):
        pass
    person = {"name": "Jon JUDGE", "title": "Competition", "pages": 5,
              "index_parts": (1, [{"prefix": PD, "role": "judge", "label": "Judge 1", "pages": 3, "intro": 1,
                                   "flags": []}])}

    with pytest.raises(ValueError):
        processor.write_papers_index(
            str(tmp_path / "internal"), str(tmp_path / "out"), {}, {DATE: {"jon_judge": person}},
            {DATE: {"file": "day.pdf", "pages": 5, "slugs": ["jon_judge"]}}, {DATE: "day.zip"},
            {}, 0, {PD: {"date": DATE}}, [], {}, {}, {}, {})

    assert not (tmp_path / "internal").exists()


# --- the real pipeline over synthetic FSM-like PDFs ------------------------------

def generate(competition, tmp_path, options):
    src, out = competition
    internal = tmp_path / "internal"
    processor.process_judging_papers(str(src), str(out), options, index_dir=str(internal))
    index = custom_papers.parse_index((internal / custom_papers.INDEX_FILE).read_bytes())
    assert index is not None
    return index, out, internal


def test_the_index_agrees_with_the_generated_pdfs(competition, tmp_path):
    index, out, _ = generate(competition, tmp_path, {"trainees": TRAINEES})
    day, = index["days"]

    assert day["date"] == REAL_DATE
    assert day["zip"]["sha256"] == custom_papers.file_sha256(out / day["zip"]["file"])
    assert day["zip"]["size"] == (out / day["zip"]["file"]).stat().st_size
    assert day["pages"] == len(PdfReader(str(out / day["file"])).pages)
    assert sum(person["pages"] for person in day["people"]) == day["pages"]
    with zipfile.ZipFile(out / day["zip"]["file"]) as archive:
        assert sorted(archive.namelist()) == sorted(person["file"] for person in day["people"])
        for person in day["people"]:
            archive.extract(person["file"], tmp_path / "packets")
            assert len(PdfReader(str(tmp_path / "packets" / person["file"])).pages) == person["pages"]

    people = people_of(index, REAL_DATE)
    # Judge 1: the JUN time schedule + two sheets; SEN has no judges' sheet: its time schedule only
    assert parts_of(people["jon_judge"]) == [(JUN, "judge", 3, []), (SEN, "judge", 1, [])]
    # the referee's calculation setup is flagged where the pipeline lists it, though no file was uploaded
    assert parts_of(people["rea_referee"]) == [(JUN, "referee", 3, ["calculationSetup"])]
    assert [(part["role"], part["pages"]) for part in people["asa_arola"]["parts"]] == [
        ("judge_trainee", 3), ("technical_trainee", 3)]


def test_no_index_dir_no_index(competition, tmp_path):
    src, out = competition

    processor.process_judging_papers(str(src), str(out), {})

    assert sorted(path.name for path in out.iterdir()) == [f"judgingPapers_{REAL_DATE}.zip",
                                                           f"judgingpapers_{REAL_DATE}.pdf"]


def test_a_cut_by_the_index_gives_the_packets_own_pages(competition, tmp_path):
    index, out, internal = generate(competition, tmp_path, {"trainees": TRAINEES})
    day, = index["days"]
    with zipfile.ZipFile(out / day["zip"]["file"]) as archive:
        archive.extractall(tmp_path / "packets")
    # the on-site supplement: one segment for everyone, no covers
    selection = {REAL_DATE: {"people": {
        person["slug"]: {"parts": [[part["prefix"], part["role"]] for part in person["parts"] if part["prefix"] == JUN]}
        for person in day["people"]}}}

    plan, = custom_papers.plan_cut(index, custom_papers.validate_selection(index, selection))
    custom = tmp_path / "custom.pdf"
    pages = custom_papers.cut_day(str(out / day["zip"]["file"]), None, plan, str(custom))

    expected = []
    for person in plan["people"]:
        texts = pages_text(tmp_path / "packets" / person["file"])
        for first, stop in person["ranges"]:
            expected += texts[first:stop]
    assert pages == len(expected) == plan["pages"]
    assert [collapse(text) for text in pages_text(custom)] == [collapse(text) for text in expected]
    # the trainee's stamped copy came along, with the trainee's name on it
    assert any("Judge Trainee" in collapse(text) and "Åsa Ärölä" in collapse(text) for text in pages_text(custom))
    # nobody's cover, and the SEN segment stayed out
    assert [person["name"] for person in plan["people"]] == ["Åsa Ärölä", "Jane SECOND", "Jon JUDGE", "Rea REFEREE"]
    assert all(person["ranges"][0][0] >= 1 for person in plan["people"])


def test_the_combined_schedule_is_stored_for_the_cut(competition, tmp_path):
    index, out, internal = generate(competition, tmp_path, {"globalCombinedSchedule": True,
                                                            "combinedScheduleCopies": 2})
    day, = index["days"]

    schedule = day["schedule"]
    assert schedule["file"] == f"combinedschedule_{REAL_DATE}.pdf" and schedule["copies"] == 2
    assert schedule["pages"] == len(PdfReader(str(internal / schedule["file"])).pages)
    assert sum(person["pages"] for person in day["people"]) + schedule["pages"] * 2 == day["pages"]

    plan, = custom_papers.plan_cut(index, custom_papers.validate_selection(index, {REAL_DATE: {"schedule": True}}))
    custom = tmp_path / "schedule-only.pdf"
    pages = custom_papers.cut_day(str(out / day["zip"]["file"]), str(internal / schedule["file"]), plan, str(custom))

    assert pages == schedule["pages"] * 2
    assert plan["people"] == []


def test_the_index_is_json_with_the_names_as_typed(competition, tmp_path):
    _index, _out, internal = generate(competition, tmp_path, {"trainees": TRAINEES})

    raw = (internal / custom_papers.INDEX_FILE).read_text(encoding="utf-8")

    assert "Åsa Ärölä" in raw and json.loads(raw)["days"]
