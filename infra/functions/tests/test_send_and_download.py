"""The send flow caps the e-mailed link at the effective (platform) deletion
date, download links of deleted data answer 410, and no address reaches the
logs."""
import base64
import logging
from datetime import datetime, timedelta, timezone

import pytest
from azure.core.exceptions import HttpResponseError

import email_sender
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

FILE = "judgingpapers_2026-11-07.pdf"
TO = "judge@example.com"
CC = "results@example.com"


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def from_now(**delta):
    return datetime.now(timezone.utc) + timedelta(**delta)


@pytest.fixture
def sendable(tables, blobs):
    """A competition with one generated, sendable PDF."""
    def make(**kwargs):
        row = put_competition(tables, **kwargs)
        tables["generatedpapers"].put(PartitionKey=row["RowKey"], RowKey=FILE, FileName=FILE)
        blobs.upload_blob(f"{row['FolderPath']}/judgePapers/{FILE}", b"%PDF")
        return row
    return make


def send(comp_id="abc12345"):
    return call(fa.send_judge_papers, make_request("POST", body={
        "id": comp_id, "fileName": FILE, "to": TO, "resultsServiceEmail": CC,
        "senderName": "Results Team", "language": "en"}))


def test_the_link_is_capped_at_the_platform_date(sendable, platform):
    platform_date = from_now(days=2).replace(minute=0, second=0, microsecond=0)
    sendable(platform_id=PLATFORM_ID, deletion=iso(from_now(days=30)))
    put_platform_competition(platform, deletion=iso(platform_date))

    body = payload(send())

    assert body["expiryDate"] == iso(platform_date)


def test_under_an_hour_left_on_the_platform_clock_is_409(sendable, platform):
    sendable(platform_id=PLATFORM_ID, deletion=iso(from_now(days=30)))
    put_platform_competition(platform, deletion=iso(from_now(minutes=30)))

    response = send()

    assert response.status_code == 409


def test_a_deleted_platform_competition_cannot_be_sent(sendable, platform):
    sendable(platform_id=PLATFORM_ID, deletion=iso(from_now(days=30)))
    put_platform_competition(platform, status="deleted")

    assert send().status_code == 410


def test_an_unreachable_platform_falls_back_to_the_cached_date(sendable, platform):
    local = from_now(days=1).replace(microsecond=0)
    sendable(platform_id=PLATFORM_ID, deletion=iso(local))
    platform.fail_get = HttpResponseError("throttled")

    body = payload(send())

    assert body["expiryDate"] == iso(local)


def test_a_far_deletion_date_keeps_the_five_day_link(sendable):
    sendable(deletion=iso(from_now(days=30)))

    expiry = fa._parse_iso_utc(payload(send())["expiryDate"])

    assert timedelta(days=4, hours=23) < expiry - datetime.now(timezone.utc) <= timedelta(days=5)


def test_the_preview_uses_the_same_cap(sendable, platform):
    sendable(platform_id=PLATFORM_ID, deletion=iso(from_now(days=30)))
    put_platform_competition(platform, deletion=iso(from_now(minutes=20)))

    response = call(fa.preview_judge_papers_email, make_request("POST", body={
        "id": "abc12345", "fileName": FILE, "senderName": "Results Team", "language": "fi"}))

    assert response.status_code == 409


def test_sending_logs_no_addresses(sendable, caplog):
    sendable(deletion=iso(from_now(days=30)))

    with caplog.at_level(logging.DEBUG):
        assert send().status_code == 200

    messages = "\n".join(r.getMessage() for r in caplog.records)
    assert "2 recipient(s)" in messages
    assert "@" not in messages


# ---------------------------------------------------------------------------
# download_judge_papers
# ---------------------------------------------------------------------------

TOKEN = "A" * 32


def download(token=TOKEN):
    return call(fa.download_judge_papers,
                make_request(params={"t": token}, user_email=None))


@pytest.fixture
def link(tables, blobs, monkeypatch, sendable):
    monkeypatch.setattr(fa, "generate_blob_read_sas_url",
                        lambda *a, **kw: "https://blob.example/file.pdf?sas")
    row = sendable()
    tables["sentemails"].put(PartitionKey=row["RowKey"], RowKey=TOKEN, FileName=FILE,
                             LinkExpiry=iso(from_now(days=2)))
    return row


def test_a_live_link_redirects(link):
    response = download()
    assert response.status_code == 302
    assert response.headers["Location"] == "https://blob.example/file.pdf?sas"


def test_a_malformed_token_is_404(link):
    assert download("short").status_code == 404


def test_a_link_whose_row_is_gone_is_410(link, tables):
    tables["sentemails"].rows.clear()
    response = download()
    assert response.status_code == 410
    assert "removed" in text(response)


def test_a_link_into_a_deleted_competition_is_410(link, tables):
    tables["competitions"].row("GLOBAL", "abc12345")["Visible"] = False
    response = download()
    assert response.status_code == 410
    assert "removed" in text(response)


def test_a_link_whose_file_is_gone_is_410(link, blobs):
    blobs.blobs.clear()
    assert download().status_code == 410


def test_an_expired_link_is_410(link, tables):
    tables["sentemails"].row("abc12345", TOKEN)["LinkExpiry"] = iso(from_now(minutes=-1))
    response = download()
    assert response.status_code == 410
    assert "expired" in text(response)


# ---------------------------------------------------------------------------
# Logs and the e-mail template
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("headers", [
    {"x-forwarded-user-email": USER_EMAIL},
    {"x-ms-client-principal-name": USER_EMAIL},
    {"x-ms-client-principal": base64.b64encode(
        f'{{"userDetails": "{USER_EMAIL}"}}'.encode()).decode()},
])
def test_authentication_logs_no_address(headers, caplog):
    with caplog.at_level(logging.DEBUG):
        response = call(fa.check_user_permission,
                        make_request(headers=headers, user_email=None))
    assert payload(response)["email"] == USER_EMAIL
    assert USER_EMAIL not in "\n".join(r.getMessage() for r in caplog.records)


def test_a_request_without_identity_logs_header_names_only(caplog):
    with caplog.at_level(logging.DEBUG):
        call(fa.check_user_permission,
             make_request(user_email=None, headers={"x-forwarded-for": "203.0.113.9"}))
    messages = "\n".join(r.getMessage() for r in caplog.records)
    assert "x-forwarded-for" in messages
    assert "203.0.113.9" not in messages


def test_the_dry_run_log_has_no_addresses(caplog):
    message = email_sender.build_message("en", TO, CC, "Results Team", "https://x/y",
                                         from_now(days=2))
    with caplog.at_level(logging.DEBUG):
        assert email_sender.send_message(message)["dryRun"] is True
    messages = "\n".join(r.getMessage() for r in caplog.records)
    assert "2 recipient(s)" in messages
    assert "@" not in messages


@pytest.mark.parametrize("language, url", [
    ("fi", "https://figureskatingtools.com/tietosuoja/"),
    ("en", "https://figureskatingtools.com/privacy/"),
])
def test_the_mail_links_the_privacy_notice(language, url):
    message = email_sender.build_message(language, TO, CC, "Results Team", "https://x/y",
                                         from_now(days=2))
    plain = message.get_body(("plain",)).get_content()
    html = message.get_body(("html",)).get_content()
    assert url in plain
    assert f'<a href="{url}">' in html
    preview = email_sender.render_preview(language, "Results Team", from_now(days=2))
    assert f'<a href="{url}">' in preview["html"]
