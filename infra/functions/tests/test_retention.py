"""Retention is the platform's alone: resolve, details, the nightly
auto-deletion and the e-mail scrubbing of deleted rows."""
import logging
from datetime import datetime, timedelta, timezone

import pytest
from azure.core.exceptions import HttpResponseError

import function_app as fa
from conftest import (
    PLATFORM_ID,
    USER_EMAIL,
    call,
    make_request,
    payload,
    put_competition,
    put_platform_competition,
    text,
)

NOW = datetime(2026, 10, 13, 3, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve(name="Spring Cup", platform_id=PLATFORM_ID):
    return call(fa.resolve_competition, make_request(
        "POST", body={"platformId": platform_id, "name": name}))


def add_metadata(blobs, row):
    blobs.upload_blob(f"{row['FolderPath']}/metadata.json",
                      f'{{"id": "{row["RowKey"]}", "createdBy": "{USER_EMAIL}"}}')


# ---------------------------------------------------------------------------
# resolve_competition
# ---------------------------------------------------------------------------

def test_resolve_syncs_the_bound_record_to_the_platform_date(tables, blobs, platform):
    put_competition(tables, platform_id=PLATFORM_ID, deletion="2026-11-01T10:00:00.123456Z")
    put_platform_competition(platform, deletion="2026-12-01T00:00:00Z")

    body = payload(resolve())

    assert body == {"id": "abc12345", "name": "Spring Cup", "created": False,
                    "deletionDate": "2026-12-01T00:00:00Z"}
    assert tables["competitions"].row("GLOBAL", "abc12345")["DeletionDate"] == "2026-12-01T00:00:00Z"


def test_a_new_record_gets_the_platform_date_and_no_clock_of_its_own(tables, blobs, platform):
    put_platform_competition(platform, deletion="2026-11-20T00:00:00Z")

    body = payload(resolve())

    assert body["created"] is True
    assert body["deletionDate"] == "2026-11-20T00:00:00Z"
    row = tables["competitions"].row("GLOBAL", body["id"])
    assert row["DeletionDate"] == "2026-11-20T00:00:00Z"
    assert row["PlatformId"] == PLATFORM_ID


def test_a_new_record_before_the_platform_has_a_date_gets_none(tables, blobs, platform):
    put_platform_competition(platform, deletion=None)

    body = payload(resolve())

    assert body["deletionDate"] is None
    assert "DeletionDate" not in tables["competitions"].row("GLOBAL", body["id"])


def test_an_adopted_record_is_synced_too(tables, blobs, platform):
    put_competition(tables, name="Spring Cup", deletion="2027-02-01T10:00:00Z")
    put_platform_competition(platform, deletion="2026-12-01T00:00:00Z")

    body = payload(resolve("Spring Cup"))

    assert body["id"] == "abc12345" and body["created"] is False
    row = tables["competitions"].row("GLOBAL", "abc12345")
    assert row["PlatformId"] == PLATFORM_ID
    assert row["DeletionDate"] == "2026-12-01T00:00:00Z"


@pytest.mark.parametrize("status, purged", [("deleted", None), ("deleted", "2026-10-10T03:00:00Z"),
                                            ("active", "2026-10-10T03:00:00Z")])
def test_resolve_refuses_a_deleted_platform_competition(tables, blobs, platform, status, purged):
    put_platform_competition(platform, status=status, purged=purged)

    response = resolve()

    assert response.status_code == 410
    assert text(response).startswith("platform_deleted")
    assert tables["competitions"].rows == {}
    assert blobs.blobs == {}


def test_an_existing_record_resolves_on_its_cached_date_when_the_platform_is_unreachable(
        tables, blobs, platform, caplog):
    put_competition(tables, platform_id=PLATFORM_ID, deletion="2026-11-01T10:00:00Z")
    platform.fail_get = HttpResponseError("throttled")

    with caplog.at_level(logging.ERROR):
        response = resolve()

    assert response.status_code == 200
    assert payload(response)["deletionDate"] == "2026-11-01T10:00:00Z"
    assert tables["competitions"].row("GLOBAL", "abc12345")["DeletionDate"] == "2026-11-01T10:00:00Z"
    assert any("unavailable" in r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)


def test_no_new_binding_while_the_platform_is_unreachable(tables, blobs, platform):
    put_competition(tables, name="Spring Cup")  # an unbound row it could adopt
    platform.fail_get = HttpResponseError("throttled")

    response = resolve()

    assert response.status_code == 503
    assert text(response).startswith("platform_unavailable")
    assert "PlatformId" not in tables["competitions"].row("GLOBAL", "abc12345")
    assert len(tables["competitions"].rows) == 1


def test_no_new_binding_to_a_platform_competition_that_does_not_exist(tables, blobs, platform):
    response = resolve()

    assert response.status_code == 404
    assert text(response).startswith("platform_not_found")
    assert tables["competitions"].rows == {}


def test_an_existing_record_whose_platform_row_is_missing_keeps_its_cached_date(tables, blobs, platform):
    put_competition(tables, platform_id=PLATFORM_ID, deletion="2026-11-01T10:00:00Z")

    body = payload(resolve())

    assert body["deletionDate"] == "2026-11-01T10:00:00Z"


def test_without_a_registry_configured_resolve_binds_without_a_date(tables, blobs):
    # Local development: PLATFORM_STORAGE_ACCOUNT unset.
    body = payload(resolve())

    assert body["created"] is True and body["deletionDate"] is None
    row = tables["competitions"].row("GLOBAL", body["id"])
    assert row["PlatformId"] == PLATFORM_ID and "DeletionDate" not in row


def test_a_differently_cased_id_finds_the_same_record(tables, blobs, platform):
    put_competition(tables, platform_id=PLATFORM_ID)
    put_platform_competition(platform)

    body = payload(resolve(platform_id=f" {PLATFORM_ID.upper()} "))

    assert body["id"] == "abc12345" and body["created"] is False
    assert len(tables["competitions"].rows) == 1


def test_a_record_stored_under_another_casing_is_found_and_normalized(tables, blobs, platform):
    put_competition(tables, platform_id=PLATFORM_ID.upper())
    put_platform_competition(platform)

    body = payload(resolve(platform_id=PLATFORM_ID.upper()))

    assert body["id"] == "abc12345" and body["created"] is False
    assert tables["competitions"].row("GLOBAL", "abc12345")["PlatformId"] == PLATFORM_ID


def test_a_new_record_stores_the_normalized_id(tables, blobs, platform):
    put_platform_competition(platform)

    body = payload(resolve(platform_id=PLATFORM_ID.upper()))

    assert tables["competitions"].row("GLOBAL", body["id"])["PlatformId"] == PLATFORM_ID
    assert ("get_entity", ("COMPETITION", PLATFORM_ID)) in platform.calls


def test_a_platform_id_that_is_not_a_guid_is_rejected(tables, blobs, platform):
    response = resolve(platform_id="not-a-guid")
    assert response.status_code == 400
    assert tables["competitions"].rows == {}


def test_no_route_creates_unbound_records_or_extends_dates():
    assert not hasattr(fa, "create_competition")
    assert not hasattr(fa, "extend_competition_deletion")
    with pytest.raises(ValueError):
        fa.create_competition_record(None, None, "Cup", USER_EMAIL, None)


# ---------------------------------------------------------------------------
# get_competition_details
# ---------------------------------------------------------------------------

def details(comp_id="abc12345"):
    return payload(call(fa.get_competition_details,
                        make_request(params={"id": comp_id})))


@pytest.fixture
def no_categories(monkeypatch):
    monkeypatch.setattr(fa, "_get_categories", lambda: [])


def test_details_report_the_platform_date(tables, blobs, platform, no_categories):
    row = put_competition(tables, platform_id=PLATFORM_ID, deletion="2026-11-01T10:00:00Z")
    add_metadata(blobs, row)
    put_platform_competition(platform, deletion="2026-12-01T00:00:00Z")
    # A generated-papers row must not shadow the competition entity.
    tables["generatedpapers"].put(PartitionKey="abc12345", RowKey="judgingpapers_2026-11-01.pdf",
                                  FileName="judgingpapers_2026-11-01.pdf", Url="https://x")

    body = details()

    assert body["deletionDate"] == "2026-12-01T00:00:00Z"
    assert "retentionManagedBy" not in body
    assert tables["competitions"].row("GLOBAL", "abc12345")["DeletionDate"] == "2026-12-01T00:00:00Z"


def test_details_fall_back_to_the_cached_date(tables, blobs, platform, no_categories):
    row = put_competition(tables, platform_id=PLATFORM_ID, deletion="2026-11-01T10:00:00Z")
    add_metadata(blobs, row)
    platform.fail_get = HttpResponseError("throttled")
    tables["generatedpapers"].put(PartitionKey="abc12345", RowKey="judgingpapers_2026-11-01.pdf",
                                  FileName="judgingpapers_2026-11-01.pdf", Url="https://x")

    assert details()["deletionDate"] == "2026-11-01T10:00:00Z"


# ---------------------------------------------------------------------------
# Nightly auto-deletion
# ---------------------------------------------------------------------------

@pytest.fixture
def sweep(tables, blobs, platform):
    def run():
        return fa.run_auto_deletion(tables["competitions"], NOW)
    return run


def stored(tables, comp_id):
    return tables["competitions"].row("GLOBAL", comp_id)


def seed(tables, blobs, comp_id, **kwargs):
    row = put_competition(tables, comp_id=comp_id, name=f"Cup {comp_id}", **kwargs)
    add_metadata(blobs, row)
    blobs.upload_blob(f"{row['FolderPath']}/A_JudgesSheetAll.pdf", b"%PDF")
    return row


def test_a_passed_platform_date_alone_never_deletes(tables, blobs, platform, sweep):
    # The platform may be in dry-run, or the competition was just extended:
    # only its verdict (deleted / purged) counts.
    seed(tables, blobs, "bound001", platform_id=PLATFORM_ID, deletion=iso(NOW - timedelta(days=3)))
    put_platform_competition(platform, deletion=iso(NOW - timedelta(hours=3)))

    assert sweep()["deleted"] == 0
    assert stored(tables, "bound001")["Visible"] is True


def test_a_bound_record_goes_once_the_platform_has_purged_it(tables, blobs, platform, sweep):
    seed(tables, blobs, "bound001", platform_id=PLATFORM_ID, deletion=iso(NOW + timedelta(days=20)))
    put_platform_competition(platform, deletion=iso(NOW - timedelta(hours=3)), status="deleted",
                             purged=iso(NOW - timedelta(hours=1)))

    assert sweep()["deleted"] == 1
    assert stored(tables, "bound001")["Visible"] is False
    assert not any(name.startswith("Cup bound001-") for name in blobs.blobs)


def test_the_sweep_reads_the_platform_with_one_client(tables, blobs, monkeypatch, platform):
    opened = []
    monkeypatch.setattr(fa, "get_platform_table_client", lambda: opened.append(1) or platform)
    other = "11111111-2222-4333-8444-555555555555"
    seed(tables, blobs, "bound001", platform_id=PLATFORM_ID)
    seed(tables, blobs, "bound002", platform_id=other)
    put_platform_competition(platform)
    put_platform_competition(platform, platform_id=other)

    fa.run_auto_deletion(tables["competitions"], NOW)

    assert len(opened) == 1
    assert len([c for c in platform.calls if c[0] == "get_entity"]) == 2


def test_the_timer_runs_after_the_platform_sweep():
    bindings = fa.auto_delete_expired_competitions._function.get_bindings()
    assert bindings[0].schedule == "0 0 4 * * *"


def test_the_platform_date_keeps_a_bound_record_past_its_cached_date(tables, blobs, platform, sweep):
    seed(tables, blobs, "bound001", platform_id=PLATFORM_ID, deletion=iso(NOW - timedelta(days=2)))
    put_platform_competition(platform, deletion=iso(NOW + timedelta(days=5)))

    stats = sweep()

    assert stats["deleted"] == 0 and stats["synced"] == 1
    assert stored(tables, "bound001")["Visible"] is True
    assert stored(tables, "bound001")["DeletionDate"] == iso(NOW + timedelta(days=5))


@pytest.mark.parametrize("status, purged", [("deleted", None), ("deleted", "2026-10-12T03:00:00Z")])
def test_a_deleted_or_purged_platform_competition_is_deleted_at_once(tables, blobs, platform, sweep,
                                                                      status, purged):
    seed(tables, blobs, "bound001", platform_id=PLATFORM_ID, deletion=iso(NOW + timedelta(days=40)))
    put_platform_competition(platform, deletion=iso(NOW + timedelta(days=40)), status=status, purged=purged)

    assert sweep()["deleted"] == 1
    assert stored(tables, "bound001")["Visible"] is False


def test_a_platform_row_without_a_date_yet_keeps_the_record(tables, blobs, platform, sweep):
    # No clock of the tool's own: the cached date doesn't decide while the
    # platform row exists (the platform backfills its date nightly).
    seed(tables, blobs, "bound001", platform_id=PLATFORM_ID, deletion=iso(NOW - timedelta(days=1)))
    put_platform_competition(platform, deletion=None)

    assert sweep()["deleted"] == 0
    assert stored(tables, "bound001")["Visible"] is True


def test_an_unreachable_platform_never_deletes_early(tables, blobs, platform, sweep, caplog):
    seed(tables, blobs, "bound001", platform_id=PLATFORM_ID, deletion=iso(NOW - timedelta(days=2)))
    platform.fail_get = HttpResponseError("throttled")

    with caplog.at_level(logging.ERROR):
        stats = sweep()

    assert stats == {"deleted": 0, "skipped": 1, "failed": 0, "synced": 0}
    assert stored(tables, "bound001")["Visible"] is True
    assert any("bound001" in r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)


def test_a_registry_that_cannot_even_be_opened_skips_bound_rows(tables, blobs, monkeypatch):
    def broken():
        raise ValueError("no credential")
    monkeypatch.setattr(fa, "get_platform_table_client", broken)
    seed(tables, blobs, "bound001", platform_id=PLATFORM_ID, deletion=iso(NOW - timedelta(days=2)))
    seed(tables, blobs, "legacy01", deletion=iso(NOW - timedelta(days=2)))

    stats = fa.run_auto_deletion(tables["competitions"], NOW)

    assert stats["skipped"] == 1 and stats["deleted"] == 1
    assert stored(tables, "bound001")["Visible"] is True
    assert stored(tables, "legacy01")["Visible"] is False


def test_without_a_registry_configured_bound_rows_are_skipped(tables, blobs, caplog):
    seed(tables, blobs, "bound001", platform_id=PLATFORM_ID, deletion=iso(NOW - timedelta(days=2)))
    seed(tables, blobs, "bound002", platform_id=PLATFORM_ID, deletion=iso(NOW - timedelta(days=2)))

    with caplog.at_level(logging.ERROR):
        stats = fa.run_auto_deletion(tables["competitions"], NOW)

    assert stats["skipped"] == 2 and stats["deleted"] == 0
    errors = [r for r in caplog.records if "PLATFORM_STORAGE_ACCOUNT" in r.getMessage()]
    assert len(errors) == 1


def test_a_missing_clock_row_keeps_the_record(tables, blobs, platform, sweep, caplog):
    # No date-based deletion without the platform: a missing row is kept.
    seed(tables, blobs, "bound001", platform_id=PLATFORM_ID, deletion=iso(NOW - timedelta(days=9)))
    seed(tables, blobs, "bound002", platform_id=PLATFORM_ID.upper(), deletion=None)

    with caplog.at_level(logging.WARNING):
        stats = sweep()

    assert stats["deleted"] == 0 and stats["skipped"] == 2
    assert stored(tables, "bound001")["Visible"] is True
    assert any("bound001" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


def test_a_missing_clock_table_skips_bound_rows(tables, blobs, platform, sweep):
    from azure.core.exceptions import ResourceNotFoundError
    error = ResourceNotFoundError("ErrorCode:TableNotFound")
    error.error_code = "TableNotFound"
    platform.fail_get = error
    seed(tables, blobs, "bound001", platform_id=PLATFORM_ID, deletion=iso(NOW - timedelta(days=9)))

    stats = sweep()

    assert stats["skipped"] == 1 and stats["deleted"] == 0


def test_legacy_unbound_rows_go_on_their_stored_date_and_get_nothing_new(tables, blobs, platform, sweep):
    seed(tables, blobs, "old00001", deletion=iso(NOW - timedelta(minutes=1)))
    seed(tables, blobs, "new00001", deletion=iso(NOW + timedelta(days=59)))
    seed(tables, blobs, "nodate01", deletion=None)  # never got a date: the legacy one applies

    assert sweep()["deleted"] == 2
    assert stored(tables, "old00001")["Visible"] is False
    assert stored(tables, "nodate01")["Visible"] is False
    assert stored(tables, "new00001")["Visible"] is True
    assert stored(tables, "new00001")["DeletionDate"] == iso(NOW + timedelta(days=59))
    assert not [c for c in tables["competitions"].updates() if c[1][1] == "new00001"]
    assert platform.calls == []


def test_the_sweep_ignores_hidden_and_legacy_name_keyed_rows(tables, blobs, platform, sweep):
    seed(tables, blobs, "hidden01", deletion=iso(NOW - timedelta(days=9)), visible=False)
    tables["competitions"].put(PartitionKey="GLOBAL", RowKey="Legacy name", Name="Legacy name")

    assert sweep()["deleted"] == 0


# ---------------------------------------------------------------------------
# E-mail scrubbing
# ---------------------------------------------------------------------------

def test_auto_deletion_leaves_no_address_on_the_row(tables, blobs, platform, sweep):
    seed(tables, blobs, "old00001", deletion=iso(NOW - timedelta(days=1)))
    tables["sentemails"].put(PartitionKey="old00001", RowKey="t" * 32, SentBy=USER_EMAIL,
                             To="judge@example.com", Cc="results@example.com")

    sweep()

    row = stored(tables, "old00001")
    assert row["CreatedBy"] == ""
    assert row["DeletedBy"] == "auto-cleanup"
    assert tables["sentemails"].rows == {}
    assert not any(name.startswith("Cup old00001-") for name in blobs.blobs)  # metadata.json too


def test_a_manual_delete_records_user_not_the_address(tables, blobs):
    seed(tables, blobs, "manual01")

    response = call(fa.delete_competition, make_request("POST", params={"id": "manual01"}))

    assert response.status_code == 200
    row = stored(tables, "manual01")
    assert row["Visible"] is False
    assert row["CreatedBy"] == ""
    assert row["DeletedBy"] == "user"
    assert "@" not in repr(row)


def test_delete_never_stores_an_address_as_the_actor(tables, blobs):
    row = seed(tables, blobs, "manual02")
    fa._delete_competition_data(dict(row), "someone@example.com")
    assert stored(tables, "manual02")["DeletedBy"] == "user"


def test_the_nightly_scrub_blanks_addresses_on_hidden_rows_only(tables):
    comps = tables["competitions"]
    comps.put(PartitionKey="GLOBAL", RowKey="hidden01", Visible=False,
              CreatedBy=USER_EMAIL, DeletedBy="other@example.com")
    comps.put(PartitionKey="GLOBAL", RowKey="hidden02", Visible=False,
              CreatedBy="-", DeletedBy="auto-cleanup")
    comps.put(PartitionKey="GLOBAL", RowKey="hidden03", Visible=False,
              CreatedBy="", DeletedBy="user")
    comps.put(PartitionKey="GLOBAL", RowKey="hidden04", Visible=False,
              CreatedBy="", DeletedBy="Some Name")
    comps.put(PartitionKey="GLOBAL", RowKey="visible1", Visible=True, CreatedBy=USER_EMAIL)
    comps.put(PartitionKey="GLOBAL", RowKey="legacy01", CreatedBy=USER_EMAIL)  # no Visible

    assert fa._scrub_hidden_rows(comps) == 2

    assert comps.row("GLOBAL", "hidden01")["CreatedBy"] == ""
    assert comps.row("GLOBAL", "hidden01")["DeletedBy"] == ""
    assert comps.row("GLOBAL", "hidden02")["DeletedBy"] == "auto-cleanup"
    assert comps.row("GLOBAL", "hidden03")["DeletedBy"] == "user"
    assert comps.row("GLOBAL", "hidden04")["DeletedBy"] == ""
    assert comps.row("GLOBAL", "visible1")["CreatedBy"] == USER_EMAIL
    assert comps.row("GLOBAL", "legacy01")["CreatedBy"] == USER_EMAIL
    # Idempotent: a second night has nothing left to do.
    assert fa._scrub_hidden_rows(comps) == 0


def test_the_timer_runs_the_sweep_and_the_scrub(tables, blobs, platform):
    seed(tables, blobs, "old00001", deletion="2020-01-01T00:00:00Z")
    tables["competitions"].put(PartitionKey="GLOBAL", RowKey="hidden01", Visible=False,
                               CreatedBy=USER_EMAIL, DeletedBy=USER_EMAIL)

    fa.auto_delete_expired_competitions._function.get_user_function()(None)

    assert stored(tables, "old00001")["Visible"] is False
    assert stored(tables, "hidden01")["CreatedBy"] == ""
    assert stored(tables, "hidden01")["DeletedBy"] == ""
