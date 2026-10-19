"""Per-competition access: the fail-closed proxy gate, identity from the
router's x-forwarded-user-* headers only, and the platform's grants
(platform_access.py) on every route that touches a competition record."""
import base64
import copy
import json

import pytest
from azure.core.exceptions import HttpResponseError

import function_app as fa
import platform_access
from conftest import (
    PLATFORM_ID,
    PROXY_SECRET,
    USER_EMAIL,
    USER_OID,
    USER_TID,
    call,
    make_request,
    payload,
    put_competition,
    put_platform_competition,
    text,
)
from fakes import FakeContainerClient

REAL_IDENTITIES_CLIENT = platform_access.get_identities_table_client
REAL_ACCESS_CLIENT = platform_access.get_access_table_client

COMP_ID = "abc12345"
SOURCE_FILE = "X_Other.pdf"
SENT_FILE = "judgingpapers_2026-11-07.pdf"
POOL_FILE = "Y_Pool.pdf"
OTHER_ID = "9e9e9e9e-1111-4222-8333-444444444444"
THIRD_ID = "7d7d7d7d-5555-4666-8777-888888888888"
STRANGER_OID = "99999999-8888-4777-8666-555555555555"


# ---------------------------------------------------------------------------
# A bound competition with something in it for every route
# ---------------------------------------------------------------------------

@pytest.fixture
def pool(monkeypatch):
    container = FakeContainerClient("competition-data")
    container.upload_blob(f"{PLATFORM_ID}/uploads/{POOL_FILE}", b"%PDF-pool")
    monkeypatch.setattr(fa, "get_platform_container_client", lambda: container)
    return container


@pytest.fixture
def competition(tables, blobs, pool, monkeypatch):
    monkeypatch.setattr(fa, "_get_categories", lambda: [])
    monkeypatch.setattr(fa, "process_judging_papers", lambda *a, **kw: {})
    row = put_competition(tables, comp_id=COMP_ID, platform_id=PLATFORM_ID,
                          deletion="2099-01-01T00:00:00Z")
    folder = row["FolderPath"]
    blobs.upload_blob(f"{folder}/metadata.json",
                      json.dumps({"id": COMP_ID, "createdBy": USER_EMAIL, "language": "fi"}))
    blobs.upload_blob(f"{folder}/{SOURCE_FILE}", b"%PDF-source")
    blobs.upload_blob(f"{folder}/judgePapers/{SENT_FILE}", b"%PDF-out")
    tables["generatedpapers"].put(PartitionKey=COMP_ID, RowKey=SENT_FILE, FileName=SENT_FILE)
    return row


def snapshot(tables, blobs, pool):
    """Every stored row and blob, to prove a refused request wrote nothing."""
    return (
        {name: copy.deepcopy(table.rows) for name, table in tables.items() if table.rows},
        {name: (blob.data, dict(blob.metadata)) for name, blob in blobs.blobs.items()},
        {name: blob.data for name, blob in pool.blobs.items()},
    )


def _send_body(**extra):
    return {"id": COMP_ID, "fileName": SENT_FILE, "senderName": "Results Team",
            "language": "en", **extra}


ROUTES = {
    # name: (role needed, request builder)
    "get_competition_details": ("read", lambda **kw: call(
        fa.get_competition_details, make_request(params={"id": COMP_ID}, **kw))),
    "resolve_competition": ("read", lambda **kw: call(
        fa.resolve_competition, make_request(
            "POST", body={"platformId": PLATFORM_ID, "name": "Spring Cup"}, **kw))),
    "save_competition_settings": ("write", lambda **kw: call(
        fa.save_competition_settings, make_request(
            "POST", body={"id": COMP_ID, "settings": {"language": "en"}}, **kw))),
    "generate_judging_papers": ("write", lambda **kw: call(
        fa.generate_judging_papers, make_request(
            "POST", body={"workingFolder": COMP_ID, "options": {}}, **kw))),
    "preview_judge_papers_email": ("write", lambda **kw: call(
        fa.preview_judge_papers_email, make_request("POST", body=_send_body(), **kw))),
    "send_judge_papers": ("write", lambda **kw: call(
        fa.send_judge_papers, make_request("POST", body=_send_body(
            to="judge@example.com", resultsServiceEmail="results@example.com"), **kw))),
    "create_download_link": ("read", lambda **kw: call(
        fa.create_download_link, make_request(
            "POST", body={"id": COMP_ID, "fileName": SENT_FILE}, **kw))),
    "upload_file": ("write", lambda **kw: call(
        fa.upload_file, make_request("POST", params={"competition": COMP_ID, "filename": "Z_New.pdf"},
                                     raw_body=b"%PDF-new", **kw))),
    "import_platform_file": ("write", lambda **kw: call(
        fa.import_platform_file, make_request(
            "POST", params={"competition": COMP_ID, "name": POOL_FILE}, **kw))),
    "delete_file": ("write", lambda **kw: call(
        fa.delete_file, make_request("POST", params={"competition": COMP_ID, "filename": SOURCE_FILE},
                                     **kw))),
    "delete_competition": ("owner", lambda **kw: call(
        fa.delete_competition, make_request("POST", params={"id": COMP_ID}, **kw))),
}
ROLES = (None, "read", "write", "owner", "admin")


def _invoke(route, role, access):
    if role == "admin":
        access.grant(None)
        return ROUTES[route][1](admin=True)
    access.grant(role)
    return ROUTES[route][1]()


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("route", sorted(ROUTES))
def test_role_matrix(route, role, competition, tables, blobs, pool, access):
    need = ROUTES[route][0]
    before = snapshot(tables, blobs, pool)

    response = _invoke(route, role, access)

    allowed = role is not None and (role == "admin" or platform_access.role_satisfies(role, need))
    if allowed:
        assert response.status_code == 200, (route, role, text(response))
        return
    if role is None:
        assert response.status_code == 404
        assert text(response).startswith("platform_not_found")
    elif need == "owner":
        assert response.status_code == 403
        assert text(response).startswith("platform_owner_required")
    else:
        assert response.status_code == 403
        assert text(response).startswith("platform_read_only")
    assert snapshot(tables, blobs, pool) == before


def test_the_role_matrix_covers_every_competition_route():
    """A new route that takes a competition must be added to ROUTES (or here)."""
    no_competition = {"health", "check_user_permission", "get_categories", "list_competitions",
                      "download_judge_papers"}
    routes = {fn.get_function_name() for fn in fa.app.get_functions()
              if any(b.get("type") == "httpTrigger" for b in fn.get_bindings_dict()["bindings"])}
    assert routes == set(ROUTES) | no_competition


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_access_tables_unreadable_is_503(route, competition, tables, blobs, pool, access):
    access.grants.fail_get = HttpResponseError("throttled")
    before = snapshot(tables, blobs, pool)

    response = ROUTES[route][1]()

    assert response.status_code == 503
    assert text(response).startswith("platform_unavailable")
    assert snapshot(tables, blobs, pool) == before


def test_no_platform_storage_account_fails_closed(competition, monkeypatch):
    monkeypatch.setattr(platform_access, "get_identities_table_client", REAL_IDENTITIES_CLIENT)
    monkeypatch.setattr(platform_access, "get_access_table_client", REAL_ACCESS_CLIENT)

    response = ROUTES["get_competition_details"][1]()

    assert response.status_code == 503
    assert text(response).startswith("platform_unavailable")


def test_admin_needs_no_access_tables(competition, monkeypatch):
    monkeypatch.setattr(platform_access, "get_identities_table_client", REAL_IDENTITIES_CLIENT)
    monkeypatch.setattr(platform_access, "get_access_table_client", REAL_ACCESS_CLIENT)

    assert ROUTES["get_competition_details"][1](admin=True).status_code == 200


def test_an_unknown_identity_has_no_access(competition):
    response = ROUTES["get_competition_details"][1](oid=STRANGER_OID)

    assert response.status_code == 404
    assert text(response).startswith("platform_not_found")


def test_access_follows_the_object_id_not_the_email(competition, access):
    # Someone else carrying the owner's address gets nothing ...
    assert ROUTES["get_competition_details"][1](oid=STRANGER_OID,
                                                user_email=USER_EMAIL).status_code == 404
    # ... and the owner keeps access under a changed address.
    assert ROUTES["get_competition_details"][1](user_email="renamed@example.com").status_code == 200


def test_a_revoked_grant_stops_working_when_the_cache_expires(competition, access, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(platform_access.time, "monotonic", lambda: clock[0])
    assert ROUTES["get_competition_details"][1]().status_code == 200

    access.grant(None, clear_cache=False)
    clock[0] += platform_access.CACHE_SECONDS - 1
    assert ROUTES["get_competition_details"][1]().status_code == 200

    clock[0] += 1
    assert ROUTES["get_competition_details"][1]().status_code == 404


@pytest.mark.parametrize("route", sorted(set(ROUTES) - {"resolve_competition"}))
def test_an_unbound_record_is_admin_only(route, tables, blobs, pool, monkeypatch, access):
    monkeypatch.setattr(fa, "_get_categories", lambda: [])
    monkeypatch.setattr(fa, "process_judging_papers", lambda *a, **kw: {})
    row = put_competition(tables, comp_id=COMP_ID, deletion="2099-01-01T00:00:00Z")
    blobs.upload_blob(f"{row['FolderPath']}/{SOURCE_FILE}", b"%PDF-source")
    blobs.upload_blob(f"{row['FolderPath']}/judgePapers/{SENT_FILE}", b"%PDF-out")
    tables["generatedpapers"].put(PartitionKey=COMP_ID, RowKey=SENT_FILE, FileName=SENT_FILE)
    before = snapshot(tables, blobs, pool)

    response = ROUTES[route][1]()

    assert response.status_code == 404
    assert text(response).startswith("platform_not_found")
    assert snapshot(tables, blobs, pool) == before
    # An admin still can (import answers not_bound: there is no pool to import from).
    expected = 409 if route == "import_platform_file" else 200
    assert ROUTES[route][1](admin=True).status_code == expected


def test_a_hidden_record_is_not_found_even_for_its_owner(competition, tables):
    tables["competitions"].row("GLOBAL", COMP_ID)["Visible"] = False

    for route in ("get_competition_details", "delete_file", "delete_competition"):
        response = ROUTES[route][1]()
        assert response.status_code == 404, route
        assert text(response).startswith("platform_not_found")


def test_a_storage_outage_is_503_not_platform_not_found(competition, tables):
    tables["competitions"].fail_get = HttpResponseError("service unavailable")

    response = ROUTES["get_competition_details"][1]()

    assert response.status_code == 503
    assert not text(response).startswith("platform_not_found")


def test_an_unknown_record_is_the_same_404(tables, blobs):
    response = call(fa.get_competition_details, make_request(params={"id": "nope0000"}))

    assert response.status_code == 404
    assert text(response).startswith("platform_not_found")


# ---------------------------------------------------------------------------
# delete_competition: POST only, owner only, id from the query or the body
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method", ["GET", "DELETE", "PUT"])
def test_delete_competition_is_post_only(method, competition, tables, blobs, pool):
    before = snapshot(tables, blobs, pool)

    response = call(fa.delete_competition, make_request(method, params={"id": COMP_ID}))

    assert response.status_code == 405
    assert response.headers["Allow"] == "POST"
    assert snapshot(tables, blobs, pool) == before


def test_delete_competition_route_declares_post_only():
    # Not app.get_functions(): the app allows indexing only once per process.
    bindings = fa.delete_competition._function.get_bindings_dict()["bindings"]
    (trigger,) = [b for b in bindings if b.get("type") == "httpTrigger"]
    assert [str(getattr(m, "value", m)).upper() for m in trigger["methods"]] == ["POST"]


def test_delete_competition_takes_the_id_from_a_json_body(competition, tables):
    response = call(fa.delete_competition, make_request("POST", body={"id": COMP_ID}))

    assert response.status_code == 200, text(response)
    assert tables["competitions"].row("GLOBAL", COMP_ID)["Visible"] is False


@pytest.mark.parametrize("body", [None, {}, {"id": ""}, {"id": 12345}, ["x"]])
def test_delete_competition_without_an_id_is_400(body, competition, tables):
    response = call(fa.delete_competition, make_request("POST", body=body))

    assert response.status_code == 400
    assert tables["competitions"].row("GLOBAL", COMP_ID)["Visible"] is True


def test_the_query_id_wins_over_the_body(competition, tables):
    response = call(fa.delete_competition, make_request(
        "POST", params={"id": "nope0000"}, body={"id": COMP_ID}))

    assert response.status_code == 404
    assert tables["competitions"].row("GLOBAL", COMP_ID)["Visible"] is True


@pytest.mark.parametrize("role", ["read", "write"])
def test_a_shared_user_cannot_delete_the_competition(role, competition, tables, access):
    access.grant(role)

    response = call(fa.delete_competition, make_request("POST", body={"id": COMP_ID}))

    assert response.status_code == 403
    assert text(response).startswith("platform_owner_required")
    assert tables["competitions"].row("GLOBAL", COMP_ID)["Visible"] is True


# ---------------------------------------------------------------------------
# resolve_competition
# ---------------------------------------------------------------------------

def resolve(**kw):
    return call(fa.resolve_competition, make_request(
        "POST", body={"platformId": PLATFORM_ID, "name": "Spring Cup"}, **kw))


def test_resolve_without_access_creates_nothing(tables, blobs, platform, access):
    put_platform_competition(platform)
    access.grant(None)

    response = resolve()

    assert response.status_code == 404
    assert text(response).startswith("platform_not_found")
    assert tables["competitions"].rows == {}
    assert blobs.blobs == {}


def test_resolve_without_access_does_not_reveal_a_deletion(tables, blobs, platform, access):
    put_platform_competition(platform, status="deleted")
    access.grant(None)

    assert resolve().status_code == 404


def test_a_reader_may_create_the_empty_bound_record(tables, blobs, platform, access):
    put_platform_competition(platform)
    access.grant("read")

    body = payload(resolve())

    assert body["created"] is True
    row = tables["competitions"].row("GLOBAL", body["id"])
    assert row["PlatformId"] == PLATFORM_ID
    assert row["CreatedBy"] == USER_EMAIL  # audit only


def test_resolve_never_adopts_another_users_unbound_record(tables, blobs, platform):
    put_competition(tables, comp_id="theirs01", name="Spring Cup", created_by="other@example.com")
    put_platform_competition(platform)

    body = payload(resolve())

    assert body["id"] != "theirs01" and body["created"] is True
    assert "PlatformId" not in tables["competitions"].row("GLOBAL", "theirs01")


def test_resolve_access_unavailable_is_503(tables, blobs, platform, access):
    put_platform_competition(platform)
    access.identities.fail_get = HttpResponseError("throttled")

    response = resolve()

    assert response.status_code == 503
    assert text(response).startswith("platform_unavailable")
    assert tables["competitions"].rows == {}


# ---------------------------------------------------------------------------
# list_competitions
# ---------------------------------------------------------------------------

@pytest.fixture
def many(tables, access):
    put_competition(tables, comp_id="mine0001", name="Mine", platform_id=PLATFORM_ID)
    put_competition(tables, comp_id="other001", name="Other", platform_id=OTHER_ID,
                    created_by="other@example.com")
    put_competition(tables, comp_id="share001", name="Shared", platform_id=THIRD_ID.upper())
    put_competition(tables, comp_id="legacy01", name="Legacy")
    put_competition(tables, comp_id="gone0001", name="Gone", platform_id=PLATFORM_ID, visible=False)
    access.grant("read", platform_id=THIRD_ID)


def listed(**kw):
    response = call(fa.list_competitions, make_request(**kw))
    assert response.status_code == 200, text(response)
    return sorted(item["id"] for item in payload(response))


def test_the_list_shows_only_what_the_caller_can_read(many):
    assert listed() == ["mine0001", "share001"]


def test_an_admin_lists_every_live_record(many):
    assert listed(admin=True) == ["legacy01", "mine0001", "other001", "share001"]


def test_a_stranger_lists_nothing(many):
    assert listed(oid=STRANGER_OID) == []


def test_the_list_fails_closed_when_access_is_unreadable(many, access):
    access.grants.fail_get = HttpResponseError("throttled")

    response = call(fa.list_competitions, make_request())

    assert response.status_code == 503
    assert text(response).startswith("platform_unavailable")


def test_only_an_admin_listing_migrates_legacy_rows(tables, monkeypatch):
    tables["competitions"].put(PartitionKey="GLOBAL", RowKey="Old Cup", Name="Old Cup")
    monkeypatch.setattr(fa, "generate_competition_id", lambda table: "new00001")

    assert listed() == []
    assert tables["competitions"].row("GLOBAL", "Old Cup") is not None

    assert listed(admin=True) == ["new00001"]


# ---------------------------------------------------------------------------
# The gate: fail-closed proxy secret, header-only identity
# ---------------------------------------------------------------------------

def whoami(**kw):
    return call(fa.check_user_permission, make_request(**kw))


@pytest.mark.parametrize("secret_setting", ["", None])
def test_an_unset_proxy_secret_rejects_everything(secret_setting, monkeypatch, competition):
    if secret_setting is None:
        monkeypatch.delenv("PROXY_SHARED_SECRET", raising=False)
    else:
        monkeypatch.setenv("PROXY_SHARED_SECRET", secret_setting)

    assert whoami().status_code == 401
    assert whoami(proxy_secret="").status_code == 401
    assert ROUTES["get_competition_details"][1](admin=True).status_code == 401
    download = call(fa.download_judge_papers,
                    make_request(params={"t": "A" * 32}, user_email=None, tid=None, oid=None))
    assert download.status_code == 401


@pytest.mark.parametrize("secret", [None, "", "wrong", PROXY_SECRET + "x", PROXY_SECRET.upper()])
def test_a_missing_or_wrong_secret_is_401(secret):
    assert whoami(proxy_secret=secret).status_code == 401


def test_the_secret_is_compared_in_constant_time(monkeypatch):
    seen = []
    real = fa.hmac.compare_digest
    monkeypatch.setattr(fa.hmac, "compare_digest", lambda a, b: seen.append((a, b)) or real(a, b))

    assert whoami().status_code == 200
    assert seen == [(PROXY_SECRET.encode(), PROXY_SECRET.encode())]


def test_a_non_ascii_secret_header_is_just_wrong():
    assert whoami(proxy_secret="sälä").status_code == 401


def _jwt(claims):
    def b64(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")
    return f"{b64({'alg': 'none'})}.{b64(claims)}.sig"


@pytest.mark.parametrize("headers", [
    {"x-forwarded-user-email": USER_EMAIL},
    {"x-ms-client-principal-name": USER_EMAIL},
    {"x-ms-client-principal-id": USER_OID},
    {"x-ms-client-principal": base64.b64encode(json.dumps({
        "userDetails": USER_EMAIL,
        "claims": [{"typ": "http://schemas.microsoft.com/identity/claims/objectidentifier", "val": USER_OID},
                   {"typ": "http://schemas.microsoft.com/identity/claims/tenantid", "val": USER_TID}],
    }).encode()).decode()},
    {"authorization": "Bearer " + _jwt({"oid": USER_OID, "tid": USER_TID,
                                         "preferred_username": USER_EMAIL})},
])
def test_spoofed_identity_headers_identify_nobody(headers, competition):
    assert whoami(tid=None, oid=None, user_email=None, headers=headers).status_code == 401
    response = call(fa.get_competition_details, make_request(
        params={"id": COMP_ID}, tid=None, oid=None, user_email=None, headers=headers))
    assert response.status_code == 401


@pytest.mark.parametrize("tid, oid", [
    (None, USER_OID),
    (USER_TID, None),
    ("", ""),
    ("not-a-guid", USER_OID),
    (USER_TID, "1234"),
    (USER_TID, f"{USER_OID}x"),
])
def test_a_missing_or_invalid_tid_or_oid_is_401(tid, oid):
    assert whoami(tid=tid, oid=oid).status_code == 401


def test_identity_headers_are_normalized(competition):
    response = ROUTES["get_competition_details"][1](tid=f" {USER_TID.upper()} ",
                                                    oid=USER_OID.upper())
    assert response.status_code == 200


def test_the_caller_is_read_from_the_forwarded_headers():
    caller = fa.get_caller(make_request(user_email=" Someone@Example.COM ", headers={
        "x-forwarded-user-name": "M%C3%A4rta%20Sk%C3%A5t%C3%A9r"}))

    assert (caller.tid, caller.oid) == (USER_TID, USER_OID)
    assert caller.email == "someone@example.com"
    assert caller.name == "Märta Skåtér"
    assert caller.is_admin is False
    assert payload(whoami(user_email=" Someone@Example.COM "))["email"] == "someone@example.com"


@pytest.mark.parametrize("value, is_admin", [("1", True), ("true", False), ("0", False),
                                             ("yes", False), ("", False)])
def test_only_admin_header_1_is_admin(value, is_admin):
    caller = fa.get_caller(make_request(headers={"x-forwarded-user-admin": value}))
    assert caller.is_admin is is_admin


def test_the_admin_header_needs_the_secret_too(competition, access):
    access.grant(None)
    response = ROUTES["get_competition_details"][1](admin=True, proxy_secret="wrong")
    assert response.status_code == 401


def test_get_categories_needs_only_a_signed_in_caller(monkeypatch, access):
    monkeypatch.setattr(fa, "_get_categories", lambda: [{"abbreviation": "X"}])
    access.grant(None)

    assert call(fa.get_categories, make_request()).status_code == 200
    assert call(fa.get_categories, make_request(oid=None)).status_code == 401


def test_the_old_identity_fallbacks_are_gone():
    assert not hasattr(fa, "get_user_email_from_header")
    assert not hasattr(fa, "_decode_jwt_payload")


def test_send_records_the_forwarded_email_only_for_audit(competition, tables, access):
    access.grant("write")

    response = ROUTES["send_judge_papers"][1](user_email="Writer@Example.com")

    assert response.status_code == 200
    (row,) = tables["sentemails"].rows.values()
    assert row["SentBy"] == "writer@example.com"
    assert row["DryRun"] is True  # no MAILGUN_API_KEY in the tests


# ---------------------------------------------------------------------------
# resolve_competition renames only for write / owner
# ---------------------------------------------------------------------------

def _bound_record(tables, blobs):
    row = put_competition(tables, comp_id=COMP_ID, name="Spring Cup", platform_id=PLATFORM_ID,
                          deletion="2099-01-01T00:00:00Z")
    blobs.upload_blob(f"{row['FolderPath']}/metadata.json",
                      json.dumps({"id": COMP_ID, "name": "Spring Cup", "createdBy": USER_EMAIL}))
    return row


def _resolve_as(name, **kw):
    return call(fa.resolve_competition, make_request(
        "POST", body={"platformId": PLATFORM_ID, "name": name}, **kw))


def test_a_reader_never_renames_the_record(tables, blobs, platform, access):
    row = _bound_record(tables, blobs)
    put_platform_competition(platform)
    access.grant("read")

    body = payload(_resolve_as("Hijacked Name"))

    assert (body["id"], body["name"], body["created"]) == (COMP_ID, "Spring Cup", False)
    assert tables["competitions"].row("GLOBAL", COMP_ID)["Name"] == "Spring Cup"
    meta = json.loads(blobs.blobs[f"{row['FolderPath']}/metadata.json"].data)
    assert meta["name"] == "Spring Cup"
    assert not [u for u in tables["competitions"].updates() if "Name" in u[2]]


@pytest.mark.parametrize("role", ["write", "owner", "admin"])
def test_write_owner_and_admin_rename_the_record(role, tables, blobs, platform, access):
    row = _bound_record(tables, blobs)
    put_platform_competition(platform)
    if role == "admin":
        access.grant(None)
        response = _resolve_as("Autumn Cup", admin=True)
    else:
        access.grant(role)
        response = _resolve_as("Autumn Cup")

    body = payload(response)

    assert (body["id"], body["name"], body["created"]) == (COMP_ID, "Autumn Cup", False)
    assert tables["competitions"].row("GLOBAL", COMP_ID)["Name"] == "Autumn Cup"
    meta = json.loads(blobs.blobs[f"{row['FolderPath']}/metadata.json"].data)
    assert meta["name"] == "Autumn Cup"
