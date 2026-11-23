"""Download links (e-mailed and copied) are stored by the hash of their token,
die with their creator's access, are listed in get_competition_details and
can be revoked (revoke_download_link)."""
import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from azure.core.exceptions import HttpResponseError

import email_sender
import function_app as fa
from conftest import (
    PLATFORM_ID,
    USER_EMAIL,
    USER_OID,
    USER_TID,
    call,
    make_request,
    payload,
    put_competition,
    text,
)

COMP_ID = "abc12345"
PDF = "judgingpapers_20261107.pdf"
OTHER_OID = "22222222-3333-4444-8555-666666666666"
OTHER_USER_ID = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
ADMIN_OID = "33333333-4444-4555-8666-777777777777"
ADMIN_USER_ID = "cccccccc-dddd-4eee-8fff-000000000000"
LEGACY_TOKEN = "L" * 32


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def from_now(**delta):
    return datetime.now(timezone.utc) + timedelta(**delta)


def sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def token_of(url):
    return url.rsplit("t=", 1)[1]


@pytest.fixture
def competition(tables, blobs, monkeypatch):
    """A bound competition with one generated PDF; SAS minting faked."""
    monkeypatch.setattr(fa, "generate_blob_read_sas_url",
                        lambda *a, **kw: "https://blob.example/file.pdf?sas")
    monkeypatch.setattr(fa, "_get_categories", lambda: [])
    row = put_competition(tables, comp_id=COMP_ID, platform_id=PLATFORM_ID,
                          deletion=iso(from_now(days=30)))
    tables["generatedpapers"].put(PartitionKey=COMP_ID, RowKey=PDF, FileName=PDF)
    blobs.upload_blob(f"{row['FolderPath']}/judgePapers/{PDF}", b"%PDF")
    return row


def create_link(**kw):
    response = call(fa.create_download_link, make_request(
        "POST", body={"id": COMP_ID, "fileName": PDF}, **kw))
    assert response.status_code == 200, text(response)
    return token_of(payload(response)["url"])


def send_email(**kw):
    return call(fa.send_judge_papers, make_request("POST", body={
        "id": COMP_ID, "fileName": PDF, "to": "judge@example.com",
        "resultsServiceEmail": "results@example.com", "senderName": "Results Team",
        "language": "en"}, **kw))


def download(token):
    return call(fa.download_judge_papers,
                make_request(params={"t": token}, user_email=None, tid=None, oid=None))


def details(**kw):
    response = call(fa.get_competition_details, make_request(params={"id": COMP_ID}, **kw))
    assert response.status_code == 200, text(response)
    return payload(response)


def revoke(link_id, comp_id=COMP_ID, method="POST", **kw):
    return call(fa.revoke_download_link, make_request(
        method, body={"id": comp_id, "linkId": link_id}, **kw))


def put_link(tables, row_key, oid=USER_OID, tid=USER_TID, channel="link", hashed=True,
             expiry=None, **extra):
    row = {"PartitionKey": COMP_ID, "RowKey": row_key, "Channel": channel, "FileName": PDF,
           "SentBy": USER_EMAIL, "SentDate": iso(from_now(hours=-1)),
           "LinkExpiry": expiry or iso(from_now(days=2)), **extra}
    if hashed:
        row["KeyKind"] = "sha256"
    if oid:
        row["SentByOid"] = oid
    if tid:
        row["SentByTid"] = tid
    return tables["sentemails"].put(**row)


# ---------------------------------------------------------------------------
# Hashed storage and the legacy fallback
# ---------------------------------------------------------------------------

def test_an_emailed_link_is_stored_by_its_hash_with_its_creator(competition, tables, monkeypatch):
    captured = {}
    real_build = email_sender.build_message

    def build(language, to, cc, sender, url, expiry):
        captured["url"] = url
        return real_build(language, to, cc, sender, url, expiry)
    monkeypatch.setattr(email_sender, "build_message", build)

    assert send_email().status_code == 200

    token = token_of(captured["url"])
    (row,) = tables["sentemails"].rows.values()
    assert row["RowKey"] == sha(token) and token not in str(row)
    assert row["KeyKind"] == "sha256" and row["Channel"] == "email"
    assert row["SentByTid"] == USER_TID and row["SentByOid"] == USER_OID
    assert row["SentByAdmin"] is False
    assert download(token).status_code == 302


def test_a_rejected_email_removes_its_hashed_row(competition, tables, monkeypatch):
    def fail(_message):
        raise email_sender.EmailSendError("rejected")
    monkeypatch.setattr(email_sender, "send_message", fail)

    assert send_email().status_code == 502
    assert tables["sentemails"].rows == {}


def test_a_copied_link_resolves_by_its_hash_and_counts_the_download(competition, tables):
    token = create_link()

    assert download(token).status_code == 302
    row = tables["sentemails"].row(COMP_ID, sha(token))
    assert row["DownloadCount"] == 1 and row["LastDownloaded"]


def test_a_legacy_clear_token_row_still_works(competition, tables):
    put_link(tables, LEGACY_TOKEN, hashed=False, tid=None)

    assert download(LEGACY_TOKEN).status_code == 302
    assert tables["sentemails"].row(COMP_ID, LEGACY_TOKEN)["DownloadCount"] == 1


def test_a_hash_is_never_accepted_as_a_token(competition, tables):
    token = create_link()

    response = download(sha(token))

    assert response.status_code == 404


def test_the_legacy_fallback_skips_rows_marked_as_hashed(competition, tables):
    # A marked row whose RowKey happens to look like a token: presenting the
    # RowKey itself must not find it.
    put_link(tables, "M" * 32)

    response = download("M" * 32)

    assert response.status_code == 410
    assert "invalid" in text(response)


def test_emails_do_not_count_toward_the_link_limit(competition, tables):
    assert send_email().status_code == 200
    assert fa._links_in_last_day(tables["sentemails"], USER_OID) == 0
    create_link()
    assert fa._links_in_last_day(tables["sentemails"], USER_OID) == 1


# ---------------------------------------------------------------------------
# The creator's access is re-checked on every download
# ---------------------------------------------------------------------------

def test_a_link_dies_when_its_creator_loses_access(competition, access):
    access.grant("read")
    token = create_link()
    assert download(token).status_code == 302

    access.grant(None)
    response = download(token)

    assert response.status_code == 410
    assert "expired" in text(response)


def test_the_link_lives_again_if_access_is_restored(competition, access):
    token = create_link()
    access.grant(None)
    assert download(token).status_code == 410
    access.grant("read")
    assert download(token).status_code == 302


def test_platform_tables_unreadable_is_a_retryable_503(competition, access):
    token = create_link()
    access.grants.fail_get = HttpResponseError("throttled")
    fa.platform_access.clear_cache()

    response = download(token)

    assert response.status_code == 503
    assert response.headers["Retry-After"]


def test_a_legacy_row_without_creator_ids_skips_the_check(competition, tables, access):
    put_link(tables, LEGACY_TOKEN, hashed=False, tid=None, oid=None)
    access.grants.fail_get = HttpResponseError("throttled")
    access.grant(None)

    assert download(LEGACY_TOKEN).status_code == 302


def test_a_pure_admin_override_link_is_marked_and_not_rechecked(competition, access, tables):
    access.identity(oid=ADMIN_OID, user_id=ADMIN_USER_ID)  # no grant of their own
    token = create_link(oid=ADMIN_OID, admin=True)

    assert tables["sentemails"].row(COMP_ID, sha(token))["SentByAdmin"] is True
    # Known limitation: admin status can't be re-read at download time.
    access.grants.fail_get = HttpResponseError("throttled")
    fa.platform_access.clear_cache()
    assert download(token).status_code == 302


def test_an_admin_with_a_grant_of_their_own_gets_an_ordinary_row(competition, access, tables):
    access.identity(oid=ADMIN_OID, user_id=ADMIN_USER_ID)
    access.grant("write", user_id=ADMIN_USER_ID)
    token = create_link(oid=ADMIN_OID, admin=True)

    assert tables["sentemails"].row(COMP_ID, sha(token))["SentByAdmin"] is False
    assert download(token).status_code == 302
    access.grant(None, user_id=ADMIN_USER_ID)
    assert download(token).status_code == 410


def test_an_admin_emailing_with_a_grant_gets_an_ordinary_row(competition, access, tables):
    access.identity(oid=ADMIN_OID, user_id=ADMIN_USER_ID)
    access.grant("owner", user_id=ADMIN_USER_ID)

    assert send_email(oid=ADMIN_OID, admin=True).status_code == 200
    (row,) = tables["sentemails"].rows.values()
    assert row["SentByAdmin"] is False


def test_an_admin_emailing_without_a_grant_is_marked(competition, access, tables):
    access.identity(oid=ADMIN_OID, user_id=ADMIN_USER_ID)

    assert send_email(oid=ADMIN_OID, admin=True).status_code == 200
    (row,) = tables["sentemails"].rows.values()
    assert row["SentByAdmin"] is True


def test_an_admin_link_with_unreadable_access_tables_is_503(competition, access, tables):
    access.grants.fail_get = HttpResponseError("throttled")
    fa.platform_access.clear_cache()

    response = call(fa.create_download_link, make_request(
        "POST", body={"id": COMP_ID, "fileName": PDF}, admin=True))

    assert response.status_code == 503
    assert text(response).startswith("platform_unavailable")
    assert tables["sentemails"].rows == {}


def test_a_non_admin_link_of_a_user_without_grant_is_dead(competition, access, tables):
    access.identity(oid=OTHER_OID, user_id=OTHER_USER_ID)
    put_link(tables, sha("x" * 32), oid=OTHER_OID)
    # Present the token whose hash is that RowKey.
    assert download("x" * 32).status_code == 410


# ---------------------------------------------------------------------------
# shareLinks in get_competition_details
# ---------------------------------------------------------------------------

@pytest.fixture
def mixed_links(competition, tables, access):
    """Mine (copied), someone else's e-mail, a legacy row, an expired one."""
    access.identity(oid=OTHER_OID, user_id=OTHER_USER_ID)
    mine = create_link()
    put_link(tables, sha("o" * 32), oid=OTHER_OID, channel="email",
             To="judge@example.com", Cc="results@example.com", SentByName="Other Person",
             SentBy="other@example.com", DownloadCount=3)
    put_link(tables, LEGACY_TOKEN, hashed=False, tid=None, oid=None, channel="",
             To="old@example.com", Cc="", SentBy="old@example.com")
    put_link(tables, sha("e" * 32), expiry=iso(from_now(minutes=-5)))
    return mine


def by_id(links):
    return {link["linkId"]: link for link in links}


@pytest.mark.parametrize("role", ["write", "owner", "admin"])
def test_writers_see_every_live_link_with_recipients(role, mixed_links, access):
    if role == "admin":
        access.grant(None)
        body = details(admin=True)
    else:
        access.grant(role)
        body = details()

    links = by_id(body["shareLinks"])
    assert set(links) == {sha(mixed_links), sha("o" * 32), sha(LEGACY_TOKEN)}
    mine = links[sha(mixed_links)]
    assert mine["channel"] == "link" and mine["createdByMe"] is True
    assert mine["fileName"] == PDF and mine["downloadCount"] == 0
    assert mine["expiration"] and mine["created"]
    assert "to" not in mine and "cc" not in mine
    other = links[sha("o" * 32)]
    assert other["channel"] == "email" and other["createdByMe"] is False
    assert other["to"] == "judge@example.com" and other["cc"] == "results@example.com"
    assert other["createdBy"] == "Other Person" and other["downloadCount"] == 3
    legacy = links[sha(LEGACY_TOKEN)]
    assert legacy["channel"] == "email" and legacy["to"] == "old@example.com"
    assert LEGACY_TOKEN not in str(body) and mixed_links not in str(body)


def test_a_reader_sees_only_their_own_links_without_recipients(mixed_links, access):
    access.grant("read")

    links = details()["shareLinks"]

    assert [link["linkId"] for link in links] == [sha(mixed_links)]
    assert "to" not in links[0] and "cc" not in links[0]
    assert "@example.com" not in str(links).replace(USER_EMAIL, "")


def test_a_reader_never_sees_recipients_of_their_own_email(competition, tables, access):
    put_link(tables, sha("r" * 32), channel="email", To="judge@example.com", Cc="x@example.com")
    access.grant("read")

    (link,) = details()["shareLinks"]

    assert link["createdByMe"] is True and "to" not in link and "cc" not in link


def test_no_links_is_an_empty_list(competition):
    assert details()["shareLinks"] == []


def test_an_unreadable_link_table_is_null_not_an_error(competition, tables):
    tables["sentemails"].fail_query = HttpResponseError("throttled")
    assert details()["shareLinks"] is None


# ---------------------------------------------------------------------------
# revoke_download_link
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role", ["write", "owner"])
def test_writers_revoke_anyones_link(role, competition, tables, access):
    access.identity(oid=OTHER_OID, user_id=OTHER_USER_ID)
    put_link(tables, sha("o" * 32), oid=OTHER_OID)
    access.grant(role)

    response = revoke(sha("o" * 32))

    assert response.status_code == 200
    assert payload(response) == {"status": "revoked", "linkId": sha("o" * 32)}
    row = tables["sentemails"].row(COMP_ID, sha("o" * 32))
    assert row["RevokedUtc"] and row["RevokedByOid"] == USER_OID and row["RevokedByTid"] == USER_TID


def test_an_admin_revokes_without_a_grant(competition, tables, access):
    put_link(tables, sha("o" * 32), oid=OTHER_OID)
    access.grant(None)
    assert revoke(sha("o" * 32), admin=True).status_code == 200
    assert tables["sentemails"].row(COMP_ID, sha("o" * 32))["RevokedUtc"]


def test_a_reader_revokes_their_own_link(competition, tables, access):
    access.grant("read")
    token = create_link()

    assert revoke(sha(token)).status_code == 200
    response = download(token)
    assert response.status_code == 410
    assert "invalid" in text(response)


def test_a_reader_cannot_revoke_someone_elses_link(competition, tables, access):
    put_link(tables, sha("o" * 32), oid=OTHER_OID)
    access.grant("read")

    response = revoke(sha("o" * 32))

    assert response.status_code == 403
    assert text(response).startswith("platform_read_only")
    assert "RevokedUtc" not in tables["sentemails"].row(COMP_ID, sha("o" * 32))


def test_no_access_is_404_and_revokes_nothing(competition, tables, access):
    put_link(tables, sha("o" * 32))
    access.grant(None)

    response = revoke(sha("o" * 32))

    assert response.status_code == 404
    assert text(response).startswith("platform_not_found")
    assert "RevokedUtc" not in tables["sentemails"].row(COMP_ID, sha("o" * 32))


def test_a_legacy_link_is_revoked_by_its_listed_id(competition, tables):
    put_link(tables, LEGACY_TOKEN, hashed=False, tid=None)
    (listed,) = details()["shareLinks"]

    assert revoke(listed["linkId"]).status_code == 200
    assert download(LEGACY_TOKEN).status_code == 410


def test_a_revoked_link_is_410_uncounted_and_unlisted(competition, tables):
    token = create_link()
    assert revoke(sha(token)).status_code == 200

    response = download(token)

    assert response.status_code == 410
    assert "invalid" in text(response)
    assert "DownloadCount" not in tables["sentemails"].row(COMP_ID, sha(token))
    assert details()["shareLinks"] == []


def test_a_revoked_legacy_link_is_410(competition, tables):
    put_link(tables, LEGACY_TOKEN, hashed=False, tid=None)
    assert revoke(sha(LEGACY_TOKEN)).status_code == 200
    assert download(LEGACY_TOKEN).status_code == 410
    assert revoke(sha(LEGACY_TOKEN)).status_code == 404


def test_a_revoked_link_still_counts_toward_the_link_limit(competition, monkeypatch):
    monkeypatch.setattr(fa, "LINK_RATE_LIMIT_PER_DAY", 1)
    token = create_link()
    assert revoke(sha(token)).status_code == 200

    response = call(fa.create_download_link, make_request(
        "POST", body={"id": COMP_ID, "fileName": PDF}))

    assert response.status_code == 429


def test_a_revoked_email_still_counts_toward_the_email_limit(competition, tables, monkeypatch):
    monkeypatch.setattr(fa, "SEND_RATE_LIMIT_PER_DAY", 1)
    assert send_email().status_code == 200
    (row,) = tables["sentemails"].rows.values()
    assert revoke(row["RowKey"]).status_code == 200
    assert fa._sends_in_last_day(tables["sentemails"], USER_EMAIL) == 1

    assert send_email().status_code == 429


def test_revoking_twice_is_link_not_found(competition):
    token = create_link()
    assert revoke(sha(token)).status_code == 200

    response = revoke(sha(token))

    assert response.status_code == 404
    assert text(response).startswith("link_not_found")


def test_a_link_of_another_competition_is_not_found(competition, tables):
    put_competition(tables, comp_id="other999", platform_id=PLATFORM_ID)
    tables["sentemails"].put(PartitionKey="other999", RowKey=sha("z" * 32), KeyKind="sha256",
                             SentByOid=USER_OID, LinkExpiry=iso(from_now(days=1)))

    assert revoke(sha("z" * 32)).status_code == 404
    assert "RevokedUtc" not in tables["sentemails"].row("other999", sha("z" * 32))


@pytest.mark.parametrize("link_id", ["", "A" * 64, "ab" * 31, "g" * 64, LEGACY_TOKEN, None, 5])
def test_a_malformed_link_id_is_400(link_id, competition):
    assert revoke(link_id).status_code == 400


@pytest.mark.parametrize("body", [[], "x", 5, None])
def test_a_non_object_body_is_400(body, competition):
    request = make_request("POST", raw_body=__import__("json").dumps(body).encode(),
                           headers={"content-type": "application/json"})
    assert call(fa.revoke_download_link, request).status_code == 400


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
def test_revoke_is_post_only(method, competition):
    response = revoke("ab" * 32, method=method)
    assert response.status_code == 405
    assert response.headers["Allow"] == "POST"


def test_revoke_needs_the_proxy_secret(competition):
    assert revoke("ab" * 32, proxy_secret="wrong").status_code == 401
