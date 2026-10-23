import azure.functions as func
import logging
import os
import tempfile
import shutil
import json
import re
import hmac
import secrets
import io
from pypdf import PdfReader
from datetime import datetime
from azure.storage.blob import BlobServiceClient, generate_blob_sas, BlobSasPermissions
from azure.identity import DefaultAzureCredential
from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import TableClient, UpdateMode
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote, urlparse
from uuid import uuid4
from processor import process_judging_papers
from categories import (
    load_categories,
    match_category,
    parse_filename_generic,
    JUDGING_METHODS,
    is_synchronized_skating,
    sanitize_judging_method_overrides,
    effective_judging_method,
)
from competition_schedule import parse_competition_schedule, get_schedule_start_time
import email_sender
from platform_clock import (
    PlatformClockUnavailable,
    effective_deletion,
    get_platform_table_client,
    normalize_platform_id,
    orphan_old_enough,
    orphan_sweep_allowed,
    platform_says_delete,
    read_platform_clock,
    record_age_reference,
    to_iso_z,
)
import platform_access
from platform_access import PlatformAccessUnavailable

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

# Maximum file upload size: 25 MB
MAX_UPLOAD_SIZE = 25 * 1024 * 1024


# Automatic competition deletion follows the platform competition's
# DeletionDate (see the platform clock section). This tool has no clock of its
# own; LEGACY_DELETION_DATE is only the date assumed for a pre-platform,
# unbound row that never got a DeletionDate (long past: due for deletion).
LEGACY_DELETION_DATE = "2026-06-12T00:00:00Z"
AUTO_CLEANUP_ACTOR = "auto-cleanup"
# DeletedBy of a competition a person deleted. No address is ever stored there:
# a hidden row is permanent statistics history, kept long after the data.
USER_DELETE_ACTOR = "user"
# CreatedBy/DeletedBy values that hold no personal data. Anything else on a
# hidden row is an address left by an older version and is blanked nightly.
_NON_PERSONAL_ACTORS = ("", "-", USER_DELETE_ACTOR, AUTO_CLEANUP_ACTOR)
# Outcomes of a platform clock read (_read_platform_clock_state).
CLOCK_OK = "ok"                    # the platform row was read
CLOCK_NOT_FOUND = "not_found"      # no such platform competition
CLOCK_UNAVAILABLE = "unavailable"  # the platform clock could not be read
CLOCK_OFF = "off"                  # PLATFORM_STORAGE_ACCOUNT unset
CLOCK_UNBOUND = "unbound"          # the record has no PlatformId


def _orphan_purge_enabled():
    """ORPHAN_PURGE_ENABLED (Bicep-owned, on by default): false = the nightly
    orphan step only logs what it would delete. Unset reads as off."""
    return (os.environ.get("ORPHAN_PURGE_ENABLED") or "").strip().lower() in ("1", "true", "yes")

# "Send judges' papers" e-mail: link lifetime (capped at the competition's
# DeletionDate), the shortest lifetime still worth sending, the per-user send
# limit, and the generated files that may be sent (the merged daily PDFs).
SEND_LINK_VALIDITY_DAYS = 5
SEND_MIN_LINK_VALIDITY = timedelta(hours=1)
SEND_RATE_LIMIT_PER_DAY = 20
# The e-mailed link is ours (PUBLIC_BASE_URL/judgepapers/download?t=<token>),
# not a blob SAS URL: Microsoft's filters junk mail linking to
# *.blob.core.windows.net. Each click mints a short-lived SAS and redirects.
DOWNLOAD_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32}")
DOWNLOAD_SAS_VALIDITY = timedelta(minutes=15)
# Links to generated files in API responses (get_competition_details): a fresh
# read-only SAS per response, minted after the access check, never stored.
GENERATED_LINK_VALIDITY = timedelta(minutes=15)
GENERATED_CONTAINER = "fs-judgepapers"
DEFAULT_PUBLIC_BASE_URL = "https://figureskatingtools.com"
_SENDABLE_FILE_RE = re.compile(r"judgingpapers_[A-Za-z0-9_-]+\.pdf")
# "Copy link" (create_download_link): the e-mail's token link, for sharing
# anywhere (WhatsApp, …) — same lifetime rules, any generated file (merged PDF
# or ZIP), no recipients. Its sentemails row has Channel "link" and the
# creator's object id, which its own per-user daily limit counts.
LINK_CHANNEL = "link"
LINK_RATE_LIMIT_PER_DAY = 100
_LINKABLE_FILE_RE = re.compile(r"[A-Za-z0-9_-]+\.(?:pdf|zip)", re.IGNORECASE)

class Caller:
    """
    The signed-in user, as forwarded by the site router (PROXY-CONTRACT.md).
    `tid`/`oid` (the Entra tenant and object id, lowercase GUIDs) are the
    identity that access is decided on; `email` and `name` are for display
    and audit only (CreatedBy, SentBy, importedBy) and never authorise
    anything. `is_admin` is the router's x-forwarded-user-admin: 1, trusted
    only because the proxy secret gate in front of it fails closed.
    """

    __slots__ = ("tid", "oid", "email", "name", "is_admin")

    def __init__(self, tid, oid, email="", name="", is_admin=False):
        self.tid = tid
        self.oid = oid
        self.email = email
        self.name = name
        self.is_admin = is_admin


def is_user_allowed(caller) -> bool:
    """
    Tool-wide authorization hook, layered on top of the per-competition access
    check (_competition_for): every authenticated user is allowed today.
    """
    return True


def _header(req: func.HttpRequest, name: str) -> str:
    """A request header by lowercase name, also tried in Title-Case ("" if absent)."""
    value = req.headers.get(name)
    if value is None:
        value = req.headers.get("-".join(part.capitalize() for part in name.split("-")))
    return value or ""


def _proxy_secret_ok(req: func.HttpRequest) -> bool:
    """
    Verify the request came from the site router by checking the shared
    secret header. The function endpoint is public, so this is what stops
    anyone from calling it directly with forged x-forwarded-user-* headers.

    Fails CLOSED: an empty PROXY_SHARED_SECRET rejects every request (only
    GET health is ungated). Compared in constant time.
    """
    expected = os.environ.get("PROXY_SHARED_SECRET") or ""
    if not expected:
        logging.error("PROXY_SHARED_SECRET is not set; rejecting the request")
        return False
    provided = _header(req, "x-proxy-secret")
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def get_caller(req: func.HttpRequest) -> Caller | None:
    """
    The caller, or None (-> 401). Identity comes ONLY from the headers the
    router sets (x-forwarded-user-tid / -oid / -email / -name / -admin), and
    only behind the proxy secret; there are no fallbacks (no Easy Auth
    principal headers, no bearer tokens). A missing or non-GUID tid or oid
    is unauthenticated.
    """
    if not _proxy_secret_ok(req):
        logging.warning("Proxy shared secret missing or mismatched; rejecting request")
        return None

    tid = _header(req, "x-forwarded-user-tid").strip().lower()
    oid = _header(req, "x-forwarded-user-oid").strip().lower()
    if platform_access.identity_key(tid, oid) is None:
        # Header names only: values can carry addresses and client IPs.
        logging.warning(f"No valid forwarded identity. Header names: "
                        f"{sorted(k.lower() for k in req.headers.keys())}")
        return None

    email = _header(req, "x-forwarded-user-email").strip().lower()
    try:
        name = unquote(_header(req, "x-forwarded-user-name")).strip()
    except Exception:
        name = ""
    is_admin = _header(req, "x-forwarded-user-admin") == "1"
    logging.info("Authenticated via the forwarded user headers")
    return Caller(tid, oid, email=email, name=name, is_admin=is_admin)


# Per-competition access (platform_access.py). Plain-text bodies starting with
# the code, like this tool's other platform_* errors.
_ACCESS_NOT_FOUND = "platform_not_found: no such competition, or you have no access to it"
_ACCESS_READ_ONLY = "platform_read_only: you have view-only access to this competition"
_ACCESS_OWNER_REQUIRED = "platform_owner_required: only the competition's owner can do this"
_ACCESS_UNAVAILABLE = "platform_unavailable: could not check access to this competition, try again"


def _access_role(caller, platform_id, need):
    """
    (role, None) when `caller` holds at least `need` ("read"/"write"/"owner")
    on the platform competition `platform_id`, else (None, error response):
    404 platform_not_found for no access at all — and for a record bound to
    no (valid) platform competition, which only an admin may touch — 403
    platform_owner_required for anyone but the owner on an owner route, 403
    platform_read_only for a read grant on a write route, 503
    platform_unavailable when the access tables can't be read (fail closed).
    An admin is "owner".
    """
    guid = normalize_platform_id(platform_id)
    if guid is None:
        if caller.is_admin:
            return "owner", None
        return None, func.HttpResponse(_ACCESS_NOT_FOUND, status_code=404)
    try:
        role = platform_access.role_for(guid, caller.tid, caller.oid, is_admin=caller.is_admin)
    except PlatformAccessUnavailable:
        logging.error(f"Platform access unavailable for platform competition {guid}")
        return None, func.HttpResponse(_ACCESS_UNAVAILABLE, status_code=503)
    if role is None:
        return None, func.HttpResponse(_ACCESS_NOT_FOUND, status_code=404)
    if not platform_access.role_satisfies(role, need):
        message = _ACCESS_OWNER_REQUIRED if need == "owner" else _ACCESS_READ_ONLY
        return None, func.HttpResponse(message, status_code=403)
    return role, None


def _access_error(caller, platform_id, need):
    """None when the caller holds `need`, else the error response (_access_role)."""
    return _access_role(caller, platform_id, need)[1]


def _competition_for(caller, comp_id, need):
    """
    (entity, None) when `comp_id` names a live record the caller holds `need`
    on, else (None, error response). An unknown or deleted (hidden) record is
    the same 404 as no access, so existence never leaks; a storage failure is
    a retryable 503, never that 404 (the site clears its selection on
    platform_not_found). Callers must run this before touching anything of
    the competition.
    """
    entity = None
    if comp_id and isinstance(comp_id, str):
        try:
            entity = _get_competition_entity_strict(comp_id)
        except Exception as e:
            logging.error(f"Could not read competition {comp_id}: {type(e).__name__}")
            return None, func.HttpResponse("Temporarily unavailable, try again later", status_code=503,
                                           headers={"Retry-After": "30"})
    if not entity or entity.get("Visible") is False:
        return None, func.HttpResponse(_ACCESS_NOT_FOUND, status_code=404)
    error = _access_error(caller, entity.get("PlatformId"), need)
    if error is not None:
        return None, error
    return entity, None


# Liveness for the site's admin diagnostics: no sign-in, no secret, no storage
# (the same shape as the platform API's /api/health).
@app.route(route="health", auth_level=func.AuthLevel.ANONYMOUS, methods=["GET"])
def health(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse(json.dumps({"status": "ok", "service": "fs-judgepapers"}), mimetype="application/json")


@app.route(route="check_user_permission", auth_level=func.AuthLevel.ANONYMOUS)
def check_user_permission(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Checking user permission...')

    caller = get_caller(req)

    if not caller:
        return func.HttpResponse(json.dumps({"allowed": False, "email": None}), mimetype="application/json", status_code=401)

    return func.HttpResponse(json.dumps({"allowed": True, "email": caller.email}), mimetype="application/json")


def get_blob_service_client():
    """Helper to connect to Blob Storage"""
    try:
        # Managed Identity
        account_name = os.environ.get("AzureWebJobsStorage__accountName")
        if account_name:
            credential = DefaultAzureCredential()
            account_url = f"https://{account_name}.blob.core.windows.net"
            return BlobServiceClient(account_url=account_url, credential=credential)
        
        # Connection String
        connection_string = os.environ.get("AzureWebJobsStorage")
        if connection_string:
            return BlobServiceClient.from_connection_string(connection_string)
            
        return None
    except Exception as e:
        logging.error(f"Failed to create blob client: {e}")
        return None

def get_platform_container_client():
    """
    Container client for the platform's shared competition file pool
    (competition-data/<platform-guid>/uploads/...), hosted in the site repo's
    storage account. Returns None when PLATFORM_STORAGE_ACCOUNT is unset, which
    cleanly turns the pool-import feature off.

    No connection string for that account exists, deliberately: access rests
    on the Storage Blob Data *Reader* grant to this Function App's system
    identity (site repo's shared-data-access.bicep), so the app can never
    write there. DefaultAzureCredential resolves to that managed identity in
    production; its dev-credential fallbacks only matter locally, where the
    developer's own RBAC decides.
    """
    account_name = os.environ.get("PLATFORM_STORAGE_ACCOUNT")
    if not account_name:
        return None
    # Creation failures propagate: the import route maps them to 502
    # "platform_unavailable", which is distinct from the deliberate
    # feature-off None (503 "platform_not_configured").
    container_name = os.environ.get("PLATFORM_DATA_CONTAINER") or "competition-data"
    client = BlobServiceClient(
        account_url=f"https://{account_name}.blob.core.windows.net",
        credential=DefaultAzureCredential(),
    )
    return client.get_container_client(container_name)


class _PoolFileTooLarge(ValueError):
    """Pool blob exceeds MAX_UPLOAD_SIZE — the import route maps it to 413."""


class _PoolReadError(Exception):
    """Reading a pool blob failed — the import route maps it to 502."""


def _copy_pool_blob(pool_container, pool_path, container, blob_path, metadata):
    """
    Copy one blob out of the platform file pool into this tool's container,
    stamping provenance metadata (poolSource / poolUploadedUtc / importedBy)
    on the destination so a later refresh can tell what it already holds.

    Read-side failures are raised as _PoolFileTooLarge (413),
    ResourceNotFoundError (404) or _PoolReadError (502); upload failures
    propagate unchanged (500) — the import route maps all four.
    """
    try:
        data = pool_container.get_blob_client(pool_path).download_blob().readall()
    except ResourceNotFoundError:
        raise
    except Exception as e:
        raise _PoolReadError(f"could not read pool blob {pool_path}: {e}") from e

    if len(data) > MAX_UPLOAD_SIZE:
        raise _PoolFileTooLarge(pool_path)

    container.upload_blob(blob_path, data, overwrite=True, metadata=metadata)


def _refresh_from_pool(entity, container, folder_path):
    """
    Re-copy every file this competition already holds that is newer in the
    platform file pool, and return the (sorted) names that were replaced.

    The HOVTP listener overwrites pool blobs under the same stable filename on
    every FS Manager re-export, and the UI hides pool files the tool already
    holds — so without this the tool would keep generating from a stale copy.
    Only names already held are refreshed: nothing is ever added or deleted.

    Best effort by design: this runs on the read path (details + generate) and
    must never break it, so every failure is logged and swallowed.
    """
    refreshed = []
    try:
        # Pinned to a lowercase GUID before it is spliced into a pool path.
        platform_id = normalize_platform_id((entity or {}).get("PlatformId"))
        if not platform_id:
            return []

        try:
            pool_container = get_platform_container_client()
        except Exception as e:
            logging.warning(f"Pool refresh: could not open the competition file pool: {e}")
            return []
        if pool_container is None:
            return []

        # What we hold today, and how fresh that copy is: the stamped pool
        # timestamp when we imported it, else the blob's own last_modified.
        own = {}
        prefix = f"{folder_path}/"
        for blob in container.list_blobs(name_starts_with=prefix, include=["metadata"]):
            name = blob.name[len(prefix):]
            if not name or '/' in name:
                continue
            if name in ("metadata.json", "init.md"):
                continue
            if not name.lower().endswith(".pdf"):
                continue
            recorded = _parse_iso_utc(_meta(blob.metadata, "poolUploadedUtc"))
            if recorded is None:
                recorded = _as_utc(blob.last_modified)
            own[name] = recorded
        if not own:
            return []

        # Newest pool candidate per name, across both pool folders.
        candidates = {}
        for source, folder in (("upload", "uploads"), ("fsm", "fsm")):
            pool_prefix = f"{platform_id}/{folder}/"
            try:
                for blob in pool_container.list_blobs(name_starts_with=pool_prefix):
                    name = blob.name[len(pool_prefix):]
                    if not name or '/' in name or name not in own:
                        continue
                    # The listing already carries the size, so an oversized blob
                    # is dropped here for free. Leaving it to _copy_pool_blob's
                    # check would mean pulling the whole thing into the
                    # function's memory only to reject it — and unlike the import
                    # route, this path has no earlier size gate.
                    if blob.size and blob.size > MAX_UPLOAD_SIZE:
                        logging.warning(
                            f"Pool refresh: {blob.name} exceeds the "
                            f"{MAX_UPLOAD_SIZE // (1024*1024)} MB limit, skipping")
                        continue
                    modified = _as_utc(blob.last_modified)
                    if modified is None:
                        continue
                    current = candidates.get(name)
                    if current is None or modified > current[0]:
                        candidates[name] = (modified, source, blob.name)
            except Exception as e:
                logging.warning(f"Pool refresh: could not list {pool_prefix}: {e}")

        for name in sorted(candidates):
            modified, source, pool_path = candidates[name]
            recorded = own.get(name)
            if recorded is not None and modified <= recorded:
                continue
            try:
                _copy_pool_blob(pool_container, pool_path, container, f"{folder_path}/{name}", {
                    "poolSource": source,
                    "poolUploadedUtc": _iso_utc(modified),
                    "importedBy": "pool-sync",
                })
                refreshed.append(name)
            except _PoolFileTooLarge:
                logging.warning(
                    f"Pool refresh: {pool_path} exceeds the {MAX_UPLOAD_SIZE // (1024*1024)} MB limit, skipping")
            except Exception as e:
                logging.warning(f"Pool refresh: could not refresh {name} from {pool_path}: {e}")

        if refreshed:
            logging.info(
                f"Pool refresh: replaced {len(refreshed)} file(s) in {folder_path} with newer pool copies")
    except Exception as e:
        logging.warning(f"Pool refresh failed for {folder_path}: {e}")

    return sorted(refreshed)


def get_table_client(table_name="generatedpapers"):
    """Helper to connect to Table Storage"""
    try:
        # Managed Identity
        account_name = os.environ.get("AzureWebJobsStorage__accountName")
        if account_name:
            credential = DefaultAzureCredential()
            endpoint = f"https://{account_name}.table.core.windows.net"
            return TableClient(endpoint=endpoint, table_name=table_name, credential=credential)
        
        # Connection String
        connection_string = os.environ.get("AzureWebJobsStorage")
        if connection_string:
            return TableClient.from_connection_string(conn_str=connection_string, table_name=table_name)
            
        return None
    except Exception as e:
        logging.error(f"Failed to create table client: {e}")
        return None

def sanitize_name(name: str) -> str:
    """Sanitize a competition display name: keep alphanumerics, spaces, hyphens, underscores."""
    return "".join([c for c in name if c.isalnum() or c in (' ', '-', '_')]).strip()


def generate_competition_id(comp_table) -> str:
    """Generate a unique 8-char hex competition id (checked against the competitions table)."""
    for _ in range(5):
        cid = uuid4().hex[:8]
        try:
            comp_table.get_entity(partition_key="GLOBAL", row_key=cid)
        except ResourceNotFoundError:
            return cid
    raise RuntimeError("Could not generate a unique competition id")


def _get_competition_entity_strict(comp_id: str):
    """
    A competition entity by its id (RowKey), None when not found. Any other
    storage failure is raised, so a caller can answer a retryable 5xx instead
    of mistaking an outage for a deleted competition.
    """
    comp_table = get_table_client("competitions")
    if not comp_table:
        raise RuntimeError("Storage configuration invalid")
    try:
        return comp_table.get_entity(partition_key="GLOBAL", row_key=comp_id)
    except ResourceNotFoundError:
        return None


def _parse_iso_utc(value):
    """
    Parse a stored ISO timestamp (naive UTC with a trailing 'Z', as written by
    `datetime.utcnow().isoformat() + "Z"`) into an aware UTC datetime.
    Returns None on any failure — callers must treat None as "unknown", never
    as "expired".
    """
    if not value or not isinstance(value, str):
        return None
    try:
        s = value[:-1] if value.endswith("Z") else value
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception as e:
        logging.warning(f"Could not parse ISO timestamp '{value}': {e}")
        return None


def _meta(metadata, key):
    """
    Case-insensitive blob-metadata read: the keys travel as HTTP headers, so
    Azure does not guarantee their case on the way back. None when absent.
    """
    for existing_key, value in (metadata or {}).items():
        if existing_key.lower() == key.lower():
            return value
    return None


def _as_utc(dt):
    """Normalize a datetime to an aware UTC one (None for anything else)."""
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso_utc(dt):
    """
    Format a datetime as the ISO-8601 UTC string this codebase stores
    everywhere (naive UTC + 'Z', round-trips through `_parse_iso_utc`).
    Returns "" for None/garbage — blob metadata values must be strings.
    """
    dt = _as_utc(dt)
    if dt is None:
        return ""
    return dt.replace(tzinfo=None).isoformat() + "Z"


# ---------------------------------------------------------------------------
# Platform retention clock. The site's platform registry owns every
# competition's deletion date (created + 60 days, extended a week at a time on
# the site only) and mirrors it into its `retentionclock` table, the only
# platform table this tool reads (platform_clock.py). A record is bound to a
# platform competition (PlatformId, a lowercase GUID) and its data is deleted
# only on the platform's verdict (deleted / purged). The row's own
# DeletionDate is just a cached copy of the platform date, for display.
# ---------------------------------------------------------------------------

def _platform_clock_reader():
    """
    A `platform_id -> (clock | None, state)` reader holding ONE platform
    table client (build one per request / per sweep) that every read gets as
    `table_client=`. Never raises: failures come back as CLOCK_UNAVAILABLE
    (logged), a missing PLATFORM_STORAGE_ACCOUNT as CLOCK_OFF. An id that is
    not a GUID reads as CLOCK_NOT_FOUND.
    """
    try:
        client = get_platform_table_client()
    except Exception as e:
        reason = type(e).__name__
        logging.error(f"Platform clock: could not open the platform clock table: {reason}")
        return lambda platform_id: (None, CLOCK_UNBOUND if not platform_id else CLOCK_UNAVAILABLE)

    def read(platform_id):
        if not platform_id:
            return None, CLOCK_UNBOUND
        if client is None:
            return None, CLOCK_OFF
        guid = normalize_platform_id(platform_id)
        if guid is None:
            return None, CLOCK_NOT_FOUND
        try:
            clock = read_platform_clock(guid, table_client=client)
        except PlatformClockUnavailable:
            logging.error(f"Platform clock unavailable for platform competition {platform_id}")
            return None, CLOCK_UNAVAILABLE
        return clock, (CLOCK_OK if clock is not None else CLOCK_NOT_FOUND)

    read.table_client = client  # the sweep's orphan-step probe reuses it
    return read


def _read_platform_clock_state(platform_id):
    """(clock | None, state) for one platform id — see _platform_clock_reader."""
    return _platform_clock_reader()(platform_id)


def _platform_deleted(clock):
    """True when the platform competition was deleted or already purged."""
    return bool(clock) and (bool(clock.get("purged")) or clock.get("status") == "deleted")


def _sync_local_deletion_date(comp_table, entity, clock):
    """
    Cache the platform's deletion date on the tool row (MERGE, only when it
    differs; best-effort — a failed write keeps the old cached date). Mutates
    the in-memory entity. Returns True when the row was written.
    """
    platform_date = (clock or {}).get("deletionDate")
    if platform_date is None or comp_table is None:
        return False
    new_value = to_iso_z(platform_date)
    if entity.get("DeletionDate") == new_value:
        return False
    try:
        comp_table.update_entity({
            "PartitionKey": "GLOBAL",
            "RowKey": entity["RowKey"],
            "DeletionDate": new_value,
        }, mode=UpdateMode.MERGE)
    except Exception as e:
        logging.warning(f"Could not cache the platform deletion date on {entity['RowKey']}: {e}")
        return False
    entity["DeletionDate"] = new_value
    return True


def _deletion_date_value(entity, clock):
    """The deletionDate to report: the platform's, else the cached copy."""
    deletion = effective_deletion(entity.get("DeletionDate"), clock)
    return to_iso_z(deletion) if deletion is not None else entity.get("DeletionDate")


def _bump_competition_counters(comp_id, uploaded_delta=0, generate_delta=0, set_last_generated=False,
                               email_sent_delta=0, link_created_delta=0):
    """
    Best-effort update of usage counters on the competitions entity.
    Never raises — counter failures must not fail the user's operation.
    """
    try:
        comp_table = get_table_client("competitions")
        if not comp_table:
            return
        entity = comp_table.get_entity(partition_key="GLOBAL", row_key=comp_id)
        update = {"PartitionKey": "GLOBAL", "RowKey": comp_id}
        if uploaded_delta:
            update["UploadedFileCount"] = max(0, int(entity.get("UploadedFileCount", 0)) + uploaded_delta)
        if generate_delta:
            update["GenerateRunCount"] = int(entity.get("GenerateRunCount", 0)) + generate_delta
        if set_last_generated:
            update["LastGeneratedDate"] = f"{datetime.utcnow().isoformat()}Z"
        if email_sent_delta:
            update["EmailSentCount"] = int(entity.get("EmailSentCount", 0)) + email_sent_delta
        if link_created_delta:
            update["LinkCreatedCount"] = int(entity.get("LinkCreatedCount", 0)) + link_created_delta
        comp_table.update_entity(update, mode=UpdateMode.MERGE)
    except Exception as e:
        logging.warning(f"Failed to update counters for competition {comp_id}: {e}")


def _store_competition_statistics(comp_id, stats):
    """
    Best-effort write of the usage-statistics snapshot (returned by the
    processor) onto the competitions entity. Overwrites the previous snapshot;
    only TotalPagesGenerated accumulates across generate runs.
    Never raises — statistics failures must not fail the user's operation.
    """
    if not stats:
        return
    try:
        comp_table = get_table_client("competitions")
        if not comp_table:
            return
        entity = comp_table.get_entity(partition_key="GLOBAL", row_key=comp_id)

        pages = int(stats.get("pages_generated", 0))
        update = {
            "PartitionKey": "GLOBAL",
            "RowKey": comp_id,
            "PagesGenerated": pages,
            "TotalPagesGenerated": int(entity.get("TotalPagesGenerated", 0)) + pages,
            "LastStatisticsDate": f"{datetime.utcnow().isoformat()}Z",
        }

        # snake_case stats key -> (PascalCase column, JSON-encode?)
        column_map = {
            "competition_type": ("CompetitionType", False),
            "categories": ("CategoriesJson", True),
            "category_count": ("CategoryCount", False),
            "competitor_count": ("CompetitorCount", False),
            "segment_count": ("SegmentCount", False),
            "segment_types": ("SegmentTypesJson", True),
            "judge_assignment_count": ("JudgeAssignmentCount", False),
            "unique_judge_count": ("UniqueJudgeCount", False),
            "unique_official_count": ("UniqueOfficialCount", False),
            "officials_by_role": ("OfficialsByRoleJson", True),
            "withdrawn_count": ("WithdrawnCount", False),
            "day_count": ("CompetitionDayCount", False),
            "first_date": ("FirstCompetitionDate", False),
            "last_date": ("LastCompetitionDate", False),
            "judging_method": ("JudgingMethod", False),
            "language": ("PacketLanguage", False),
            "combined_schedule": ("CombinedSchedule", False),
            "combined_schedule_copies": ("CombinedScheduleCopies", False),
        }
        for key, (column, as_json) in column_map.items():
            value = stats.get(key)
            if value is None:
                continue  # skip unknowns (e.g. CompetitorCount without a schedule)
            update[column] = json.dumps(value, ensure_ascii=False) if as_json else value

        comp_table.update_entity(update, mode=UpdateMode.MERGE)
    except Exception as e:
        logging.warning(f"Failed to store statistics for competition {comp_id}: {e}")


def migrate_legacy_row(comp_table, entity):
    """
    Migrate a legacy competitions row (RowKey = competition name, name-based blob
    folder) to the id-keyed schema. Azure Tables cannot rename a RowKey, so a new
    entity is created and the old one deleted. The blob folder is left untouched;
    FolderPath points at it. generatedpapers rows are re-keyed name -> id.
    Returns the new entity.
    """
    old_name = entity["RowKey"]
    new_id = generate_competition_id(comp_table)
    logging.info(f"Migrating legacy competition '{old_name}' to id {new_id}")

    new_entity = {
        "PartitionKey": "GLOBAL",
        "RowKey": new_id,
        "Name": entity.get("Name", old_name),
        "FolderPath": old_name,
        "Visible": True,
        "CreatedBy": entity.get("CreatedBy", "-"),
        "CreatedDate": entity.get("CreatedDate", "-"),
        "UploadedFileCount": 0,
        "GenerateRunCount": 0
    }

    # Re-key generatedpapers rows (PartitionKey: old name -> new id)
    try:
        papers_table = get_table_client()
        if papers_table:
            safe_pk = old_name.replace("'", "''")
            for paper in list(papers_table.query_entities(f"PartitionKey eq '{safe_pk}'")):
                new_paper = dict(paper)
                new_paper["PartitionKey"] = new_id
                papers_table.upsert_entity(new_paper)
                papers_table.delete_entity(partition_key=old_name, row_key=paper["RowKey"])
    except Exception as e:
        logging.warning(f"Failed to re-key generatedpapers rows for '{old_name}': {e}")

    comp_table.create_entity(new_entity)
    comp_table.delete_entity(partition_key="GLOBAL", row_key=old_name)
    return new_entity


def _user_delegation_key(blob_service_client, start_time, expiry):
    """
    The user-delegation key a managed identity signs read SASes with, or None
    with a connection string (local dev signs with the account key). Fetch it
    once per response, not once per link.
    """
    if os.environ.get("AzureWebJobsStorage__accountName"):
        return blob_service_client.get_user_delegation_key(start_time, expiry)
    return None


def generate_blob_read_sas_url(blob_service_client, container_name, blob_name, start_time, expiry,
                               user_delegation_key=None):
    """
    Read-only SAS URL for one blob, valid from start_time to expiry (UTC).
    Managed identity signs with a user-delegation key (max 7 days; pass one in
    to reuse it, else one is fetched), a connection string with the account
    key. Returns None when neither credential can sign. The path is
    percent-encoded (folder names keep spaces): a raw space makes browsers
    re-encode the whole URL, SAS query included, which Azure rejects.
    """
    account_name = blob_service_client.account_name
    encoded_path = quote(f"{container_name}/{blob_name}", safe="/")
    blob_url_base = f"https://{account_name}.blob.core.windows.net/{encoded_path}"
    sas_token = ""

    # Managed Identity Logic
    if os.environ.get("AzureWebJobsStorage__accountName"):
         ud_key = user_delegation_key or blob_service_client.get_user_delegation_key(start_time, expiry)
         sas_token = generate_blob_sas(
             account_name=account_name,
             container_name=container_name,
             blob_name=blob_name,
             user_delegation_key=ud_key,
             permission=BlobSasPermissions(read=True),
             expiry=expiry,
             start=start_time
         )
    else:
         # Connection String Logic (Dev)
         conn_str = os.environ.get("AzureWebJobsStorage")
         if conn_str:
             items = dict(item.split('=', 1) for item in conn_str.split(';') if '=' in item)
             key = items.get('AccountKey')
             if key:
                 sas_token = generate_blob_sas(
                     account_name=items.get('AccountName'),
                     container_name=container_name,
                     blob_name=blob_name,
                     account_key=key,
                     permission=BlobSasPermissions(read=True),
                     expiry=expiry,
                     start=start_time
                 )

    return f"{blob_url_base}?{sas_token}" if sas_token else None


def store_generated_file(blob_name, competition, filename, file_size=0):
    """
    Record a generated file (merged PDF / ZIP) as a generatedpapers row: its
    blob name, never a link. Download links are minted per response, after
    the access check (_generated_file_links). REPLACE, so a row written by an
    older version loses its stored 5-day SAS URL when the file is regenerated.
    """
    try:
        table_client = get_table_client()
        if not table_client:
            logging.warning("No table client available, generated file not recorded")
            return

        # Ensure table exists (idempotent usually, checking first saves errors)
        try:
             table_client.create_table()
        except Exception:
             pass

        desc = "Individual judge papers (ZIP)" if filename.lower().endswith('.zip') else "All judge papers (PDF)"
        table_client.upsert_entity({
            "PartitionKey": competition,
            "RowKey": filename.replace('/', '_').replace('\\', '_'),
            "BlobName": blob_name,
            "Description": desc,
            "FileName": filename,
            "FileSize": int(file_size)
        }, mode=UpdateMode.REPLACE)
        logging.info(f"Recorded generated file {filename}")
    except Exception as e:
        logging.error(f"Error recording generated file: {e}")


def _generated_blob_name(paper, folder_path):
    """
    The blob a generatedpapers row stands for, or None. Rows written before
    BlobName existed stored a 5-day SAS URL instead: only the blob name is
    taken from it (the URL itself is never returned again). The first
    candidate inside the competition's own judgePapers folder wins — BlobName,
    the old URL's path, then `<folder>/judgePapers/<FileName>` — so a link
    never points outside that folder.
    """
    prefix = f"{folder_path.strip('/')}/judgePapers/"
    candidates = [paper.get("BlobName")]
    if paper.get("Url"):
        path = unquote(urlparse(str(paper["Url"])).path).lstrip("/")
        container_name, _, rest = path.partition("/")
        candidates.append(rest if container_name == GENERATED_CONTAINER else None)
    if paper.get("FileName"):
        candidates.append(f"{prefix}{paper['FileName']}")
    for blob_name in candidates:
        if isinstance(blob_name, str) and blob_name.startswith(prefix) and blob_name != prefix:
            return blob_name
    return None


def _generated_file_links(blob_service_client, entity, papers, now=None):
    """
    The generatedFiles list of a response: one fresh read-only SAS per file,
    valid GENERATED_LINK_VALIDITY (15 min). Call only after the caller's
    access check. `expiration` is the link's expiry; url/expiration are None
    when no link could be signed.
    """
    # Whole seconds: a SAS carries no fractions, so `expiration` matches `se`.
    now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    # Start a few minutes back so clock skew can't make it "not yet valid".
    start, expiry = now - timedelta(minutes=5), now + GENERATED_LINK_VALIDITY
    folder_path = entity.get("FolderPath", entity["RowKey"])
    ud_key, ud_key_failed = None, False
    links = []
    for paper in papers:
        url = None
        blob_name = _generated_blob_name(paper, folder_path)
        if blob_name and not ud_key_failed:
            try:
                if ud_key is None:
                    ud_key = _user_delegation_key(blob_service_client, start, expiry)
                url = generate_blob_read_sas_url(blob_service_client, GENERATED_CONTAINER, blob_name,
                                                 start, expiry, user_delegation_key=ud_key)
            except Exception as e:
                ud_key_failed = True
                logging.error(f"Could not sign generated-file links: {type(e).__name__}")
        links.append({
            "fileName": paper.get("FileName"),
            "url": url,
            "description": paper.get("Description"),
            "expiration": _iso_utc(expiry) if url else None,
            "size": paper.get("FileSize"),
        })
    return links


@app.route(route="list_competitions", auth_level=func.AuthLevel.ANONYMOUS)
def list_competitions(req: func.HttpRequest) -> func.HttpResponse:
    """
    The records the caller may read: those bound to a platform competition
    they hold a grant on (any role). Admins see every live record, including
    unbound legacy ones; only an admin's listing re-keys legacy name-keyed rows.
    503 `platform_unavailable` when access can't be checked (fail closed).
    """
    logging.info('Listing competitions...')

    caller = get_caller(req)
    if not caller:
        return func.HttpResponse("Unauthorized", status_code=401)

    try:
        # The competitions table is the source of truth (permanent history).
        # Rows with Visible=False (soft-deleted) are kept for statistics but hidden here.
        table_client = get_table_client("competitions")
        if not table_client:
            return func.HttpResponse("Storage configuration invalid", status_code=500)

        # Ensure table exists
        try: table_client.create_table()
        except: pass

        competitions = []
        for entity in list(table_client.query_entities("PartitionKey eq 'GLOBAL'")):
            # Hide soft-deleted competitions
            if entity.get("Visible") is False:
                continue

            # Lazily migrate legacy rows (RowKey = name) to the id-keyed
            # schema. They are unbound, so only an admin can see them at all.
            if "FolderPath" not in entity:
                if not caller.is_admin:
                    continue
                try:
                    entity = migrate_legacy_row(table_client, entity)
                except Exception as e:
                    logging.error(f"Failed to migrate legacy competition '{entity['RowKey']}': {e}")

            denied = _access_error(caller, entity.get("PlatformId"), "read")
            if denied is not None:
                if denied.status_code == 503:
                    return denied
                continue

            competitions.append({
                "id": entity["RowKey"],
                "name": entity.get("Name", entity["RowKey"]),
                "createdBy": entity.get("CreatedBy", "-"),
                "createdDate": entity.get("CreatedDate", "-"),
                "deletionDate": entity.get("DeletionDate", "-")
            })

        return func.HttpResponse(json.dumps(competitions), mimetype="application/json")
    except Exception as e:
        logging.error(f"Error listing competitions: {e}")
        return func.HttpResponse(json.dumps({"error": "Internal server error"}), status_code=500, mimetype="application/json")


def create_competition_record(comp_table, blob_service_client, safe_name, email, platform_id,
                              deletion_date=None):
    """
    Create a competition: the blob "folder" (its metadata.json) plus the
    permanent competitions history row, always bound to a platform
    competition (`platform_id`) — `resolve_competition` is the only creator.
    `deletion_date` is the platform's date to cache on the row (ISO-8601 Z);
    the tool gives a record no date of its own. Returns the created entity.
    """
    if not platform_id:
        raise ValueError("a competition record must be bound to a platform competition")
    new_id = generate_competition_id(comp_table)
    folder_path = f"{safe_name}-{new_id}"

    # Create metadata.json file to establish the "folder"
    now = datetime.utcnow()
    metadata = {
        "id": new_id,
        "name": safe_name,
        "createdBy": email,
        "createdDate": f"{now.isoformat()}Z"
    }
    metadata["platformId"] = platform_id

    container = blob_service_client.get_container_client("fs-judgepapers")
    container.upload_blob(f"{folder_path}/metadata.json", json.dumps(metadata, indent=4), overwrite=True)

    # Permanent history row — never deleted, only hidden via Visible=False
    entity = {
        "PartitionKey": "GLOBAL",
        "RowKey": new_id,
        "Name": safe_name,
        "FolderPath": folder_path,
        "Visible": True,
        "CreatedBy": metadata["createdBy"],
        "CreatedDate": metadata["createdDate"],
        "PlatformId": platform_id,
        "UploadedFileCount": 0,
        "GenerateRunCount": 0
    }
    if deletion_date:
        entity["DeletionDate"] = deletion_date
    comp_table.create_entity(entity)
    return entity


def _newest_competition(entities):
    """Pick the entity with the newest CreatedDate (unparseable dates sort oldest)."""
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    return max(entities, key=lambda e: _parse_iso_utc(e.get("CreatedDate")) or oldest)


def find_competition_by_platform_id(comp_table, platform_id, raw_platform_id=None):
    """
    Find the live competition bound to a platform competition GUID.

    `platform_id` is the normalized (lowercase) GUID; `raw_platform_id`, the
    id as the client sent it, is looked up too when it differs, so a record
    stored under another casing is found rather than duplicated (the match is
    rewritten to the normalized id). The PlatformId filter runs server-side;
    `Visible is False` is filtered in Python because legacy rows lack the
    property altogether (a `Visible eq true` filter would drop them).
    Soft-deleted rows keep their PlatformId but their blobs are gone, so they
    must never be resurrected. Newest CreatedDate wins if several visible
    rows share the id.
    """
    candidates = [platform_id]
    if raw_platform_id and raw_platform_id != platform_id:
        candidates.append(raw_platform_id)
    rows = []
    for candidate in candidates:
        safe_pid = str(candidate).replace("'", "''")
        rows.extend(comp_table.query_entities(
            f"PartitionKey eq 'GLOBAL' and PlatformId eq '{safe_pid}'"
        ))
    visible = [e for e in rows if e.get("Visible") is not False]
    if not visible:
        return None
    entity = _newest_competition(visible)
    if entity.get("PlatformId") != platform_id:
        try:
            comp_table.update_entity({
                "PartitionKey": "GLOBAL",
                "RowKey": entity["RowKey"],
                "PlatformId": platform_id,
            }, mode=UpdateMode.MERGE)
            entity["PlatformId"] = platform_id
        except Exception as e:
            logging.warning(f"Could not normalize the PlatformId of {entity['RowKey']}: {e}")
    return entity


def _sync_competition_name(comp_table, blob_service_client, entity, name):
    """
    Bring a platform-bound record's display Name in line with the platform
    name after a rename on the site (callers with write access only: the
    name comes from the client). Table write first (authoritative — the new
    name is only reported once it is stored); the metadata.json rewrite is
    best-effort. FolderPath is never touched: blob paths and generated-paper
    URLs hang off it. Returns the name callers should report.
    """
    new_name = sanitize_name(str(name or ""))
    old_name = entity.get("Name", entity["RowKey"])
    if not new_name or new_name == old_name:
        return old_name

    try:
        comp_table.update_entity({
            "PartitionKey": "GLOBAL",
            "RowKey": entity["RowKey"],
            "Name": new_name
        }, mode=UpdateMode.MERGE)
    except Exception as e:
        logging.warning(f"Could not rename competition {entity['RowKey']} to '{new_name}': {e}")
        return old_name

    entity["Name"] = new_name

    # metadata.json carries the display name too; keep it in step, best-effort.
    # Read it strictly here rather than via `_read_competition_metadata`: that
    # helper maps a missing/unreadable/malformed file to {}, and rewriting
    # {} + name would wipe id, createdBy, language and judgingMethodOverrides.
    # Any read problem means: leave the blob alone, the table is authoritative.
    try:
        folder_path = entity.get("FolderPath", entity["RowKey"])
        container = blob_service_client.get_container_client("fs-judgepapers")
        metadata_blob = container.get_blob_client(f"{folder_path}/metadata.json")
        meta = json.loads(metadata_blob.download_blob().readall())
        if not isinstance(meta, dict):
            raise ValueError("metadata.json is not a JSON object")
        meta["name"] = new_name
        metadata_blob.upload_blob(json.dumps(meta, indent=4), overwrite=True)
    except Exception as e:
        logging.warning(f"Left metadata.json of {entity['RowKey']} untouched after rename: {e}")

    logging.info(f"Synced competition {entity['RowKey']} name '{old_name}' -> '{new_name}'")
    return new_name


def resolve_competition_record(comp_table, blob_service_client, platform_id, name, email,
                               clock=None, clock_state=CLOCK_OK, raw_platform_id=None,
                               can_rename=False):
    """
    Core of POST /resolve_competition: platform-id lookup (syncing the stored
    Name when the platform competition has been renamed) -> create. Pure with
    respect to HTTP (clients are injected) so it can be driven by fakes.
    `clock`/`clock_state` is the platform clock already read by the caller:
    its deletion date is cached on the record. The caller has already checked
    that the user may read the platform competition. `name` comes from the
    client, so an existing record is renamed to it only when `can_rename`
    (the caller holds write or owner); a reader just gets the stored name.

    Unbound records are never adopted by name any more: that let anyone with
    any platform competition of the same name capture another user's record.
    A NEW binding (creation) needs a platform competition that demonstrably
    exists — otherwise nothing would ever delete the record — so it raises
    _PlatformBindingRefused when the registry says not found (404) or can't
    be read (503). Only CLOCK_OFF (local dev, no registry configured) binds
    without a date. Returns {id, name, created, deletionDate}.
    """
    entity = find_competition_by_platform_id(comp_table, platform_id, raw_platform_id)
    if entity is not None:
        if can_rename:
            result_name = _sync_competition_name(comp_table, blob_service_client, entity, name)
        else:
            result_name = entity.get("Name", entity["RowKey"])
        created = False
    else:
        if clock_state == CLOCK_NOT_FOUND:
            raise _PlatformBindingRefused(404, "platform_not_found: no such competition on the platform")
        if clock_state == CLOCK_UNAVAILABLE:
            raise _PlatformBindingRefused(
                503, "platform_unavailable: could not read the competition from the platform, try again")
        result_name = sanitize_name(name)
        platform_date = (clock or {}).get("deletionDate")
        entity = create_competition_record(
            comp_table, blob_service_client, result_name, email, platform_id,
            deletion_date=to_iso_z(platform_date) if platform_date else None,
        )
        created = True

    _sync_local_deletion_date(comp_table, entity, clock)
    return {
        "id": entity["RowKey"],
        "name": result_name,
        "created": created,
        "deletionDate": _deletion_date_value(entity, clock),
    }


class _PlatformBindingRefused(Exception):
    """resolve_competition must not bind a new record — maps to an HTTP status."""

    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@app.route(route="resolve_competition", auth_level=func.AuthLevel.ANONYMOUS, methods=["POST"])
def resolve_competition(req: func.HttpRequest) -> func.HttpResponse:
    """
    Bind the site's active platform competition to this tool's competition
    record: look it up by PlatformId (following platform renames), else create
    one. Body {platformId, name} -> {id, name, created, deletionDate} (the
    platform's date). Needs read access to the platform competition (404
    `platform_not_found` without it, 503 `platform_unavailable` when access
    can't be checked) — creating the empty bound record is allowed with read,
    but only write/owner may rename an existing record to the sent name.
    410 `platform_deleted` (and nothing created) when the platform competition
    was deleted or purged; a new binding also needs the platform row to be
    readable (404 `platform_not_found` / 503 `platform_unavailable`).
    """
    logging.info('Resolving platform competition...')

    caller = get_caller(req)
    if not caller:
        return func.HttpResponse("Unauthorized", status_code=401)

    try:
        req_body = req.get_json()
    except ValueError:
        return func.HttpResponse("Invalid JSON body", status_code=400)

    raw_platform_id = (req_body or {}).get('platformId')
    name = (req_body or {}).get('name')
    if not raw_platform_id or not isinstance(raw_platform_id, str):
        return func.HttpResponse("Missing platformId parameter", status_code=400)
    # One spelling everywhere (platform read, lookup, store): a differently
    # cased GUID must never bind a second record.
    platform_id = normalize_platform_id(raw_platform_id)
    if not platform_id:
        return func.HttpResponse("Invalid platformId parameter", status_code=400)
    if not name or not sanitize_name(str(name)):
        return func.HttpResponse("Missing or invalid name parameter", status_code=400)

    # Access before anything else, so a competition the caller can't see
    # reveals nothing (not even that it was deleted) and gets no record.
    role, denied = _access_role(caller, platform_id, "read")
    if denied is not None:
        return denied

    try:
        # The platform's retention clock first: a deleted/purged platform
        # competition must not get a fresh tool record just because a stale
        # browser tab still has it selected.
        clock, clock_state = _read_platform_clock_state(platform_id)
        if _platform_deleted(clock):
            return func.HttpResponse(
                "platform_deleted: this competition has been deleted", status_code=410)

        blob_service_client = get_blob_service_client()
        if not blob_service_client:
            return func.HttpResponse("Storage configuration invalid", status_code=500)

        comp_table = get_table_client("competitions")
        if not comp_table:
            return func.HttpResponse("Storage configuration invalid", status_code=500)

        # Ensure table exists
        try: comp_table.create_table()
        except: pass

        try:
            result = resolve_competition_record(
                comp_table, blob_service_client, platform_id, str(name), caller.email,
                clock=clock, clock_state=clock_state, raw_platform_id=raw_platform_id,
                can_rename=platform_access.role_satisfies(role, "write"),
            )
        except _PlatformBindingRefused as refused:
            return func.HttpResponse(refused.message, status_code=refused.status_code)
        logging.info(f"Resolved platform {platform_id} -> competition {result['id']} (created={result['created']})")
        return func.HttpResponse(json.dumps(result), mimetype="application/json")
    except Exception as e:
        logging.error(f"Error resolving competition: {e}")
        return func.HttpResponse("Internal server error", status_code=500)


def _delete_competition_data(entity, deleted_by):
    """
    Shared deletion flow used by the HTTP endpoint and the auto-deletion timer:
    delete the competition's blobs and generatedpapers rows, then soft-delete
    the competitions row (Visible=False + deletion audit). Returns the number
    of blobs deleted. Raises on hard failures (caller decides how to handle).
    `deleted_by` is USER_DELETE_ACTOR or AUTO_CLEANUP_ACTOR, never an address
    (anything else is stored as USER_DELETE_ACTOR). metadata.json, which holds
    createdBy, goes with the blobs.
    """
    comp_id = entity["RowKey"]
    folder_path = entity.get("FolderPath", comp_id)

    blob_service_client = get_blob_service_client()
    if not blob_service_client:
        raise RuntimeError("Storage configuration invalid")

    container = blob_service_client.get_container_client("fs-judgepapers")

    # List all blobs with this prefix and delete them
    blobs = container.list_blobs(name_starts_with=f"{folder_path}/")
    count = 0
    for blob in blobs:
        container.delete_blob(blob.name)
        count += 1

    # Delete generated-papers table entities for this competition
    try:
        table_client = get_table_client()
        if table_client:
            safe_pk = comp_id.replace("'", "''")
            entities = table_client.query_entities(f"PartitionKey eq '{safe_pk}'")
            table_count = 0
            for paper in entities:
                table_client.delete_entity(partition_key=paper['PartitionKey'], row_key=paper['RowKey'])
                table_count += 1
            logging.info(f"Deleted {table_count} table rows for competition {comp_id}")
    except Exception as table_err:
        logging.warning(f"Error deleting table entities: {table_err}")

    # Delete the send-e-mail audit rows. They hold recipient addresses, so a
    # failure must abort before the soft-delete below: a hidden competition is
    # never cleaned up again, and the caller (or the timer) can simply retry.
    sent_table = get_table_client("sentemails")
    if not sent_table:
        raise RuntimeError("Storage configuration invalid")
    try:
        safe_pk = comp_id.replace("'", "''")
        for row in sent_table.query_entities(f"PartitionKey eq '{safe_pk}'"):
            sent_table.delete_entity(partition_key=row['PartitionKey'], row_key=row['RowKey'])
    except ResourceNotFoundError:
        pass  # the table doesn't exist yet: nothing was ever sent

    # Keep the competitions row as permanent history: hide it and record the
    # deletion — without any address. The row outlives the data indefinitely
    # (statistics), so CreatedBy is blanked and DeletedBy says only what kind
    # of actor deleted it.
    if deleted_by not in (USER_DELETE_ACTOR, AUTO_CLEANUP_ACTOR):
        deleted_by = USER_DELETE_ACTOR
    comp_table = get_table_client("competitions")
    comp_table.update_entity({
        "PartitionKey": "GLOBAL",
        "RowKey": comp_id,
        "Visible": False,
        "DeletedDate": f"{datetime.utcnow().isoformat()}Z",
        "DeletedBy": deleted_by,
        "CreatedBy": "",
    }, mode=UpdateMode.MERGE)

    return count


def _id_from_query_or_body(req: func.HttpRequest):
    """`?id=` or a JSON body {"id"}; None when neither carries a string."""
    comp_id = req.params.get('id')
    if not comp_id:
        try:
            body = req.get_json()
        except ValueError:
            body = None
        comp_id = body.get('id') if isinstance(body, dict) else None
    return comp_id if isinstance(comp_id, str) and comp_id else None


@app.route(route="delete_competition", auth_level=func.AuthLevel.ANONYMOUS, methods=["POST"])
def delete_competition(req: func.HttpRequest) -> func.HttpResponse:
    """
    Delete this tool's record of a competition (its blobs, generated-file and
    sent-e-mail rows; the history row is hidden). POST only, id from `?id=`
    or a JSON body {"id"}. Owner only (an admin counts as owner): anyone else
    with access gets 403 platform_owner_required.
    """
    logging.info('Deleting competition...')

    if req.method.upper() != "POST":
        return func.HttpResponse("Method not allowed", status_code=405, headers={"Allow": "POST"})

    caller = get_caller(req)
    if not caller:
        return func.HttpResponse("Unauthorized", status_code=401)

    comp_id = _id_from_query_or_body(req)
    if not comp_id:
        return func.HttpResponse("Missing id parameter", status_code=400)

    try:
        entity, denied = _competition_for(caller, comp_id, "owner")
        if denied is not None:
            return denied

        count = _delete_competition_data(entity, USER_DELETE_ACTOR)

        return func.HttpResponse(json.dumps({"deleted": count}), mimetype="application/json")
    except Exception as e:
        logging.error(f"Error deleting competition: {e}")
        return func.HttpResponse("Internal server error", status_code=500)


def _scrub_hidden_rows(comp_table):
    """
    Blank CreatedBy/DeletedBy on hidden (deleted) rows that still hold an
    address — rows deleted before deletion stopped recording one. Visible
    rows keep their CreatedBy until they are deleted. Best-effort per row;
    returns the number of rows scrubbed.
    """
    scrubbed = 0
    for entity in list(comp_table.query_entities("PartitionKey eq 'GLOBAL'")):
        if entity.get("Visible") is not False:
            continue
        update = {}
        for column in ("CreatedBy", "DeletedBy"):
            value = entity.get(column)
            if value is not None and value not in _NON_PERSONAL_ACTORS:
                update[column] = ""
        if not update:
            continue
        try:
            comp_table.update_entity({
                "PartitionKey": "GLOBAL",
                "RowKey": entity["RowKey"],
                **update,
            }, mode=UpdateMode.MERGE)
            scrubbed += 1
        except Exception as e:
            logging.error(f"Could not scrub the deleted competition row {entity['RowKey']}: {e}")
    return scrubbed


def _orphan_reference(entity):
    """When an orphaned row's 60 days count from: CreatedDate, else the newest
    timestamp the row has (its statistics/generation dates, Table Timestamp)."""
    metadata = getattr(entity, "metadata", None) or {}
    return record_age_reference(
        entity.get("CreatedDate"),
        entity.get("LastGeneratedDate"),
        entity.get("LastStatisticsDate"),
        metadata.get("timestamp"),
    )


def run_auto_deletion(comp_table, now, clock_reader=None, orphan_purge=None):
    """
    One auto-deletion sweep; the platform decides. A bound row is deleted
    only on the platform's verdict (`platform_says_delete`: the competition is
    deleted or purged) — never on a date, the platform may be in dry-run or an
    overdue competition may just have been extended — and otherwise gets the
    platform date cached. An unreadable platform, or none configured, skips
    the row with an error. A legacy row without PlatformId is deleted on its
    stored DeletionDate (LEGACY_DELETION_DATE when it has none); nothing new
    is written for it. One platform table client serves the whole sweep.

    Orphans: a live row with no PlatformId (legacy name-keyed rows included)
    or one that isn't a GUID, or whose clock row a successful read did not
    find, is deleted with the same flow (data gone, statistics kept, CreatedBy
    blanked) once it is more than 60 days old (`_orphan_reference`); younger
    ones are kept. The step runs only when one probe of the clock table read
    at least one row (or the tool has no bound rows) — an unreadable,
    unconfigured or empty clock table aborts it for the run — and only
    deletes with ORPHAN_PURGE_ENABLED on (otherwise it logs what it would).
    Returns {deleted, skipped, failed, synced, orphaned, orphan_kept,
    orphan_dry_run, orphan_skipped}.
    """
    reader = clock_reader or _platform_clock_reader()
    purge = _orphan_purge_enabled() if orphan_purge is None else orphan_purge
    stats = {"deleted": 0, "skipped": 0, "failed": 0, "synced": 0,
             "orphaned": 0, "orphan_kept": 0, "orphan_dry_run": 0, "orphan_skipped": 0}
    off_logged = False
    live = [e for e in comp_table.query_entities("PartitionKey eq 'GLOBAL'")
            if e.get("Visible") is not False]
    bound_count = sum(1 for e in live if e.get("PlatformId"))
    orphan_ok, orphan_reason = orphan_sweep_allowed(getattr(reader, "table_client", None), bound_count)
    if not orphan_ok:
        logging.error(f"Auto-deletion: orphan step aborted for this run (platform clock {orphan_reason}; "
                      f"{bound_count} bound record(s))")

    def handle_orphan(entity, why):
        comp_id = entity["RowKey"]
        if not orphan_ok:
            stats["orphan_skipped"] += 1
            return
        if not orphan_old_enough(_orphan_reference(entity), now):
            stats["orphan_kept"] += 1
            logging.warning(f"Auto-deletion: {comp_id} is orphaned ({why}) but not 60 days old; kept")
            return
        if not purge:
            stats["orphan_dry_run"] += 1
            logging.warning(f"Auto-deletion: dry run, would delete orphaned competition {comp_id} ({why})")
            return
        try:
            _delete_competition_data(entity, AUTO_CLEANUP_ACTOR)
            stats["orphaned"] += 1
            logging.info(f"Auto-deleted orphaned competition {comp_id} ({why})")
        except Exception as e:
            stats["failed"] += 1
            logging.error(f"Auto-deletion failed for orphaned competition {comp_id}: {e}")

    for entity in live:
        comp_id = entity["RowKey"]
        platform_id = entity.get("PlatformId")
        # Legacy name-keyed rows (no FolderPath) are migrated lazily by an
        # admin's list_competitions and never carry a PlatformId: the only rule
        # that applies to them here is the orphan one.
        if "FolderPath" not in entity:
            handle_orphan(entity, "legacy row without PlatformId")
            continue

        if platform_id and normalize_platform_id(platform_id) is None:
            handle_orphan(entity, "PlatformId is not a GUID")
            continue

        clock, state = reader(platform_id)

        if state in (CLOCK_UNAVAILABLE, CLOCK_OFF):
            stats["skipped"] += 1
            if state == CLOCK_UNAVAILABLE:
                logging.error(f"Auto-deletion: platform clock unavailable for {comp_id}; skipped")
            elif not off_logged:
                off_logged = True
                logging.error("Auto-deletion: PLATFORM_STORAGE_ACCOUNT is not set; "
                              "platform-bound competitions are skipped")
            continue

        if state == CLOCK_NOT_FOUND:
            handle_orphan(entity, "no platform clock row")
            continue

        if state == CLOCK_OK:
            expired = platform_says_delete(clock)
            if not expired and _sync_local_deletion_date(comp_table, entity, clock):
                stats["synced"] += 1
        else:
            # CLOCK_UNBOUND: a legacy row's stored date (it can't be extended).
            # Never delete on an unparseable date.
            deletion = _parse_iso_utc(entity.get("DeletionDate") or LEGACY_DELETION_DATE)
            expired = deletion is not None and deletion <= now
            if not expired:
                handle_orphan(entity, "no PlatformId")
                continue

        if not expired:
            continue
        try:
            _delete_competition_data(entity, AUTO_CLEANUP_ACTOR)
            stats["deleted"] += 1
            logging.info(f"Auto-deleted expired competition {comp_id}")
        except Exception as e:
            stats["failed"] += 1
            logging.error(f"Auto-deletion failed for {comp_id}: {e}")
    return stats


@app.timer_trigger(schedule="0 0 4 * * *", arg_name="timer", run_on_startup=False)
def auto_delete_expired_competitions(timer: func.TimerRequest) -> None:
    """
    Daily sweep (04:00 UTC, an hour after the platform's 03:00 sweep, so it
    follows the platform's verdicts the same night): delete expired
    competitions with the same flow as manual deletion (run_auto_deletion),
    then blank leftover addresses on already-deleted rows.
    """
    logging.info("Running auto-deletion sweep for expired competitions...")
    try:
        comp_table = get_table_client("competitions")
        if not comp_table:
            logging.error("Auto-deletion: storage configuration invalid")
            return

        stats = run_auto_deletion(comp_table, datetime.now(timezone.utc))
        logging.info(
            f"Auto-deletion sweep complete. Deleted {stats['deleted']}, skipped {stats['skipped']} "
            f"(platform clock unreadable), failed {stats['failed']}, synced {stats['synced']} date(s). "
            f"Orphans: deleted {stats['orphaned']}, kept (under 60 days) {stats['orphan_kept']}, "
            f"dry run {stats['orphan_dry_run']}, skipped (step aborted) {stats['orphan_skipped']}.")
    except Exception as e:
        logging.error(f"Auto-deletion sweep error: {e}")

    try:
        comp_table = get_table_client("competitions")
        if comp_table:
            scrubbed = _scrub_hidden_rows(comp_table)
            if scrubbed:
                logging.info(f"Scrubbed addresses from {scrubbed} deleted competition row(s).")
    except Exception as e:
        logging.error(f"Deleted-row scrub error: {e}")


def _get_categories():
    """Load categories from the Azure Table, with caching."""
    try:
        table_client = get_table_client("categories")
        if table_client:
            return load_categories(table_client)
    except Exception as e:
        logging.warning(f"Failed to load categories table: {e}")
    return []


def parse_competition_file(filename: str, categories=None):
    """
    Parses the filename to extract Type, Category, Segment, JudgingMethod, and Suffix.
    Uses the 'categories' Azure Table for abbreviation matching (longest-prefix-match).
    Segment detection (QUAL/FNL) and split/group numbers are parsed generically.
    """
    try:
        if categories is None:
            categories = _get_categories()

        result = parse_filename_generic(filename, categories)
        return result
    except Exception:
        return None


def _read_competition_metadata(container, folder_path):
    """
    Reads {folder_path}/metadata.json and returns it as a dict.

    Best-effort: a missing, unreadable or malformed file yields {} so callers
    fall back to defaults rather than failing the whole request.
    """
    try:
        metadata_blob = container.get_blob_client(f"{folder_path}/metadata.json")
        if not metadata_blob.exists():
            return {}
        meta = json.loads(metadata_blob.download_blob().readall())
        return meta if isinstance(meta, dict) else {}
    except Exception as e:
        logging.warning(f"Could not read metadata.json for {folder_path}: {e}")
        return {}

@app.route(route="get_categories", auth_level=func.AuthLevel.ANONYMOUS)
def get_categories(req: func.HttpRequest) -> func.HttpResponse:
    """Returns all competition categories from the categories table."""
    logging.info('Getting categories...')

    caller = get_caller(req)
    if not caller:
        return func.HttpResponse("Unauthorized", status_code=401)

    try:
        categories = _get_categories()
        return func.HttpResponse(json.dumps(categories), mimetype="application/json")
    except Exception as e:
        logging.error(f"Error getting categories: {e}")
        return func.HttpResponse(json.dumps({"error": "Internal server error"}), status_code=500, mimetype="application/json")


@app.route(route="get_competition_details", auth_level=func.AuthLevel.ANONYMOUS)
def get_competition_details(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Getting competition details...')

    caller = get_caller(req)
    if not caller:
        return func.HttpResponse("Unauthorized", status_code=401)

    comp_id = req.params.get('id')
    if not comp_id:
        return func.HttpResponse("Missing id parameter", status_code=400)

    try:
        entity, denied = _competition_for(caller, comp_id, "read")
        if denied is not None:
            return denied

        folder_path = entity.get("FolderPath", entity["RowKey"])
        display_name = entity.get("Name", folder_path)

        # Retention: the platform's date (cached on the row), else the cached copy.
        clock, _clock_state = _read_platform_clock_state(entity.get("PlatformId"))
        if (clock or {}).get("deletionDate") is not None:
            _sync_local_deletion_date(get_table_client("competitions"), entity, clock)
        deletion_date = _deletion_date_value(entity, clock)

        blob_service_client = get_blob_service_client()
        if not blob_service_client: return func.HttpResponse("Config Error", status_code=500)

        container = blob_service_client.get_container_client("fs-judgepapers")

        # Pre-load categories once for all files
        categories = _get_categories()

        # Load competition settings from metadata.json
        meta = _read_competition_metadata(container, folder_path)
        competition_language = meta.get('language', 'fi')  # Default to Finnish
        judging_overrides = sanitize_judging_method_overrides(meta.get('judgingMethodOverrides'))

        # FS Manager re-exports overwrite the pool copy under the same name;
        # pull any newer pool version in before listing what we hold.
        refreshed = _refresh_from_pool(entity, container, folder_path)

        blobs = container.list_blobs(name_starts_with=f"{folder_path}/", include=["metadata"])
        
        files_data = []
        structure = {} # { category: { segment: [files] } }
        
        detected_types = set()
        detected_names = set()
        detected_dates = set()
        detected_category_codes = set()
        
        # For enriching segment names:
        # Maps (categoryCode, raw_segment_marker) -> actual segment name
        prefix_segment_names = {}  # prefix -> segment name from JudgesSheetAll line 2
        schedule_blob_name = None  # Track CompetitionSchedule blob for later parsing

        for blob in blobs:
            # Filter out files in subfolders (only process root of competition folder)
            # blob.name is "{folder_path}/{filename}"
            # We want to skip "{folder_path}/{subfolder}/{filename}"
            if '/' in blob.name[len(folder_path)+1:]:
                continue

            # Skip the init.md or metadata.json file
            if blob.name.endswith('init.md') or blob.name.endswith('metadata.json'):
                continue
                
            parsed = parse_competition_file(blob.name, categories)
            if parsed:
                # files_data, structure and competitionFiles share this dict.
                # lastModified = when this copy was written; uploadedUtc = when
                # the file itself was uploaded / pushed by FSM (the pool time
                # stamped at import, else the copy time for direct uploads).
                parsed['lastModified'] = _iso_utc(blob.last_modified)
                parsed['uploadedUtc'] = _meta(blob.metadata, 'poolUploadedUtc') or parsed['lastModified']
                parsed['poolSource'] = _meta(blob.metadata, 'poolSource')

                # judgingMethod is reported as the *effective* value so the UI
                # validation and the MUPI badge need no override rules of their
                # own; the table default is kept alongside for the switch.
                parsed['defaultJudgingMethod'] = parsed.get('judgingMethod', '')
                parsed['judgingMethodOverridable'] = is_synchronized_skating(parsed.get('type'))
                parsed['judgingMethod'] = effective_judging_method(
                    parsed.get('categoryCode'),
                    parsed.get('type'),
                    parsed['defaultJudgingMethod'],
                    judging_overrides,
                )

                files_data.append(parsed)
                cat = parsed['category']
                seg = parsed['segment']
                
                if parsed.get('categoryCode'):
                    detected_category_codes.add(parsed['categoryCode'])
                
                # Track CompetitionSchedule blob for later parsing
                if parsed['suffix'] == 'CompetitionSchedule.pdf':
                    schedule_blob_name = blob.name
                
                # Analyze Type (now from categories table, skip 'Competition' pseudo-type)
                if parsed.get('type') and parsed['type'] != 'Competition':
                    detected_types.add(parsed['type'])
                
                # Analyze Name (from JudgesSheetAll)
                if "JudgesDetailsAll" in parsed['suffix'] or "JudgesSheetAll" in parsed['suffix']:
                    try:
                        blob_client = container.get_blob_client(blob.name)
                        stream = io.BytesIO()
                        blob_client.download_blob().readinto(stream)
                        stream.seek(0)
                        
                        reader = PdfReader(stream)
                        if len(reader.pages) > 0:
                            # Use layout mode to respect visual order (Top-down)
                            try:
                                text = reader.pages[0].extract_text(extraction_mode="layout")
                            except Exception:
                                text = reader.pages[0].extract_text()
                                
                            if text:
                                lines = text.splitlines()
                                non_empty = [l.strip() for l in lines if l.strip()]
                                
                                # Line 1 = competition name
                                if non_empty:
                                    detected_names.add(non_empty[0])
                                    parsed['competition_name'] = non_empty[0]
                                
                                # Line 2 = full segment description (category + segment name)
                                # Extract just the segment part by stripping the category display name
                                if len(non_empty) >= 2:
                                    full_seg_line = non_empty[1]
                                    seg_name = full_seg_line
                                    
                                    # Get category display name for this file
                                    cat_display = parsed.get('category', '')
                                    # Strip "#N" split suffix if present
                                    cat_base = re.sub(r'\s*#\d+$', '', cat_display)
                                    
                                    if cat_base:
                                        upper_line = full_seg_line.upper()
                                        upper_cat = cat_base.upper()
                                        if upper_line.startswith(upper_cat):
                                            seg_name = full_seg_line[len(cat_base):].strip()
                                        else:
                                            # Fuzzy: normalize punctuation and try again
                                            norm_line = re.sub(r'[,./\-]', ' ', upper_line)
                                            norm_line = re.sub(r'\s+', ' ', norm_line).strip()
                                            norm_cat = re.sub(r'[,./\-]', ' ', upper_cat)
                                            norm_cat = re.sub(r'\s+', ' ', norm_cat).strip()
                                            if norm_line.startswith(norm_cat):
                                                # Walk original string to find split position
                                                ci = 0
                                                for i, ch in enumerate(full_seg_line):
                                                    if ci >= len(norm_cat):
                                                        seg_name = full_seg_line[i:].strip()
                                                        break
                                                    uch = ch.upper()
                                                    if uch in ',.-/' or (uch == ' ' and ci > 0 and norm_cat[ci-1] == ' '):
                                                        continue
                                                    if ci < len(norm_cat) and uch == norm_cat[ci]:
                                                        ci += 1
                                    
                                    if seg_name:
                                        file_prefix = parsed.get('prefix', '')
                                        if file_prefix:
                                            prefix_segment_names[file_prefix] = seg_name
                                            parsed['segment_display_name'] = seg_name
                    except Exception as ex:
                        logging.warning(f"Error reading PDF {blob.name}: {ex}")

                # Analyze Dates (from StartListwithTimes)
                if "StartListwithTimes" in parsed['suffix']:
                    try:
                        blob_client = container.get_blob_client(blob.name)
                        stream = io.BytesIO()
                        blob_client.download_blob().readinto(stream)
                        stream.seek(0)
                        
                        reader = PdfReader(stream)
                        if len(reader.pages) > 0:
                            try:
                                text = reader.pages[0].extract_text(extraction_mode="layout")
                            except:
                                text = reader.pages[0].extract_text()

                            if text:
                                # Look for Event Date pattern: dd MONTH yyyy (e.g. 25 OCTOBER 2025)
                                # Usually appears in header like: SATURDAY, 25 OCTOBER 2025
                                # We ignore the d.m.yyyy format to avoid capturing "Printed at" footer timestamps
                                months = r"(?:JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)"
                                pattern = r"\b(\d{1,2})\s+(" + months + r")\s+(\d{4})\b"
                                
                                matches = re.findall(pattern, text, re.IGNORECASE)
                                for match in matches:
                                    day, month_name, year = match
                                    try:
                                        # Parse date (e.g. "25 October 2025")
                                        dt_str = f"{day} {month_name.title()} {year}"
                                        dt = datetime.strptime(dt_str, "%d %B %Y")
                                        
                                        # Store as YYYY-MM-DD for sorting
                                        detected_dates.add(dt.strftime("%Y-%m-%d"))
                                    except Exception as e:
                                        logging.warning(f"Date parse error: {e}")
                    except Exception as ex:
                        logging.warning(f"Error reading PDF {blob.name} for dates: {ex}")

                # Competition-wide files (e.g. CompetitionSchedule) go to a
                # separate list, not into the per-category structure.
                if parsed.get('type') == 'Competition':
                    pass  # handled below via competitionFiles
                else:
                    if cat not in structure:
                        structure[cat] = {}
                    if seg not in structure[cat]:
                        structure[cat][seg] = []
                    
                    structure[cat][seg].append(parsed)
            else:
                # Unparsed file
                if "Uncategorized" not in structure:
                    structure["Uncategorized"] = {}
                if "Files" not in structure["Uncategorized"]:
                    structure["Uncategorized"]["Files"] = []
                structure["Uncategorized"]["Files"].append({
                    "filename": blob.name.split('/')[-1],
                    "suffix": blob.name.split('/')[-1],
                    "lastModified": _iso_utc(blob.last_modified),
                    "uploadedUtc": _meta(blob.metadata, 'poolUploadedUtc') or _iso_utc(blob.last_modified)
                })

        # ---------------------------------------------------------
        # Enrich segment names using JudgesSheetAll line 2
        # ---------------------------------------------------------
        # The raw segment keys are "QUAL", "FNL", "Category General", etc.
        # Enrich them with actual names from JudgesSheetAll (e.g., "PDK1 (Starlight Waltz)")
        # by looking up the prefix→segment_name mapping we built above.
        enriched_structure = {}
        for cat, segments in structure.items():
            enriched_structure[cat] = {}
            for seg_key, files in segments.items():
                # Find the best display name for this segment
                display_seg = seg_key
                if seg_key not in ("Category General", "General"):
                    # Look through files in this segment to find a prefix with a known name
                    for f in files:
                        file_prefix = f.get('prefix', '')
                        if file_prefix and file_prefix in prefix_segment_names:
                            display_seg = prefix_segment_names[file_prefix]
                            break
                    # If still a raw marker, apply fallback display names
                    if display_seg == seg_key:
                        if seg_key == "QUAL":
                            display_seg = "Short Program (QUAL)"
                        elif seg_key == "FNL":
                            display_seg = "Free Skating (FNL)"
                
                # Update the segment field in each file's parsed data too
                for f in files:
                    f['segment'] = display_seg
                
                if display_seg not in enriched_structure[cat]:
                    enriched_structure[cat][display_seg] = []
                enriched_structure[cat][display_seg].extend(files)
        
        structure = enriched_structure

        # Parse CompetitionSchedule if present (for schedule info in response)
        schedule_data = []
        if schedule_blob_name:
            try:
                blob_client = container.get_blob_client(schedule_blob_name)
                stream = io.BytesIO()
                blob_client.download_blob().readinto(stream)
                stream.seek(0)
                schedule_data = parse_competition_schedule(stream, categories)
            except Exception as ex:
                logging.warning(f"Error parsing CompetitionSchedule: {ex}")

        # Process metadata
        comp_type_display = list(detected_types)[0] if len(detected_types) == 1 else "Unknown" if len(detected_types) == 0 else "Mixed"
        comp_full_name = list(detected_names)[0] if len(detected_names) == 1 else "-"         
        
        # Process dates
        comp_date_display = "-"
        if detected_dates:
            sorted_dates = sorted(list(detected_dates))
            start_date = datetime.strptime(sorted_dates[0], "%Y-%m-%d")
            end_date = datetime.strptime(sorted_dates[-1], "%Y-%m-%d")
            
            # Format: d.M.yyyy
            start_str = f"{start_date.day}.{start_date.month}.{start_date.year}"
            end_str = f"{end_date.day}.{end_date.month}.{end_date.year}"
            
            if start_str == end_str:
                comp_date_display = start_str
            else:
                comp_date_display = f"{start_str} - {end_str}"

        alerts = []
        if len(detected_names) > 1:
            alerts.append(f"Multiple competition names found: {', '.join(detected_names)}")

        # Generated files (keyed by competition id), each with a fresh
        # short-lived link: the access check above has passed.
        generated_links = []
        try:
            table_client = get_table_client()
            if table_client:
                 safe_pk = comp_id.replace("'", "''")
                 try:
                     # `papers`, not `entity`: the competition entity is still in use.
                     papers = list(table_client.query_entities(f"PartitionKey eq '{safe_pk}'"))
                     generated_links = _generated_file_links(blob_service_client, entity, papers)
                 except ResourceNotFoundError:
                     pass
        except Exception as e:
            logging.warning(f"Could not fetch generated links: {e}")

        # Collect competition-wide files (e.g. CompetitionSchedule)
        competition_files = [f for f in files_data if f.get('type') == 'Competition']

        return func.HttpResponse(json.dumps({
            "id": comp_id,
            "name": display_name,
            "fullName": comp_full_name,
            "type": comp_type_display,
            "date": comp_date_display,
            "language": competition_language,
            # The UI sends this map back in full on every save, so it needs the
            # stored one even for categories it cannot currently see.
            "judgingMethodOverrides": judging_overrides,
            "files": files_data,
            "structure": structure,
            "competitionFiles": competition_files,
            "alerts": alerts,
            "categories": list(detected_category_codes),
            "generatedFiles": generated_links,
            # Names whose copy was just replaced by a newer one from the pool.
            "refreshedFromPool": refreshed,
            # The site UI has no competition list anymore; the auto-delete date
            # is surfaced in the detail view instead. It is the platform's
            # date, extended on the site only.
            "deletionDate": deletion_date,
        }), mimetype="application/json")
    except Exception as e:
        logging.error(f"Error getting details: {e}")
        return func.HttpResponse("Internal server error", status_code=500)


@app.route(route="save_competition_settings", auth_level=func.AuthLevel.ANONYMOUS, methods=["POST"])
def save_competition_settings(req: func.HttpRequest) -> func.HttpResponse:
    """
    Save competition settings (e.g. language, judgingMethodOverrides) to
    metadata.json. Settings are merged per key; a value that is itself a map
    (judgingMethodOverrides) is replaced wholesale, so the client must send it
    in full.
    """
    logging.info('Saving competition settings...')

    caller = get_caller(req)
    if not caller:
        return func.HttpResponse("Unauthorized", status_code=401)

    try:
        req_body = req.get_json()
        comp_id = req_body.get('id')
        settings = req_body.get('settings', {})
    except ValueError:
        return func.HttpResponse("Invalid JSON body", status_code=400)

    if not comp_id:
        return func.HttpResponse("Missing id parameter", status_code=400)

    # `settings` comes straight from the body: a null or non-object value would
    # otherwise blow up the `in` check below and the merge with a 500.
    if not isinstance(settings, dict):
        return func.HttpResponse("settings must be an object", status_code=400)

    # Reject a malformed override map outright instead of silently sanitizing
    # it away: the client would otherwise believe its choice was stored.
    if 'judgingMethodOverrides' in settings:
        overrides = settings['judgingMethodOverrides']
        if not isinstance(overrides, dict) or sanitize_judging_method_overrides(overrides) != overrides:
            return func.HttpResponse(
                "judgingMethodOverrides must be an object mapping category abbreviation to "
                f"one of {', '.join(JUDGING_METHODS)}",
                status_code=400
            )

    try:
        entity, denied = _competition_for(caller, comp_id, "write")
        if denied is not None:
            return denied

        folder_path = entity.get("FolderPath", entity["RowKey"])

        blob_service_client = get_blob_service_client()
        if not blob_service_client:
            return func.HttpResponse("Storage configuration error", status_code=500)

        container = blob_service_client.get_container_client("fs-judgepapers")

        # Read existing metadata
        existing_meta = _read_competition_metadata(container, folder_path)

        # Merge new settings into existing metadata
        for key, value in settings.items():
            existing_meta[key] = value

        # Write back
        container.upload_blob(
            f"{folder_path}/metadata.json",
            json.dumps(existing_meta, indent=4),
            overwrite=True
        )

        return func.HttpResponse("Settings saved", status_code=200)
    except Exception as e:
        logging.error(f"Error saving settings: {e}")
        return func.HttpResponse("Internal server error", status_code=500)


@app.route(route="generate_judging_papers", auth_level=func.AuthLevel.ANONYMOUS)
def generate_judging_papers(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Python HTTP trigger function processed a request.')

    # 1. Security Check
    caller = get_caller(req)

    if not caller:
        return func.HttpResponse("Unauthorized", status_code=401)
    if not is_user_allowed(caller):
        return func.HttpResponse("Forbidden: You are not on the allow list.", status_code=403)

    try:
        req_body = req.get_json()
        # workingFolder carries the competition id; the blob folder is resolved from the table
        comp_id = req_body.get('workingFolder')
        options = req_body.get('options', {})
    except ValueError:
        return func.HttpResponse("Invalid JSON body", status_code=400)

    # `options` is mutated below (judgingMethodOverrides) and read by the
    # processor with .get(); a null or non-object body value would 500.
    if not isinstance(options, dict):
        options = {}

    if not comp_id:
        return func.HttpResponse("Please pass a workingFolder in the request body", status_code=400)

    entity, denied = _competition_for(caller, comp_id, "write")
    if denied is not None:
        return denied

    working_folder = entity.get("FolderPath", entity["RowKey"])

    # Connect to Blob Storage
    try:
        blob_service_client = get_blob_service_client()
        if not blob_service_client:
             return func.HttpResponse("Storage configuration not found (AzureWebJobsStorage or AzureWebJobsStorage__accountName)", status_code=500)
            
        logging.info("Connected to Blob Storage")
    
        container_name = "fs-judgepapers"
        container_client = blob_service_client.get_container_client(container_name)

        # Create temp directories
        temp_dir = tempfile.mkdtemp()
        source_dir = os.path.join(temp_dir, "source")
        output_dir = os.path.join(temp_dir, "output")
        os.makedirs(source_dir)
        os.makedirs(output_dir)

        # Generate from the current FSM export, not a stale copy.
        _refresh_from_pool(entity, container_client, working_folder)

        # Download files
        logging.info(f"Downloading files from {working_folder}...")
        blobs = container_client.list_blobs(name_starts_with=working_folder)
        download_count = 0
        for blob in blobs:
            # Calculate relative path to maintain structure inside the working folder
            clean_working_folder = working_folder.strip("/")
            if blob.name.startswith(clean_working_folder + "/"):
                relative_path = blob.name[len(clean_working_folder)+1:]
            elif blob.name == clean_working_folder:
                continue 
            else:
                if not blob.name.startswith(clean_working_folder + "/"):
                    continue
                relative_path = blob.name[len(clean_working_folder)+1:]

            local_path = os.path.join(source_dir, relative_path)
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            
            with open(local_path, "wb") as download_file:
                download_file.write(container_client.download_blob(blob.name).readall())
            download_count += 1
        
        logging.info(f"Downloaded {download_count} files.")

        if download_count == 0:
             return func.HttpResponse(f"No files found in folder '{working_folder}'", status_code=404)

        # metadata.json is the only source of truth for the overrides; a value
        # sent by the client in options is overwritten, never trusted.
        meta = _read_competition_metadata(container_client, working_folder)
        options['judgingMethodOverrides'] = sanitize_judging_method_overrides(
            meta.get('judgingMethodOverrides')
        )

        # Run the processor
        logging.info("Running processor...")
        stats = process_judging_papers(source_dir, output_dir, options=options)

        # Upload results
        logging.info("Uploading results...")
        upload_count = 0
        for root, dirs, files in os.walk(output_dir):
            for file in files:
                local_file_path = os.path.join(root, file)
                relative_path = os.path.relpath(local_file_path, output_dir)
                
                # Upload to workingFolder/judgePapers/relative_path
                blob_name = f"{clean_working_folder}/judgePapers/{relative_path}".replace("\\", "/")
                
                # Get file size
                file_size = os.path.getsize(local_file_path)

                with open(local_file_path, "rb") as data:
                    container_client.upload_blob(name=blob_name, data=data, overwrite=True)
                upload_count += 1
                
                # Check for generate files (PDF summaries or ZIPs)
                if file.lower().endswith('.zip') or (file.lower().startswith('judgingpapers_') and file.lower().endswith('.pdf')):
                     store_generated_file(blob_name, comp_id, file, file_size)

        logging.info(f"Uploaded {upload_count} files.")

        _bump_competition_counters(comp_id, generate_delta=1, set_last_generated=True)
        _store_competition_statistics(comp_id, stats or {})

        return func.HttpResponse(f"Successfully processed {download_count} files and generated {upload_count} output files.", status_code=200)

    except Exception as e:
        logging.error(f"Error: {e}", exc_info=True)
        return func.HttpResponse("Error processing request. Check server logs for details.", status_code=500)
    finally:
        # Cleanup
        if 'temp_dir' in locals() and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
def _sends_in_last_day(sent_table, email):
    """Number of e-mails this user sent in the last 24 h (any competition).
    Copied links (Channel "link") have their own limit and don't count here."""
    since = _iso_utc(datetime.now(timezone.utc) - timedelta(days=1))
    safe_email = email.replace("'", "''")
    rows = sent_table.query_entities(
        f"SentBy eq '{safe_email}' and SentDate ge '{since}'", select=["RowKey", "Channel"])
    return sum(1 for row in rows if row.get("Channel") != LINK_CHANNEL)


def _links_in_last_day(sent_table, oid):
    """Number of links this user created in the last 24 h (any competition)."""
    since = _iso_utc(datetime.now(timezone.utc) - timedelta(days=1))
    rows = sent_table.query_entities(
        f"SentByOid eq '{oid}' and SentDate ge '{since}'", select=["RowKey"])
    return sum(1 for _ in rows)


def _link_expiry(entity, refusal):
    """
    (now, expiry, None) for a new download link of `entity` — the e-mailed one
    and the copied one alike — or (None, None, error response). Fresh 5 days,
    but never outliving the competition's auto-deletion: the platform's date
    for a bound record (the local one when the platform can't be read), else
    this tool's own. 410 platform_deleted for a deleted platform competition,
    409 when under SEND_MIN_LINK_VALIDITY remains (`refusal` ends that message).
    """
    clock, _clock_state = _read_platform_clock_state(entity.get("PlatformId"))
    if _platform_deleted(clock):
        return None, None, func.HttpResponse(
            "platform_deleted: this competition has been deleted", status_code=410)
    now = datetime.now(timezone.utc)
    expiry = now + timedelta(days=SEND_LINK_VALIDITY_DAYS)
    deletion = effective_deletion(entity.get("DeletionDate"), clock)
    if deletion and deletion < expiry:
        expiry = deletion
    if expiry - now < SEND_MIN_LINK_VALIDITY:
        return None, None, func.HttpResponse(
            f"The competition is about to be deleted; extend its retention {refusal}",
            status_code=409)
    return now, expiry, None


def _public_download_url(token):
    """The public link download_judge_papers resolves (never a storage URL)."""
    base_url = (os.environ.get("PUBLIC_BASE_URL") or DEFAULT_PUBLIC_BASE_URL).rstrip("/")
    return f"{base_url}/judgepapers/download?t={token}"


def _parse_send_request(req: func.HttpRequest, require_recipients: bool):
    """
    Shared auth, validation and lookups of send_judge_papers and its preview.
    Returns (context dict, None) or (None, error HttpResponse). The preview
    passes require_recipients=False: it renders before the addresses are typed.
    """
    caller = get_caller(req)
    if not caller:
        return None, func.HttpResponse("Unauthorized", status_code=401)
    if not is_user_allowed(caller):
        return None, func.HttpResponse("Forbidden", status_code=403)
    email = caller.email

    try:
        req_body = req.get_json()
    except ValueError:
        return None, func.HttpResponse("Invalid JSON body", status_code=400)
    if not isinstance(req_body, dict):
        return None, func.HttpResponse("Invalid JSON body", status_code=400)

    comp_id = req_body.get('id')
    file_name = req_body.get('fileName')
    to = email_sender.validate_email_address(req_body.get('to'))
    results_service_email = email_sender.validate_email_address(req_body.get('resultsServiceEmail'))
    sender_name = email_sender.validate_sender_name(req_body.get('senderName'))
    language = req_body.get('language')

    if not comp_id or not isinstance(comp_id, str):
        return None, func.HttpResponse("Missing id parameter", status_code=400)
    if not isinstance(file_name, str) or not _SENDABLE_FILE_RE.fullmatch(file_name):
        return None, func.HttpResponse("Only the merged judges' papers PDFs can be sent", status_code=400)
    if require_recipients and not to:
        return None, func.HttpResponse("Invalid recipient e-mail address", status_code=400)
    if require_recipients and not results_service_email:
        return None, func.HttpResponse("Invalid Results Service e-mail address", status_code=400)
    if not sender_name:
        return None, func.HttpResponse(
            f"Invalid sender name (1-{email_sender.SENDER_NAME_MAX_LENGTH} characters, no / : @ < >)",
            status_code=400)
    if language not in email_sender.LANGUAGES:
        return None, func.HttpResponse("Invalid language", status_code=400)

    entity, denied = _competition_for(caller, comp_id, "write")
    if denied is not None:
        return None, denied

    # The file must be one this competition actually generated: never mint
    # a SAS for an arbitrary blob name taken from the request.
    papers_table = get_table_client()
    if not papers_table:
        return None, func.HttpResponse("Storage configuration invalid", status_code=500)
    try:
        papers_table.get_entity(partition_key=comp_id, row_key=file_name)
    except ResourceNotFoundError:
        return None, func.HttpResponse("Generated file not found", status_code=404)

    now, expiry, error = _link_expiry(entity, "before sending")
    if error is not None:
        return None, error

    return {
        "email": email,
        "compId": comp_id,
        "entity": entity,
        "fileName": file_name,
        "to": to,
        "resultsServiceEmail": results_service_email,
        "senderName": sender_name,
        "language": language,
        "now": now,
        "expiry": expiry,
    }, None


@app.route(route="preview_judge_papers_email", auth_level=func.AuthLevel.ANONYMOUS, methods=["POST"])
def preview_judge_papers_email(req: func.HttpRequest) -> func.HttpResponse:
    """
    Render the send_judge_papers e-mail without sending it (same body; the
    addresses are optional). Mints no link and doesn't count toward the limit.
    """
    try:
        ctx, error = _parse_send_request(req, require_recipients=False)
        if error:
            return error
        preview = email_sender.render_preview(ctx["language"], ctx["senderName"], ctx["expiry"])
        return func.HttpResponse(json.dumps({
            **preview,
            "to": ctx["to"],
            "cc": ctx["resultsServiceEmail"],
            "expiryDisplay": email_sender.format_expiry(ctx["expiry"], ctx["language"]),
        }), mimetype="application/json")
    except Exception as e:
        logging.error(f"Error previewing judges' papers e-mail: {e}", exc_info=True)
        return func.HttpResponse("Internal server error", status_code=500)


@app.route(route="send_judge_papers", auth_level=func.AuthLevel.ANONYMOUS, methods=["POST"])
def send_judge_papers(req: func.HttpRequest) -> func.HttpResponse:
    """
    E-mail a download link of one merged judges'-papers PDF.
    Body: {id, fileName, to, resultsServiceEmail, senderName, language: fi|en}.
    The Results Service e-mail gets a copy (Cc); there is no Reply-To.
    """
    logging.info("Sending judges' papers e-mail...")

    try:
        ctx, error = _parse_send_request(req, require_recipients=True)
        if error:
            return error
        email, comp_id, file_name = ctx["email"], ctx["compId"], ctx["fileName"]
        to, results_service_email = ctx["to"], ctx["resultsServiceEmail"]
        language, now, expiry = ctx["language"], ctx["now"], ctx["expiry"]

        blob_service_client = get_blob_service_client()
        if not blob_service_client:
            return func.HttpResponse("Storage configuration invalid", status_code=500)
        container_name = "fs-judgepapers"
        folder_path = ctx["entity"].get("FolderPath", comp_id).strip("/")
        blob_name = f"{folder_path}/judgePapers/{file_name}"
        if not blob_service_client.get_container_client(container_name).get_blob_client(blob_name).exists():
            return func.HttpResponse("Generated file not found", status_code=404)

        sent_table = get_table_client("sentemails")
        if not sent_table:
            return func.HttpResponse("Storage configuration invalid", status_code=500)
        try:
            sent_table.create_table()
        except Exception:
            pass

        # The audit row doubles as the download link: its RowKey is the token
        # download_judge_papers resolves, so it is written before sending.
        token = secrets.token_urlsafe(24)
        url = _public_download_url(token)
        sent_table.create_entity({
            "PartitionKey": comp_id,
            "RowKey": token,
            "SentBy": email,
            "To": to,
            "Cc": results_service_email,
            "FileName": file_name,
            "Language": language,
            "SentDate": _iso_utc(now),
            "LinkExpiry": _iso_utc(expiry),
            "MailgunId": "",
            "DryRun": False,
        })

        # Count after writing our own row, so concurrent sends all see each
        # other and can't overshoot the limit (under contention they may both
        # be refused, never both allowed). Fails closed.
        try:
            over_limit = _sends_in_last_day(sent_table, email) > SEND_RATE_LIMIT_PER_DAY
        except Exception as e:
            logging.error(f"Send rate-limit check failed: {e}")
            over_limit = None
        if over_limit is not False:
            try:
                sent_table.delete_entity(partition_key=comp_id, row_key=token)
            except Exception as de:
                logging.warning(f"Failed to remove the refused e-mail's link row: {de}")
            if over_limit is None:
                return func.HttpResponse("Could not check the sending limit, try again later", status_code=503)
            return func.HttpResponse(
                f"Daily limit of {SEND_RATE_LIMIT_PER_DAY} e-mails reached, try again later",
                status_code=429)

        message = email_sender.build_message(
            language, to, results_service_email, ctx["senderName"], url, expiry)
        try:
            result = email_sender.send_message(message)
        except email_sender.EmailSendError as e:
            logging.error(f"Judges' papers e-mail for {comp_id} failed: {e}")
            try:
                sent_table.delete_entity(partition_key=comp_id, row_key=token)
            except Exception as de:
                logging.warning(f"Failed to remove the unsent e-mail's link row: {de}")
            return func.HttpResponse("The e-mail service rejected the message, try again later", status_code=502)

        # No addresses in the log: they live in the sentemails row, which is
        # deleted with the competition, while logs are kept on their own clock.
        recipient_count = sum(1 for address in (to, results_service_email) if address)
        logging.info(f"Sent {file_name} of {comp_id} to {recipient_count} recipient(s), "
                     f"Mailgun id {result['id']}, dry run {result['dryRun']}")

        try:
            sent_table.update_entity({
                "PartitionKey": comp_id,
                "RowKey": token,
                "MailgunId": result["id"] or "",
                "DryRun": result["dryRun"],
            }, mode=UpdateMode.MERGE)
        except Exception as e:
            logging.warning(f"Failed to record the Mailgun id on the sent-email row: {e}")
        _bump_competition_counters(comp_id, email_sent_delta=1)

        return func.HttpResponse(json.dumps({
            "expiryDate": _iso_utc(expiry),
            "expiryDisplay": email_sender.format_expiry(expiry, language),
            "dryRun": result["dryRun"],
        }), mimetype="application/json")
    except Exception as e:
        logging.error(f"Error sending judges' papers e-mail: {e}", exc_info=True)
        return func.HttpResponse("Internal server error", status_code=500)


@app.route(route="create_download_link", auth_level=func.AuthLevel.ANONYMOUS, methods=["POST"])
def create_download_link(req: func.HttpRequest) -> func.HttpResponse:
    """
    "Copy link": a download link of one generated file to share anywhere
    (WhatsApp, a chat, …). Body {id, fileName} -> {url, expiration}.

    The same public /judgepapers/download?t=<token> link the e-mail carries,
    with the same lifetime (_link_expiry: 5 days, capped at the competition's
    effective deletion date; 409 under an hour, 410 platform_deleted), served
    unchanged by download_judge_papers. Any generated file (merged PDF or
    ZIP) with a generatedpapers row. Read access is enough: a reader can
    download the file and pass it on anyway. Nothing is sent; the sentemails
    row (Channel "link", no recipients) is the link and its audit, deleted
    with the competition. LINK_RATE_LIMIT_PER_DAY links per user per 24 h,
    counted after writing like the e-mail limit, failing closed (503).
    """
    if req.method.upper() != "POST":
        return func.HttpResponse("Method not allowed", status_code=405, headers={"Allow": "POST"})

    caller = get_caller(req)
    if not caller:
        return func.HttpResponse("Unauthorized", status_code=401)
    if not is_user_allowed(caller):
        return func.HttpResponse("Forbidden", status_code=403)

    try:
        req_body = req.get_json()
    except ValueError:
        req_body = None
    if not isinstance(req_body, dict):
        return func.HttpResponse("Invalid JSON body", status_code=400)
    comp_id = req_body.get('id')
    file_name = req_body.get('fileName')
    if not comp_id or not isinstance(comp_id, str):
        return func.HttpResponse("Missing id parameter", status_code=400)
    if not isinstance(file_name, str) or not _LINKABLE_FILE_RE.fullmatch(file_name):
        return func.HttpResponse("Only generated judges' papers can be linked", status_code=400)

    try:
        entity, denied = _competition_for(caller, comp_id, "read")
        if denied is not None:
            return denied

        # Only a file this competition actually generated, like the e-mail.
        papers_table = get_table_client()
        if not papers_table:
            return func.HttpResponse("Storage configuration invalid", status_code=500)
        try:
            papers_table.get_entity(partition_key=comp_id, row_key=file_name)
        except ResourceNotFoundError:
            return func.HttpResponse("Generated file not found", status_code=404)

        now, expiry, error = _link_expiry(entity, "before sharing a link")
        if error is not None:
            return error

        blob_service_client = get_blob_service_client()
        if not blob_service_client:
            return func.HttpResponse("Storage configuration invalid", status_code=500)
        folder_path = entity.get("FolderPath", comp_id).strip("/")
        blob_name = f"{folder_path}/judgePapers/{file_name}"
        if not blob_service_client.get_container_client(GENERATED_CONTAINER).get_blob_client(blob_name).exists():
            return func.HttpResponse("Generated file not found", status_code=404)

        sent_table = get_table_client("sentemails")
        if not sent_table:
            return func.HttpResponse("Storage configuration invalid", status_code=500)
        try:
            sent_table.create_table()
        except Exception:
            pass

        token = secrets.token_urlsafe(24)
        sent_table.create_entity({
            "PartitionKey": comp_id,
            "RowKey": token,
            "Channel": LINK_CHANNEL,
            "SentBy": caller.email,
            "SentByOid": caller.oid,
            "To": "",
            "Cc": "",
            "FileName": file_name,
            "Language": "",
            "SentDate": _iso_utc(now),
            "LinkExpiry": _iso_utc(expiry),
            "MailgunId": "",
            "DryRun": False,
        })

        # Counted after writing our own row, like the e-mail limit; fails closed.
        try:
            over_limit = _links_in_last_day(sent_table, caller.oid) > LINK_RATE_LIMIT_PER_DAY
        except Exception as e:
            logging.error(f"Link rate-limit check failed: {e}")
            over_limit = None
        if over_limit is not False:
            try:
                sent_table.delete_entity(partition_key=comp_id, row_key=token)
            except Exception as de:
                logging.warning(f"Failed to remove the refused link's row: {de}")
            if over_limit is None:
                return func.HttpResponse("Could not check the link limit, try again later", status_code=503)
            return func.HttpResponse(
                f"Daily limit of {LINK_RATE_LIMIT_PER_DAY} links reached, try again later",
                status_code=429)

        logging.info(f"Created a download link of {file_name} in {comp_id}")
        _bump_competition_counters(comp_id, link_created_delta=1)
        return func.HttpResponse(json.dumps({
            "url": _public_download_url(token),
            "expiration": _iso_utc(expiry),
        }), mimetype="application/json")
    except Exception as e:
        logging.error(f"Error creating a judges' papers download link: {e}", exc_info=True)
        return func.HttpResponse("Internal server error", status_code=500)


_DOWNLOAD_NOT_FOUND = ("Linkkiä ei löydy tai tiedosto on poistettu.\n"
                       "This link is invalid or the file has been removed.\n")
_DOWNLOAD_EXPIRED = ("Linkki on vanhentunut. Pyydä lähettäjältä uusi linkki.\n"
                     "This link has expired. Please ask the sender for a new one.\n")
_DOWNLOAD_GONE = ("Tiedosto on poistettu.\n"
                  "This file has been removed.\n")


def _download_error(message, status_code):
    return func.HttpResponse(message, status_code=status_code, mimetype="text/plain",
                             charset="utf-8", headers={"Cache-Control": "no-store"})


@app.route(route="download_judge_papers", auth_level=func.AuthLevel.ANONYMOUS, methods=["GET", "HEAD"])
def download_judge_papers(req: func.HttpRequest) -> func.HttpResponse:
    """
    Resolve an e-mailed or copied download link (?t=<token>, the sentemails
    RowKey written by send_judge_papers or create_download_link) to a
    short-lived read SAS and redirect there. The recipient isn't signed in, so
    this is the one route with no user header — the proxy secret still applies.
    """
    if not _proxy_secret_ok(req):
        return func.HttpResponse("Unauthorized", status_code=401)

    try:
        token = req.params.get("t") or ""
        if not DOWNLOAD_TOKEN_RE.fullmatch(token):
            return _download_error(_DOWNLOAD_NOT_FOUND, 404)

        sent_table = get_table_client("sentemails")
        if not sent_table:
            return func.HttpResponse("Storage configuration invalid", status_code=500)
        # Cross-partition scan, fine at a few dozen rows a year. The token is
        # regex-checked above, so it can't break out of the filter string.
        row = next(iter(sent_table.query_entities(f"RowKey eq '{token}'")), None)
        if not row:
            # A well-formed token with no row is almost always a link whose
            # competition was deleted (its sentemails rows go with it): 410.
            return _download_error(_DOWNLOAD_NOT_FOUND, 410)

        now = datetime.now(timezone.utc)
        link_expiry = _parse_iso_utc(row.get("LinkExpiry"))
        if not link_expiry or link_expiry <= now:
            return _download_error(_DOWNLOAD_EXPIRED, 410)

        comp_id = row["PartitionKey"]
        # Strict lookup: only a missing or hidden competition is "gone" (410);
        # a storage failure must stay a retryable 5xx.
        try:
            entity = _get_competition_entity_strict(comp_id)
        except Exception as e:
            logging.error(f"Download link: could not read competition {comp_id}: {e}")
            return func.HttpResponse("Temporarily unavailable, try again later", status_code=503,
                                     headers={"Cache-Control": "no-store", "Retry-After": "60"})
        if not entity or entity.get("Visible") is False:
            return _download_error(_DOWNLOAD_GONE, 410)

        blob_service_client = get_blob_service_client()
        if not blob_service_client:
            return func.HttpResponse("Storage configuration invalid", status_code=500)
        container_name = "fs-judgepapers"
        folder_path = entity.get("FolderPath", comp_id).strip("/")
        blob_name = f"{folder_path}/judgePapers/{row.get('FileName', '')}"
        if not blob_service_client.get_container_client(container_name).get_blob_client(blob_name).exists():
            return _download_error(_DOWNLOAD_GONE, 410)

        # Start a few minutes back so clock skew can't make it "not yet valid".
        url = generate_blob_read_sas_url(
            blob_service_client, container_name, blob_name,
            now - timedelta(minutes=5), min(now + DOWNLOAD_SAS_VALIDITY, link_expiry))
        if not url:
            return func.HttpResponse("Could not create a download link", status_code=500)

        # Indicative only: mail scanners open links too.
        try:
            sent_table.update_entity({
                "PartitionKey": comp_id,
                "RowKey": token,
                "DownloadCount": int(row.get("DownloadCount") or 0) + 1,
                "LastDownloaded": _iso_utc(now),
            }, mode=UpdateMode.MERGE)
        except Exception as e:
            logging.warning(f"Failed to count the download of {token[:6]}…: {e}")

        return func.HttpResponse(status_code=302, headers={
            "Location": url,
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
        })
    except Exception as e:
        logging.error(f"Error resolving a judges' papers download link: {e}", exc_info=True)
        return func.HttpResponse("Internal server error", status_code=500)


@app.route(route="upload_file", auth_level=func.AuthLevel.ANONYMOUS, methods=["POST"])
def upload_file(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Uploading file...')

    caller = get_caller(req)
    if not caller:
        return func.HttpResponse("Unauthorized", status_code=401)
        
    competition = req.params.get('competition')
    filename = req.params.get('filename')
    
    if not competition or not filename:
        return func.HttpResponse("Missing competition or filename", status_code=400)
    
    # Sanitize filename: strip path traversal characters
    filename = os.path.basename(filename)
    
    if not filename.lower().endswith('.pdf'):
        return func.HttpResponse("Only PDF files are allowed", status_code=400)

    # Check file size limit
    content_length = req.headers.get('Content-Length')
    if content_length and int(content_length) > MAX_UPLOAD_SIZE:
        return func.HttpResponse(f"File too large. Maximum size is {MAX_UPLOAD_SIZE // (1024*1024)} MB.", status_code=413)

    try:
        file_content = req.get_body()

        if len(file_content) > MAX_UPLOAD_SIZE:
            return func.HttpResponse(f"File too large. Maximum size is {MAX_UPLOAD_SIZE // (1024*1024)} MB.", status_code=413)

        # 'competition' carries the competition id; resolve the blob folder from the table
        entity, denied = _competition_for(caller, competition, "write")
        if denied is not None:
            return denied

        folder_path = entity.get("FolderPath", entity["RowKey"])

        blob_service_client = get_blob_service_client()
        if not blob_service_client: return func.HttpResponse("Config Error", status_code=500)

        container = blob_service_client.get_container_client("fs-judgepapers")

        blob_path = f"{folder_path}/{filename}"

        container.upload_blob(blob_path, file_content, overwrite=True)

        _bump_competition_counters(competition, uploaded_delta=1)

        return func.HttpResponse(json.dumps({"filename": filename, "status": "uploaded"}), mimetype="application/json")
    except Exception as e:
        logging.error(f"Error uploading file: {e}")
        return func.HttpResponse("Internal server error", status_code=500)

@app.route(route="import_platform_file", auth_level=func.AuthLevel.ANONYMOUS, methods=["POST"])
def import_platform_file(req: func.HttpRequest) -> func.HttpResponse:
    """
    Copy a file from the platform's shared competition file pool into this
    tool's competition folder. Query: competition (this tool's competition id),
    name (pool blob file name) and the optional source — "upload" (the default,
    files people uploaded) or "fsm" (files the HOVTP listener pushed). The pool
    path is composed server-side from the competition's bound PlatformId and
    the source's fixed folder name, so a client can never point this at another
    competition's files.
    """
    logging.info('Importing file from the platform file pool...')

    caller = get_caller(req)
    if not caller:
        return func.HttpResponse("Unauthorized", status_code=401)

    competition = req.params.get('competition')
    name = req.params.get('name')

    if not competition or not name:
        return func.HttpResponse("Missing competition or name", status_code=400)

    # The pool has two folders; the client picks one by name, never by path.
    source = req.params.get('source') or 'upload'
    if source not in ('upload', 'fsm'):
        return func.HttpResponse(
            "invalid_source: source must be 'upload' or 'fsm'", status_code=400)

    # Sanitize: only the basename is ever used, both for the pool lookup and
    # for the destination blob path.
    filename = os.path.basename(name)

    if not filename.lower().endswith('.pdf'):
        return func.HttpResponse("Only PDF files are allowed", status_code=400)

    entity, denied = _competition_for(caller, competition, "write")
    if denied is not None:
        return denied

    raw_platform_id = entity.get("PlatformId")
    if not raw_platform_id:
        return func.HttpResponse(
            "not_bound: this competition is not linked to a platform competition",
            status_code=409)
    # The binding originates from a client body (resolve_competition), so pin it
    # to a lowercase GUID before splicing it into the pool blob path.
    platform_id = normalize_platform_id(raw_platform_id)
    if not platform_id:
        logging.warning(f"Rejecting non-UUID PlatformId {raw_platform_id!r} on competition {competition}")
        return func.HttpResponse(
            "not_bound: this competition's platform link is invalid",
            status_code=409)

    try:
        pool_container = get_platform_container_client()
    except Exception as e:
        logging.error(f"Platform pool client creation failed: {e}")
        return func.HttpResponse(
            "platform_unavailable: could not read the competition file pool",
            status_code=502)
    if pool_container is None:
        return func.HttpResponse(
            "platform_not_configured: the platform file pool is not configured",
            status_code=503)

    pool_path = f"{platform_id}/{'fsm' if source == 'fsm' else 'uploads'}/{filename}"

    try:
        properties = pool_container.get_blob_client(pool_path).get_blob_properties()
        if properties.size and properties.size > MAX_UPLOAD_SIZE:
            return func.HttpResponse(f"File too large. Maximum size is {MAX_UPLOAD_SIZE // (1024*1024)} MB.", status_code=413)
    except ResourceNotFoundError:
        return func.HttpResponse("File not found in the competition file pool", status_code=404)
    except Exception as e:
        logging.error(f"Error reading platform pool blob {pool_path}: {e}")
        return func.HttpResponse(
            "platform_unavailable: could not read the competition file pool",
            status_code=502)

    try:
        folder_path = entity.get("FolderPath", entity["RowKey"])

        blob_service_client = get_blob_service_client()
        if not blob_service_client: return func.HttpResponse("Config Error", status_code=500)

        container = blob_service_client.get_container_client("fs-judgepapers")

        blob_path = f"{folder_path}/{filename}"
    except Exception as e:
        logging.error(f"Error importing file: {e}")
        return func.HttpResponse("Internal server error", status_code=500)

    # Provenance: which pool folder the copy came from, how fresh the pool blob
    # was, and who pulled it — `_refresh_from_pool` compares against this.
    metadata = {
        "poolSource": source,
        "poolUploadedUtc": _iso_utc(getattr(properties, "last_modified", None)),
        "importedBy": caller.email,
    }

    try:
        _copy_pool_blob(pool_container, pool_path, container, blob_path, metadata)
    except _PoolFileTooLarge:
        return func.HttpResponse(f"File too large. Maximum size is {MAX_UPLOAD_SIZE // (1024*1024)} MB.", status_code=413)
    except ResourceNotFoundError:
        return func.HttpResponse("File not found in the competition file pool", status_code=404)
    except _PoolReadError as e:
        logging.error(f"Error reading platform pool blob {pool_path}: {e}")
        return func.HttpResponse(
            "platform_unavailable: could not read the competition file pool",
            status_code=502)
    except Exception as e:
        logging.error(f"Error importing file: {e}")
        return func.HttpResponse("Internal server error", status_code=500)

    try:
        _bump_competition_counters(competition, uploaded_delta=1)

        return func.HttpResponse(json.dumps({"filename": filename, "status": "uploaded"}), mimetype="application/json")
    except Exception as e:
        logging.error(f"Error importing file: {e}")
        return func.HttpResponse("Internal server error", status_code=500)

@app.route(route="delete_file", auth_level=func.AuthLevel.ANONYMOUS, methods=["DELETE", "POST"])
def delete_file(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Deleting file...')

    caller = get_caller(req)
    if not caller:
        return func.HttpResponse("Unauthorized", status_code=401)
        
    competition = req.params.get('competition')
    filename = req.params.get('filename')
    
    if not competition or not filename:
        return func.HttpResponse("Missing competition or filename", status_code=400)

    try:
        # 'competition' carries the competition id; resolve the blob folder from the table
        entity, denied = _competition_for(caller, competition, "write")
        if denied is not None:
            return denied

        folder_path = entity.get("FolderPath", entity["RowKey"])

        blob_service_client = get_blob_service_client()
        if not blob_service_client: return func.HttpResponse("Config Error", status_code=500)

        container = blob_service_client.get_container_client("fs-judgepapers")

        blob_path = f"{folder_path}/{filename}"

        if container.get_blob_client(blob_path).exists():
            container.delete_blob(blob_path)

            # Try to delete associated table entity (if distinct generated file)
            try:
                table_client = get_table_client()
                if table_client:
                    simple_filename = os.path.basename(filename)
                    row_key = simple_filename.replace('/', '_').replace('\\', '_')
                    # We ignore errors if the entity does not exist
                    table_client.delete_entity(partition_key=competition, row_key=row_key)
                    logging.info(f"Deleted table entity for {simple_filename}")
            except ResourceNotFoundError:
                pass
            except Exception as table_err:
                logging.warning(f"Error deleting table entity: {table_err}")

            # Only source uploads count toward UploadedFileCount (not generated outputs)
            if not filename.startswith('judgePapers/'):
                _bump_competition_counters(competition, uploaded_delta=-1)

            return func.HttpResponse(json.dumps({"status": "deleted"}), mimetype="application/json")
        else:
            return func.HttpResponse("File not found", status_code=404)
            
    except Exception as e:
        logging.error(f"Error deleting file: {e}")
        return func.HttpResponse("Internal server error", status_code=500)
