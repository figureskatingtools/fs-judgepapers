"""platform_clock.py — the shared module (byte-identical in fs-judgepapers,
fs-protocolgenerator and fs-scoremodifier) that reads a competition's deletion
date from the site's platform registry."""
from datetime import datetime, timedelta, timezone

import pytest
from azure.core.exceptions import HttpResponseError

import platform_clock as pc
from conftest import PLATFORM_ID, put_platform_competition
from fakes import FakeTableClient

NOW = datetime(2026, 10, 13, 3, 0, tzinfo=timezone.utc)


@pytest.fixture
def registry():
    return FakeTableClient("platform-competitions")


def test_parse_utc_accepts_z_offset_naive_and_datetimes():
    expected = datetime(2026, 12, 1, tzinfo=timezone.utc)
    assert pc.parse_utc("2026-12-01T00:00:00Z") == expected
    assert pc.parse_utc("2026-12-01T00:00:00+00:00") == expected
    assert pc.parse_utc("2026-12-01T00:00:00") == expected
    assert pc.parse_utc("2026-12-01T02:00:00+02:00") == expected
    assert pc.parse_utc(datetime(2026, 12, 1)) == expected
    assert pc.parse_utc(None) is None
    assert pc.parse_utc("") is None
    assert pc.parse_utc("not a date") is None


def test_read_returns_the_platform_date_status_and_purged(registry):
    put_platform_competition(registry, deletion="2026-12-01T00:00:00Z")
    clock = pc.read_platform_clock(PLATFORM_ID, table_client=registry)
    assert clock == {
        "deletionDate": datetime(2026, 12, 1, tzinfo=timezone.utc),
        "status": "active",
        "purged": False,
    }


def test_read_looks_the_row_up_by_lowercase_guid(registry):
    put_platform_competition(registry)
    assert pc.read_platform_clock(PLATFORM_ID.upper(), table_client=registry) is not None
    assert ("get_entity", ("COMPETITION", PLATFORM_ID.lower())) in registry.calls


def test_read_reports_a_purged_tombstone(registry):
    put_platform_competition(registry, status="deleted", purged="2026-10-10T03:00:00Z")
    clock = pc.read_platform_clock(PLATFORM_ID, table_client=registry)
    assert clock["purged"] is True
    assert clock["status"] == "deleted"


def test_not_found_means_none_so_the_local_date_applies(registry):
    assert pc.read_platform_clock(PLATFORM_ID, table_client=registry) is None


def test_a_non_guid_id_is_never_looked_up(registry):
    assert pc.read_platform_clock("abc' or 1 eq 1", table_client=registry) is None
    assert pc.read_platform_clock(None, table_client=registry) is None
    assert registry.calls == []


def test_feature_off_without_platform_storage_account(monkeypatch):
    monkeypatch.delenv("PLATFORM_STORAGE_ACCOUNT", raising=False)
    assert pc.get_platform_table_client() is None
    assert pc.read_platform_clock(PLATFORM_ID) is None


def test_a_read_failure_raises_unavailable(registry):
    registry.fail_get = HttpResponseError("throttled")
    with pytest.raises(pc.PlatformClockUnavailable):
        pc.read_platform_clock(PLATFORM_ID, table_client=registry)


def test_a_client_construction_failure_raises_unavailable(monkeypatch):
    def broken():
        raise ValueError("no credential")
    monkeypatch.setattr(pc, "get_platform_table_client", broken)
    with pytest.raises(pc.PlatformClockUnavailable):
        pc.read_platform_clock(PLATFORM_ID)


def test_the_platform_date_wins_over_the_local_one():
    platform_date = datetime(2026, 12, 1, tzinfo=timezone.utc)
    clock = {"deletionDate": platform_date, "status": "active", "purged": False}
    assert pc.effective_deletion("2027-01-01T10:00:00Z", clock) == platform_date


def test_the_local_date_applies_without_a_platform_date():
    local = datetime(2027, 1, 1, 10, tzinfo=timezone.utc)
    assert pc.effective_deletion("2027-01-01T10:00:00Z", None) == local
    no_date = {"deletionDate": None, "status": "active", "purged": False}
    assert pc.effective_deletion("2027-01-01T10:00:00Z", no_date) == local
    assert pc.effective_deletion(None, None) is None


@pytest.mark.parametrize("clock, expected", [
    (None, False),
    ({"deletionDate": NOW + timedelta(days=1), "status": "active", "purged": False}, False),
    ({"deletionDate": NOW, "status": "active", "purged": False}, True),
    ({"deletionDate": NOW - timedelta(hours=3), "status": "active", "purged": False}, True),
    ({"deletionDate": NOW + timedelta(days=9), "status": "deleted", "purged": False}, True),
    ({"deletionDate": NOW + timedelta(days=9), "status": "deleted", "purged": True}, True),
    ({"deletionDate": None, "status": "active", "purged": False}, False),
])
def test_platform_says_delete(clock, expected):
    assert pc.platform_says_delete(clock, NOW) is expected


def test_to_iso_z():
    assert pc.to_iso_z(datetime(2026, 12, 1, 2, tzinfo=timezone(timedelta(hours=2)))) == "2026-12-01T00:00:00Z"
