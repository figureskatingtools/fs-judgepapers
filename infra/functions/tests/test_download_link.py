"""create_download_link ("Copy link"): the e-mail's token link for sharing
anywhere, with the same lifetime rules, served by download_judge_papers."""
import logging
from datetime import datetime, timedelta, timezone

import pytest

import function_app as fa
from conftest import (
    PLATFORM_ID,
    USER_EMAIL,
    USER_OID,
    call,
    make_request,
    payload,
    put_competition,
    put_platform_competition,
    text,
)

PDF = "judgingpapers_20261107.pdf"
ZIP = "judgingPapers_20261107.zip"


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def from_now(**delta):
    return datetime.now(timezone.utc) + timedelta(**delta)


@pytest.fixture
def generated(tables, blobs):
    """A bound competition with a generated PDF and ZIP."""
    def make(**kwargs):
        kwargs.setdefault("platform_id", PLATFORM_ID)
        kwargs.setdefault("deletion", iso(from_now(days=30)))
        row = put_competition(tables, **kwargs)
        for name in (PDF, ZIP):
            tables["generatedpapers"].put(PartitionKey=row["RowKey"], RowKey=name, FileName=name)
            blobs.upload_blob(f"{row['FolderPath']}/judgePapers/{name}", b"%PDF")
        return row
    return make


def create(file_name=PDF, comp_id="abc12345", method="POST", **kw):
    return call(fa.create_download_link, make_request(
        method, body={"id": comp_id, "fileName": file_name}, **kw))


def token_of(url):
    return url.rsplit("t=", 1)[1]


def test_a_reader_gets_a_five_day_token_link(generated, tables, access, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://test.example.com/")
    generated()
    access.grant("read")

    body = payload(create())

    assert body["url"].startswith("https://test.example.com/judgepapers/download?t=")
    expiry = fa._parse_iso_utc(body["expiration"])
    assert timedelta(days=4, hours=23) < expiry - datetime.now(timezone.utc) <= timedelta(days=5)
    row = tables["sentemails"].row("abc12345", token_of(body["url"]))
    assert row["Channel"] == "link"
    assert row["SentBy"] == USER_EMAIL and row["SentByOid"] == USER_OID
    assert row["To"] == "" and row["Cc"] == ""
    assert row["FileName"] == PDF
    assert row["LinkExpiry"] == body["expiration"]
    assert tables["competitions"].row("GLOBAL", "abc12345")["LinkCreatedCount"] == 1


def test_a_zip_can_be_linked_too(generated):
    generated()
    assert create(ZIP).status_code == 200


def test_the_link_is_capped_at_the_platform_date(generated, platform):
    platform_date = from_now(days=2).replace(minute=0, second=0, microsecond=0)
    generated()
    put_platform_competition(platform, deletion=iso(platform_date))

    assert payload(create())["expiration"] == iso(platform_date)


def test_under_an_hour_left_is_409_and_writes_nothing(generated, platform, tables):
    generated()
    put_platform_competition(platform, deletion=iso(from_now(minutes=30)))

    response = create()

    assert response.status_code == 409
    assert "about to be deleted" in text(response)
    assert tables["sentemails"].rows == {}


def test_a_deleted_platform_competition_is_410(generated, platform, tables):
    generated()
    put_platform_competition(platform, status="deleted")

    assert create().status_code == 410
    assert tables["sentemails"].rows == {}


def test_a_file_without_a_generated_row_is_404(generated, tables):
    generated()
    tables["generatedpapers"].rows.clear()

    assert create().status_code == 404
    assert tables["sentemails"].rows == {}


def test_a_generated_row_whose_blob_is_gone_is_404(generated, blobs, tables):
    generated()
    blobs.blobs.clear()

    assert create().status_code == 404
    assert tables["sentemails"].rows == {}


@pytest.mark.parametrize("file_name", ["../metadata.json", "X_Other.pdf/x", "a b.pdf", "notes.txt", "", None])
def test_only_plain_generated_file_names_are_accepted(file_name, generated):
    generated()
    assert create(file_name).status_code == 400


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
def test_post_only(method, generated, tables):
    generated()

    response = create(method=method)

    assert response.status_code == 405
    assert response.headers["Allow"] == "POST"
    assert tables["sentemails"].rows == {}


def test_route_declares_post_only():
    bindings = fa.create_download_link._function.get_bindings_dict()["bindings"]
    (trigger,) = [b for b in bindings if b.get("type") == "httpTrigger"]
    assert [str(getattr(m, "value", m)).upper() for m in trigger["methods"]] == ["POST"]


def test_the_link_works_through_download_judge_papers(generated, monkeypatch):
    monkeypatch.setattr(fa, "generate_blob_read_sas_url",
                        lambda *a, **kw: "https://blob.example/file.pdf?sas")
    generated()
    url = payload(create())["url"]

    response = call(fa.download_judge_papers,
                    make_request(params={"t": token_of(url)}, user_email=None, tid=None, oid=None))

    assert response.status_code == 302
    assert response.headers["Location"] == "https://blob.example/file.pdf?sas"


def test_the_daily_link_limit_is_429_and_removes_the_refused_row(generated, tables, monkeypatch):
    generated()
    monkeypatch.setattr(fa, "LINK_RATE_LIMIT_PER_DAY", 2)

    assert create().status_code == 200
    assert create().status_code == 200
    response = create()

    assert response.status_code == 429
    assert len(tables["sentemails"].rows) == 2


def test_links_do_not_count_toward_the_email_limit(generated, tables):
    generated()
    for _ in range(3):
        assert create().status_code == 200

    assert fa._sends_in_last_day(tables["sentemails"], USER_EMAIL) == 0


def test_an_unreadable_limit_fails_closed(generated, tables, monkeypatch):
    generated()

    def broken(*_a, **_kw):
        raise RuntimeError("table down")
    monkeypatch.setattr(fa, "_links_in_last_day", broken)

    assert create().status_code == 503
    assert tables["sentemails"].rows == {}


def test_creating_a_link_logs_no_address(generated, caplog):
    generated()
    with caplog.at_level(logging.DEBUG):
        assert create().status_code == 200
    assert "@" not in "\n".join(r.getMessage() for r in caplog.records)
