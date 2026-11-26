"""Custom generate (issue #85), custom_papers: the index's checks, a selection
against an index, the cut plan, the cut itself and the contents page.

`make_papers` writes what a generate run leaves behind (day ZIP, day document,
stored schedule, index) from small PDFs whose every page says whose it is, so
a cut can be read back page by page. test_papers_index.py covers how the real
pipeline fills the index, test_custom_routes.py the routes.
"""
import json
import os
import zipfile

import pytest
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas

import custom_papers
import processor
from combine_judging_papers import merge_pdfs
from custom_papers import PapersChanged, SelectionError

DATE, DAY2 = "20261107", "20261108"
SP, FS, SEN = "JUNW-QUAL", "JUNW-FNL", "SENM-FNL"
SEGMENTS = {
    SP: {"date": DATE, "start": "10:00", "end": "11:30", "category": "JUNIORIT NAISET", "segment": "SHORT PROGRAM"},
    FS: {"date": DATE, "start": "15:00", "end": "17:00", "category": "JUNIORIT NAISET", "segment": "FREE SKATING"},
    SEN: {"date": DAY2, "start": "09:00", "end": "10:00", "category": "SENIORIT MIEHET", "segment": "FREE SKATING"},
}
# (slug, name, cover pages, [(prefix, role, label, pages)]): the third person
# has two roles in one segment and a part without pages
PEOPLE = [
    ("anna_judge", "Anna JUDGE", 1, [(SP, "judge", "Judge 1", 3), (FS, "judge", "Judge 1", 2)]),
    ("bert_referee", "Bert REFEREE", 1, [(SP, "referee", "Referee", 4), (FS, "referee", "Referee", 3)]),
    ("cara_operator", "Cara OPERATOR", 1, [(SP, "data_operator", "Data Operator", 2),
                                           (SP, "replay_operator", "Replay Operator", 2),
                                           (FS, "data_operator", "Data Operator", 0)]),
]
PEOPLE2 = [("anna_judge", "Anna JUDGE", 1, [(SEN, "judge", "Judge 3", 2)])]
SCHEDULE_PAGES, SCHEDULE_COPIES = 2, 2


def text_pdf(path, texts):
    c = canvas.Canvas(str(path))
    for text in texts:
        c.drawString(72, 720, text)
        c.showPage()
    c.save()


def page_texts(path):
    return [" ".join(page.extract_text().split()) for page in PdfReader(str(path)).pages]


def make_papers(output_dir, index_dir, options=None, days=None, schedule=True):
    """
    The output of a generate run over `days` ([(date, people)], default one
    day with PEOPLE): judgingPapers_{date}.zip of the packets (page n of a
    packet reads '<slug> n'), judgingpapers_{date}.pdf, and in index_dir the
    two-page schedule ('schedule n') and the index, which is returned.
    """
    output_dir, index_dir = str(output_dir), str(index_dir)
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(index_dir, exist_ok=True)
    index_days, prefixes = [], {}
    for date, people in days or [(DATE, PEOPLE)]:
        packets, index_people = [], []
        for slug, name, cover, parts in people:
            pages = cover + sum(part[3] for part in parts)
            file_name = f"judgingpapers_{date}_{slug}.pdf"
            text_pdf(os.path.join(output_dir, file_name), [f"{slug} {n}" for n in range(1, pages + 1)])
            packets.append(os.path.join(output_dir, file_name))
            index_people.append({
                "slug": slug, "name": name, "file": file_name, "pages": pages, "cover": cover,
                "parts": [{"prefix": prefix, "role": role, "label": label, "pages": count,
                           "intro": min(1, count), "flags": []} for prefix, role, label, count in parts]})
            prefixes.update((part[0], SEGMENTS[part[0]]) for part in parts)
        zip_name = f"judgingPapers_{date}.zip"
        zip_path = os.path.join(output_dir, zip_name)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in packets:
                archive.write(path, os.path.basename(path))
        entry, copies = None, []
        if schedule:
            entry = {"file": f"combinedschedule_{date}.pdf", "pages": SCHEDULE_PAGES, "copies": SCHEDULE_COPIES}
            schedule_path = os.path.join(index_dir, entry["file"])
            text_pdf(schedule_path, [f"schedule {n}" for n in range(1, SCHEDULE_PAGES + 1)])
            entry.update(size=os.path.getsize(schedule_path), sha256=custom_papers.file_sha256(schedule_path))
            copies = [schedule_path] * SCHEDULE_COPIES
        day_file = f"judgingpapers_{date}.pdf"
        day_pages = merge_pdfs(os.path.join(output_dir, day_file), packets + copies)
        for path in packets:
            os.remove(path)
        index_days.append({
            "date": date, "title": "Syyskisa 2026", "file": day_file, "pages": day_pages,
            "zip": {"file": zip_name, "size": os.path.getsize(zip_path),
                    "sha256": custom_papers.file_sha256(zip_path)},
            "schedule": entry, "people": index_people})
    index = custom_papers.build_index(index_days, prefixes, processor.effective_options(options or {}, prefixes))
    with open(os.path.join(index_dir, custom_papers.INDEX_FILE), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False)
    return index


def seed_papers(container, folder, tmp_path, generation="ab" * 16, **kw):
    """
    Upload what generate_judging_papers stores for a make_papers run: the day
    ZIP and PDF under judgePapers/, the schedule and the completed index under
    judgePapers/_internal/. Returns the stored index.
    """
    out, internal = tmp_path / "seed-out", tmp_path / "seed-internal"
    index = make_papers(out, internal, **kw)
    for path in out.iterdir():
        container.upload_blob(f"{folder}/judgePapers/{path.name}", path.read_bytes(), overwrite=True)
    prefix = f"{folder}/judgePapers/{custom_papers.INTERNAL_FOLDER}"
    for path in internal.iterdir():
        if path.name != custom_papers.INDEX_FILE:
            container.upload_blob(f"{prefix}/{path.name}", path.read_bytes(), overwrite=True)
    index.update(generation=generation, generatedUtc="2026-11-07T08:00:00Z",
                 inputs={"files": "", "options": custom_papers.digest(index["options"])})
    for day in index["days"]:   # the blob versions the index describes
        day["zip"]["etag"] = container.blobs[f"{folder}/judgePapers/{day['zip']['file']}"].etag.strip('"')
        if day["schedule"]:
            day["schedule"]["etag"] = container.blobs[f"{prefix}/{day['schedule']['file']}"].etag.strip('"')
    container.upload_blob(f"{prefix}/{custom_papers.INDEX_FILE}", json.dumps(index).encode("utf-8"), overwrite=True)
    return index


@pytest.fixture
def papers(tmp_path):
    """(index, output dir, index dir) of a one-day run."""
    out, internal = tmp_path / "out", tmp_path / "internal"
    return make_papers(out, internal), out, internal


def everything(index):
    """The selection with every tick of the index on."""
    return {day["date"]: {
        "schedule": bool(day["schedule"]),
        "people": {person["slug"]: {"cover": True, "parts": [[part["prefix"], part["role"]] for part in person["parts"]]}
                   for person in day["people"]}} for day in index["days"]}


def segment_only(index, prefix):
    """One segment for everyone, without covers: the on-site supplement."""
    return {day["date"]: {"people": {
        person["slug"]: {"parts": [[part["prefix"], part["role"]] for part in person["parts"] if part["prefix"] == prefix]}
        for person in day["people"]}} for day in index["days"]}


def cut(papers, selection, contents=False, tmp_name="custom.pdf"):
    index, out, internal = papers
    plan = custom_papers.plan_cut(index, custom_papers.validate_selection(index, selection))[0]
    contents_path = None
    if contents:
        contents_path = str(out / "contents.pdf")
        custom_papers.create_contents_pdf(contents_path, index, plan)
    schedule_path = str(internal / plan["schedule"]["file"]) if plan["schedule"] else None
    output_path = str(out / tmp_name)
    pages = custom_papers.cut_day(str(out / plan["zip"]["file"]), schedule_path, plan, output_path, contents_path)
    return output_path, pages, plan


# --- the index ---------------------------------------------------------------

def test_an_index_reads_back_and_its_pages_add_up(papers):
    index, out, internal = papers
    read = custom_papers.parse_index((internal / custom_papers.INDEX_FILE).read_bytes())

    assert read == index
    day = read["days"][0]
    assert sum(person["pages"] for person in day["people"]) + SCHEDULE_PAGES * SCHEDULE_COPIES == day["pages"]
    assert day["pages"] == len(PdfReader(str(out / day["file"])).pages)


@pytest.mark.parametrize("damage", [
    lambda index: index.update(version=custom_papers.INDEX_VERSION + 1),
    lambda index: index.pop("segments"),
    lambda index: index["days"][0].update(date="7.11.2026"),
    lambda index: index["days"][0]["zip"].pop("sha256"),
    lambda index: index["days"][0]["people"][0].update(pages=99),          # parts don't add up
    lambda index: index["days"][0]["people"][0]["parts"][0].update(prefix="GONE"),
    lambda index: index["days"][0]["people"][0]["parts"][0].update(pages=True),
    lambda index: index["days"][0].update(schedule={"file": "x.pdf"}),
    lambda index: index["days"][0]["schedule"].pop("sha256"),
    # the names an index gives are opened as they are: never a path
    lambda index: index["days"][0]["zip"].update(file="../other-record/judgePapers/x.zip"),
    lambda index: index["days"][0]["schedule"].update(file="../metadata.json"),
    lambda index: index["days"][0]["people"][0].update(file="a/b.pdf"),
])
def test_a_damaged_index_is_no_index(papers, damage):
    index = json.loads(json.dumps(papers[0]))
    damage(index)

    assert custom_papers.parse_index(json.dumps(index)) is None


@pytest.mark.parametrize("raw", [b"", b"not json", b"[]", b"\xff\xfe", None])
def test_junk_is_no_index(raw):
    assert custom_papers.parse_index(raw) is None


def test_the_site_gets_the_tree_without_file_names_or_hashes(papers):
    index = dict(papers[0], generation="g1", generatedUtc="2026-11-07T08:00:00Z")

    sent = custom_papers.client_index(index)

    assert sent["generation"] == "g1" and sent["generatedUtc"] == "2026-11-07T08:00:00Z"
    assert sent["segments"][SP] == SEGMENTS[SP]
    day = sent["days"][0]
    assert day["schedule"] == {"pages": SCHEDULE_PAGES, "copies": SCHEDULE_COPIES}
    assert day["people"][0] == {
        "slug": "anna_judge", "name": "Anna JUDGE", "pages": 6, "cover": 1,
        "parts": [{"prefix": SP, "role": "judge", "label": "Judge 1", "pages": 3, "intro": 1, "flags": []},
                  {"prefix": FS, "role": "judge", "label": "Judge 1", "pages": 2, "intro": 1, "flags": []}]}
    text = json.dumps(sent)
    assert "sha256" not in text and ".zip" not in text and ".pdf" not in text


def test_the_files_fingerprint_follows_every_upload():
    blobs = [("B_StartListwithTimes.pdf", '"2"'), ("A_JudgesSheetAll.pdf", '"1"')]

    assert custom_papers.files_fingerprint(blobs) == custom_papers.files_fingerprint(reversed(blobs))
    assert custom_papers.files_fingerprint(blobs) != custom_papers.files_fingerprint(blobs[:1])
    assert custom_papers.files_fingerprint(blobs) != custom_papers.files_fingerprint(
        [blobs[0], ("A_JudgesSheetAll.pdf", '"3"')])


# --- the selection -------------------------------------------------------------

def test_a_selection_is_normalized(papers):
    index = papers[0]

    chosen = custom_papers.validate_selection(index, {DATE: {
        "schedule": True,
        "people": {"anna_judge": {"cover": True, "parts": [[FS, "judge"]]},
                   "bert_referee": {"cover": False, "parts": []},     # nothing ticked: dropped
                   "cara_operator": {"parts": [[SP, "replay_operator"]]}}}})

    assert chosen == {DATE: {"schedule": True, "people": {
        "anna_judge": {"cover": True, "parts": [(FS, "judge")]},
        "cara_operator": {"cover": False, "parts": [(SP, "replay_operator")]}}}}
    assert custom_papers.selection_json(chosen)[DATE]["people"]["anna_judge"]["parts"] == [[FS, "judge"]]


@pytest.mark.parametrize("selection, message", [
    (None, "the selection must be an object of days"),
    ([], "the selection must be an object of days"),
    ({}, "nothing is selected"),
    ({DATE: {"people": {}}}, "nothing is selected"),
    ({"20270101": {"schedule": True}}, "unknown day"),
    ({DATE: []}, "a day takes only schedule and people"),
    ({DATE: {"everything": True}}, "a day takes only schedule and people"),
    ({DATE: {"schedule": 1}}, "a day's schedule must be true or false"),
    ({DATE: {"people": []}}, "a day's people must be an object"),
    ({DATE: {"people": {"nobody": {"cover": True}}}}, "unknown person"),
    ({DATE: {"people": {"anna_judge": {"cover": True, "sheets": []}}}}, "a person takes only cover and parts"),
    ({DATE: {"people": {"anna_judge": {"cover": "yes"}}}}, "a person's cover must be true or false"),
    ({DATE: {"people": {"anna_judge": {"parts": "all"}}}}, "a person's parts must be a list"),
    ({DATE: {"people": {"anna_judge": {"parts": [f"{SP}|judge"]}}}}, "a part is [prefix, role]"),
    ({DATE: {"people": {"anna_judge": {"parts": [[SP, "referee"]]}}}}, "unknown part"),
    ({DATE: {"people": {"anna_judge": {"parts": [[SEN, "judge"]]}}}}, "unknown part"),
    ({DATE: {"people": {"anna_judge": {"parts": [[SP, "judge"], [SP, "judge"]]}}}}, "a part is listed twice"),
])
def test_a_selection_that_does_not_fit_the_index_is_refused(papers, selection, message):
    with pytest.raises(SelectionError) as error:
        custom_papers.validate_selection(papers[0], selection)

    assert str(error.value) == message


def test_a_schedule_only_where_the_day_has_one(tmp_path):
    index = make_papers(tmp_path / "out", tmp_path / "internal", schedule=False)

    with pytest.raises(SelectionError, match="that day has no combined schedule"):
        custom_papers.validate_selection(index, {DATE: {"schedule": True}})


def test_a_refusal_never_names_a_person_or_a_segment(papers):
    with pytest.raises(SelectionError) as error:
        custom_papers.validate_selection(papers[0], {DATE: {"people": {"Secret PERSON": {"parts": [["SECRET", "x"]]}}}})

    assert "ecret" not in str(error.value)


# --- the plan --------------------------------------------------------------------

def test_the_plan_cuts_page_ranges_in_packet_order(papers):
    index = papers[0]
    chosen = custom_papers.validate_selection(index, {DATE: {"schedule": True, "people": {
        # the keys' order does not matter: the day document's order does
        "cara_operator": {"parts": [[SP, "replay_operator"], [FS, "data_operator"], [SP, "data_operator"]]},
        "anna_judge": {"cover": True, "parts": [[FS, "judge"]]},
        "bert_referee": {"cover": True, "parts": [[SP, "referee"]]}}}})

    plan, = custom_papers.plan_cut(index, chosen)

    assert [(person["name"], person["ranges"], person["pages"]) for person in plan["people"]] == [
        ("Anna JUDGE", [(0, 1), (4, 6)], 3),        # cover, then FS after the three SP pages
        ("Bert REFEREE", [(0, 5)], 5),              # cover + SP are neighbours: one range
        ("Cara OPERATOR", [(1, 5)], 4),             # both SP roles; the FS part has no pages
    ]
    assert plan["people"][2]["labels"] == ["Data Operator", "Replay Operator"]
    assert plan["people"][2]["segments"] == [SP]
    assert plan["segments"] == 2
    assert plan["pages"] == 3 + 5 + 4 + SCHEDULE_PAGES * SCHEDULE_COPIES
    assert plan["schedule"] == index["days"][0]["schedule"]


def test_a_selection_without_pages_plans_nothing(papers):
    index = papers[0]
    chosen = custom_papers.validate_selection(
        index, {DATE: {"people": {"cara_operator": {"parts": [[FS, "data_operator"]]}}}})

    assert custom_papers.plan_cut(index, chosen) == []


def test_days_are_planned_in_date_order(tmp_path):
    index = make_papers(tmp_path / "out", tmp_path / "internal", days=[(DATE, PEOPLE), (DAY2, PEOPLE2)])
    selection = everything(index)
    chosen = custom_papers.validate_selection(index, dict(reversed(list(selection.items()))))

    plans = custom_papers.plan_cut(index, chosen)

    assert [plan["date"] for plan in plans] == [DATE, DAY2]
    assert plans[1]["pages"] == 3 + SCHEDULE_PAGES * SCHEDULE_COPIES


# --- the cut ---------------------------------------------------------------------

def test_everything_selected_is_the_day_document(papers):
    index, out, _ = papers

    path, pages, _plan = cut(papers, everything(index))

    assert pages == index["days"][0]["pages"]
    assert page_texts(path) == page_texts(out / index["days"][0]["file"])


def test_one_segment_for_everyone_without_covers(papers):
    path, pages, plan = cut(papers, segment_only(papers[0], FS))

    assert page_texts(path) == ["anna_judge 5", "anna_judge 6",
                                "bert_referee 6", "bert_referee 7", "bert_referee 8"]
    assert pages == plan["pages"] == 5
    assert [person["name"] for person in plan["people"]] == ["Anna JUDGE", "Bert REFEREE"]


def test_a_role_left_out_and_the_schedule_kept(papers):
    index = papers[0]
    selection = everything(index)
    selection[DATE]["people"]["cara_operator"]["parts"] = [[SP, "replay_operator"]]

    path, _pages, _plan = cut(papers, selection)

    texts = page_texts(path)
    assert [t for t in texts if t.startswith("cara")] == ["cara_operator 1", "cara_operator 4", "cara_operator 5"]
    assert texts[-4:] == ["schedule 1", "schedule 2", "schedule 1", "schedule 2"]


def test_the_contents_page_goes_in_front_and_counts_itself(papers):
    index = papers[0]

    path, pages, plan = cut(papers, segment_only(index, SP), contents=True)

    texts = page_texts(path)
    assert pages == plan["pages"] + 1 == 12
    front = texts[0]
    assert "CUSTOM PRINT · Saturday 07.11.2026" in front and "Syyskisa 2026" in front
    assert "1 segment · 3 people · 11 pages of papers" in front
    # each person's row: the pages they hold in this PDF, their roles, their segments
    assert "2 – 4 Anna JUDGE Judge 1 JUNIORIT NAISET, SHORT PROGRAM" in front
    assert "5 – 8 Bert REFEREE Referee JUNIORIT NAISET, SHORT PROGRAM" in front
    assert "9 – 12 Cara OPERATOR Data Operator, Replay Operator JUNIORIT NAISET, SHORT PROGRAM" in front
    assert texts[1] == "anna_judge 2" and texts[4] == "bert_referee 2" and texts[11] == "cara_operator 5"


def test_contents_rows_of_covers_and_the_schedule(papers):
    index = papers[0]
    chosen = custom_papers.validate_selection(index, {DATE: {"schedule": True, "people": {
        "anna_judge": {"cover": True}, "bert_referee": {"parts": [[FS, "referee"]]}}}})
    plan, = custom_papers.plan_cut(index, chosen)

    assert custom_papers.contents_rows(index, plan, 2) == [
        (2, 2, "Anna JUDGE", "Cover page", []),
        (3, 5, "Bert REFEREE", "Referee", ["JUNIORIT NAISET, FREE SKATING"]),
        (6, 9, "Combined time schedule", "2 copies", []),
    ]


def test_a_long_contents_list_numbers_after_all_its_own_pages(tmp_path):
    crowd = [(f"person_{n:03d}", f"Person NUMBER{n:03d}", 1, [(SP, "judge", "Judge 1", 1)]) for n in range(120)]
    index = make_papers(tmp_path / "out", tmp_path / "internal", days=[(DATE, crowd)], schedule=False)
    plan, = custom_papers.plan_cut(index, custom_papers.validate_selection(index, everything(index)))
    path = tmp_path / "contents.pdf"

    own_pages = custom_papers.create_contents_pdf(str(path), index, plan)

    texts = page_texts(path)
    assert own_pages == len(texts) > 1
    # the first person's pages start right after the contents pages
    assert f"{own_pages + 1} – {own_pages + 2} Person NUMBER000" in texts[0]
    assert f"{own_pages + 239} – {own_pages + 240} Person NUMBER119" in texts[-1]
    assert all("Pages Person Role Segments" in text for text in texts)   # the header row repeats


def test_names_with_markup_characters_are_printed_as_they_are(tmp_path):
    people = [("a_b", "Åsa <B> & ÄRÖLÄ", 1, [(SP, "judge", "Judge 1", 1)])]
    index = make_papers(tmp_path / "out", tmp_path / "internal", days=[(DATE, people)], schedule=False)
    plan, = custom_papers.plan_cut(index, custom_papers.validate_selection(index, everything(index)))
    path = tmp_path / "contents.pdf"

    custom_papers.create_contents_pdf(str(path), index, plan)

    assert "Åsa <B> & ÄRÖLÄ" in page_texts(path)[0]


def test_a_packet_that_is_not_the_index_s_stops_the_cut(papers):
    index, out, _ = papers
    index["days"][0]["people"][0]["pages"] += 1
    index["days"][0]["people"][0]["parts"][0]["pages"] += 1

    with pytest.raises(PapersChanged):
        cut(papers, everything(index))
    assert not (out / "custom.pdf").exists()


def test_a_packet_missing_from_the_zip_stops_the_cut(papers):
    index, _out, _ = papers
    index["days"][0]["people"][1]["file"] = "judgingpapers_20261107_someone_else.pdf"

    with pytest.raises(PapersChanged):
        cut(papers, everything(index))


def test_another_schedule_stops_the_cut(papers):
    index, _out, internal = papers
    text_pdf(internal / f"combinedschedule_{DATE}.pdf", ["schedule 1"])

    with pytest.raises(PapersChanged):
        cut(papers, everything(index))


def test_the_pdf_is_written_even_when_identical_objects_cannot_be_merged(papers, monkeypatch, capsys):
    def refuse(self, *args, **kwargs):
        raise RuntimeError("no")

    monkeypatch.setattr(PdfWriter, "compress_identical_objects", refuse)

    path, pages, _plan = cut(papers, segment_only(papers[0], FS))

    assert pages == 5 and len(page_texts(path)) == 5
    assert "without merging identical objects (RuntimeError)" in capsys.readouterr().out
