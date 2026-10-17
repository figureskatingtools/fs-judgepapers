"""The Functions host loads `function_app` flatly from the app directory, so the
tests put that directory on sys.path and import it the same way.

Every test runs against in-memory doubles (`fakes.py`): one fake table per
table name behind `get_table_client`, one fake blob service behind
`get_blob_service_client`, and — only for tests that ask for the `platform`
fixture — a fake of the site's platform `competitions` table behind
`get_platform_table_client`. Otherwise the platform clock is switched off,
exactly like a deployment without PLATFORM_STORAGE_ACCOUNT.

Competition access (platform_access.py) always runs against the `access`
fixture's fake `useridentities` / `competitionaccess` tables: the default
caller (USER_TID/USER_OID -> USER_ID) owns PLATFORM_ID, nothing else.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import azure.functions as func  # noqa: E402

import function_app as fa  # noqa: E402
import platform_access  # noqa: E402
from fakes import FakeBlobServiceClient, FakeTableClient  # noqa: E402

PROXY_SECRET = "test-proxy-secret"
USER_EMAIL = "operator@example.com"
PLATFORM_ID = "0b6f3c1e-8d2a-4f5b-9c7e-1a2b3c4d5e6f"
USER_TID = "72f988bf-86f1-41af-91ab-2d7cd011db47"
USER_OID = "11111111-2222-4333-8444-555555555555"
USER_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


class Tables(dict):
    """name -> FakeTableClient, created on first use."""

    def __missing__(self, name):
        self[name] = FakeTableClient(name)
        return self[name]


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """No real Azure anywhere: every storage entry point is faked or off."""
    monkeypatch.setenv("PROXY_SHARED_SECRET", PROXY_SECRET)
    for name in ("MAILGUN_API_KEY", "PLATFORM_STORAGE_ACCOUNT", "AzureWebJobsStorage",
                 "AzureWebJobsStorage__accountName", "EXAMPLE_VIDEO_URL", "PUBLIC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(fa, "get_platform_table_client", lambda: None)
    monkeypatch.setattr(fa, "get_platform_container_client", lambda: None)
    monkeypatch.setattr(fa, "get_table_client", lambda *a, **kw: pytest.fail("use the tables fixture"))
    monkeypatch.setattr(fa, "get_blob_service_client", lambda: pytest.fail("use the blobs fixture"))


class AccessTables:
    """The platform's `useridentities` and `competitionaccess` tables."""

    def __init__(self):
        self.identities = FakeTableClient("useridentities")
        self.grants = FakeTableClient("competitionaccess")

    def identity(self, tid=USER_TID, oid=USER_OID, user_id=USER_ID):
        self.identities.put(PartitionKey="IDENTITY", RowKey=f"{tid}_{oid}", UserId=user_id)

    def grant(self, role, platform_id=PLATFORM_ID, user_id=USER_ID):
        key = (platform_id.lower(), user_id)
        if role is None:
            self.grants.rows.pop(key, None)
        else:
            self.grants.put(PartitionKey=key[0], RowKey=user_id, Role=role)
        platform_access.clear_cache()


@pytest.fixture(autouse=True)
def access(monkeypatch):
    """Fake platform access tables; the default caller owns PLATFORM_ID."""
    tables = AccessTables()
    tables.identity()
    tables.grant("owner")
    monkeypatch.setattr(platform_access, "get_identities_table_client", lambda: tables.identities)
    monkeypatch.setattr(platform_access, "get_access_table_client", lambda: tables.grants)
    platform_access.clear_cache()
    yield tables
    platform_access.clear_cache()


@pytest.fixture
def tables(monkeypatch):
    registry = Tables()
    monkeypatch.setattr(fa, "get_table_client",
                        lambda table_name="generatedpapers": registry[table_name])
    return registry


@pytest.fixture
def blobs(monkeypatch):
    service = FakeBlobServiceClient()
    monkeypatch.setattr(fa, "get_blob_service_client", lambda: service)
    return service.get_container_client("fs-judgepapers")


@pytest.fixture
def platform(monkeypatch):
    """The site's platform `competitions` table (the retention clock)."""
    client = FakeTableClient("platform-competitions")
    monkeypatch.setattr(fa, "get_platform_table_client", lambda: client)
    return client


def put_platform_competition(platform, platform_id=PLATFORM_ID, deletion="2026-12-01T00:00:00Z",
                             status="active", purged=None):
    row = {"PartitionKey": "COMPETITION", "RowKey": platform_id.lower(), "Status": status}
    if deletion is not None:
        row["DeletionDate"] = deletion
    if purged:
        row["PurgedUtc"] = purged
    return platform.put(**row)


def put_competition(tables, comp_id="abc12345", name="Spring Cup", platform_id=None,
                    deletion="2026-11-01T10:00:00Z", visible=True, created_by=USER_EMAIL, **extra):
    row = {
        "PartitionKey": "GLOBAL",
        "RowKey": comp_id,
        "Name": name,
        "FolderPath": f"{name}-{comp_id}",
        "Visible": visible,
        "CreatedBy": created_by,
        "CreatedDate": "2026-09-01T10:00:00Z",
        **extra,
    }
    if deletion is not None:
        row["DeletionDate"] = deletion
    if platform_id:
        row["PlatformId"] = platform_id
    return tables["competitions"].put(**row)


def make_request(method="GET", path="/api/x", *, body=None, params=None, headers=None,
                 proxy_secret=PROXY_SECRET, user_email=USER_EMAIL, tid=USER_TID, oid=USER_OID,
                 admin=False, raw_body=None):
    """A request as the site router sends it; None leaves a header out."""
    request_headers = {}
    if proxy_secret is not None:
        request_headers["x-proxy-secret"] = proxy_secret
    if user_email is not None:
        request_headers["x-forwarded-user-email"] = user_email
    if tid is not None:
        request_headers["x-forwarded-user-tid"] = tid
    if oid is not None:
        request_headers["x-forwarded-user-oid"] = oid
    if admin:
        request_headers["x-forwarded-user-admin"] = "1"
    request_headers.update(headers or {})
    payload_bytes = raw_body or b""
    if body is not None:
        payload_bytes = json.dumps(body).encode("utf-8")
        request_headers.setdefault("content-type", "application/json")
    return func.HttpRequest(method=method, url=f"https://example.invalid{path}",
                            headers=request_headers, params=params or {},
                            route_params={}, body=payload_bytes)


def call(route, request):
    """Invoke a decorated function: `@app.route` leaves a FunctionBuilder behind."""
    target = getattr(route, "_function", None)
    return (target.get_user_function() if target is not None else route)(request)


def payload(response):
    return json.loads(response.get_body().decode("utf-8"))


def text(response):
    return response.get_body().decode("utf-8")
