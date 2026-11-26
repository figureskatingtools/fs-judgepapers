"""Trainees (issue #93): the stored list in metadata.json (trainees.py), the
settings route that writes it (strict, allowlisted, never wiping metadata.json
on a storage error), the details and generate routes that read it, and
delete_file refusing the record's own metadata.json."""
import hashlib
import json
import os
import unicodedata

import pytest
from azure.core.exceptions import HttpResponseError

import function_app as fa
import trainees as tr
from conftest import PLATFORM_ID, USER_EMAIL, call, make_request, payload, put_competition, text

COMP_ID = "abc12345"
SEG_A = "FSKMSINGLES-JUNIOR----QUAL000100--"
SEG_B = "FSKXICEDANCEJUNIOR----QUAL000100--"
NAME = "Väinö Åkerström"
UNDRAWABLE = "Li 李"   # CJK: no glyph in Liberation Sans


def trainee(name=NAME, role="judge", segments=(SEG_A,)):
    return {"name": name, "role": role, "segments": list(segments)}


# ---------------------------------------------------------------------------
# parse_trainees (strict, writes)
# ---------------------------------------------------------------------------

def test_parse_accepts_a_valid_list():
    raw = [trainee(), trainee(role="technical", segments=[SEG_B]), trainee("Ann Lee", segments=[])]

    clean, error = tr.parse_trainees(raw)

    assert error is None
    assert clean == raw


def test_parse_accepts_an_empty_list():
    assert tr.parse_trainees([]) == ([], None)


def test_parse_normalizes_name_segments_and_key_order():
    raw = [{"segments": [SEG_B, SEG_A, SEG_B, SEG_A], "role": "judge", "name": "  Väinö   Åkerström "}]

    clean, error = tr.parse_trainees(raw)

    assert error is None
    assert clean == [{"name": NAME, "role": "judge", "segments": [SEG_B, SEG_A]}]
    assert list(clean[0]) == ["name", "role", "segments"]


@pytest.mark.parametrize("name", ["Иван Петров", "Šárka Čermáková", "Łukasz Żółć", "O'Neil-Smith Jr."])
def test_parse_accepts_names_the_font_can_print(name):
    clean, error = tr.parse_trainees([trainee(name)])

    assert error is None
    assert clean[0]["name"] == name


@pytest.mark.parametrize("raw, message", [
    (None, "trainees must be a list"),
    ({"name": NAME}, "trainees must be a list"),
    ("x", "trainees must be a list"),
    ([trainee()] * 51, "at most 50 trainees"),
])
def test_parse_refuses_a_malformed_list(raw, message):
    assert tr.parse_trainees(raw) == (None, message)


def test_parse_accepts_the_maximum_counts():
    segments = [f"S{i}" for i in range(tr.MAX_SEGMENTS)]
    clean, error = tr.parse_trainees([trainee(segments=segments)] * tr.MAX_TRAINEES)

    assert error is None
    assert len(clean) == tr.MAX_TRAINEES and len(clean[0]["segments"]) == tr.MAX_SEGMENTS


BAD_ENTRIES = [
    # (entry, rule fragment)
    ("not an object", "exactly the keys"),
    ([NAME, "judge", []], "exactly the keys"),
    ({"name": NAME, "role": "judge"}, "exactly the keys"),
    ({**trainee(), "email": "x@example.com"}, "exactly the keys"),
    (trainee(name=""), "name must be 1 to 80 characters"),
    (trainee(name="   "), "name must be 1 to 80 characters"),
    (trainee(name=None), "name must be 1 to 80 characters"),
    (trainee(name=42), "name must be 1 to 80 characters"),
    (trainee(name="A" * 81), "name must be 1 to 80 characters"),
    (trainee(name="Ann\u0000Lee"), "name must be 1 to 80 characters"),
    (trainee(name="Ann​Lee"), "name must be 1 to 80 characters"),
    (trainee(name="ann@example.com"), "name must be 1 to 80 characters"),
    (trainee(name="Ann/Lee"), "name must be 1 to 80 characters"),
    (trainee(name=UNDRAWABLE), "the papers' font cannot print"),
    (trainee(name="Ann 😀"), "the papers' font cannot print"),
    (trainee(role="referee"), "role must be one of judge, technical"),
    (trainee(role="Judge"), "role must be one of judge, technical"),
    (trainee(role=None), "role must be one of judge, technical"),
    (trainee(role=["judge"]), "role must be one of judge, technical"),
    ({"name": NAME, "role": "judge", "segments": SEG_A}, "segments must be a list of at most 200"),
    ({"name": NAME, "role": "judge", "segments": None}, "segments must be a list of at most 200"),
    (trainee(segments=[f"S{i}" for i in range(201)]), "segments must be a list of at most 200"),
    (trainee(segments=[""]), "each segment must be 1 to 100 characters"),
    (trainee(segments=["A" * 101]), "each segment must be 1 to 100 characters"),
    (trainee(segments=["a/b"]), "each segment must be 1 to 100 characters"),
    (trainee(segments=["a\\b"]), "each segment must be 1 to 100 characters"),
    (trainee(segments=["a\nb"]), "each segment must be 1 to 100 characters"),
    (trainee(segments=[SEG_A, 7]), "each segment must be 1 to 100 characters"),
    (trainee(segments=[[SEG_A]]), "each segment must be 1 to 100 characters"),
]


@pytest.mark.parametrize("entry, rule", BAD_ENTRIES)
def test_parse_names_the_rule_and_the_position_never_the_name(entry, rule):
    clean, error = tr.parse_trainees([trainee("Ann Lee"), entry])

    assert clean is None
    assert error.startswith("trainee 2: ")
    assert rule in error
    for name in (NAME, "Ann Lee", "Ann", "Väinö", "ann@example.com", "李"):
        assert name not in error


# ---------------------------------------------------------------------------
# sanitize_trainees (lenient, reads)
# ---------------------------------------------------------------------------

def test_sanitize_keeps_a_valid_list_as_stored():
    stored = [trainee(), trainee("Ann Lee", "technical", [SEG_B])]

    assert tr.sanitize_trainees(stored) == stored


@pytest.mark.parametrize("raw", [None, {}, "x", 7, {"trainees": []}])
def test_sanitize_of_a_non_list_is_empty(raw):
    assert tr.sanitize_trainees(raw) == []


def test_sanitize_drops_bad_entries_and_bad_segments():
    raw = [
        "junk",
        {"role": "judge", "segments": [SEG_A]},          # no name
        trainee(name=UNDRAWABLE),
        trainee(role="referee"),
        trainee(name="A" * 81),
        {**trainee("Ann Lee"), "extra": True},            # extra key ignored on read
        {"name": "  Bo   Ek ", "role": "technical", "segments": [SEG_B, "", "a/b", 5, SEG_B, SEG_A]},
        {"name": "Cy Do", "role": "judge", "segments": "not a list"},
        {"name": "Ed Fo", "role": "judge"},
    ]

    assert tr.sanitize_trainees(raw) == [
        trainee("Ann Lee"),
        {"name": "Bo Ek", "role": "technical", "segments": [SEG_B, SEG_A]},
        {"name": "Cy Do", "role": "judge", "segments": []},
        {"name": "Ed Fo", "role": "judge", "segments": []},
    ]


def test_sanitize_caps_the_counts():
    raw = [trainee(segments=[f"S{i}" for i in range(250)])] * 60

    clean = tr.sanitize_trainees(raw)

    assert len(clean) == tr.MAX_TRAINEES
    assert clean[0]["segments"] == [f"S{i}" for i in range(tr.MAX_SEGMENTS)]


def test_trainee_tasks_and_roles():
    assert tr.TRAINEE_ROLES == ("judge", "technical")
    assert tr.TRAINEE_TASKS == {"judge": ("judge_trainee", "Judge Trainee"),
                                "technical": ("technical_trainee", "Technical Trainee")}


def _hashed(key):
    return "trainee-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]


@pytest.mark.parametrize("name, slug", [
    (NAME, "vaino_akerstrom"),
    ("Ann Lee", "ann_lee"),
    ("Иван Петров", _hashed("иван петров")),     # slugify keeps nothing but the space
    ("李", _hashed("李")),
    ("", _hashed("")),
])
def test_trainee_slug(name, slug):
    assert tr.trainee_slug(name) == slug


def test_the_fallback_slug_is_one_per_name_and_never_a_slugify_result():
    slug = tr.trainee_slug("Иван Петров")

    assert tr.trainee_slug("  иван   ПЕТРОВ ") == slug
    assert tr.trainee_slug(unicodedata.normalize("NFD", "Иван Петров")) == slug
    assert tr.trainee_slug("Пётр Иванов") != slug
    assert slug.startswith("trainee-") and len(slug) == len("trainee-") + 8
    # slugify drops "-", so no panel member or Latin-named trainee can get it
    assert tr.trainee_slug("Trainee 2") == "trainee_2"
    assert "-" not in tr.slugify("trainee-" + slug[8:])


# ---------------------------------------------------------------------------
# Names: NFC, letters, one packet per slug
# ---------------------------------------------------------------------------

def test_parse_and_sanitize_store_the_nfc_form():
    decomposed = unicodedata.normalize("NFD", NAME)
    assert decomposed != NAME

    clean, error = tr.parse_trainees([trainee(decomposed)])

    assert error is None and clean[0]["name"] == NAME
    assert tr.sanitize_trainees([trainee(decomposed)])[0]["name"] == NAME


@pytest.mark.parametrize("name", ["\u0308", "- . -", "'", "\u0301\u0308"])
def test_parse_refuses_a_name_without_a_letter_or_digit(name):
    assert tr.parse_trainees([trainee(name)]) == (
        None, "trainee 1: name must contain at least one letter or digit")
    assert tr.sanitize_trainees([trainee(name)]) == []


@pytest.mark.parametrize("segment", [" ", f" {SEG_A}", f"{SEG_A} ", "\u00a0X"])
def test_parse_refuses_a_segment_with_outer_spaces(segment):
    clean, error = tr.parse_trainees([trainee(segments=[segment])])

    assert clean is None and "without leading or trailing spaces" in error
    assert tr.sanitize_trainees([trainee(segments=[segment, SEG_A])])[0]["segments"] == [SEG_A]


@pytest.mark.parametrize("first, second", [
    ("Anna-Maria Xu", "Annamaria Xu"),        # both annamaria_xu
    ("O'Neil Lee", "ONeil Lee"),              # both oneil_lee
])
def test_parse_refuses_two_names_that_would_share_a_packet(first, second):
    assert tr.trainee_slug(first) == tr.trainee_slug(second)
    clean, error = tr.parse_trainees([trainee("Bo Ek"), trainee(first), trainee(second, "technical")])

    assert clean is None
    assert error == ("trainee 3: name is too close to trainee 2's (they would share one packet); "
                     "use the same spelling for the same person")


def test_parse_accepts_the_same_person_in_two_roles_whatever_the_case_or_spacing():
    raw = [trainee("Ann Lee"), trainee("ANN  lee", "technical", [SEG_B]), trainee("Ann Lée", segments=[SEG_B]),
           trainee("Иван Петров"), trainee("иван петров", "technical")]

    clean, error = tr.parse_trainees(raw)

    assert error is None and len(clean) == 5


@pytest.mark.parametrize("a, b, same", [
    ("Nina Gustafsson", "Nina GUSTAFSSON", True),          # case
    ("Vaino Akerstrom", "Väinö ÅKERSTRÖM", True),          # diacritics
    ("  Väinö   Åkerström ", unicodedata.normalize("NFD", NAME), True),   # spacing, Unicode form
    ("Pётр Иванов", "Pетр Иванов", True),
    ("Annamaria Xu", "Anna-Maria Xu", False),              # same slug, another person
    ("O'Neil Lee", "ONeil Lee", False),
    ("Ann Lee", "Anne Lee", False),
])
def test_same_person_differs_only_in_case_spacing_and_diacritics(a, b, same):
    assert tr.same_person(a, b) is same
    assert (tr.person_key(a) == tr.person_key(b)) is same


def test_separate_slug_is_never_a_slugify_result_or_a_trainee_slug():
    slug = tr.separate_slug("annamaria_xu")

    assert slug == "annamaria_xu-trainee"
    assert "-" not in tr.slugify(slug)
    assert not slug.startswith("trainee-")
    assert tr.separate_slug(tr.trainee_slug("Иван Петров")).endswith("-trainee")


def test_the_numbered_fallback_no_longer_collides_with_a_real_name():
    # used to be trainee_2 for both
    clean, error = tr.parse_trainees([trainee("Trainee 2"), trainee("Иван Петров")])

    assert error is None and len(clean) == 2


def test_sanitize_drops_a_later_entry_that_would_share_a_packet():
    raw = [trainee("Anna-Maria Xu"), trainee("Annamaria Xu", "technical"), trainee("ANNA-MARIA XU", "technical")]

    assert tr.sanitize_trainees(raw) == [trainee("Anna-Maria Xu"), trainee("ANNA-MARIA XU", "technical")]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@pytest.fixture
def competition(tables, blobs, monkeypatch):
    monkeypatch.setattr(fa, "_get_categories", lambda: [])
    row = put_competition(tables, comp_id=COMP_ID, platform_id=PLATFORM_ID,
                          deletion="2099-01-01T00:00:00Z")
    blobs.upload_blob(f"{row['FolderPath']}/metadata.json", json.dumps({
        "id": COMP_ID, "name": "Spring Cup", "createdBy": USER_EMAIL,
        "judgingMethodOverrides": {"SYNC": "MUPI"},
    }))
    blobs.upload_blob(f"{row['FolderPath']}/X_Other.pdf", b"%PDF-source")
    return row


def metadata(blobs, row):
    return json.loads(blobs.blobs[f"{row['FolderPath']}/metadata.json"].data)


def save(settings, **kw):
    return call(fa.save_competition_settings, make_request(
        "POST", body={"id": COMP_ID, "settings": settings}, **kw))


def test_settings_store_the_normalized_list_and_keep_the_other_keys(competition, blobs, caplog):
    caplog.set_level("DEBUG")
    raw = [{"segments": [SEG_A, SEG_A], "name": "  Väinö  Åkerström", "role": "judge"}]

    response = save({"trainees": raw})

    assert response.status_code == 200
    assert text(response) == "Settings saved"
    assert metadata(blobs, competition) == {
        "id": COMP_ID, "name": "Spring Cup", "createdBy": USER_EMAIL,
        "judgingMethodOverrides": {"SYNC": "MUPI"},
        "trainees": [trainee()],
    }
    assert "Väinö" not in caplog.text and "Åkerström" not in caplog.text


def test_settings_save_both_keys_and_an_empty_list(competition, blobs):
    assert save({"trainees": [trainee()]}).status_code == 200

    response = save({"trainees": [], "judgingMethodOverrides": {}})

    assert response.status_code == 200
    meta = metadata(blobs, competition)
    assert meta["trainees"] == [] and meta["judgingMethodOverrides"] == {}


@pytest.mark.parametrize("settings, message", [
    ({"trainees": [trainee(role="referee")]}, "trainee 1: role must be one of judge, technical"),
    ({"trainees": None}, "trainees must be a list"),
    ({"trainees": [trainee(name=UNDRAWABLE)]}, "trainee 1: name has a character the papers' font cannot print"),
    ({"language": "en"}, "Unknown setting; allowed: judgingMethodOverrides, trainees"),
    ({"name": "Hijacked"}, "Unknown setting; allowed: judgingMethodOverrides, trainees"),
    ({"trainees": [], "createdBy": "x@example.com"}, "Unknown setting; allowed: judgingMethodOverrides, trainees"),
])
def test_settings_refuse_malformed_trainees_and_unknown_keys(settings, message, competition, blobs):
    before = blobs.blobs[f"{competition['FolderPath']}/metadata.json"].data

    response = save(settings)

    assert response.status_code == 400
    assert text(response) == message
    assert blobs.blobs[f"{competition['FolderPath']}/metadata.json"].data == before


@pytest.mark.parametrize("body", [[{"id": COMP_ID}], "x", 7, None])
def test_settings_refuse_a_body_that_is_not_an_object(body, competition, blobs):
    before = blobs.blobs[f"{competition['FolderPath']}/metadata.json"].data

    response = call(fa.save_competition_settings, make_request(
        "POST", raw_body=json.dumps(body).encode("utf-8"), headers={"content-type": "application/json"}))

    assert response.status_code == 400
    assert text(response) == "Invalid JSON body"
    assert blobs.blobs[f"{competition['FolderPath']}/metadata.json"].data == before


def test_settings_storage_error_is_503_and_writes_nothing(competition, blobs, monkeypatch):
    path = f"{competition['FolderPath']}/metadata.json"
    before = blobs.blobs[path].data
    original = blobs.get_blob_client

    def flaky(name):
        client = original(name)
        if name == path:
            def fail():
                raise HttpResponseError("throttled")
            client.download_blob = fail
        return client

    monkeypatch.setattr(blobs, "get_blob_client", flaky)

    response = save({"trainees": [trainee()]})

    assert response.status_code == 503
    assert text(response) == "Storage unavailable, try again"
    assert blobs.blobs[path].data == before


@pytest.mark.parametrize("content", [None, b"not json", b"[1, 2]", b"\xff\xfe", b"null"])
def test_settings_rebuild_a_missing_or_malformed_metadata_json(content, competition, blobs):
    path = f"{competition['FolderPath']}/metadata.json"
    if content is None:
        del blobs.blobs[path]
    else:
        blobs.upload_blob(path, content, overwrite=True)

    response = save({"trainees": [trainee()]})

    assert response.status_code == 200
    assert metadata(blobs, competition) == {"id": COMP_ID, "name": "Spring Cup", "trainees": [trainee()]}


def test_settings_read_only_caller_is_refused_and_nothing_written(competition, blobs, access):
    access.grant("read")
    before = blobs.blobs[f"{competition['FolderPath']}/metadata.json"].data

    response = save({"trainees": [trainee()]})

    assert response.status_code == 403
    assert text(response).startswith("platform_read_only")
    assert blobs.blobs[f"{competition['FolderPath']}/metadata.json"].data == before


def test_details_return_the_sanitized_trainees(competition, blobs):
    path = f"{competition['FolderPath']}/metadata.json"
    meta = metadata(blobs, competition)
    meta["trainees"] = [trainee(), trainee(name=UNDRAWABLE), {"name": "Bo Ek", "role": "x", "segments": []}]
    blobs.upload_blob(path, json.dumps(meta), overwrite=True)

    response = call(fa.get_competition_details, make_request(params={"id": COMP_ID}))

    assert response.status_code == 200, text(response)
    assert payload(response)["trainees"] == [trainee()]


def test_details_without_trainees_return_an_empty_list(competition):
    response = call(fa.get_competition_details, make_request(params={"id": COMP_ID}))

    assert payload(response)["trainees"] == []


def _generate(monkeypatch, stats, options=None):
    seen = {}

    def process(source_dir, output_dir, options=None, index_dir=None):
        seen["options"] = options
        with open(os.path.join(output_dir, "judgingpapers_2026-11-07.pdf"), "wb") as fh:
            fh.write(b"%PDF-out")
        return stats

    monkeypatch.setattr(fa, "process_judging_papers", process)
    response = call(fa.generate_judging_papers, make_request(
        "POST", body={"workingFolder": COMP_ID, "options": options or {}}))
    return response, seen.get("options")


def test_generate_passes_the_stored_trainees_not_the_clients(competition, blobs, monkeypatch):
    assert save({"trainees": [trainee()]}).status_code == 200

    response, options = _generate(monkeypatch, {}, options={
        "trainees": [trainee("Injected Name", "technical", [SEG_B])], "globalCombinedSchedule": True})

    assert response.status_code == 200, text(response)
    assert options["trainees"] == [trainee()]
    assert options["globalCombinedSchedule"] is True
    assert "Warning" not in text(response)


def test_generate_without_stored_trainees_passes_an_empty_list(competition, monkeypatch):
    _response, options = _generate(monkeypatch, None, options={"trainees": [trainee()]})

    assert options["trainees"] == []


@pytest.mark.parametrize("stats, warning", [
    ({"trainee_sheets_unstamped": 3},
     " Warning: 3 page(s) of trainee sheets still show the original name."),
    ({"trainee_segments_without_sheets": 1},
     " Warning: 1 trainee segment(s) got no sheets to copy."),
    ({"trainee_sheets_unstamped": 3, "trainee_segments_without_sheets": 1},
     " Warning: 3 page(s) of trainee sheets still show the original name. "
     "1 trainee segment(s) got no sheets to copy."),
])
def test_generate_warns_once_about_trainee_sheets(stats, warning, competition, monkeypatch):
    response, _options = _generate(monkeypatch, stats)

    assert response.status_code == 200
    assert text(response).endswith(" output files." + warning)
    assert text(response).count(" Warning:") == 1


@pytest.mark.parametrize("value", [0, None, -1, "3", True, 2.0])
def test_generate_has_no_warning_without_trainee_problems(value, competition, monkeypatch):
    response, _options = _generate(monkeypatch, {"trainee_sheets_unstamped": value,
                                                 "trainee_segments_without_sheets": value})

    assert response.status_code == 200
    assert "Warning" not in text(response)


def test_generate_stores_the_trainee_statistics(competition, tables, monkeypatch):
    _generate(monkeypatch, {"trainee_count": 2, "trainee_assignment_count": 5})

    row = tables["competitions"].row("GLOBAL", COMP_ID)
    assert (row["TraineeCount"], row["TraineeAssignmentCount"]) == (2, 5)


def _delete_file(filename):
    return call(fa.delete_file, make_request(
        "POST", params={"competition": COMP_ID, "filename": filename}))


@pytest.mark.parametrize("filename", ["metadata.json", "init.md"])
def test_delete_file_refuses_the_records_own_files(filename, competition, blobs):
    path = f"{competition['FolderPath']}/{filename}"
    blobs.upload_blob(path, b"{}", overwrite=True)

    response = _delete_file(filename)

    assert response.status_code == 400
    assert text(response) == "Invalid filename"
    assert path in blobs.blobs


def test_delete_file_still_deletes_a_competition_file(competition, blobs):
    response = _delete_file("X_Other.pdf")

    assert payload(response) == {"status": "deleted"}
    assert f"{competition['FolderPath']}/X_Other.pdf" not in blobs.blobs


# ---------------------------------------------------------------------------
# An unreadable metadata.json is a 503, never empty settings
# ---------------------------------------------------------------------------

@pytest.fixture
def unreadable_metadata(competition, blobs, monkeypatch):
    """Every download of the competition's metadata.json fails (throttled)."""
    path = f"{competition['FolderPath']}/metadata.json"
    original = blobs.get_blob_client

    def flaky(name):
        client = original(name)
        if name == path:
            def fail():
                raise HttpResponseError("throttled")
            client.download_blob = fail
        return client

    monkeypatch.setattr(blobs, "get_blob_client", flaky)
    return path


def test_details_with_an_unreadable_metadata_json_is_503(unreadable_metadata, blobs):
    before = blobs.blobs[unreadable_metadata].data

    response = call(fa.get_competition_details, make_request(params={"id": COMP_ID}))

    assert response.status_code == 503
    assert text(response) == "Storage unavailable, try again"
    assert blobs.blobs[unreadable_metadata].data == before


def test_details_without_metadata_json_are_empty_settings_and_write_nothing(competition, blobs, caplog):
    path = f"{competition['FolderPath']}/metadata.json"
    del blobs.blobs[path]
    caplog.set_level("WARNING")

    response = call(fa.get_competition_details, make_request(params={"id": COMP_ID}))

    assert response.status_code == 200, text(response)
    assert payload(response)["trainees"] == [] and payload(response)["judgingMethodOverrides"] == {}
    assert path not in blobs.blobs
    assert "metadata.json" not in caplog.text


def test_generate_with_an_unreadable_metadata_json_is_503_and_generates_nothing(
        unreadable_metadata, blobs, tables, monkeypatch):
    monkeypatch.setattr(fa, "process_judging_papers", lambda *a, **k: pytest.fail("generated"))
    before = dict(blobs.blobs)

    response = call(fa.generate_judging_papers, make_request(
        "POST", body={"workingFolder": COMP_ID, "options": {"trainees": [trainee()]}}))

    assert response.status_code == 503
    assert text(response) == "Storage unavailable, try again"
    assert blobs.blobs == before
    assert "GenerateRunCount" not in tables["competitions"].row("GLOBAL", COMP_ID)


# ---------------------------------------------------------------------------
# Optimistic concurrency on metadata.json
# ---------------------------------------------------------------------------

def concurrent_writer(blobs, path, change, times=1):
    """Before each of our next `times` uploads of path, someone else writes `change` into it."""
    left = [times]

    def before_upload(name):
        if name != path or left[0] == 0:
            return
        left[0] -= 1
        blobs.before_upload = None                     # their own write isn't intercepted
        meta = json.loads(blobs.blobs[path].data) if path in blobs.blobs else {}
        meta.update(change)
        blobs.upload_blob(path, json.dumps(meta), overwrite=True)
        blobs.before_upload = before_upload

    blobs.before_upload = before_upload


def test_a_concurrent_save_of_the_other_key_survives(competition, blobs):
    path = f"{competition['FolderPath']}/metadata.json"
    concurrent_writer(blobs, path, {"judgingMethodOverrides": {"SYNC": "ISU"}})

    response = save({"trainees": [trainee()]})

    assert response.status_code == 200
    meta = metadata(blobs, competition)
    assert meta["trainees"] == [trainee()]                       # ours
    assert meta["judgingMethodOverrides"] == {"SYNC": "ISU"}     # theirs
    assert meta["createdBy"] == USER_EMAIL


def test_a_concurrent_creator_of_a_missing_file_is_not_overwritten(competition, blobs):
    path = f"{competition['FolderPath']}/metadata.json"
    del blobs.blobs[path]
    concurrent_writer(blobs, path, {"id": COMP_ID, "createdBy": USER_EMAIL,
                                    "judgingMethodOverrides": {"S": "ISU"}})

    response = save({"trainees": [trainee()]})

    assert response.status_code == 200
    meta = metadata(blobs, competition)
    assert meta["createdBy"] == USER_EMAIL and meta["judgingMethodOverrides"] == {"S": "ISU"}
    assert meta["trainees"] == [trainee()]


def test_a_file_that_keeps_changing_is_409_and_keeps_the_other_writes(competition, blobs):
    path = f"{competition['FolderPath']}/metadata.json"
    concurrent_writer(blobs, path, {"judgingMethodOverrides": {"SYNC": "ISU"}},
                      times=fa.SETTINGS_SAVE_ATTEMPTS)

    response = save({"trainees": [trainee()]})

    assert response.status_code == 409
    assert text(response) == "Settings changed meanwhile, try again"
    meta = metadata(blobs, competition)
    assert "trainees" not in meta and meta["judgingMethodOverrides"] == {"SYNC": "ISU"}


def test_a_rename_racing_a_save_leaves_metadata_json_alone(competition, blobs, tables):
    path = f"{competition['FolderPath']}/metadata.json"
    concurrent_writer(blobs, path, {"trainees": [trainee()]})
    entity = dict(tables["competitions"].row("GLOBAL", COMP_ID))

    name = fa._sync_competition_name(tables["competitions"], fa.get_blob_service_client(), entity, "Autumn Cup")

    assert name == "Autumn Cup"                                     # the table is authoritative
    assert tables["competitions"].row("GLOBAL", COMP_ID)["Name"] == "Autumn Cup"
    meta = metadata(blobs, competition)
    assert meta["trainees"] == [trainee()] and meta["name"] == "Spring Cup"


def test_a_rename_without_a_race_updates_metadata_json(competition, blobs, tables):
    entity = dict(tables["competitions"].row("GLOBAL", COMP_ID))

    fa._sync_competition_name(tables["competitions"], fa.get_blob_service_client(), entity, "Autumn Cup")

    meta = metadata(blobs, competition)
    assert meta["name"] == "Autumn Cup" and meta["judgingMethodOverrides"] == {"SYNC": "MUPI"}


def test_the_fake_blob_store_behaves_like_the_sdk(blobs):
    from azure.core import MatchConditions
    from azure.core.exceptions import ResourceExistsError, ResourceModifiedError

    client = blobs.get_blob_client("a/metadata.json")
    client.upload_blob(b"{}", overwrite=False)
    with pytest.raises(ResourceExistsError):
        client.upload_blob(b"{}", overwrite=False)
    etag = client.download_blob().properties.etag
    client.upload_blob(b'{"a": 1}', overwrite=True, etag=etag, match_condition=MatchConditions.IfNotModified)
    assert client.download_blob().properties.etag != etag
    with pytest.raises(ResourceModifiedError):
        client.upload_blob(b"{}", overwrite=True, etag=etag, match_condition=MatchConditions.IfNotModified)
    with pytest.raises(ResourceExistsError):     # the SDK converts a 412 when overwrite=False
        client.upload_blob(b"{}", overwrite=False, etag=etag, match_condition=MatchConditions.IfNotModified)
    assert client.download_blob().readall() == b'{"a": 1}'
