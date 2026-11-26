"""Trainees in the pipeline (issue #93): processor.process_judging_papers gives
every trainee a packet of copies of an official's sheets with the trainee's
name stamped on them (trainee_sheets.stamp_trainee_sheet).

The harness runs over empty placeholder files like test_pattern_dance's: the
stamp is a stub that only touches its destination (or raises), merge_pdfs
records the file lists, create_cover_pdf records its arguments. The
end-to-end run of the real pipeline over real PDFs is in
test_trainee_pipeline.py.
"""
import json

import pytest
from pypdf import PdfWriter

import custom_papers
import processor
from processor import day_summary
from trainees import trainee_slug
from trainee_sheets import UnsupportedLayout

ICE_DANCE = {"abbreviation": "FSKXICEDANCE-ADVNOV", "displayName": "Advanced Novice Ice Dance",
             "displayNameFi": "Jäätanssi, Advanced Novice", "competitionType": "Figure skating",
             "judgingMethod": "ISU"}
PD = "FSKXICEDANCE-ADVNOV-QUAL0001PK"
FD = "FSKXICEDANCE-ADVNOV-FNL000100--"
LINES = {
    PD: "JÄÄTANSSI, ADVANCED NOVICE PDK1 (STARLIGHT WALTZ)",
    FD: "JÄÄTANSSI, ADVANCED NOVICE FREE DANCE",
}
TIMES = {PD: "10:00", FD: "15:00"}
DATE = "20261107"
PANEL = [("referee", "Rea REFEREE", "Referee"), ("judge", "Jon JUDGE", "Judge 1"),
         ("judge", "Jane SECOND", "Judge 2"),
         ("technical_controller", "Tia CONTROLLER", "Technical Controller"),
         ("technical_specialist_1", "Taru SPECIALIST", "Technical Specialist"),
         ("technical_specialist_2", "Toni SECOND", "Technical Specialist"),
         ("data_operator", "Dan OPERATOR", "Data Operator"), ("replay_operator", "Rasmus REPLAY", "Replay Operator")]
SUFFIXES = ("StartListwithTimes", "ISUPanelofJudgesandTechnicalPanel", "JudgesSheetAll",
            "TechnicalControllerSheet", "TechnicalSpecialistSheet1", "TechnicalSpecialistSheet2")
TRAINEE = "Väinö Åkerström"
SLUG = "vaino_akerstrom"


def judge(name, segments):
    return {"name": name, "role": "judge", "segments": segments}


def technical(name, segments):
    return {"name": name, "role": "technical", "segments": segments}


class Run:
    """What one process_judging_papers run produced."""

    def __init__(self, packets, stats, stamps, covers, src, index=None):
        self.packets = packets    # packet file name -> [merged file names]
        self.index = index        # the papers index of the run (custom_papers), None when not written
        self.stats = stats
        self.stamps = stamps      # [(src name, dst name, kind, name)]
        self.covers = covers      # slug -> (name, title, [(time, label, role)]) of the personal covers
        self.summaries = covers.get("_day", [])
        self.src = src

    def packet(self, slug):
        return self.packets[f"judgingpapers_{DATE}_{slug}.pdf"]

    @property
    def personal(self):
        """The personal packets' file names (not the day document)."""
        return sorted(name for name in self.packets if name != f"judgingpapers_{DATE}.pdf")


@pytest.fixture
def run(tmp_path, monkeypatch):
    """
    run(options, panels=None, missing=(), stamp=None) -> Run. panels maps a
    prefix to its panel (default: PANEL on both); every named judge and the
    referee get their split JudgesSheetAll file. `missing` lists source file
    names to leave out; `stamp` replaces the stamp stub.
    """
    count = [0]

    def _run(options, panels=None, missing=(), stamp=None, prepare=None, dates=None):
        count[0] += 1
        src = tmp_path / f"src{count[0]}"
        src.mkdir()
        panels = panels or {PD: PANEL, FD: PANEL}
        for prefix in LINES:
            names = [f"{prefix}_{suffix}.pdf" for suffix in SUFFIXES]
            names += [f"{prefix}_JudgesSheetAll_{role}_{processor.slugify(name)}.pdf"
                      for role, name, _ in panels.get(prefix, []) if role in ("judge", "referee")]
            for name in names:
                if name not in missing:
                    (src / name).touch()
        if prepare:
            prepare(src)

        def prefix_of(path):
            return path.rsplit("/", 1)[-1].split("_", 1)[0]

        packets, stamps, covers = {}, [], {}
        pages_of = {}  # merged file -> its pages

        def merge(output_path, file_list, page_counts=None):
            packets[output_path.rsplit("/", 1)[-1]] = [p.rsplit("/", 1)[-1] for p in file_list]
            open(output_path, "wb").close()   # step 4 merges the packets that exist
            # a placeholder file counts as one page, a packet as the pages merged into it
            counts = [pages_of.get(path, 1) for path in file_list]
            if page_counts is not None:
                page_counts.extend(counts)
            pages_of[output_path] = sum(counts)
            return sum(counts)

        def cover(path, title, dt, name, segments=None, summary=None):
            open(path, "wb").close()
            if summary is None:
                slug = path.rsplit("/", 1)[-1][len(f"coverpage_{DATE}_"):-len(".pdf")]
                covers[slug] = (name, title, segments)
            else:
                covers.setdefault("_day", []).append(summary)

        def stamp_stub(src_pdf, dst_pdf, kind, name):
            stamps.append((src_pdf.rsplit("/", 1)[-1], dst_pdf.rsplit("/", 1)[-1], kind, name))
            open(dst_pdf, "wb").close()
            return 1, 0, 0

        def filter_stub(src_pdf, dst_pdf):
            open(dst_pdf, "wb").close()
            return 0

        monkeypatch.setattr(processor, "_get_categories_for_processor", lambda: [ICE_DANCE])
        monkeypatch.setattr(processor, "split_pdf", lambda path: [])
        monkeypatch.setattr(processor, "extract_segment_name_from_pdf", lambda path: LINES[prefix_of(path)])
        monkeypatch.setattr(processor, "extract_title_from_pdf", lambda path: "Competition")
        monkeypatch.setattr(processor, "create_cover_pdf", cover)
        monkeypatch.setattr(processor, "get_date_from_start_list",
                            lambda path: (dates or {}).get(prefix_of(path), DATE))
        monkeypatch.setattr(processor, "get_first_start_time_from_start_list",
                            lambda path: TIMES[prefix_of(path)])
        monkeypatch.setattr(processor, "get_last_end_time_from_start_list", lambda path: None)
        monkeypatch.setattr(processor, "get_panel_info", lambda path: panels.get(prefix_of(path), []))
        monkeypatch.setattr(processor, "filter_withdrawn_pages", filter_stub)
        monkeypatch.setattr(processor, "merge_pdfs", merge)
        monkeypatch.setattr(processor, "stamp_trainee_sheet", stamp or stamp_stub)

        index_dir = tmp_path / f"index{count[0]}"
        stats = processor.process_judging_papers(str(src), str(tmp_path / f"out{count[0]}"), options,
                                                 index_dir=str(index_dir))
        index_file = index_dir / custom_papers.INDEX_FILE
        index = json.loads(index_file.read_text(encoding="utf-8")) if index_file.exists() else None
        return Run(packets, stats, stamps, covers, src, index)

    return _run


def cover_name(prefix):
    return f"coverpage_{DATE}_{prefix}.pdf"


# --- A trainee's packet ------------------------------------------------------

def test_judge_trainee_gets_a_stamped_copy_of_judge_1(run):
    r = run({"trainees": [judge(TRAINEE, [PD, FD])]})

    assert r.packet(SLUG) == [
        cover_name(SLUG),
        f"{PD}_StartListwithTimes.pdf", f"{PD}_JudgesSheetAll_trainee_{SLUG}.pdf",
        f"{FD}_StartListwithTimes.pdf", f"{FD}_JudgesSheetAll_trainee_{SLUG}.pdf",
    ]
    assert r.stamps == [
        (f"{PD}_JudgesSheetAll_judge_jon_judge.pdf", f"{PD}_JudgesSheetAll_trainee_{SLUG}.pdf", "judge", TRAINEE),
        (f"{FD}_JudgesSheetAll_judge_jon_judge.pdf", f"{FD}_JudgesSheetAll_trainee_{SLUG}.pdf", "judge", TRAINEE),
    ]
    name, title, segments = r.covers[SLUG]
    assert name == TRAINEE and title == "Competition"
    assert [(t, role) for t, _, role in segments] == [("10:00", "Judge Trainee"), ("15:00", "Judge Trainee")]


def test_the_first_judge_with_sheets_is_the_base(run):
    r = run({"trainees": [judge(TRAINEE, [FD])]},
            missing=(f"{FD}_JudgesSheetAll_judge_jon_judge.pdf",))

    assert r.stamps[0][0] == f"{FD}_JudgesSheetAll_judge_jane_second.pdf"


def test_intro_page_follows_the_options(run):
    r = run({"trainees": [judge(TRAINEE, [FD])], "globalOmitSegmentCover": True})

    assert r.packet(SLUG) == [cover_name(SLUG), f"{FD}_JudgesSheetAll_trainee_{SLUG}.pdf"]


@pytest.mark.parametrize("all_papers", [False, True])
def test_technical_trainee_gets_ts2_in_a_pattern_dance_and_ts1_elsewhere(run, all_papers):
    r = run({"trainees": [technical(TRAINEE, [PD, FD])], "globalPatternDanceAllPapers": all_papers})

    # The Pattern Dance skip (TS 1 / DO / RO) never swallows a trainee's sheet
    assert r.packet(SLUG) == [
        cover_name(SLUG),
        f"{PD}_StartListwithTimes.pdf", f"{PD}_TechnicalSpecialistSheet2_trainee_{SLUG}.pdf",
        f"{FD}_StartListwithTimes.pdf", f"{FD}_TechnicalSpecialistSheet1_trainee_{SLUG}.pdf",
    ]
    # Always stamped from the copy without the withdrawn skaters' pages
    assert r.stamps == [
        (f"{PD}_TechnicalSpecialistSheet2_nowd.pdf", f"{PD}_TechnicalSpecialistSheet2_trainee_{SLUG}.pdf",
         "technical", TRAINEE),
        (f"{FD}_TechnicalSpecialistSheet1_nowd.pdf", f"{FD}_TechnicalSpecialistSheet1_trainee_{SLUG}.pdf",
         "technical", TRAINEE),
    ]
    assert [role for _, _, role in r.covers[SLUG][2]] == ["Technical Trainee", "Technical Trainee"]


def test_technical_trainee_falls_back_to_the_other_ts_sheet(run):
    r = run({"trainees": [technical(TRAINEE, [PD, FD])]},
            missing=(f"{PD}_TechnicalSpecialistSheet2.pdf", f"{FD}_TechnicalSpecialistSheet1.pdf"))

    assert [dst for _, dst, _, _ in r.stamps] == [
        f"{PD}_TechnicalSpecialistSheet1_trainee_{SLUG}.pdf",
        f"{FD}_TechnicalSpecialistSheet2_trainee_{SLUG}.pdf",
    ]


def test_no_ts_sheet_at_all_adds_nothing(run):
    r = run({"trainees": [technical(TRAINEE, [FD])]},
            missing=(f"{FD}_TechnicalSpecialistSheet1.pdf", f"{FD}_TechnicalSpecialistSheet2.pdf"))

    assert r.packet(SLUG) == [cover_name(SLUG), f"{FD}_StartListwithTimes.pdf"]
    assert r.stamps == []
    assert r.stats["trainee_segments_without_sheets"] == 1


def test_judge_and_technical_trainee_in_one_packet(run):
    r = run({"trainees": [judge(TRAINEE, [FD]), technical(TRAINEE, [PD])]})

    assert r.packet(SLUG) == [
        cover_name(SLUG),
        f"{PD}_StartListwithTimes.pdf", f"{PD}_TechnicalSpecialistSheet2_trainee_{SLUG}.pdf",
        f"{FD}_StartListwithTimes.pdf", f"{FD}_JudgesSheetAll_trainee_{SLUG}.pdf",
    ]
    assert [role for _, _, role in r.covers[SLUG][2]] == ["Technical Trainee", "Judge Trainee"]


# --- One packet per person ---------------------------------------------------

def test_a_panel_member_of_the_day_gets_the_trainee_segment_in_their_packet(run):
    # Dan operates data in both segments and is a judge trainee in the FD
    r = run({"trainees": [judge("Dan Operator", [FD])]})

    assert len(r.personal) == len(PANEL)
    # The panel role first, then the trainee task, each after the segment's intro page
    assert r.packet("dan_operator") == [
        cover_name("dan_operator"),
        f"{PD}_StartListwithTimes.pdf",               # the PD's planned program content is skipped
        f"{FD}_StartListwithTimes.pdf", f"{FD}_PlannedProgramContent.pdf",
        f"{FD}_StartListwithTimes.pdf", f"{FD}_JudgesSheetAll_trainee_dan_operator.pdf",
    ]
    # The panel's spelling of the name stays, on the cover and on the stamp
    name, _, segments = r.covers["dan_operator"]
    assert name == "Dan OPERATOR"
    assert r.stamps[0][3] == "Dan OPERATOR"
    assert [role for _, _, role in segments] == ["Data Operator", "Data Operator", "Judge Trainee"]
    assert segments[-1][0] == "15:00" and "FREE DANCE" in segments[-1][1]


@pytest.mark.parametrize("trainee, slug", [
    (judge("Jon Judge", [FD]), "jon_judge"),                  # judges the FD
    (judge("Rea Referee", [FD]), "rea_referee"),              # referees it
    (technical("Taru Specialist", [FD]), "taru_specialist"),  # TS 1
    (technical("Toni Second", [FD]), "toni_second"),          # TS 2
])
def test_a_trainee_task_is_dropped_where_the_person_holds_those_sheets(run, trainee, slug):
    baseline = run({})
    r = run({"trainees": [trainee]})

    assert r.packet(slug) == baseline.packet(slug)
    assert r.stamps == []
    assert all(role != "Judge Trainee" and role != "Technical Trainee" for _, _, role in r.covers[slug][2])
    assert r.stats["trainee_assignment_count"] == 0


LOOKALIKE = ("judge", "Anna-Maria XU", "Judge 3")    # slugifies like the trainee Annamaria Xu
OTHER = "Annamaria Xu"
OWN = "annamaria_xu-trainee"


def test_a_lookalike_panel_member_on_the_trainees_segment_is_another_person(run, capsys):
    r = run({"trainees": [judge(OTHER, [FD])]}, panels={PD: PANEL, FD: PANEL + [LOOKALIKE]})

    # The panel member's packet is theirs alone, unchanged
    assert r.packet("annamaria_xu") == [cover_name("annamaria_xu"), f"{FD}_StartListwithTimes.pdf",
                                        f"{FD}_JudgesSheetAll_judge_annamaria_xu.pdf"]
    assert r.covers["annamaria_xu"][0] == "Anna-Maria XU"
    # The trainee keeps the task (no conflict: another person judges) in a packet of their own
    assert r.packet(OWN) == [cover_name(OWN), f"{FD}_StartListwithTimes.pdf",
                             f"{FD}_JudgesSheetAll_trainee_{OWN}.pdf"]
    assert r.covers[OWN][0] == OTHER and r.covers[OWN][2][0][2] == "Judge Trainee"
    assert r.stamps == [(f"{FD}_JudgesSheetAll_judge_jon_judge.pdf", f"{FD}_JudgesSheetAll_trainee_{OWN}.pdf",
                         "judge", OTHER)]
    assert r.stats["trainee_assignment_count"] == 1 and r.stats["trainee_count"] == 1
    out = capsys.readouterr().out
    assert "annamaria" not in out.lower()


def test_a_lookalike_panel_member_on_another_segment_is_another_person(run):
    r = run({"trainees": [technical(OTHER, [FD])]}, panels={PD: PANEL + [LOOKALIKE], FD: PANEL})

    assert r.packet("annamaria_xu") == [cover_name("annamaria_xu"), f"{PD}_StartListwithTimes.pdf",
                                        f"{PD}_JudgesSheetAll_judge_annamaria_xu.pdf"]
    assert r.packet(OWN) == [cover_name(OWN), f"{FD}_StartListwithTimes.pdf",
                             f"{FD}_TechnicalSpecialistSheet1_trainee_{OWN}.pdf"]
    assert r.stamps[0][3] == OTHER


def test_the_separate_slug_is_the_same_on_every_day(run):
    day2 = "20261108"
    # The look-alike is on the panel of day 1 only; the trainee has a segment on both days
    r = run({"trainees": [judge(OTHER, [PD, FD])]}, panels={PD: PANEL + [LOOKALIKE], FD: PANEL},
            dates={FD: day2})

    assert f"judgingpapers_{DATE}_{OWN}.pdf" in r.packets
    assert f"judgingpapers_{day2}_{OWN}.pdf" in r.packets
    assert f"judgingpapers_{day2}_annamaria_xu.pdf" not in r.packets
    assert r.packets[f"judgingpapers_{day2}_{OWN}.pdf"][-1] == f"{FD}_JudgesSheetAll_trainee_{OWN}.pdf"


@pytest.mark.parametrize("panel_name, typed", [
    ("Nina GUSTAFSSON", "nina  gustafsson"),          # case and spacing only
    ("Väinö ÅKERSTRÖM", "Vaino Akerstrom"),          # diacritics only
])
def test_case_and_diacritics_only_still_share_one_packet(run, panel_name, typed):
    slug = processor.slugify(panel_name)
    panel = PANEL + [("data_operator", panel_name, "Data Operator")]

    r = run({"trainees": [judge(typed, [FD])]}, panels={PD: PANEL, FD: panel})

    assert r.packet(slug)[-2:] == [f"{FD}_StartListwithTimes.pdf", f"{FD}_JudgesSheetAll_trainee_{slug}.pdf"]
    assert not any(name.endswith("-trainee.pdf") for name in r.packets)
    assert r.covers[slug][0] == panel_name and r.stamps[0][3] == panel_name


def test_the_same_person_with_other_diacritics_still_drops_a_covered_task(run):
    # Väinö judges the FD; typed without diacritics he is still the same person
    panel = [role for role in PANEL if role[0] != "judge"] + [("judge", "Väinö ÅKERSTRÖM", "Judge 1")]

    r = run({"trainees": [judge("Vaino Akerstrom", [FD])]}, panels={PD: PANEL, FD: panel})

    assert r.stamps == [] and r.stats["trainee_assignment_count"] == 0


def test_other_trainee_roles_on_ones_own_segment_are_kept(run):
    # Jon judges the FD; a technical trainee task there is no conflict
    r = run({"trainees": [technical("Jon Judge", [FD])]})

    files = r.packet("jon_judge")
    assert f"{FD}_JudgesSheetAll_judge_jon_judge.pdf" in files
    assert f"{FD}_TechnicalSpecialistSheet1_trainee_jon_judge.pdf" in files


# --- Robustness --------------------------------------------------------------

def test_a_stored_segment_without_files_is_ignored(run):
    baseline = run({})
    r = run({"trainees": [judge(TRAINEE, ["FSKXICEDANCE-ADVNOV-QUAL000200--"])]})

    assert r.packets == baseline.packets
    assert r.stamps == []

    r = run({"trainees": [judge(TRAINEE, ["FSKXICEDANCE-ADVNOV-QUAL000200--", FD])]})
    assert r.packet(SLUG) == [cover_name(SLUG), f"{FD}_StartListwithTimes.pdf",
                              f"{FD}_JudgesSheetAll_trainee_{SLUG}.pdf"]


def test_a_segment_without_a_start_list_is_ignored(run):
    r = run({"trainees": [judge(TRAINEE, [PD, FD])]}, missing=(f"{PD}_StartListwithTimes.pdf",))

    assert r.packet(SLUG) == [cover_name(SLUG), f"{FD}_StartListwithTimes.pdf",
                              f"{FD}_JudgesSheetAll_trainee_{SLUG}.pdf"]


def test_a_trainee_listed_twice_gets_the_segment_once(run):
    r = run({"trainees": [judge(TRAINEE, [FD]), judge(f"  {TRAINEE} ", [FD, FD])]})

    assert r.packet(SLUG) == [cover_name(SLUG), f"{FD}_StartListwithTimes.pdf",
                              f"{FD}_JudgesSheetAll_trainee_{SLUG}.pdf"]
    assert len(r.stamps) == 1
    assert r.stats["trainee_count"] == 1 and r.stats["trainee_assignment_count"] == 1


def test_a_name_without_letters_a_to_z_gets_a_hashed_slug(run):
    slug = trainee_slug("Ωμέγα")
    assert slug.startswith("trainee-")

    r = run({"trainees": [judge("Trainee 2", [PD]), judge("Ωμέγα", [FD]), technical("ΩΜΈΓΑ", [PD])]})

    # Two people, two packets (the numbered fallback once gave both trainee_2)
    assert r.packet("trainee_2")[-1] == f"{PD}_JudgesSheetAll_trainee_trainee_2.pdf"
    assert r.covers["trainee_2"][0] == "Trainee 2"
    # The same name in two roles shares one packet
    assert r.packet(slug) == [cover_name(slug),
                              f"{PD}_StartListwithTimes.pdf", f"{PD}_TechnicalSpecialistSheet2_trainee_{slug}.pdf",
                              f"{FD}_StartListwithTimes.pdf", f"{FD}_JudgesSheetAll_trainee_{slug}.pdf"]
    assert r.covers[slug][0] == "Ωμέγα"


def test_names_that_would_share_a_packet_keep_only_the_first(run):
    r = run({"trainees": [judge("Anna-Maria Xu", [FD]), judge("Annamaria Xu", [PD])]})

    assert r.packet("annamaria_xu") == [cover_name("annamaria_xu"), f"{FD}_StartListwithTimes.pdf",
                                        f"{FD}_JudgesSheetAll_trainee_annamaria_xu.pdf"]
    assert [name for _, _, _, name in r.stamps] == ["Anna-Maria Xu"]


def test_a_stamp_that_raises_puts_the_base_sheets_in(run, capsys):
    def prepare(src):
        writer = PdfWriter()
        for _ in range(3):
            writer.add_blank_page(width=595, height=842)
        writer.write(str(src / f"{FD}_JudgesSheetAll_judge_jon_judge.pdf"))

    def refuse(src_pdf, dst_pdf, kind, name):
        raise UnsupportedLayout("no role line found")

    r = run({"trainees": [judge(TRAINEE, [FD]), technical(TRAINEE, [FD])]}, stamp=refuse, prepare=prepare)

    files = r.packet(SLUG)
    assert f"{FD}_JudgesSheetAll_judge_jon_judge.pdf" in files
    # The TS sheet is an empty placeholder: unreadable, so the segment got no sheet
    assert f"{FD}_TechnicalSpecialistSheet1_nowd.pdf" not in files
    assert not any("_trainee_" in f for f in files)
    assert r.stats["trainee_sheets_unstamped"] == 3
    assert r.stats["trainee_segments_without_sheets"] == 1
    out = capsys.readouterr().out
    assert "could not be stamped" in out and "its source can't be read" in out
    assert TRAINEE not in out and SLUG not in out


def test_a_file_already_named_like_a_stamped_copy_is_stamped_over(run):
    planted = f"{FD}_JudgesSheetAll_trainee_{SLUG}.pdf"

    def prepare(src):
        (src / planted).write_bytes(b"uploaded by someone")

    def stamp(src_pdf, dst_pdf, kind, name):
        stamps.append(dst_pdf.rsplit("/", 1)[-1])
        with open(dst_pdf, "wb") as fh:
            fh.write(b"stamped")
        return 1, 0, 0

    stamps = []
    r = run({"trainees": [judge(TRAINEE, [FD])]}, stamp=stamp, prepare=prepare)

    assert stamps == [planted]
    assert (r.src / planted).read_bytes() == b"stamped"
    assert r.packet(SLUG)[-1] == planted


def test_each_copy_is_stamped_once_per_run(run):
    # A trainee joining a panel member's packet in two segments: one stamp per segment
    r = run({"trainees": [judge("Dan Operator", [PD, FD]), judge("DAN  OPERATOR", [FD])]})

    assert [dst for _, dst, _, _ in r.stamps] == [f"{PD}_JudgesSheetAll_trainee_dan_operator.pdf",
                                                  f"{FD}_JudgesSheetAll_trainee_dan_operator.pdf"]


def test_no_named_judge_on_the_segment_adds_no_sheets(run, capsys):
    unnamed = [role for role in PANEL if role[0] != "judge"]
    r = run({"trainees": [judge(TRAINEE, [FD])]}, panels={PD: PANEL, FD: unnamed})

    assert r.packet(SLUG) == [cover_name(SLUG), f"{FD}_StartListwithTimes.pdf"]
    assert r.stamps == []
    assert r.stats["trainee_segments_without_sheets"] == 1
    out = capsys.readouterr().out
    assert f"no judge's sheets to copy for a judge trainee in {FD}" in out


def test_logs_carry_no_trainee_names(run, capsys):
    run({"trainees": [judge(TRAINEE, [PD, FD]), technical("Åsa Ärölä", [PD])]})

    out = capsys.readouterr().out
    for text in (TRAINEE, SLUG, "Åsa", "asa_arola"):
        assert text not in out


# --- Day document and statistics ---------------------------------------------

def test_the_day_documents_check_block_counts_trainees(run):
    r = run({"trainees": [judge(TRAINEE, [FD]), technical("Åsa Ärölä", [PD]), judge("Dan Operator", [FD])]})

    [summary] = r.summaries
    roles = dict(summary["roles"])
    assert roles["trainee"] == 3          # Dan counts as data operator and as trainee
    assert roles["data_operator"] == 1
    assert summary["roles"][-1][0] == "trainee"
    assert summary["packets"] == len(PANEL) + 2


def test_day_summary_trainee_group():
    persons = {
        "jon": {"tasks": [("A", "judge")]},
        "dan": {"tasks": [("A", "data_operator"), ("B", "judge_trainee")]},
        "asa": {"tasks": [("A", "technical_trainee"), ("B", "judge_trainee")]},
    }

    summary = day_summary(DATE, persons, 10, "x.pdf", {}, {}, {})

    assert summary["roles"] == [("judge", 1), ("data_operator", 1), ("trainee", 2)]


def test_statistics(run):
    baseline = run({})
    r = run({"trainees": [judge(TRAINEE, [PD, FD]), technical(TRAINEE, [FD]),
                          judge("Dan Operator", [FD]), technical("Taru Specialist", [FD])]})

    assert r.stats["trainee_count"] == 2               # Taru's task is dropped (TS 1 there)
    assert r.stats["trainee_assignment_count"] == 4
    assert r.stats["trainee_sheets_unstamped"] == 0
    # The panel statistics are about the panel only: Väinö is no official, Dan already was one
    for key in ("unique_official_count", "unique_judge_count", "judge_assignment_count", "officials_by_role",
                "segment_count"):
        assert r.stats[key] == baseline.stats[key], key
    assert baseline.stats["unique_official_count"] == len(PANEL)
    assert r.stats["pages_generated"] > baseline.stats["pages_generated"]


@pytest.mark.parametrize("options", [{}, {"trainees": []}, {"trainees": None}, {"trainees": "x"},
                                     {"trainees": [{"name": "", "role": "judge", "segments": [FD]}]}])
def test_without_trainees_the_packets_are_unchanged(run, options):
    r = run(options)

    assert r.stamps == []
    assert len(r.personal) == len(PANEL)
    assert r.packet("jon_judge") == [
        cover_name("jon_judge"),
        f"{PD}_StartListwithTimes.pdf", f"{PD}_JudgesSheetAll_judge_jon_judge.pdf",
        f"{FD}_StartListwithTimes.pdf", f"{FD}_JudgesSheetAll_judge_jon_judge.pdf",
    ]
    assert r.stats["trainee_count"] == 0
    assert r.stats["trainee_assignment_count"] == 0
    assert r.stats["trainee_sheets_unstamped"] == 0
    assert r.stats["trainee_segments_without_sheets"] == 0
    assert not any("_trainee_" in path.name for path in r.src.iterdir())
    [summary] = r.summaries
    assert "trainee" not in dict(summary["roles"])
