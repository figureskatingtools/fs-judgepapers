"""Retention follows the platform clock: resolve, details, extend, the nightly
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


def future(days):
    return iso(datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
               + timedelta(days=days))


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

    assert body["id"] == "abc12345"
    assert body["deletionDate"] == "2026-12-01T00:00:00Z"
    assert body["retentionManagedBy"] == "platform"
    assert tables["competitions"].row("GLOBAL", "abc12345")["DeletionDate"] == "2026-12-01T00:00:00Z"


def test_a_new_record_gets_the_platform_date_not_its_own_60_days(tables, blobs, platform):
    put_platform_competition(platform, deletion="2026-11-20T00:00:00Z")

    body = payload(resolve())

    assert body["created"] is True
    assert body["deletionDate"] == "2026-11-20T00:00:00Z"
    assert tables["competitions"].row("GLOBAL", body["id"])["DeletionDate"] == "2026-11-20T00:00:00Z"


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


def test_resolve_works_on_the_local_date_when_the_platform_is_unreachable(tables, blobs, platform, caplog):
    put_competition(tables, platform_id=PLATFORM_ID, deletion="2026-11-01T10:00:00Z")
    platform.fail_get = HttpResponseError("throttled")

    with caplog.at_level(logging.ERROR):
        response = resolve()

    assert response.status_code == 200
    body = payload(response)
    assert body["deletionDate"] == "2026-11-01T10:00:00Z"
    assert body["retentionManagedBy"] == "platform"
    assert tables["competitions"].row("GLOBAL", "abc12345")["DeletionDate"] == "2026-11-01T10:00:00Z"
    assert any("unavailable" in r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)


def test_resolve_keeps_the_tool_clock_when_the_feature_is_off(tables, blobs):
    put_competition(tables, platform_id=PLATFORM_ID, deletion="2026-11-01T10:00:00Z")

    body = payload(resolve())

    assert body["deletionDate"] == "2026-11-01T10:00:00Z"
    assert body["retentionManagedBy"] == "tool"


def test_resolve_keeps_the_local_date_when_the_platform_row_is_missing(tables, blobs, platform):
    put_competition(tables, platform_id=PLATFORM_ID, deletion="2026-11-01T10:00:00Z")

    body = payload(resolve())

    assert body["deletionDate"] == "2026-11-01T10:00:00Z"
    assert body["retentionManagedBy"] == "tool"


# ---------------------------------------------------------------------------
# get_competition_details
# ---------------------------------------------------------------------------

def details(comp_id="abc12345"):
    return payload(call(fa.get_competition_details,
                        make_request(params={"id": comp_id})))


@pytest.fixture
def no_categories(monkeypatch):
    monkeypatch.setattr(fa, "_get_categories", lambda: [])


def test_details_report_the_platform_date_and_who_manages_it(tables, blobs, platform, no_categories):
    row = put_competition(tables, platform_id=PLATFORM_ID, deletion="2026-11-01T10:00:00Z")
    add_metadata(blobs, row)
    put_platform_competition(platform, deletion="2026-12-01T00:00:00Z")
    # A generated-papers row must not shadow the competition entity.
    tables["generatedpapers"].put(PartitionKey="abc12345", RowKey="judgingpapers_2026-11-01.pdf",
                                  FileName="judgingpapers_2026-11-01.pdf", Url="https://x")

    body = details()

    assert body["deletionDate"] == "2026-12-01T00:00:00Z"
    assert body["retentionManagedBy"] == "platform"
    assert tables["competitions"].row("GLOBAL", "abc12345")["DeletionDate"] == "2026-12-01T00:00:00Z"


def test_details_of_an_unbound_record_report_the_tool_clock(tables, blobs, no_categories):
    row = put_competition(tables, deletion="2026-11-01T10:00:00Z")
    add_metadata(blobs, row)
    tables["generatedpapers"].put(PartitionKey="abc12345", RowKey="judgingpapers_2026-11-01.pdf",
                                  FileName="judgingpapers_2026-11-01.pdf", Url="https://x")

    body = details()

    assert body["deletionDate"] == "2026-11-01T10:00:00Z"
    assert body["retentionManagedBy"] == "tool"


# ---------------------------------------------------------------------------
# extend_competition_deletion
# ---------------------------------------------------------------------------

def extend(comp_id="abc12345"):
    return call(fa.extend_competition_deletion,
                make_request("POST", params={"id": comp_id}))


def test_extend_of_a_bound_record_only_syncs_the_platform_date(tables, platform):
    put_competition(tables, platform_id=PLATFORM_ID, deletion="2026-11-01T10:00:00Z")
    put_platform_competition(platform, deletion=future(30))

    body = payload(extend())

    assert body == {"deletionDate": future(30), "retentionManagedBy": "platform"}
    assert tables["competitions"].row("GLOBAL", "abc12345")["DeletionDate"] == future(30)


def test_extend_of_an_unbound_record_adds_a_week(tables):
    start = datetime.now(timezone.utc) + timedelta(days=10)
    put_competition(tables, deletion=start.replace(tzinfo=None).isoformat() + "Z")

    body = payload(extend())

    new = fa._parse_iso_utc(body["deletionDate"])
    assert new == start + timedelta(days=7)
    assert body["retentionManagedBy"] == "tool"


def test_extend_falls_back_to_a_week_when_the_platform_row_is_missing(tables, platform):
    start = datetime.now(timezone.utc) + timedelta(days=10)
    put_competition(tables, platform_id=PLATFORM_ID,
                    deletion=start.replace(tzinfo=None).isoformat() + "Z")

    body = payload(extend())

    assert fa._parse_iso_utc(body["deletionDate"]) == start + timedelta(days=7)


def test_extend_of_a_bound_record_extends_the_local_fallback_when_the_platform_is_unreachable(
        tables, platform):
    # Before the site grants the table role (or during an outage) the tool's
    # Extend button keeps working; the next successful read syncs the date back.
    start = datetime.now(timezone.utc) + timedelta(days=10)
    put_competition(tables, platform_id=PLATFORM_ID,
                    deletion=start.replace(tzinfo=None).isoformat() + "Z")
    platform.fail_get = HttpResponseError("forbidden")

    response = extend()

    assert response.status_code == 200
    body = payload(response)
    assert fa._parse_iso_utc(body["deletionDate"]) == start + timedelta(days=7)
    assert body["retentionManagedBy"] == "platform"


def test_extend_of_a_deleted_platform_competition_is_410(tables, platform):
    put_competition(tables, platform_id=PLATFORM_ID)
    put_platform_competition(platform, status="deleted")

    assert extend().status_code == 410


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


def test_a_bound_record_is_deleted_when_the_platform_date_passes(tables, blobs, platform, sweep):
    seed(tables, blobs, "bound001", platform_id=PLATFORM_ID, deletion=iso(NOW + timedelta(days=20)))
    put_platform_competition(platform, deletion=iso(NOW - timedelta(hours=3)))

    assert sweep()["deleted"] == 1
    assert stored(tables, "bound001")["Visible"] is False
    assert not any(name.startswith("Cup bound001-") for name in blobs.blobs)


def test_the_platform_date_keeps_a_bound_record_past_its_local_date(tables, blobs, platform, sweep):
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
    seed(tables, blobs, "local001", deletion=iso(NOW - timedelta(days=2)))

    stats = fa.run_auto_deletion(tables["competitions"], NOW)

    assert stats["skipped"] == 1 and stats["deleted"] == 1
    assert stored(tables, "bound001")["Visible"] is True
    assert stored(tables, "local001")["Visible"] is False


def test_a_missing_platform_row_falls_back_to_the_local_date(tables, blobs, platform, sweep):
    seed(tables, blobs, "expired1", platform_id=PLATFORM_ID, deletion=iso(NOW - timedelta(days=1)))
    other = "11111111-2222-4333-8444-555555555555"
    seed(tables, blobs, "current1", platform_id=other, deletion=iso(NOW + timedelta(days=1)))

    assert sweep()["deleted"] == 1
    assert stored(tables, "expired1")["Visible"] is False
    assert stored(tables, "current1")["Visible"] is True


def test_a_platform_row_without_a_date_yet_falls_back_to_the_local_date(tables, blobs, platform, sweep):
    seed(tables, blobs, "bound001", platform_id=PLATFORM_ID, deletion=iso(NOW - timedelta(days=1)))
    put_platform_competition(platform, deletion=None)

    assert sweep()["deleted"] == 1


def test_unbound_records_keep_the_local_60_day_clock(tables, blobs, platform, sweep):
    seed(tables, blobs, "old00001", deletion=iso(NOW - timedelta(minutes=1)))
    seed(tables, blobs, "new00001", deletion=iso(NOW + timedelta(days=59)))

    assert sweep()["deleted"] == 1
    assert stored(tables, "old00001")["Visible"] is False
    assert stored(tables, "new00001")["Visible"] is True
    assert platform.calls == []


def test_the_sweep_ignores_hidden_and_legacy_rows(tables, blobs, platform, sweep):
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
