"""
Shared storage + auth helpers for the Protocol Generator backend.

Mirrors the patterns proven in fs-judgepapers: a public Function App that trusts
the Web App proxy (verified by a shared secret), Managed-Identity access to Blob
and Table storage, a permanent `competitions` registry table with soft-deletes,
and per-competition blob folders holding a `metadata.json` structure document.
"""
import azure.functions as func
import logging
import os
import base64
import json
from uuid import uuid4
from datetime import datetime, timedelta, timezone

from azure.storage.blob import BlobServiceClient, generate_blob_sas, BlobSasPermissions
from azure.identity import DefaultAzureCredential
from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import TableClient, UpdateMode

# Blob container holding every competition folder.
CONTAINER_NAME = "fs-protocolgenerator"

# Maximum single-file upload size: 50 MB (result-PDF sets and photos can be large).
MAX_UPLOAD_SIZE = 50 * 1024 * 1024

# Maximum ZIP upload size for the bulk fallback-picture import: a whole
# competition's accreditation photos easily pass 50 MB. Azure Functions caps an
# HTTP request body at ~100 MB, so this is the practical ceiling.
MAX_ZIP_UPLOAD_SIZE = 100 * 1024 * 1024

# Automatic competition deletion lifecycle (same policy as fs-judgepapers).
DELETION_RETENTION_DAYS = 60
DELETION_EXTENSION_DAYS = 7
LEGACY_DELETION_DATE = "2026-06-12T00:00:00Z"
AUTO_CLEANUP_ACTOR = "auto-cleanup"


# ── Auth ────────────────────────────────────────────────────────────────────

def _proxy_secret_ok(req: func.HttpRequest) -> bool:
    """Verify the request came from the Web App proxy via the shared secret.
    Enforced only when PROXY_SHARED_SECRET is set (local dev fails open)."""
    expected = os.environ.get("PROXY_SHARED_SECRET")
    if not expected:
        return True
    provided = req.headers.get("X-Proxy-Secret") or req.headers.get("x-proxy-secret")
    return provided == expected


def _decode_jwt_payload(token: str):
    """Decode a JWT payload without verification (base64url only)."""
    try:
        parts = token.split('.')
        if len(parts) != 3:
            return None
        payload_b64 = parts[1] + '=' * (4 - len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception as e:
        logging.error(f"Error decoding JWT payload: {e}")
        return None


def get_user_email_from_header(req: func.HttpRequest):
    """Extract the authenticated user's email from Easy Auth headers, the
    Web App proxy's forwarded header, or a bearer token. Returns None if the
    request did not come through the trusted proxy or carries no identity."""
    if not _proxy_secret_ok(req):
        logging.warning("Proxy shared secret missing or mismatched; rejecting request")
        return None

    val = req.headers.get("X-MS-CLIENT-PRINCIPAL-NAME") or req.headers.get("x-ms-client-principal-name")
    if val:
        return val

    forwarded = req.headers.get("X-Forwarded-User-Email") or req.headers.get("x-forwarded-user-email")
    if forwarded:
        return forwarded

    header = req.headers.get("x-ms-client-principal") or req.headers.get("X-MS-CLIENT-PRINCIPAL")
    if header:
        try:
            principal = json.loads(base64.b64decode(header).decode("utf-8"))
            return principal.get("userDetails")
        except Exception as e:
            logging.error(f"Error parsing auth header: {e}")

    auth_header = req.headers.get("Authorization") or req.headers.get("authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        claims = _decode_jwt_payload(auth_header[7:])
        if claims:
            email = (claims.get("preferred_username") or claims.get("email")
                     or claims.get("upn") or claims.get("unique_name"))
            if not email:
                emails = claims.get("emails")
                if isinstance(emails, list) and emails:
                    email = emails[0]
            if not email:
                email = claims.get("name") or claims.get("oid")
            if email:
                return email

    return None


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
    if entity.get("DeletionDate"):
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


# ── SAS links for generated protocol downloads ────────────────────────────────

def create_and_store_sas_link(blob_service_client, blob_name, competition, filename, file_size=0):
    """Create a 5-day read SAS for a generated protocol and store it in the
    generatedprotocols table for retrieval by get_competition_details."""
    try:
        table_client = get_table_client()
        if not table_client:
            return
        try:
            table_client.create_table()
        except Exception:
            pass

        start_time = datetime.utcnow()
        expiry = start_time + timedelta(days=5)
        sas_token = ""
        account_name = blob_service_client.account_name
        blob_url_base = f"https://{account_name}.blob.core.windows.net/{CONTAINER_NAME}/{blob_name}"

        if os.environ.get("AzureWebJobsStorage__accountName"):
            ud_key = blob_service_client.get_user_delegation_key(start_time, expiry)
            sas_token = generate_blob_sas(
                account_name=account_name, container_name=CONTAINER_NAME, blob_name=blob_name,
                user_delegation_key=ud_key, permission=BlobSasPermissions(read=True),
                expiry=expiry, start=start_time)
        else:
            conn_str = os.environ.get("AzureWebJobsStorage")
            if conn_str:
                items = dict(item.split('=', 1) for item in conn_str.split(';') if '=' in item)
                key = items.get('AccountKey')
                if key:
                    sas_token = generate_blob_sas(
                        account_name=items.get('AccountName'), container_name=CONTAINER_NAME,
                        blob_name=blob_name, account_key=key,
                        permission=BlobSasPermissions(read=True), expiry=expiry, start=start_time)

        if sas_token:
            entity = {
                "PartitionKey": competition,
                "RowKey": filename.replace('/', '_').replace('\\', '_'),
                "Url": f"{blob_url_base}?{sas_token}",
                "ExpirationDate": expiry.isoformat(),
                "Description": "Competition protocol (PDF)",
                "FileName": filename,
                "FileSize": int(file_size),
            }
            table_client.upsert_entity(entity)
    except Exception as e:
        logging.error(f"Error creating SAS: {e}")


def json_response(payload, status_code=200):
    return func.HttpResponse(
        json.dumps(payload, ensure_ascii=False),
        mimetype="application/json",
        status_code=status_code,
    )
