"""Links to generated files: only the blob name is stored; every response that
lists the files mints a fresh read-only SAS valid 15 minutes, after the access
check. Rows written by older versions held a 5-day SAS URL — it is never
returned again, only its blob name is used."""
import base64
import json
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from azure.storage.blob import UserDelegationKey

import function_app as fa
from conftest import PLATFORM_ID, USER_EMAIL, call, make_request, payload, put_competition, text

COMP_ID = "abc12345"
NAME = "Spring Cup"
FOLDER = f"{NAME}-{COMP_ID}"
NEW_FILE = "judgingpapers_2026-11-07.pdf"
OLD_FILE = "judgingpapers_2026-11-08.pdf"
ZIP_FILE = "judges.zip"
OLD_URL = ("https://stfakejudgepapers.blob.core.windows.net/fs-judgepapers/"
           f"{FOLDER}/judgePapers/{OLD_FILE}?se=2026-11-12T10%3A00%3A00Z&sp=r&sig=OLDSIG")
ACCOUNT_KEY = base64.b64encode(b"\0" * 32).decode()


@pytest.fixture
def signing(monkeypatch):
    """Account-key signing, like local dev (the tests never reach Azure)."""
    monkeypatch.setenv("AzureWebJobsStorage",
                       f"DefaultEndpointsProtocol=https;AccountName=stfakejudgepapers;"
                       f"AccountKey={ACCOUNT_KEY};EndpointSuffix=core.windows.net")


@pytest.fixture
def seeded(tables, blobs, monkeypatch):
    monkeypatch.setattr(fa, "_get_categories", lambda: [])
    put_competition(tables, comp_id=COMP_ID, name=NAME, platform_id=PLATFORM_ID,
                    deletion="2099-01-01T00:00:00Z")
    blobs.upload_blob(f"{FOLDER}/metadata.json", json.dumps({"id": COMP_ID, "createdBy": USER_EMAIL}))
    for name in (NEW_FILE, OLD_FILE, ZIP_FILE):
        blobs.upload_blob(f"{FOLDER}/judgePapers/{name}", b"%PDF-out")
    papers = tables["generatedpapers"]
    papers.put(PartitionKey=COMP_ID, RowKey=NEW_FILE, FileName=NEW_FILE, FileSize=10,
               BlobName=f"{FOLDER}/judgePapers/{NEW_FILE}", Description="All judge papers (PDF)")
    # Written by an older version: a stored 5-day SAS URL.
    papers.put(PartitionKey=COMP_ID, RowKey=OLD_FILE, FileName=OLD_FILE, FileSize=10,
               Url=OLD_URL, ExpirationDate="2026-11-12T10:00:00", Description="All judge papers (PDF)")
    # Neither BlobName nor Url: the default judgePapers/<FileName>.
    papers.put(PartitionKey=COMP_ID, RowKey=ZIP_FILE, FileName=ZIP_FILE, FileSize=10,
               Description="Individual judge papers (ZIP)")
    return papers


def details(**kw):
    return call(fa.get_competition_details, make_request(params={"id": COMP_ID}, **kw))


def links_by_file(response):
    assert response.status_code == 200, text(response)
    return {f["fileName"]: f for f in payload(response)["generatedFiles"]}


def _blob_path(url):
    return urlparse(url).path


def _expiry(url):
    se = parse_qs(urlparse(url).query)["se"][0]
    return datetime.strptime(se, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


@pytest.mark.parametrize("role", ["read", "write", "owner"])
def test_details_mint_fresh_15_minute_links(role, seeded, signing, access):
    access.grant(role)
    before = datetime.now(timezone.utc)

    links = links_by_file(details())

    assert set(links) == {NEW_FILE, OLD_FILE, ZIP_FILE}
    for name, link in links.items():
        url = link["url"]
        assert url.startswith("https://stfakejudgepapers.blob.core.windows.net/fs-judgepapers/")
        # The folder name's space is percent-encoded in the path.
        assert _blob_path(url) == f"/fs-judgepapers/Spring%20Cup-{COMP_ID}/judgePapers/{name}"
        query = parse_qs(urlparse(url).query)
        assert query["sp"] == ["r"]
        expiry = _expiry(url)
        assert before + timedelta(minutes=14) <= expiry <= before + timedelta(minutes=16)
        assert fa._parse_iso_utc(link["expiration"]) == expiry
    assert "OLDSIG" not in json.dumps(links)
    assert links[OLD_FILE]["url"] != OLD_URL


def test_old_rows_keep_their_stored_url_out_of_responses(seeded, signing):
    links = links_by_file(details())

    assert "2026-11-12" not in json.dumps(links)


def test_a_link_never_points_outside_the_competitions_folder(seeded, signing):
    seeded.put(PartitionKey=COMP_ID, RowKey="evil.pdf", FileName="evil.pdf",
               BlobName="Other Cup-zzzzzzzz/judgePapers/evil.pdf")
    seeded.put(PartitionKey=COMP_ID, RowKey="evil2.pdf", FileName="evil2.pdf",
               Url="https://x.blob.core.windows.net/fs-judgepapers/Other%20Cup-zzzzzzzz/x.pdf?sig=1")
    seeded.put(PartitionKey=COMP_ID, RowKey="evil3.pdf", FileName="evil3.pdf",
               Url="https://x.blob.core.windows.net/other-container/" + FOLDER + "/judgePapers/x.pdf")
    seeded.put(PartitionKey=COMP_ID, RowKey="nothing", BlobName="Other Cup-zzzzzzzz/judgePapers/y.pdf")

    links = links_by_file(details())

    # Each falls back to its own FileName inside this competition's folder.
    for name in ("evil.pdf", "evil2.pdf", "evil3.pdf"):
        assert _blob_path(links[name]["url"]) == f"/fs-judgepapers/Spring%20Cup-{COMP_ID}/judgePapers/{name}"
    # No FileName to fall back to: no link at all.
    assert links[None]["url"] is None and links[None]["expiration"] is None
    assert "Other" not in json.dumps(links)


def test_no_link_is_signed_before_the_access_check(seeded, signing, access, monkeypatch):
    signed = []
    monkeypatch.setattr(fa, "generate_blob_read_sas_url", lambda *a, **kw: signed.append(a) or "u")
    access.grant(None)

    response = details()

    assert response.status_code == 404
    assert text(response).startswith("platform_not_found")
    assert signed == []


def test_managed_identity_fetches_one_delegation_key_per_response(seeded, monkeypatch):
    monkeypatch.setenv("AzureWebJobsStorage__accountName", "stfakejudgepapers")
    service = fa.get_blob_service_client()
    requested = []

    def get_user_delegation_key(start, expiry):
        requested.append((start, expiry))
        key = UserDelegationKey()
        key.signed_oid = key.signed_tid = "00000000-0000-0000-0000-000000000000"
        key.signed_start = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        key.signed_expiry = expiry.strftime("%Y-%m-%dT%H:%M:%SZ")
        key.signed_service, key.signed_version = "b", "2021-08-06"
        key.value = ACCOUNT_KEY
        return key

    monkeypatch.setattr(service, "get_user_delegation_key", get_user_delegation_key, raising=False)
    before = datetime.now(timezone.utc)

    links = links_by_file(details())

    assert len(requested) == 1
    start, expiry = requested[0]
    assert expiry - start == timedelta(minutes=20)  # 5 min skew allowance + 15 min
    assert before + timedelta(minutes=14) <= expiry <= before + timedelta(minutes=16)
    assert all(link["url"] and "skoid=" in link["url"] for link in links.values())


def test_a_signing_failure_leaves_the_links_empty(seeded, monkeypatch):
    monkeypatch.setenv("AzureWebJobsStorage__accountName", "stfakejudgepapers")
    service = fa.get_blob_service_client()

    def broken(start, expiry):
        raise RuntimeError("no delegation key")

    monkeypatch.setattr(service, "get_user_delegation_key", broken, raising=False)

    links = links_by_file(details())

    assert all(link["url"] is None for link in links.values())


def test_generation_stores_the_blob_name_not_a_link(seeded, signing, monkeypatch):
    def process(source_dir, output_dir, options=None):
        for name in (OLD_FILE, "judgingpapers_2026-11-09.pdf"):
            with open(os.path.join(output_dir, name), "wb") as fh:
                fh.write(b"%PDF-new")
        return {}

    monkeypatch.setattr(fa, "process_judging_papers", process)
    monkeypatch.setattr(fa, "get_platform_container_client", lambda: None)

    response = call(fa.generate_judging_papers, make_request(
        "POST", body={"workingFolder": COMP_ID, "options": {}}))

    assert response.status_code == 200, text(response)
    for name in (OLD_FILE, "judgingpapers_2026-11-09.pdf"):
        row = seeded.row(COMP_ID, name)
        assert row["BlobName"] == f"{FOLDER}/judgePapers/{name}"
        # The regenerated old row lost its stored 5-day SAS URL.
        assert "Url" not in row and "ExpirationDate" not in row
        assert row["FileSize"] == len(b"%PDF-new")
