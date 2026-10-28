"""
Shared storage + auth helpers for the Protocol Generator backend.

Mirrors the patterns proven in fs-judgepapers: a public Function App that trusts
the site router (verified by a fail-closed shared secret) for the caller's
identity, Managed-Identity access to Blob and Table storage, a permanent
`competitions` registry table with soft-deletes, and per-competition blob
folders holding a `metadata.json` structure document.
"""
import azure.functions as func
import logging
import os
import hmac
import json
import re
from uuid import uuid4
from datetime import datetime, timedelta, timezone
from typing import NamedTuple
from urllib.parse import quote, unquote

from azure.storage.blob import BlobServiceClient, generate_blob_sas, BlobSasPermissions
from azure.identity import DefaultAzureCredential
from azure.core import MatchConditions
from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import TableClient, UpdateMode

import platform_clock as pc

# Blob container holding every competition folder.
CONTAINER_NAME = "fs-protocolgenerator"

# Platform-owned container holding the shared per-competition file pool. Read
# only, and only when PLATFORM_STORAGE_ACCOUNT names the platform's account.
PLATFORM_DATA_CONTAINER = "competition-data"

# Maximum single-file upload size: 50 MB (result-PDF sets and photos can be large).
MAX_UPLOAD_SIZE = 50 * 1024 * 1024

# Maximum ZIP upload size for the bulk fallback-picture import: a whole
# competition's accreditation photos easily pass 50 MB. Azure Functions caps an
# HTTP request body at ~100 MB, so this is the practical ceiling.
MAX_ZIP_UPLOAD_SIZE = 100 * 1024 * 1024

# Automatic competition deletion. The platform registry owns every
# competition's deletion date (platform_clock.py); this tool keeps no clock of
# its own. LEGACY_DELETION_DATE only dates legacy rows that were never bound to
# a platform competition and predate the DeletionDate property.
LEGACY_DELETION_DATE = "2026-06-12T00:00:00Z"
AUTO_CLEANUP_ACTOR = "auto-cleanup"
# A deleted row records *how* it went, never who: no e-mail address outlives
# the competition's data.
USER_DELETE_ACTOR = "user"
# CreatedBy / DeletedBy values that are not an address (and so need no scrub).
NON_PERSONAL_ACTORS = ("", "-", USER_DELETE_ACTOR, AUTO_CLEANUP_ACTOR)


# ── Auth ────────────────────────────────────────────────────────────────────
#
# Every request reaches this app through the figureskatingtools-site router
# (PROXY-CONTRACT.md). The router proves itself with the shared secret and
# forwards the signed-in principal as x-forwarded-user-* headers; it drops any
# copy a browser sent. Those headers are the ONLY identity source — trusted
# solely because the secret matched, which is why the secret check fails closed.

_GUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


class Caller(NamedTuple):
    """The signed-in user as the router forwarded them. `tid`/`oid` (lowercase
    Entra GUIDs) are the identity every access decision uses; `email` and
    `name` are for display and audit fields only, never for authorisation."""
    tid: str
    oid: str
    email: str
    name: str
    is_admin: bool


def _guid(value):
    text = str(value or "").strip().lower()
    return text if _GUID_RE.match(text) else None


def _proxy_secret_ok(req: func.HttpRequest) -> bool:
    """The request came from the site router: x-proxy-secret equals
    PROXY_SHARED_SECRET (constant-time). Fails CLOSED — with the setting empty
    or unset nothing is accepted."""
    expected = os.environ.get("PROXY_SHARED_SECRET") or ""
    if not expected:
        return False
    provided = req.headers.get("x-proxy-secret") or ""
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def get_caller(req: func.HttpRequest):
    """The router-forwarded caller, or None (→ 401) when the proxy secret does
    not match or the tenant/object id is missing or not a GUID."""
    if not _proxy_secret_ok(req):
        logging.warning("Proxy shared secret missing or mismatched; rejecting request")
        return None
    tid = _guid(req.headers.get("x-forwarded-user-tid"))
    oid = _guid(req.headers.get("x-forwarded-user-oid"))
    if tid is None or oid is None:
        logging.warning("Forwarded identity missing or invalid; rejecting request")
        return None
    email = (req.headers.get("x-forwarded-user-email") or "").strip().lower()
    # encodeURIComponent-encoded by the router (headers are Latin-1 only).
    name = unquote(req.headers.get("x-forwarded-user-name") or "").strip()
    return Caller(tid=tid, oid=oid, email=email, name=name,
                  is_admin=req.headers.get("x-forwarded-user-admin") == "1")


# ── Storage clients ───────────────────────────────────────────────────────────

def get_blob_service_client():
    try:
        account_name = os.environ.get("AzureWebJobsStorage__accountName")
        if account_name:
            credential = DefaultAzureCredential()
            account_url = f"https://{account_name}.blob.core.windows.net"
            return BlobServiceClient(account_url=account_url, credential=credential)
        connection_string = os.environ.get("AzureWebJobsStorage")
        if connection_string:
            return BlobServiceClient.from_connection_string(connection_string)
        return None
    except Exception as e:
        logging.error(f"Failed to create blob client: {e}")
        return None


def get_container_client():
    bsc = get_blob_service_client()
    if not bsc:
        return None
    return bsc.get_container_client(CONTAINER_NAME)


def get_platform_container_client():
    """Container client for the platform's shared competition file pool
    (`competition-data/<platform-guid>/uploads/...`), or None when
    PLATFORM_STORAGE_ACCOUNT is unset — the import feature is then cleanly off.

    This app holds Storage Blob Data *Reader* on that account (granted by the
    site repo's shared-data-access.bicep), so the credential is the same
    Managed Identity used for our own storage — only the account differs."""
    account_name = os.environ.get("PLATFORM_STORAGE_ACCOUNT")
    if not account_name:
        return None
    # Creation failures propagate: callers map them to "platform unavailable"
    # (502), which is distinct from the deliberate feature-off None (503).
    container_name = os.environ.get("PLATFORM_DATA_CONTAINER") or PLATFORM_DATA_CONTAINER
    account_url = f"https://{account_name}.blob.core.windows.net"
    bsc = BlobServiceClient(account_url=account_url, credential=DefaultAzureCredential())
    return bsc.get_container_client(container_name)


def get_table_client(table_name="generatedprotocols"):
    try:
        account_name = os.environ.get("AzureWebJobsStorage__accountName")
        if account_name:
            credential = DefaultAzureCredential()
            endpoint = f"https://{account_name}.table.core.windows.net"
            return TableClient(endpoint=endpoint, table_name=table_name, credential=credential)
        connection_string = os.environ.get("AzureWebJobsStorage")
        if connection_string:
            return TableClient.from_connection_string(conn_str=connection_string, table_name=table_name)
        return None
    except Exception as e:
        logging.error(f"Failed to create table client: {e}")
        return None


# ── Competition registry (the permanent `competitions` table) ─────────────────

def sanitize_name(name: str) -> str:
    return "".join([c for c in name if c.isalnum() or c in (' ', '-', '_')]).strip()


MAX_CODE_LENGTH = 64   # the platform's own cap on a competition code


def sanitize_code(code) -> str:
    """The platform's competition code (already a slug there, e.g.
    'winter-cup-2026'), made safe for a file name; '' when none."""
    if not isinstance(code, str):
        return ""
    return sanitize_name(code).replace(" ", "-")[:MAX_CODE_LENGTH].strip("-_")


def generate_competition_id(comp_table) -> str:
    for _ in range(5):
        cid = uuid4().hex[:8]
        try:
            comp_table.get_entity(partition_key="GLOBAL", row_key=cid)
        except ResourceNotFoundError:
            return cid
    raise RuntimeError("Could not generate a unique competition id")


def get_competition_entity(comp_id: str):
    try:
        comp_table = get_table_client("competitions")
        if not comp_table:
            return None
        return comp_table.get_entity(partition_key="GLOBAL", row_key=comp_id)
    except ResourceNotFoundError:
        return None
    except Exception as e:
        logging.error(f"Error fetching competition entity {comp_id}: {e}")
        return None


def parse_iso_utc(value):
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


def ensure_deletion_date(comp_table, entity):
    """Backfill LEGACY_DELETION_DATE onto a legacy *unbound* row without a
    date. A bound row is left alone: its date is the platform's."""
    if entity.get("DeletionDate") or entity.get("PlatformId"):
        return entity
    entity["DeletionDate"] = LEGACY_DELETION_DATE
    try:
        comp_table.update_entity({
            "PartitionKey": "GLOBAL",
            "RowKey": entity["RowKey"],
            "DeletionDate": LEGACY_DELETION_DATE,
        }, mode=UpdateMode.MERGE)
    except Exception as e:
        logging.warning(f"Failed to backfill DeletionDate for {entity['RowKey']}: {e}")
    return entity


def sync_local_deletion_date(comp_table, entity, clock):
    """Cache the platform's deletion date on a bound record (MERGE), so the
    stored DeletionDate is the last date the platform reported — shown while
    the platform cannot be read. Best-effort; a no-op when the platform has no
    date or the cached one already matches. Returns the record's DeletionDate
    string (None when it has none)."""
    platform_date = (clock or {}).get("deletionDate")
    if platform_date is None:
        return entity.get("DeletionDate")
    platform_str = pc.to_iso_z(platform_date)
    if pc.parse_utc(entity.get("DeletionDate")) != platform_date:
        try:
            comp_table.update_entity({
                "PartitionKey": "GLOBAL",
                "RowKey": entity["RowKey"],
                "DeletionDate": platform_str,
            }, mode=UpdateMode.MERGE)
        except Exception as e:
            logging.warning(
                f"Could not sync DeletionDate for {entity['RowKey']}: {type(e).__name__}")
    entity["DeletionDate"] = platform_str
    return platform_str


# ── metadata.json (the per-competition structure document) ────────────────────

def metadata_blob_path(folder_path: str) -> str:
    return f"{folder_path}/metadata.json"


def read_structure(folder_path: str):
    """Read and parse a competition's metadata.json structure document."""
    container = get_container_client()
    if not container:
        return None
    blob = container.get_blob_client(metadata_blob_path(folder_path))
    if not blob.exists():
        return None
    return json.loads(blob.download_blob().readall())


def write_structure(folder_path: str, structure: dict):
    """Persist the structure document back to metadata.json (overwrites)."""
    container = get_container_client()
    if not container:
        raise RuntimeError("Storage configuration invalid")
    container.upload_blob(
        metadata_blob_path(folder_path),
        json.dumps(structure, ensure_ascii=False, indent=2),
        overwrite=True,
    )


# ── generated protocol downloads ──────────────────────────────────────────────
#
# A generated protocol is listed in the `generatedprotocols` table (one row per
# PDF: FileName, FileSize, Description) and its download link is minted on
# every `get_competition_details`, after the access check: a read-only SAS on
# that one blob, valid DOWNLOAD_LINK_TTL. No link is ever stored, so revoking
# someone's access leaves them at most that long with a link they already had.
# Rows written before this carried a 5-day `Url`/`ExpirationDate`; those columns
# are ignored and scrubbed (`scrub_stored_link`).

DOWNLOAD_LINK_TTL = timedelta(minutes=15)
# The SAS (and its user delegation key) starts this far in the past so a
# browser or storage clock running slightly behind still accepts it.
SAS_CLOCK_SKEW = timedelta(minutes=5)
STORED_LINK_FIELDS = ("Url", "ExpirationDate")


def protocol_blob_name(folder_path: str, file_name: str) -> str:
    """The blob a generated protocol lives in, derived from the competition's
    folder and the row's file name — never from a stored URL."""
    return f"{folder_path}/protocols/{os.path.basename(file_name or '')}"


def protocol_file_name(code, name) -> str:
    """`<competition code>_protocol.pdf`, the way FS Manager names its files —
    the cleaned competition name while the record has no code. The same for
    every generation of one competition, so regenerating replaces the PDF."""
    stem = (sanitize_code(code)
            or sanitize_name(name or "").replace(" ", "_")
            or "competition")
    return f"{stem}_protocol.pdf"


def record_generated_protocol(competition, filename, file_size=0, generated_at=None):
    """List a freshly generated protocol for `get_competition_details`, with
    when it was generated (`GeneratedAt`, ISO UTC). Stores no link: those are
    minted per request (`mint_read_urls`)."""
    try:
        table_client = get_table_client()
        if not table_client:
            return
        try:
            table_client.create_table()
        except Exception:
            pass
        table_client.upsert_entity({
            "PartitionKey": competition,
            "RowKey": filename.replace('/', '_').replace('\\', '_'),
            "Description": "Competition protocol (PDF)",
            "FileName": filename,
            "FileSize": int(file_size),
            "GeneratedAt": generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }, mode=UpdateMode.REPLACE)
    except Exception as e:
        logging.error(f"Error recording generated protocol: {type(e).__name__}")


def scrub_stored_link(table_client, entity):
    """Rewrite a legacy row without its stored long-lived link (best effort:
    the link is ignored either way). Update-only and conditional on the etag
    the row was read with, so a protocol deleted (or rewritten) since is never
    recreated or overwritten: the SDK then raises and the scrub is skipped."""
    if not any(field in entity for field in STORED_LINK_FIELDS):
        return
    try:
        clean = {k: v for k, v in entity.items() if k not in STORED_LINK_FIELDS}
        etag = (getattr(entity, "metadata", None) or {}).get("etag")
        if etag:
            table_client.update_entity(clean, mode=UpdateMode.REPLACE, etag=etag,
                                       match_condition=MatchConditions.IfNotModified)
        else:
            # Without an etag the SDK's default If-Match "*" still refuses a
            # missing row.
            table_client.update_entity(clean, mode=UpdateMode.REPLACE)
    except Exception as e:
        logging.warning(f"Could not scrub a stored protocol link: {type(e).__name__}")


def mint_read_urls(blob_service_client, blob_names, now=None):
    """`({blob name: url}, expiry)`: a read-only SAS URL per blob, valid until
    `expiry` = now + DOWNLOAD_LINK_TTL (one user delegation key for the whole
    batch, itself expiring then). `({}, None)` when nothing can be signed."""
    blob_names = list(blob_names)
    if not blob_names or blob_service_client is None:
        return {}, None
    now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    start = now - SAS_CLOCK_SKEW
    expiry = now + DOWNLOAD_LINK_TTL
    account_name = blob_service_client.account_name
    sign = None
    if os.environ.get("AzureWebJobsStorage__accountName"):
        ud_key = blob_service_client.get_user_delegation_key(start, expiry)
        sign = lambda name: generate_blob_sas(
            account_name=account_name, container_name=CONTAINER_NAME, blob_name=name,
            user_delegation_key=ud_key, permission=BlobSasPermissions(read=True),
            expiry=expiry, start=start)
    else:
        conn_str = os.environ.get("AzureWebJobsStorage")
        items = dict(item.split('=', 1) for item in (conn_str or "").split(';') if '=' in item)
        key = items.get('AccountKey')
        if key:
            account_name = items.get('AccountName') or account_name
            sign = lambda name: generate_blob_sas(
                account_name=account_name, container_name=CONTAINER_NAME, blob_name=name,
                account_key=key, permission=BlobSasPermissions(read=True),
                expiry=expiry, start=start)
    if sign is None:
        return {}, None
    base = blob_service_client.url.rstrip("/")
    return ({name: f"{base}/{CONTAINER_NAME}/{quote(name)}?{sign(name)}"
             for name in blob_names}, expiry)


def json_response(payload, status_code=200):
    return func.HttpResponse(
        json.dumps(payload, ensure_ascii=False),
        mimetype="application/json",
        status_code=status_code,
    )
