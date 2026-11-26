"""Custom generate (issue #85), the routes: generate_judging_papers stores the
papers index, get_papers_index says whether it is current, and
generate_custom_papers cuts a selection out of the generated papers.

The processor is replaced by test_custom_papers.make_papers, which writes real
(small) PDFs, so the custom PDFs are read back page by page.
"""
import io
import json
import os

import pytest
from pypdf import PdfReader
from reportlab.pdfgen import canvas

import custom_papers
import function_app as fa
from conftest import PLATFORM_ID, USER_EMAIL, call, make_request, payload, put_competition, text
from test_custom_papers import DATE, DAY2, FS, PEOPLE, PEOPLE2, SP, everything, make_papers, segment_only
from test_generated_links import signing  # noqa: F401 (fixture)

COMP_ID = "abc12345"
NAME = "Spring Cup"
FOLDER = f"{NAME}-{COMP_ID}"
INTERNAL = f"{FOLDER}/judgePapers/{custom_papers.INTERNAL_FOLDER}"
INDEX_BLOB = f"{INTERNAL}/{custom_papers.INDEX_FILE}"
SELECTION_BLOB = f"{INTERNAL}/{custom_papers.SELECTION_FILE}"
SCHEDULE_BLOB = f"{INTERNAL}/combinedschedule_{DATE}.pdf"
ZIP_BLOB = f"{FOLDER}/judgePapers/judgingPapers_{DATE}.zip"
DAY_BLOB = f"{FOLDER}/judgePapers/judgingpapers_{DATE}.pdf"
SOURCES = ("JUNW-QUAL_StartListwithTimes.pdf", "JUNW-QUAL_JudgesSheetAll.pdf", "JUNW-FNL_StartListwithTimes.pdf")
TRAINEES = [{"name": "Väinö Åkerström", "role": "judge", "segments": [SP]}]


@pytest.fixture
def competition(tables, blobs, monkeypatch):
    """A bound competition with three uploaded files; generate() runs make_papers."""
    monkeypatch.setattr(fa, "_get_categories", lambda: [])
    put_competition(tables, comp_id=COMP_ID, name=NAME, platform_id=PLATFORM_ID,
                    deletion="2099-01-01T00:00:00Z", PagesGenerated=40, TotalPagesGenerated=40)
    blobs.upload_blob(f"{FOLDER}/metadata.json", json.dumps({"id": COMP_ID, "createdBy": USER_EMAIL}))
    for name in SOURCES:
        blobs.upload_blob(f"{FOLDER}/{name}", b"%PDF-source")
    seen = {}

    def process(source_dir, output_dir, options=None, index_dir=None):
        seen["sources"] = sorted(os.path.relpath(os.path.join(root, name), source_dir)
                                 for root, _dirs, names in os.walk(source_dir) for name in names)
        seen["options"] = options
        make_papers(output_dir, index_dir, options, days=seen.get("days"))
        return {}

    monkeypatch.setattr(fa, "process_judging_papers", process)
    return seen


def generate(options=None):
    response = call(fa.generate_judging_papers, make_request(
        "POST", body={"workingFolder": COMP_ID, "options": options or {}}))
    assert response.status_code == 200, text(response)
    return response


def index_state(options=None, **kw):
    return call(fa.get_papers_index, make_request("POST", body={"id": COMP_ID, "options": options or {}}, **kw))


def current_index(options=None):
    response = index_state(options)
    assert response.status_code == 200, text(response)
    body = payload(response)
    assert body["current"] is True, body["reason"]
    return body["index"]


def custom(selection, generation=None, **extra):
    if generation is None:
        generation = json.loads(fa.get_blob_service_client().get_container_client("fs-judgepapers")
                                .blobs[INDEX_BLOB].data)["generation"]
    return call(fa.generate_custom_papers, make_request(
        "POST", body={"id": COMP_ID, "generation": generation, "selection": selection, **extra}))


def blob_texts(blobs, name):
    return [" ".join(page.extract_text().split()) for page in PdfReader(io.BytesIO(blobs.blobs[name].data)).pages]


def stored(blobs):
    return {name: (blob.data, blob.etag) for name, blob in blobs.blobs.items()}


def another_schedule(blobs):
    """Another run's schedule in the stored one's place: as many pages, other bytes."""
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer)
    for line in ("other 1", "other 2"):
        c.drawString(72, 720, line)
        c.showPage()
    c.save()
    blobs.upload_blob(SCHEDULE_BLOB, buffer.getvalue(), overwrite=True)


# --- generate stores the index ---------------------------------------------------

def test_generate_stores_the_index_next_to_its_papers(competition, blobs, tables):
    response = generate()

    assert text(response) == "Successfully processed 4 files and generated 2 output files."
    index = custom_papers.parse_index(blobs.blobs[INDEX_BLOB].data)
    assert index is not None
    assert len(index["generation"]) == 32 and index["generatedUtc"].endswith("Z")
    assert index["inputs"] == {
        "files": custom_papers.files_fingerprint(
            (name, blobs.blobs[f"{FOLDER}/{name}"].etag) for name in SOURCES),
        "options": custom_papers.digest(index["options"]),
    }
    day = index["days"][0]
    assert day["zip"]["size"] == len(blobs.blobs[ZIP_BLOB].data)
    # the index names the very blob versions this run wrote
    assert day["zip"]["etag"] == blobs.blobs[ZIP_BLOB].etag.strip('"')
    assert day["schedule"]["etag"] == blobs.blobs[SCHEDULE_BLOB].etag.strip('"')
    assert day["schedule"]["size"] == len(blobs.blobs[SCHEDULE_BLOB].data)
    assert len(day["schedule"]["sha256"]) == 64
    # internal blobs are no generated files
    assert sorted(key[1] for key in tables["generatedpapers"].rows) == [
        f"judgingPapers_{DATE}.zip", f"judgingpapers_{DATE}.pdf"]


def test_earlier_output_is_not_downloaded_as_input(competition, blobs):
    generate()
    blobs.upload_blob(f"{FOLDER}/judgePapers/judgingpapers_{DATE}_custom1.pdf", b"%PDF-custom")

    generate()

    assert competition["sources"] == sorted(SOURCES + ("metadata.json",))


def test_the_old_index_is_gone_before_the_papers_are_replaced(competition, blobs):
    generate()
    first = json.loads(blobs.blobs[INDEX_BLOB].data)["generation"]
    seen = []
    blobs.before_upload = lambda name: seen.append((name, INDEX_BLOB in blobs.blobs))

    generate()

    # while the ZIP and the day PDF were written there was no index; it came last
    assert (ZIP_BLOB, False) in seen and (DAY_BLOB, False) in seen
    assert seen[-1][0] == INDEX_BLOB
    assert json.loads(blobs.blobs[INDEX_BLOB].data)["generation"] != first


def test_a_failed_run_keeps_the_papers_and_their_index(competition, blobs, monkeypatch):
    generate()
    before = stored(blobs)
    monkeypatch.setattr(fa, "process_judging_papers",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))

    response = call(fa.generate_judging_papers, make_request(
        "POST", body={"workingFolder": COMP_ID, "options": {}}))

    assert response.status_code == 500
    assert stored(blobs) == before


def test_a_run_without_an_index_leaves_none(competition, blobs, monkeypatch):
    generate()

    def process(source_dir, output_dir, options=None, index_dir=None):
        with open(os.path.join(output_dir, f"judgingpapers_{DATE}.pdf"), "wb") as f:
            f.write(b"%PDF-other")
        return {}

    monkeypatch.setattr(fa, "process_judging_papers", process)
    generate()

    assert INDEX_BLOB not in blobs.blobs
    assert payload(index_state())["reason"] == "none"


def test_schedules_of_days_that_are_gone_are_removed(competition, blobs):
    competition["days"] = [(DATE, PEOPLE), (DAY2, PEOPLE2)]
    generate()
    assert f"{INTERNAL}/combinedschedule_{DAY2}.pdf" in blobs.blobs

    competition["days"] = [(DATE, PEOPLE)]
    generate()

    assert f"{INTERNAL}/combinedschedule_{DAY2}.pdf" not in blobs.blobs
    assert SCHEDULE_BLOB in blobs.blobs


# --- get_papers_index --------------------------------------------------------------

def test_no_index_before_the_first_generate(competition):
    body = payload(index_state())

    assert body == {"current": False, "reason": "none", "index": None, "lastSelection": None,
                    "refreshedFromPool": []}


def test_right_after_a_generate_the_index_is_current(competition):
    generate()

    index = current_index()

    assert [day["date"] for day in index["days"]] == [DATE]
    assert [person["name"] for person in index["days"][0]["people"]] == ["Anna JUDGE", "Bert REFEREE", "Cara OPERATOR"]
    assert index["segments"][FS]["segment"] == "FREE SKATING"
    assert len(index["generation"]) == 32
    raw = json.dumps(index)
    assert "sha256" not in raw and ".zip" not in raw and "inputs" not in raw


@pytest.mark.parametrize("change, reason", [
    (lambda blobs: blobs.upload_blob(f"{FOLDER}/SENM-FNL_StartListwithTimes.pdf", b"%PDF-new"), "files"),
    (lambda blobs: blobs.upload_blob(f"{FOLDER}/{SOURCES[0]}", b"%PDF-again", overwrite=True), "files"),
    (lambda blobs: blobs.delete_blob(f"{FOLDER}/{SOURCES[1]}"), "files"),
    (lambda blobs: blobs.delete_blob(ZIP_BLOB), "outputs"),
    (lambda blobs: blobs.upload_blob(ZIP_BLOB, b"another zip", overwrite=True), "outputs"),
    # written again, even with the same bytes and size: not the blob the index was made for
    (lambda blobs: blobs.upload_blob(ZIP_BLOB, blobs.blobs[ZIP_BLOB].data, overwrite=True), "outputs"),
    (lambda blobs: blobs.upload_blob(ZIP_BLOB, bytes(len(blobs.blobs[ZIP_BLOB].data)), overwrite=True), "outputs"),
    (lambda blobs: blobs.delete_blob(SCHEDULE_BLOB), "outputs"),
    (another_schedule, "outputs"),
    (lambda blobs: blobs.upload_blob(INDEX_BLOB, b"{}", overwrite=True), "none"),
    (lambda blobs: blobs.upload_blob(
        f"{FOLDER}/metadata.json", json.dumps({"id": COMP_ID, "trainees": TRAINEES}), overwrite=True), "options"),
])
def test_what_makes_the_index_stale(competition, blobs, change, reason):
    generate()
    current_index()

    change(blobs)

    assert payload(index_state()) == {"current": False, "reason": reason, "index": None,
                                      "lastSelection": None, "refreshedFromPool": []}


def test_a_rename_or_a_settings_save_that_keeps_the_trainees_changes_nothing(competition, blobs):
    generate()

    blobs.upload_blob(f"{FOLDER}/metadata.json", json.dumps(
        {"id": COMP_ID, "name": "Renamed", "judgingMethodOverrides": {"X": "MUPI"}, "trainees": []}), overwrite=True)

    current_index()


def test_the_options_are_compared_as_the_pipeline_reads_them(competition):
    generate({"globalSegmentCover": False, "segmentCovers": {SP: False}})

    # other spellings of the same papers
    current_index({})
    current_index({"segmentCovers": {FS: False}, "combinedScheduleCopies": 9, "somethingElse": True})
    # other papers
    for options in ({"globalSegmentCover": True}, {"omitSegmentCovers": {SP: True}},
                    {"globalCombinedSchedule": True}, {"globalPatternDanceAllPapers": True}):
        assert payload(index_state(options))["reason"] == "options", options


def test_the_trainees_come_from_metadata_never_from_the_client(competition, blobs):
    blobs.upload_blob(f"{FOLDER}/metadata.json", json.dumps({"id": COMP_ID, "trainees": TRAINEES}), overwrite=True)
    generate({"trainees": []})
    assert competition["options"]["trainees"] == TRAINEES

    current_index({"trainees": [{"name": "Someone ELSE", "role": "judge", "segments": [SP]}]})


def test_a_newer_pool_copy_makes_the_index_stale(competition, blobs, monkeypatch):
    generate()

    def refresh(entity, container, folder_path):
        container.upload_blob(f"{folder_path}/{SOURCES[0]}", b"%PDF-reexported", overwrite=True)
        return [SOURCES[0]]

    monkeypatch.setattr(fa, "_refresh_from_pool", refresh)

    body = payload(index_state())

    assert (body["current"], body["reason"], body["refreshedFromPool"]) == (False, "files", [SOURCES[0]])


def test_unreadable_settings_are_no_answer(competition, monkeypatch):
    generate()
    monkeypatch.setattr(fa, "_load_competition_metadata",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("storage")))

    response = index_state()

    assert (response.status_code, text(response)) == (503, "Storage unavailable, try again")


@pytest.mark.parametrize("request_kw", [
    {"raw_body": b"not json"},
    {"body": []},
    {"body": {"options": {}}},
])
def test_the_index_route_needs_a_competition_id(competition, request_kw):
    assert call(fa.get_papers_index, make_request("POST", **request_kw)).status_code == 400


# --- generate_custom_papers ----------------------------------------------------------

def test_one_segment_for_everyone(competition, blobs, tables, signing):
    generate()
    index = current_index()
    untouched = {name: blobs.blobs[name].etag for name in (ZIP_BLOB, DAY_BLOB, INDEX_BLOB)}

    response = custom(segment_only(index, FS), generation=index["generation"])

    assert response.status_code == 200, text(response)
    name = f"judgingpapers_{DATE}_custom1.pdf"
    made, = payload(response)["files"]
    description = "Custom papers 1: 2 packets, 1 segment, 6 pages (PDF)"
    assert (made["fileName"], made["date"], made["packets"], made["segments"], made["pages"]) == (name, DATE, 2, 1, 6)
    assert made["description"] == description
    assert made["size"] == len(blobs.blobs[f"{FOLDER}/judgePapers/{name}"].data)
    assert f"/{FOLDER.replace(' ', '%20')}/judgePapers/{name}?" in made["url"] and made["expiration"]

    texts = blob_texts(blobs, f"{FOLDER}/judgePapers/{name}")
    assert "CUSTOM PRINT" in texts[0] and "2 – 3 Anna JUDGE" in texts[0] and "4 – 6 Bert REFEREE" in texts[0]
    assert texts[1:] == ["anna_judge 5", "anna_judge 6", "bert_referee 6", "bert_referee 7", "bert_referee 8"]

    row = tables["generatedpapers"].row(COMP_ID, name)
    assert (row["FileName"], row["Description"], row["BlobName"]) == (name, description, f"{FOLDER}/judgePapers/{name}")
    # the full papers and their index are as they were
    assert {key: blobs.blobs[key].etag for key in untouched} == untouched
    # usage counters; the statistics snapshot stays the full run's
    competition_row = tables["competitions"].row("GLOBAL", COMP_ID)
    assert (competition_row["CustomGenerateRunCount"], competition_row["CustomPagesGenerated"]) == (1, 6)
    assert (competition_row["PagesGenerated"], competition_row["TotalPagesGenerated"]) == (40, 40)


def test_without_the_contents_page(competition, blobs):
    generate()

    response = custom(segment_only(current_index(), FS), contents=False)

    made, = payload(response)["files"]
    assert made["pages"] == 5
    assert blob_texts(blobs, f"{FOLDER}/judgePapers/{made['fileName']}")[0] == "anna_judge 5"


@pytest.mark.parametrize("contents", [True, None, "no", 0])
def test_only_a_json_false_leaves_the_contents_page_out(competition, contents):
    generate()

    made, = payload(custom(segment_only(current_index(), FS), contents=contents))["files"]

    assert made["pages"] == 6


def test_everything_ticked_is_the_day_document_plus_contents(competition, blobs):
    generate()
    index = current_index()

    made, = payload(custom(everything(index)))["files"]

    assert blob_texts(blobs, f"{FOLDER}/judgePapers/{made['fileName']}")[1:] == blob_texts(blobs, DAY_BLOB)
    assert made["description"] == "Custom papers 1: 3 packets, 2 segments, 24 pages (PDF)"


def test_every_run_gets_the_next_number_and_nothing_is_overwritten(competition, blobs, tables):
    generate()
    index = current_index()
    names = [payload(custom(segment_only(index, prefix)))["files"][0]["fileName"] for prefix in (FS, SP, FS)]

    assert names == [f"judgingpapers_{DATE}_custom{n}.pdf" for n in (1, 2, 3)]
    assert blob_texts(blobs, f"{FOLDER}/judgePapers/{names[0]}") == blob_texts(blobs, f"{FOLDER}/judgePapers/{names[2]}")

    # a deleted one frees no number below the highest in use
    for name in names[:2]:
        assert call(fa.delete_file, make_request("POST", params={
            "competition": COMP_ID, "filename": f"judgePapers/{name}"})).status_code == 200
        assert tables["generatedpapers"].row(COMP_ID, name) is None
    assert payload(custom(segment_only(index, SP)))["files"][0]["fileName"] == f"judgingpapers_{DATE}_custom4.pdf"


def test_a_name_taken_meanwhile_is_skipped(competition, blobs):
    generate()
    index = current_index()
    taken = f"{FOLDER}/judgePapers/judgingpapers_{DATE}_custom1.pdf"

    def someone_else(name):
        if name == taken and taken not in blobs.blobs:
            blobs.before_upload = None
            blobs.upload_blob(taken, b"%PDF-theirs")

    blobs.before_upload = someone_else

    made, = payload(custom(segment_only(index, FS)))["files"]

    assert made["fileName"] == f"judgingpapers_{DATE}_custom2.pdf"
    assert blobs.blobs[taken].data == b"%PDF-theirs"


def test_one_pdf_per_selected_day(competition, blobs):
    competition["days"] = [(DATE, PEOPLE), (DAY2, PEOPLE2)]
    generate()
    index = current_index()
    selection = everything(index)
    selection[DATE] = {"schedule": True}

    files = payload(custom(selection, contents=False))["files"]

    assert [(made["fileName"], made["packets"], made["pages"]) for made in files] == [
        (f"judgingpapers_{DATE}_custom1.pdf", 0, 4), (f"judgingpapers_{DAY2}_custom1.pdf", 1, 7)]
    assert blob_texts(blobs, f"{FOLDER}/judgePapers/{files[0]['fileName']}") == ["schedule 1", "schedule 2"] * 2
    assert files[0]["description"] == "Custom papers 1: 0 packets, 0 segments, 4 pages (PDF)"


def test_the_selection_is_remembered(competition, blobs):
    generate()
    index = current_index()
    selection = {DATE: {"schedule": False, "people": {
        "anna_judge": {"cover": True, "parts": [[FS, "judge"]]},
        "bert_referee": {"cover": False, "parts": []}}}}

    assert custom(selection).status_code == 200

    last = payload(index_state())["lastSelection"]
    assert last["selection"] == {DATE: {"schedule": False, "people": {
        "anna_judge": {"cover": True, "parts": [[FS, "judge"]]}}}}
    assert last["pages"] == 4 and last["savedUtc"].endswith("Z")
    # it outlives the papers it was made from
    blobs.upload_blob(f"{FOLDER}/{SOURCES[0]}", b"%PDF-again", overwrite=True)
    assert payload(index_state())["lastSelection"] == last


def test_a_custom_pdf_is_a_generated_file_like_the_others(competition):
    generate()
    made, = payload(custom(segment_only(current_index(), FS)))["files"]

    assert fa._SENDABLE_FILE_RE.fullmatch(made["fileName"])
    assert fa._LINKABLE_FILE_RE.fullmatch(made["fileName"])
    listed = {f["fileName"]: f for f in payload(call(
        fa.get_competition_details, make_request(params={"id": COMP_ID})))["generatedFiles"]}
    assert listed[made["fileName"]]["description"] == made["description"]
    assert sorted(listed) == [f"judgingPapers_{DATE}.zip", f"judgingpapers_{DATE}.pdf", made["fileName"]]


def refused(blobs, tables, response, status, body):
    """A refusal, and nothing written by it."""
    assert (response.status_code, text(response)) == (status, body)
    assert not any("_custom" in name for name in blobs.blobs)
    assert SELECTION_BLOB not in blobs.blobs
    assert "CustomGenerateRunCount" not in tables["competitions"].row("GLOBAL", COMP_ID)


def test_another_generation_is_refused(competition, blobs, tables):
    generate()
    index = current_index()
    generate()    # the papers were made again meanwhile

    refused(blobs, tables, custom(segment_only(index, FS), generation=index["generation"]), 409, fa.PAPERS_CHANGED)


@pytest.mark.parametrize("generation", ["", "nope", 7, None])
def test_a_missing_or_wrong_generation_is_refused(competition, blobs, tables, generation):
    generate()
    index = current_index()

    response = call(fa.generate_custom_papers, make_request("POST", body={
        "id": COMP_ID, "generation": generation, "selection": segment_only(index, FS)}))

    refused(blobs, tables, response, 409, fa.PAPERS_CHANGED)


def test_no_index_is_refused(competition, blobs, tables):
    refused(blobs, tables, custom({DATE: {"schedule": True}}, generation="x" * 32), 409, fa.PAPERS_CHANGED)


@pytest.mark.parametrize("damage", [
    lambda blobs: blobs.upload_blob(ZIP_BLOB, blobs.blobs[ZIP_BLOB].data + b" ", overwrite=True),
    lambda blobs: blobs.delete_blob(ZIP_BLOB),
    lambda blobs: blobs.delete_blob(SCHEDULE_BLOB),
    another_schedule,    # as many pages: only its bytes tell
])
def test_papers_that_are_not_the_index_s_are_refused(competition, blobs, tables, damage):
    generate()
    index = current_index()
    damage(blobs)

    refused(blobs, tables, custom(everything(index), generation=index["generation"]), 409, fa.PAPERS_CHANGED)


def test_an_overlapping_run_never_leaves_an_index_that_passes_for_current(competition, blobs, tables):
    # another run's ZIP lands after this run's ZIP and before this run's index
    def other_run(name):
        if name == INDEX_BLOB:
            blobs.before_upload = None
            blobs.upload_blob(ZIP_BLOB, b"the other run's zip", overwrite=True)

    blobs.before_upload = other_run
    generate()

    body = payload(index_state())
    assert (body["current"], body["reason"], body["index"]) == (False, "outputs", None)
    # and nothing is cut by it
    refused(blobs, tables, custom({DATE: {"schedule": True}}), 409, fa.PAPERS_CHANGED)


def test_a_changed_second_day_leaves_no_file_of_the_first(competition, blobs, tables):
    competition["days"] = [(DATE, PEOPLE), (DAY2, PEOPLE2)]
    generate()
    index = current_index()
    blobs.delete_blob(f"{FOLDER}/judgePapers/judgingPapers_{DAY2}.zip")

    refused(blobs, tables, custom(everything(index), generation=index["generation"]), 409, fa.PAPERS_CHANGED)


@pytest.mark.parametrize("selection, message", [
    (None, "the selection must be an object of days"),
    ({}, "nothing is selected"),
    ({"20270101": {"schedule": True}}, "unknown day"),
    ({DATE: {"people": {"Someone SECRET": {"cover": True}}}}, "unknown person"),
    ({DATE: {"people": {"anna_judge": {"parts": [["SECRET-PREFIX", "judge"]]}}}}, "unknown part"),
    ({DATE: {"people": {"cara_operator": {"parts": [[FS, "data_operator"]]}}}}, "nothing with pages is selected"),
])
def test_a_selection_that_does_not_fit_is_refused(competition, blobs, tables, selection, message):
    generate()

    response = custom(selection)

    refused(blobs, tables, response, 400, f"Invalid selection: {message}")
    assert "SECRET" not in text(response)


def test_a_huge_body_is_refused(competition, blobs, tables):
    generate()
    body = json.dumps({"id": COMP_ID, "generation": "x", "selection": {},
                       "padding": "x" * custom_papers.MAX_REQUEST_BYTES}).encode()

    response = call(fa.generate_custom_papers, make_request("POST", raw_body=body))

    refused(blobs, tables, response, 413, "The selection is too large")


@pytest.mark.parametrize("request_kw", [
    {"raw_body": b"not json"},
    {"body": []},
    {"body": {"generation": "x", "selection": {}}},
])
def test_the_custom_route_needs_a_competition_id(competition, request_kw):
    assert call(fa.generate_custom_papers, make_request("POST", **request_kw)).status_code == 400


def test_an_unreadable_index_is_a_retryable_error(competition, blobs, tables, monkeypatch):
    generate()
    index = current_index()
    monkeypatch.setattr(fa, "_read_papers_index", lambda *a: (_ for _ in ()).throw(RuntimeError("storage")))

    refused(blobs, tables, custom(segment_only(index, FS), generation=index["generation"]),
            503, "Storage unavailable, try again")
    assert index_state().status_code == 503


def test_the_logs_carry_no_names(competition, caplog):
    generate()
    with caplog.at_level("INFO"):
        custom(everything(current_index()))

    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "Custom papers of abc12345: 1 file(s), 24 page(s)" in logged
    assert not any(word in logged for word in ("JUDGE", "REFEREE", "OPERATOR", "anna", "bert", "cara"))
