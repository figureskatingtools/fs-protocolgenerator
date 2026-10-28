import azure.functions as func
import logging
import os
import io
import json
import re
import traceback
import zipfile
from datetime import date, datetime, timezone

from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import UpdateMode

import platform_access as pa
import platform_clock as pc
import storage_helpers as sh
import structure as st
from schedule_parser import parse_schedule_data
import dt_partic
import odf_birth_dates
from dt_partic import parse_participants, parse_team_rosters
import fallback_photos
import image_sanitize
import roster_matching
from results_parser import parse_top_three, count_result_rows, parse_result_rows
from assemble import assemble_protocol
from pdf_sanitize import SanitizeError

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp") + image_sanitize.HEIF_EXTS
# HEIC/HEIF uploads are converted to JPEG on arrival (browsers other than Safari
# cannot preview them); full resolution, generous quality — the protocol
# re-encodes it to its printed size anyway.
HEIF_STORE_QUALITY = 92


# ── small helpers ─────────────────────────────────────────────────────────────

def _require_user(req):
    """The router-forwarded caller (`sh.Caller`), or None → 401."""
    return sh.get_caller(req)


def _unauthorized():
    return func.HttpResponse("Unauthorized", status_code=401)


# ── competition access (see platform_access.py) ───────────────────────────────
#
# The platform registry decides who may see or change a competition. Every
# route that names a competition — by this tool's record id or by a platform
# GUID — asks for "read", "write" or "owner" and gets one of these refusals:
# 404 platform_not_found (no access, unknown, or deleted here: existence is
# never revealed), 403 platform_read_only (a read grant on a write route),
# 403 platform_owner_required (a read or write grant on an owner route — an
# admin counts as owner), 503 platform_unavailable (the access tables could
# not be read — fail closed).

def _platform_error(code, message, status):
    return sh.json_response({"error": code, "message": message}, status)


def _not_found():
    return _platform_error("platform_not_found",
                           "No such competition, or you do not have access to it", 404)


def _role_for(caller, platform_id):
    """The caller's role on a platform competition ("owner"|"write"|"read"|None).
    A record with no (valid) PlatformId is legacy, from before platform
    binding: only an admin may touch it. Raises pa.PlatformAccessUnavailable."""
    if pc.normalize_platform_id(platform_id) is None:
        return "owner" if caller.is_admin else None
    return pa.role_for(platform_id, caller.tid, caller.oid, is_admin=caller.is_admin)


def _check_access(caller, platform_id, need):
    """`(role, None)` when the caller holds `need` ("read"|"write"|"owner")
    on that platform competition, else `(None, error response)`."""
    try:
        role = _role_for(caller, platform_id)
    except pa.PlatformAccessUnavailable:
        return None, _platform_error("platform_unavailable",
                                     "Could not check access to this competition", 503)
    if role is None:
        return None, _not_found()
    if not pa.role_satisfies(role, need):
        if need == "owner":
            return None, _platform_error("platform_owner_required",
                                         "Only the competition's owner can do this", 403)
        return None, _platform_error("platform_read_only",
                                     "You have view-only access to this competition", 403)
    return role, None


def _require_competition(caller, comp_id, need):
    """`(entity, folder_path, None)` for a competition record the caller holds
    `need` on, else `(None, None, response)`. A missing or soft-deleted record
    answers exactly like one the caller may not see."""
    entity = sh.get_competition_entity(comp_id)
    if not entity or entity.get("Visible") is False:
        return None, None, _not_found()
    _, refusal = _check_access(caller, entity.get("PlatformId"), need)
    if refusal:
        return None, None, refusal
    return entity, entity.get("FolderPath", entity["RowKey"]), None


def _truthy(value) -> bool:
    """A boolean that may arrive as a query-param string or as real JSON."""
    return value is True or (isinstance(value, str) and value.strip().lower() in ("1", "true"))


def _kind_for(filename: str) -> str:
    lower = filename.lower()
    if lower.endswith(".pdf"):
        return "pdf"
    if lower.endswith(".xml"):
        return "xml"
    if lower.endswith(IMAGE_EXTS):
        return "image"
    return "other"


def _file_bytes_getter(folder_path, structure):
    """Build a get_file_bytes(file_id) closure for the assembler."""
    container = sh.get_container_client()

    def _get(file_id):
        meta = structure.get("files", {}).get(file_id)
        if not meta or not meta.get("blob"):
            return None
        blob = container.get_blob_client(meta["blob"])
        if not blob.exists():
            return None
        return blob.download_blob().readall()

    return _get


def _fill_podium_from_results(structure, category):
    """Read a category's total-results PDF and pre-fill any *empty* podium name
    fields with the top three ('<code> - <name>'). User-entered names are kept."""
    file_id = category.get("totalResultsPdf")
    meta = structure.get("files", {}).get(file_id) if file_id else None
    if not meta or meta.get("kind") != "pdf" or not meta.get("blob"):
        return
    try:
        blob = sh.get_container_client().get_blob_client(meta["blob"])
        if not blob.exists():
            return
        top = parse_top_three(blob.download_blob().readall())
    except Exception as e:
        logging.warning(f"Top-three podium autofill failed: {e}")
        return
    if not top:
        return
    podium = category.setdefault("podium", {"photo": None, "names": ["", "", ""]})
    names = (list(podium.get("names") or []) + ["", "", ""])[:3]
    for i in range(3):
        if not (names[i] or "").strip() and i < len(top) and top[i]:
            names[i] = top[i]
    podium["names"] = names


def _coerce_count(value):
    """Normalise a user-entered unit count to a non-negative int, or None when
    blank/invalid (so a cleared field reverts to 'unknown')."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _fill_segment_count_from_results(structure, segment):
    """Read a segment's results PDF and pre-fill its `unitCount` (number of
    competition units that performed the segment) when it is still unset. A
    user-entered count is kept."""
    if segment is None or segment.get("unitCount") is not None:
        return
    file_id = segment.get("resultsPdf")
    meta = structure.get("files", {}).get(file_id) if file_id else None
    if not meta or meta.get("kind") != "pdf" or not meta.get("blob"):
        return
    try:
        blob = sh.get_container_client().get_blob_client(meta["blob"])
        if not blob.exists():
            return
        count = count_result_rows(blob.download_blob().readall())
    except Exception as e:
        logging.warning(f"Segment unit-count autofill failed: {e}")
        return
    if count > 0:
        segment["unitCount"] = count


# ── competition registry ──────────────────────────────────────────────────────

# Liveness for the site's admin diagnostics: no sign-in, no secret, no storage
# (the same shape as the platform API's /api/health).
@app.route(route="health", auth_level=func.AuthLevel.ANONYMOUS, methods=["GET"])
def health(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse(json.dumps({"status": "ok", "service": "fs-protocolgenerator"}), mimetype="application/json")


@app.route(route="check_user_permission", auth_level=func.AuthLevel.ANONYMOUS, methods=["GET"])
def check_user_permission(req: func.HttpRequest) -> func.HttpResponse:
    caller = _require_user(req)
    if not caller:
        return sh.json_response({"allowed": False, "email": None}, 401)
    return sh.json_response({"allowed": True, "email": caller.email})


@app.route(route="list_competitions", auth_level=func.AuthLevel.ANONYMOUS, methods=["GET"])
def list_competitions(req: func.HttpRequest) -> func.HttpResponse:
    """The visible records the caller may read on the platform (an admin: all,
    legacy unbound records included). 503 when access cannot be checked."""
    caller = _require_user(req)
    if not caller:
        return _unauthorized()
    try:
        comp_table = sh.get_table_client("competitions")
        if not comp_table:
            return func.HttpResponse("Storage configuration invalid", status_code=500)
        try:
            comp_table.create_table()
        except Exception:
            pass

        competitions = []
        for entity in list(comp_table.query_entities("PartitionKey eq 'GLOBAL'")):
            if entity.get("Visible") is False:
                continue
            try:
                if _role_for(caller, entity.get("PlatformId")) is None:
                    continue
            except pa.PlatformAccessUnavailable:
                return _platform_error("platform_unavailable",
                                       "Could not check access to the competitions", 503)
            sh.ensure_deletion_date(comp_table, entity)
            competitions.append({
                "id": entity["RowKey"],
                "name": entity.get("Name", entity["RowKey"]),
                "createdBy": entity.get("CreatedBy", "-"),
                "createdDate": entity.get("CreatedDate", "-"),
                "deletionDate": entity.get("DeletionDate", "-"),
            })
        return sh.json_response(competitions)
    except Exception as e:
        logging.error(f"Error listing competitions: {e}")
        return sh.json_response({"error": "Internal server error"}, 500)


def _competitions_table():
    """The permanent registry table client (created on first use), or None when
    storage is not configured."""
    comp_table = sh.get_table_client("competitions")
    if not comp_table:
        return None
    try:
        comp_table.create_table()
    except Exception:
        pass
    return comp_table


def _create_competition_record(comp_table, email, safe_name, dates, platform_id,
                               rink="", dates_auto=False, deletion_date=None, code=""):
    """Seed a new competition bound to a platform competition — unique id,
    metadata.json structure document and registry entity — and return its id.
    The only creation path is `resolve_competition`. The record has no
    retention clock of its own: `DeletionDate` is only a cached copy of the
    platform's date (`deletion_date`), absent until the platform reports one."""
    if not platform_id:
        raise ValueError("A competition must be bound to a platform competition")
    new_id = sh.generate_competition_id(comp_table)
    folder_path = f"{safe_name}-{new_id}"
    now = datetime.utcnow()
    created_date = f"{now.isoformat()}Z"

    structure = st.new_structure(new_id, safe_name, dates, email, created_date)
    if rink:
        structure["event"]["rink"] = rink
    if dates and dates_auto:
        structure["event"]["datesAuto"] = True
    structure["platformId"] = platform_id
    sh.write_structure(folder_path, structure)

    entity = {
        "PartitionKey": "GLOBAL",
        "RowKey": new_id,
        "Name": safe_name,
        "FolderPath": folder_path,
        "Visible": True,
        "CreatedBy": email,
        "CreatedDate": created_date,
        "PlatformId": platform_id,
    }
    if code:
        entity["Code"] = code
    if deletion_date:
        entity["DeletionDate"] = deletion_date
    comp_table.create_entity(entity)
    return new_id


# ── platform retention clock (see platform_clock.py) ─────────────────────────

def _platform_table():
    """`(client, error)`: the one platform clock table client for this request
    or sweep (None when PLATFORM_STORAGE_ACCOUNT is unset), or the exception
    type name when it could not be created."""
    try:
        return pc.get_platform_table_client(), None
    except Exception as e:
        logging.error(f"Platform clock client creation failed: {type(e).__name__}")
        return None, type(e).__name__


def _read_clock(platform_id, table_client, table_error=None):
    """`(clock, unavailable)` for a bound platform id: the platform's clock row
    (None when unbound, not a GUID, absent, or the feature is off) and whether
    the platform could not be read at all."""
    if not platform_id:
        return None, False
    if table_error:
        return None, True
    if table_client is None:
        return None, False
    try:
        return pc.read_platform_clock(platform_id, table_client=table_client), False
    except pc.PlatformClockUnavailable:
        logging.error(f"Platform clock unavailable for platform competition {platform_id}")
        return None, True


def _refuse_new_binding(clock, unavailable, configured):
    """The error response that blocks a *new* binding (a created record), or
    None. A record bound to a platform competition that does not exist would
    never be deleted, so the platform must confirm it: 404 `platform_not_found`
    when it has no such competition, 503 `platform_unavailable` when it cannot
    be read. With the platform not configured (local dev) the binding goes
    ahead without a date. Existing bound records never come here."""
    if unavailable:
        return sh.json_response(
            {"error": "platform_unavailable",
             "message": "Could not reach the competition registry"}, 503)
    if clock is not None or not configured:
        return None
    return sh.json_response(
        {"error": "platform_not_found",
         "message": "No such competition in the competition registry"}, 404)


def _cached_date_passed(entity, now) -> bool:
    """The record's stored DeletionDate has passed (False when it has none)."""
    deletion = pc.parse_utc(entity.get("DeletionDate"))
    return deletion is not None and deletion <= now


# ── platform binding (the site's shared competition selector) ──────────────────

_ISO_DATE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")


def _fi_date(value) -> str:
    """Finnish dd.MM.yyyy rendering of a platform date. The site sends ISO
    (`2025-01-25`, sometimes a span or with a time part); protocols print Finnish
    dates everywhere. Anything that isn't a recognisable ISO date passes through
    untouched, so a hand-written "1.-2.5.2026" survives a re-resolve."""
    text = str(value or "").strip()
    if not text:
        return ""

    def _fi(match):
        try:
            return date(int(match.group(1)), int(match.group(2)),
                        int(match.group(3))).strftime("%d.%m.%Y")
        except ValueError:
            return match.group(0)

    # Drop a trailing ISO time part ("2025-01-25T00:00:00Z" -> the day), then
    # reformat every ISO date in place, so a span ("2026-05-01 – 2026-05-02")
    # keeps its separator.
    day_only = re.sub(r"^(\d{4}-\d{1,2}-\d{1,2})[T ]\d{1,2}:\d{2}.*$", r"\1", text)
    return _ISO_DATE.sub(_fi, day_only)


def _backfill_platform_event(structure, dates, venue) -> bool:
    """Fill *empty* event fields from the platform's competition data; True when
    something changed. Never clobbers: an existing value is either user-entered or
    a better source (the schedule's full date span).

    The one exception is a stored *raw ISO* date: only the earlier version of this
    route could have written that (no user types "2025-01-25" into the Finnish
    date field), so it is repaired in place and flagged auto, letting a schedule
    re-parse widen it to the full span."""
    event = structure.setdefault("event", {})
    changed = False
    if venue and not (event.get("rink") or "").strip():
        event["rink"] = venue
        changed = True
    stored = (event.get("dates") or "").strip()
    if dates and not stored:
        event["dates"] = _fi_date(dates)
        event["datesAuto"] = True
        changed = True
    elif stored and _ISO_DATE.fullmatch(stored):
        event["dates"] = _fi_date(stored)
        event["datesAuto"] = True
        changed = True
    return changed


def _newest(entities):
    """The entity with the newest CreatedDate (ISO strings sort correctly)."""
    return max(entities, key=lambda e: e.get("CreatedDate") or "")


def _find_bound_competition(comp_table, platform_id):
    """The visible registry entity already bound to a platform competition id
    (newest wins), or None. Soft-deleted rows keep their PlatformId but their
    blobs are gone, so `Visible` is filtered in Python — legacy rows predate the
    property entirely and must still count as visible."""
    safe_pid = platform_id.replace("'", "''")
    rows = [e for e in comp_table.query_entities(
                f"PartitionKey eq 'GLOBAL' and PlatformId eq '{safe_pid}'")
            if e.get("Visible") is not False]
    return _newest(rows) if rows else None


def _sync_competition_name(comp_table, entity, safe_name) -> str:
    """Table half of a platform rename: MERGE `Name` when the sanitized platform
    name differs from the stored one. Authoritative — the new name is reported
    only once stored; on failure the old name is returned and nothing else
    changes. FolderPath is never touched (blob paths and protocol filenames hang
    off it). Returns the name to report."""
    old_name = entity.get("Name", entity["RowKey"])
    if not safe_name or safe_name == old_name:
        return old_name
    try:
        comp_table.update_entity({
            "PartitionKey": "GLOBAL", "RowKey": entity["RowKey"], "Name": safe_name,
        }, mode=UpdateMode.MERGE)
    except Exception as e:
        logging.warning(
            f"Could not rename competition {entity['RowKey']} to '{safe_name}': {e}")
        return old_name
    entity["Name"] = safe_name
    logging.info(
        f"Renamed competition {entity['RowKey']}: '{old_name}' -> '{safe_name}'")
    return safe_name


def _sync_competition_code(comp_table, entity, code) -> None:
    """MERGE the platform's competition code (`Code`, what the protocol PDF is
    named after) when it differs from the stored one. An empty code — a client
    that sends none — never clears a stored one. Best effort: the old code
    stays on failure."""
    if not code or code == entity.get("Code"):
        return
    try:
        comp_table.update_entity({
            "PartitionKey": "GLOBAL", "RowKey": entity["RowKey"], "Code": code,
        }, mode=UpdateMode.MERGE)
    except Exception as e:
        logging.warning(f"Could not store the code of competition {entity['RowKey']}: "
                        f"{type(e).__name__}")
        return
    entity["Code"] = code


def _rename_structure(structure, old_name, new_name) -> bool:
    """Blob half of a rename: the record `name` (page heading, protocol filename)
    always follows, while `event.title` — the user-editable "Protocol title"
    printed on the PDFs — follows only while it still equals the old record name,
    i.e. nobody customised it. True when something changed."""
    if new_name == old_name:
        return False
    structure["name"] = new_name
    event = structure.setdefault("event", {})
    if event.get("title") == old_name:
        event["title"] = new_name
    return True


@app.route(route="resolve_competition", auth_level=func.AuthLevel.ANONYMOUS, methods=["POST"])
def resolve_competition(req: func.HttpRequest) -> func.HttpResponse:
    """Map the site's active platform competition to this tool's competition,
    creating it on first use:
    {platformId, name, code?, dates?, venue?} -> {id, name, created, deletionDate}.

    Needs read access to the platform competition (`_check_access`: 404
    `platform_not_found` / 503 `platform_unavailable`); a new record is an
    empty shell, so read is enough to create it. Only a record already bound to
    that PlatformId is ever returned — an unbound record is never adopted by
    name (that would let anyone claim another user's record). The rename sync
    and the event backfill below write client-supplied values into an existing
    record, so they run only for a caller with write access.

    The platform owns the deletion date: it is cached on the record and
    returned (null until the platform reports one). A platform competition that
    has been deleted is a 410 `platform_deleted` (no record is created for it).
    A *new* record needs the platform to confirm the competition: 404
    `platform_not_found` / 503 `platform_unavailable` otherwise
    (`_refuse_new_binding`). An existing bound record resolves on its cached
    date when the platform clock cannot be read or does not know it.

    The platform's date (ISO) and venue seed `event.dates` (as Finnish dd.MM.yyyy,
    flagged `datesAuto` so the schedule's full span may later replace it) and
    `event.rink`. On a writer's hit the same values only *backfill* empty
    fields, and the hit also follows a rename of the platform competition (the
    stored `Name`, the structure `name` and — while still untouched — the
    printed protocol title; never the folder path) and of its code (`Code`,
    which names the generated protocol PDF)."""
    caller = _require_user(req)
    if not caller:
        return _unauthorized()
    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse("Invalid JSON body", status_code=400)
    if not isinstance(body, dict):
        return func.HttpResponse("Invalid JSON body", status_code=400)

    raw_platform_id = (body.get('platformId') or '').strip()
    name = (body.get('name') or '').strip()
    dates = body.get('dates') or ''
    venue = (body.get('venue') or '').strip()
    code = sh.sanitize_code(body.get('code'))
    if not raw_platform_id:
        return func.HttpResponse("Missing platformId", status_code=400)
    # One canonical form (lowercase GUID) for the platform read and every local
    # lookup and store, so differently-cased ids never make a second record.
    platform_id = pc.normalize_platform_id(raw_platform_id)
    if not platform_id:
        return sh.json_response(
            {"error": "invalid_platform_id",
             "message": "platformId must be a competition GUID"}, 400)
    safe_name = sh.sanitize_name(name)
    if not safe_name:
        return func.HttpResponse("Invalid name", status_code=400)
    # Read access to the platform competition is enough: an existing record is
    # only opened, and a new one is an empty shell bound to that competition.
    role, refusal = _check_access(caller, platform_id, "read")
    if refusal:
        return refusal
    # The name, code, dates and venue come from the client: only a writer's
    # resolve may push them into an existing record (rename sync and backfill).
    can_write = pa.role_satisfies(role, "write")

    try:
        comp_table = _competitions_table()
        if not comp_table:
            return func.HttpResponse("Storage configuration invalid", status_code=500)

        platform_table, table_error = _platform_table()
        clock, unavailable = _read_clock(platform_id, platform_table, table_error)
        if pc.platform_says_delete(clock):
            return sh.json_response(
                {"error": "platform_deleted",
                 "message": "This competition has been deleted"}, 410)

        entity = _find_bound_competition(comp_table, platform_id)
        if entity:
            folder_path = entity.get("FolderPath", entity["RowKey"])
            old_name = entity.get("Name", entity["RowKey"])
            new_name = (_sync_competition_name(comp_table, entity, safe_name)
                        if can_write else old_name)
            if can_write:
                _sync_competition_code(comp_table, entity, code)
            if can_write and (new_name != old_name or dates or venue):
                try:
                    structure = sh.read_structure(folder_path)
                    if isinstance(structure, dict):
                        changed = _backfill_platform_event(structure, dates, venue)
                        changed = _rename_structure(structure, old_name, new_name) or changed
                        if changed:
                            sh.write_structure(folder_path, structure)
                except Exception as e:
                    # Backfill and rename mirror are niceties; never fail the binding over them.
                    logging.warning(f"Structure sync failed for {folder_path}: {e}")
            deletion_date = sh.sync_local_deletion_date(comp_table, entity, clock)
            return sh.json_response({
                "id": entity["RowKey"],
                "name": new_name,
                "created": False,
                "deletionDate": deletion_date,
            })

        refusal = _refuse_new_binding(clock, unavailable,
                                      configured=platform_table is not None or bool(table_error))
        if refusal:
            return refusal
        platform_date = (clock or {}).get("deletionDate")
        deletion_date = pc.to_iso_z(platform_date) if platform_date else None
        new_id = _create_competition_record(
            comp_table, caller.email, safe_name, _fi_date(dates), platform_id,
            rink=venue, dates_auto=bool(dates), deletion_date=deletion_date, code=code)
        return sh.json_response({
            "id": new_id, "name": safe_name, "created": True,
            "deletionDate": deletion_date,
        })
    except Exception as e:
        logging.error(f"Error resolving competition: {e}")
        return func.HttpResponse("Internal server error", status_code=500)


def _delete_competition_data(entity, deleted_by):
    """Delete a competition's blobs and protocol links and soft-delete its row.
    The row keeps no e-mail address: CreatedBy is blanked and DeletedBy says
    only how it went ("user" or "auto-cleanup")."""
    if deleted_by not in (sh.USER_DELETE_ACTOR, sh.AUTO_CLEANUP_ACTOR):
        deleted_by = sh.USER_DELETE_ACTOR
    comp_id = entity["RowKey"]
    folder_path = entity.get("FolderPath", comp_id)
    container = sh.get_container_client()
    if not container:
        raise RuntimeError("Storage configuration invalid")
    count = 0
    for blob in container.list_blobs(name_starts_with=f"{folder_path}/"):
        container.delete_blob(blob.name)
        count += 1
    try:
        table_client = sh.get_table_client()
        if table_client:
            safe_pk = comp_id.replace("'", "''")
            for paper in table_client.query_entities(f"PartitionKey eq '{safe_pk}'"):
                table_client.delete_entity(partition_key=paper['PartitionKey'], row_key=paper['RowKey'])
    except Exception as e:
        logging.warning(f"Error deleting generated rows: {e}")
    comp_table = sh.get_table_client("competitions")
    comp_table.update_entity({
        "PartitionKey": "GLOBAL", "RowKey": comp_id, "Visible": False,
        "DeletedDate": f"{datetime.utcnow().isoformat()}Z",
        "CreatedBy": "", "DeletedBy": deleted_by,
    }, mode=UpdateMode.MERGE)
    return count


def _scrub_hidden_rows(comp_table) -> int:
    """Blank CreatedBy/DeletedBy on soft-deleted rows that still hold an e-mail
    address (rows deleted before deletion stopped recording one). Returns the
    number of rows scrubbed."""
    scrubbed = 0
    for entity in list(comp_table.query_entities("PartitionKey eq 'GLOBAL'")):
        if entity.get("Visible") is not False:
            continue
        patch = {field: "" for field in ("CreatedBy", "DeletedBy")
                 if field in entity
                 and str(entity.get(field) or "") not in sh.NON_PERSONAL_ACTORS}
        if not patch:
            continue
        try:
            comp_table.update_entity({
                "PartitionKey": "GLOBAL", "RowKey": entity["RowKey"], **patch,
            }, mode=UpdateMode.MERGE)
            scrubbed += 1
        except Exception as e:
            logging.error(f"Could not scrub deleted competition {entity['RowKey']}: "
                          f"{type(e).__name__}")
    if scrubbed:
        logging.info(f"Scrubbed e-mail addresses from {scrubbed} deleted competition(s)")
    return scrubbed


@app.route(route="delete_competition", auth_level=func.AuthLevel.ANONYMOUS, methods=["POST"])
def delete_competition(req: func.HttpRequest) -> func.HttpResponse:
    """Delete this tool's record and data for a competition. POST only (a GET
    link must never delete anything) and owner only (an admin counts as
    owner). The id comes from the `id` query param or a JSON body `{"id"}`."""
    if req.method.upper() != "POST":
        return func.HttpResponse("Method not allowed", status_code=405,
                                 headers={"Allow": "POST"})
    caller = _require_user(req)
    if not caller:
        return _unauthorized()
    comp_id = req.params.get('id')
    if not comp_id:
        try:
            body = req.get_json()
        except ValueError:
            body = None
        if isinstance(body, dict) and isinstance(body.get('id'), str):
            comp_id = body['id']
    if not comp_id:
        return func.HttpResponse("Missing id parameter", status_code=400)
    try:
        entity, _, denied = _require_competition(caller, comp_id, "owner")
        if denied:
            return denied
        count = _delete_competition_data(entity, sh.USER_DELETE_ACTOR)
        return sh.json_response({"deleted": count})
    except Exception as e:
        logging.error(f"Error deleting competition: {e}")
        return func.HttpResponse("Internal server error", status_code=500)


# 04:00 UTC: after the platform's 03:00 sweep, so its verdicts land the same night.
@app.timer_trigger(schedule="0 0 4 * * *", arg_name="timer", run_on_startup=False)
def auto_delete_expired_competitions(timer: func.TimerRequest) -> None:
    _run_auto_delete(datetime.now(timezone.utc))


def _orphan_purge_enabled():
    """ORPHAN_PURGE_ENABLED (Bicep-owned, on by default): false = the nightly
    orphan step only logs what it would delete. Unset reads as off."""
    return (os.environ.get("ORPHAN_PURGE_ENABLED") or "").strip().lower() in ("1", "true", "yes")


def _orphan_reference(entity):
    """When an orphaned row's 60 days count from: CreatedDate, else the newest
    timestamp the row has (DeletionDate is a deadline, not a timestamp of the
    record, so it is not one of them; the Table Timestamp is)."""
    metadata = getattr(entity, "metadata", None) or {}
    return pc.record_age_reference(entity.get("CreatedDate"), metadata.get("timestamp"))


def _run_auto_delete(now, orphan_purge=None):
    """The nightly sweep. The platform decides: a bound record's data is
    deleted only on its verdict (`platform_says_delete`: the competition is
    deleted or purged) — never on a date alone. A platform that cannot be
    read or is not configured means skip (error) — never delete. Legacy rows
    with no PlatformId go on their stored date.

    Orphans: a live record with no PlatformId (or one that is not a GUID), or
    whose clock row a successful read did not find, is deleted with the same
    flow (data gone, CreatedBy blanked, the row kept as history) once it is
    more than 60 days old (`_orphan_reference`); younger ones are kept. The
    step runs only when one probe of the clock table read at least one row
    (or nothing is bound) — unreadable, unconfigured or empty aborts it for
    the run — and deletes only with ORPHAN_PURGE_ENABLED on (otherwise it
    logs what it would). Finally, deleted rows still holding an address are
    scrubbed. Returns the run's counts."""
    purge = _orphan_purge_enabled() if orphan_purge is None else orphan_purge
    stats = {"deleted": 0, "skipped": 0, "failed": 0, "orphaned": 0,
             "orphan_kept": 0, "orphan_dry_run": 0, "orphan_skipped": 0}
    try:
        comp_table = sh.get_table_client("competitions")
        if not comp_table:
            return stats
        platform_table, table_error = _platform_table()
        platform_error = table_error or (
            None if platform_table else "PLATFORM_STORAGE_ACCOUNT unset")
        live = [e for e in comp_table.query_entities("PartitionKey eq 'GLOBAL'")
                if e.get("Visible") is not False]
        bound_count = sum(1 for e in live if e.get("PlatformId"))
        if table_error:
            orphan_ok, orphan_reason = False, "unavailable"
        else:
            orphan_ok, orphan_reason = pc.orphan_sweep_allowed(platform_table, bound_count)
        if not orphan_ok:
            logging.error(f"Auto-deletion: orphan step aborted for this run (platform clock "
                          f"{orphan_reason}; {bound_count} bound record(s))")

        def handle_orphan(entity, why):
            comp_id = entity["RowKey"]
            if not orphan_ok:
                stats["orphan_skipped"] += 1
                return
            if not pc.orphan_old_enough(_orphan_reference(entity), now):
                stats["orphan_kept"] += 1
                logging.warning(f"Auto-deletion: {comp_id} is orphaned ({why}) "
                                "but not 60 days old; kept")
                return
            if not purge:
                stats["orphan_dry_run"] += 1
                logging.warning(f"Auto-deletion: dry run, would delete orphaned "
                                f"competition {comp_id} ({why})")
                return
            try:
                _delete_competition_data(entity, sh.AUTO_CLEANUP_ACTOR)
                stats["orphaned"] += 1
                logging.info(f"Auto-deleted orphaned competition {comp_id} ({why})")
            except Exception as e:
                stats["failed"] += 1
                logging.error(f"Auto-deletion failed for orphaned competition {comp_id}: {e}")

        for entity in live:
            platform_id = entity.get("PlatformId")
            if "FolderPath" not in entity:
                # A row from before FolderPath; never bound to the platform.
                handle_orphan(entity, "legacy row without FolderPath")
                continue
            if not platform_id:
                sh.ensure_deletion_date(comp_table, entity)
                due = _cached_date_passed(entity, now)
                if not due:
                    handle_orphan(entity, "no PlatformId")
                    continue
            elif pc.normalize_platform_id(platform_id) is None:
                handle_orphan(entity, "PlatformId is not a GUID")
                continue
            elif platform_error:
                stats["skipped"] += 1
                continue
            else:
                clock, unavailable = _read_clock(platform_id, platform_table)
                if unavailable:
                    stats["skipped"] += 1
                    logging.error(f"Auto-deletion skipped for {entity['RowKey']}: "
                                  "platform clock unavailable")
                    continue
                if clock is None:
                    handle_orphan(entity, "no platform clock row")
                    continue
                sh.sync_local_deletion_date(comp_table, entity, clock)
                due = pc.platform_says_delete(clock)
            if not due:
                continue
            try:
                _delete_competition_data(entity, sh.AUTO_CLEANUP_ACTOR)
                stats["deleted"] += 1
            except Exception as e:
                stats["failed"] += 1
                logging.error(f"Auto-deletion failed for {entity['RowKey']}: {e}")
        if platform_error and stats["skipped"]:
            logging.error(f"Auto-deletion skipped {stats['skipped']} platform-bound competition(s): "
                          f"platform registry unavailable ({platform_error})")
        logging.info(
            f"Auto-deletion sweep complete. Deleted {stats['deleted']}, skipped {stats['skipped']} "
            f"(platform clock unreadable), failed {stats['failed']}. Orphans: deleted "
            f"{stats['orphaned']}, kept (under 60 days) {stats['orphan_kept']}, dry run "
            f"{stats['orphan_dry_run']}, skipped (step aborted) {stats['orphan_skipped']}.")
    except Exception as e:
        logging.error(f"Auto-deletion sweep error: {e}")
    try:
        comp_table = sh.get_table_client("competitions")
        if comp_table:
            _scrub_hidden_rows(comp_table)
    except Exception as e:
        logging.error(f"Deleted-row scrub error: {type(e).__name__}")
    return stats


# ── details / event settings ──────────────────────────────────────────────────

def _generated_protocols(comp_id, folder_path):
    """The competition's generated protocols, each with a download link minted
    now — the caller has just passed the access check — valid for
    `sh.DOWNLOAD_LINK_TTL` (`expiration`). The blob is derived from the
    record's folder and the row's file name; a link stored on a legacy row is
    ignored and scrubbed. `url` is null when no link could be signed."""
    try:
        table_client = sh.get_table_client()
        if not table_client:
            return []
        safe_pk = comp_id.replace("'", "''")
        rows = list(table_client.query_entities(f"PartitionKey eq '{safe_pk}'"))
    except Exception as e:
        logging.warning(f"Could not list generated protocols: {type(e).__name__}")
        return []
    rows = [r for r in rows if r.get("FileName")]
    for row in rows:
        sh.scrub_stored_link(table_client, row)
    blobs = {r["RowKey"]: sh.protocol_blob_name(folder_path, r["FileName"]) for r in rows}
    try:
        urls, expiry = sh.mint_read_urls(sh.get_blob_service_client(), blobs.values())
    except Exception as e:
        logging.error(f"Could not sign protocol download links: {type(e).__name__}")
        urls, expiry = {}, None
    expiration = expiry.isoformat().replace("+00:00", "Z") if expiry else None
    return [{
        "fileName": os.path.basename(r["FileName"]),
        "url": urls.get(blobs[r["RowKey"]]),
        "description": r.get("Description"),
        "expiration": expiration if urls.get(blobs[r["RowKey"]]) else None,
        "size": r.get("FileSize"),
        "generatedAt": r.get("GeneratedAt") or None,
    } for r in rows]


@app.route(route="get_competition_details", auth_level=func.AuthLevel.ANONYMOUS, methods=["GET"])
def get_competition_details(req: func.HttpRequest) -> func.HttpResponse:
    caller = _require_user(req)
    if not caller:
        return _unauthorized()
    comp_id = req.params.get('id')
    if not comp_id:
        return func.HttpResponse("Missing id parameter", status_code=400)
    try:
        entity, folder_path, denied = _require_competition(caller, comp_id, "read")
        if denied:
            return denied
        structure = sh.read_structure(folder_path)
        if structure is None:
            return func.HttpResponse("Competition data missing", status_code=404)

        assigned = st.assigned_file_ids(structure)
        unassigned = [fid for fid in structure.get("files", {}) if fid not in assigned]

        generated = _generated_protocols(entity["RowKey"], folder_path)

        comp_table = sh.get_table_client("competitions")
        clock, _ = (_read_clock(entity["PlatformId"], *_platform_table())
                    if entity.get("PlatformId") else (None, False))
        deletion_date = (sh.sync_local_deletion_date(comp_table, entity, clock)
                         if comp_table else entity.get("DeletionDate"))
        return sh.json_response({
            "structure": structure,
            "unassigned": unassigned,
            "generatedFiles": generated,
            # The site UI has no competition list anymore; retention (auto-delete
            # date) is surfaced in the detail view instead: the platform's date,
            # cached on the record (null until the platform reports one).
            # Extending happens on the platform only.
            "deletionDate": deletion_date,
        })
    except Exception as e:
        logging.error(f"Error getting details: {e}")
        return func.HttpResponse("Internal server error", status_code=500)


@app.route(route="save_event_settings", auth_level=func.AuthLevel.ANONYMOUS, methods=["POST"])
def save_event_settings(req: func.HttpRequest) -> func.HttpResponse:
    caller = _require_user(req)
    if not caller:
        return _unauthorized()
    try:
        body = req.get_json()
        comp_id = body.get('id')
        event = body.get('event', {})
    except ValueError:
        return func.HttpResponse("Invalid JSON body", status_code=400)
    if not comp_id:
        return func.HttpResponse("Missing id", status_code=400)
    try:
        entity, folder_path, denied = _require_competition(caller, comp_id, "write")
        if denied:
            return denied
        structure = sh.read_structure(folder_path)
        allowed = ("title", "organization", "authorization", "city", "rink", "dates")
        for key in allowed:
            if key in event:
                structure["event"][key] = event[key]
        # The user reviewed the dates: a later schedule re-parse must not touch them.
        if "dates" in event:
            structure["event"].pop("datesAuto", None)
        sh.write_structure(folder_path, structure)
        return sh.json_response({"event": structure["event"]})
    except Exception as e:
        logging.error(f"Error saving event settings: {e}")
        return func.HttpResponse("Internal server error", status_code=500)


# ── files: upload / fetch / assign / delete ───────────────────────────────────

class UnreadableImage(ValueError):
    """An upload claiming to be an image that cannot be decoded."""


def _birth_dates_not_removed():
    """422 for a participant file whose dates of birth could not be removed
    (odf_birth_dates.BirthDateError): nothing was stored. Same code and status
    as the platform's file pool refusing the same file."""
    return sh.json_response(
        {"error": "birth_dates_not_removed",
         "message": "The birth dates in this participant file could not be removed, "
                    "so it was not stored."}, 422)


def _heif_to_jpeg(filename, body):
    """(filename, body) unchanged for anything but HEIC/HEIF; those become a
    metadata-free JPEG named `<stem>.jpg` (raises UnreadableImage)."""
    stem, ext = os.path.splitext(filename)
    if ext.lower() not in image_sanitize.HEIF_EXTS:
        return filename, body
    try:
        cleaned = image_sanitize.clean_jpeg(body, quality=HEIF_STORE_QUALITY)
    except Exception as e:
        raise UnreadableImage(type(e).__name__) from None
    if cleaned is None:
        raise UnreadableImage("PIL is not available")
    return f"{stem}.jpg", cleaned[0]


def _register_upload(structure, folder_path, filename, body, params):
    """Store an uploaded file's bytes, register it in the structure and — when
    the caller named a slot — assign it there straight away. Returns
    (file_id, meta); persisting the structure is the caller's job.

    `params` is a plain dict of the request's query params, so the browser
    upload and the competition-pool import describe a target identically:
    `slotKind` (+ `categoryId`/`segmentId`/`teamId`/`role`) and `autoAssigned`.
    A target the structure does not have (a stale category id) leaves the file
    in the tray rather than failing the upload. A HEIC/HEIF image is stored
    converted to JPEG (UnreadableImage when it does not decode). An FS Manager
    participant file — recognised by its content, as the platform does — is
    stored without its dates of birth (odf_birth_dates.BirthDateError when they
    cannot be removed, before anything is written)."""
    filename, body = _heif_to_jpeg(filename, body)
    body = odf_birth_dates.strip_upload(body)
    file_id = st.new_id("file")
    blob_path = f"{folder_path}/uploads/{file_id}_{filename}"
    container = sh.get_container_client()
    container.upload_blob(blob_path, body, overwrite=True)

    meta = {
        "filename": filename,
        "kind": _kind_for(filename),
        "size": len(body),
        "uploadedAt": f"{datetime.utcnow().isoformat()}Z",
        "blob": blob_path,
    }
    structure.setdefault("files", {})[file_id] = meta

    # Optional immediate slot assignment from query params.
    slot_kind = params.get('slotKind')
    if slot_kind:
        target = {"kind": slot_kind}
        for p in ("categoryId", "segmentId", "teamId", "role"):
            if params.get(p):
                target[p] = params.get(p)
        try:
            st.assign_file(structure, target, file_id)
            if slot_kind == "totalResults":
                cat = st.find_category(structure, target.get("categoryId"))
                if cat:
                    _fill_podium_from_results(structure, cat)
                _rematch_after_results(structure, folder_path)
            elif slot_kind == "segment" and target.get("role") == "results":
                cat = st.find_category(structure, target.get("categoryId"))
                seg = st.find_segment(cat, target.get("segmentId")) if cat else None
                _fill_segment_count_from_results(structure, seg)
            elif slot_kind == "categoryTitle":
                cat = st.find_category(structure, target.get("categoryId"))
                st.apply_title_discipline(cat, filename)
            # Tag only a placement that actually happened: a file that falls
            # back to the tray (the except below) is nothing to confirm.
            if _truthy(params.get('autoAssigned')):
                meta["autoAssigned"] = True
        except KeyError as ke:
            logging.warning(f"Upload assign failed: {ke}")

    return file_id, meta


@app.route(route="upload_file", auth_level=func.AuthLevel.ANONYMOUS, methods=["POST"])
def upload_file(req: func.HttpRequest) -> func.HttpResponse:
    caller = _require_user(req)
    if not caller:
        return _unauthorized()
    comp_id = req.params.get('competition')
    filename = req.params.get('filename')
    if not comp_id or not filename:
        return func.HttpResponse("Missing competition or filename", status_code=400)
    filename = os.path.basename(filename)
    kind = _kind_for(filename)
    if kind == "other":
        return func.HttpResponse("Unsupported file type (PDF, image or XML only)", status_code=400)

    content_length = req.headers.get('Content-Length')
    if content_length and int(content_length) > sh.MAX_UPLOAD_SIZE:
        return func.HttpResponse(f"File too large (max {sh.MAX_UPLOAD_SIZE // (1024*1024)} MB).", status_code=413)
    body = req.get_body()
    if len(body) > sh.MAX_UPLOAD_SIZE:
        return func.HttpResponse(f"File too large (max {sh.MAX_UPLOAD_SIZE // (1024*1024)} MB).", status_code=413)

    try:
        entity, folder_path, denied = _require_competition(caller, comp_id, "write")
        if denied:
            return denied
        structure = sh.read_structure(folder_path)
        file_id, meta = _register_upload(structure, folder_path, filename, body, dict(req.params))
        sh.write_structure(folder_path, structure)
        return sh.json_response({"fileId": file_id, "file": meta})
    except UnreadableImage:
        return func.HttpResponse("Could not read the image file.", status_code=400)
    except odf_birth_dates.BirthDateError as e:
        logging.warning(f"Participant file upload refused: {e}")
        return _birth_dates_not_removed()
    except Exception as e:
        logging.error(f"Error uploading file: {e}")
        return func.HttpResponse("Internal server error", status_code=500)


def _read_pool_file(entity, filename, source):
    """Read one file out of the platform's shared competition file pool.

    `(bytes, None)` on success, `(None, response)` with the route-ready error
    otherwise. The folder is derived from the competition row's bound
    `PlatformId` plus a fixed folder name, never from the client: the `source`
    query value `upload` selects the `uploads/` folder (what people uploaded)
    and `fsm` selects `fsm/` (what the HOVTP listener pushed) — note the
    singular query value against the plural folder. `filename` must already be
    a basename and `source` already validated by the caller (the two routes
    word that 400 differently). Shared by `import_platform_file` and
    `parse_schedule`."""
    platform_id = entity.get("PlatformId")
    if not platform_id:
        return None, sh.json_response(
            {"error": "not_bound",
             "message": "This competition is not linked to a platform competition"}, 409)
    try:
        container = sh.get_platform_container_client()
    except Exception as e:
        logging.error(f"Platform pool client creation failed: {e}")
        return None, sh.json_response(
            {"error": "platform_unavailable",
             "message": "Could not reach the competition file pool"}, 502)
    if container is None:
        return None, sh.json_response(
            {"error": "platform_not_configured",
             "message": "The shared competition file pool is not configured"}, 503)

    pool_path = f"{platform_id}/{'fsm' if source == 'fsm' else 'uploads'}/{filename}"
    try:
        blob = container.get_blob_client(pool_path)
        if not blob.exists():
            return None, sh.json_response(
                {"error": "pool_file_not_found",
                 "message": "File not found in the competition files"}, 404)
        body = blob.download_blob().readall()
    except ResourceNotFoundError:
        return None, sh.json_response(
            {"error": "pool_file_not_found",
             "message": "File not found in the competition files"}, 404)
    except Exception as e:
        # The pool filename (and the SDK message quoting its URL) can name a
        # person, so only the folder and the exception type are logged.
        logging.error(f"Platform pool read failed in {platform_id}/{source}: "
                      f"{type(e).__name__}")
        return None, sh.json_response(
            {"error": "platform_unavailable",
             "message": "The shared competition files are unavailable"}, 502)

    if len(body) > sh.MAX_UPLOAD_SIZE:
        return None, sh.json_response(
            {"error": "file_too_large",
             "message": f"File too large (max {sh.MAX_UPLOAD_SIZE // (1024*1024)} MB)."}, 413)
    return body, None


@app.route(route="import_platform_file", auth_level=func.AuthLevel.ANONYMOUS, methods=["POST"])
def import_platform_file(req: func.HttpRequest) -> func.HttpResponse:
    """Copy a file out of the platform's shared competition file pool into this
    competition, registering it exactly like a browser upload.

    The pool folder is derived server-side from the competition's bound
    PlatformId — the client only names a file, never a path or a GUID. The
    optional `source` query param picks which of the pool's two folders to read:
    `upload` (the default, files people uploaded) or `fsm` (files the HOVTP
    listener pushed); anything else is a 400 `invalid_source`. Errors
    carry a machine-readable code so the frontend can fall back to a direct
    upload when the feature is off or the competition is unbound."""
    caller = _require_user(req)
    if not caller:
        return _unauthorized()
    comp_id = req.params.get('competition')
    name = req.params.get('name')
    if not comp_id or not name:
        return sh.json_response(
            {"error": "missing_parameter", "message": "Missing competition or name"}, 400)
    filename = os.path.basename(name)
    if _kind_for(filename) == "other":
        return sh.json_response(
            {"error": "unsupported_type",
             "message": "Unsupported file type (PDF, image or XML only)"}, 400)
    # The pool has two folders; the client picks one by name, never by path.
    source = req.params.get('source') or 'upload'
    if source not in ('upload', 'fsm'):
        return sh.json_response(
            {"error": "invalid_source", "message": "source must be 'upload' or 'fsm'"}, 400)

    try:
        entity, folder_path, denied = _require_competition(caller, comp_id, "write")
        if denied:
            return denied
        body, err = _read_pool_file(entity, filename, source)
        if err:
            return err

        structure = sh.read_structure(folder_path)
        file_id, meta = _register_upload(structure, folder_path, filename, body, dict(req.params))
        # Which pool file this copy came from, so the UI can mark it imported.
        meta["poolName"] = filename
        sh.write_structure(folder_path, structure)
        return sh.json_response({"fileId": file_id, "file": meta})
    except UnreadableImage:
        return sh.json_response(
            {"error": "unreadable_image", "message": "Could not read the image file"}, 400)
    except odf_birth_dates.BirthDateError as e:
        logging.warning(f"Participant file import refused: {e}")
        return _birth_dates_not_removed()
    except Exception as e:
        logging.error(f"Error importing platform file: {e}")
        return sh.json_response({"error": "internal_error", "message": "Internal server error"}, 500)


@app.route(route="get_file", auth_level=func.AuthLevel.ANONYMOUS, methods=["GET"])
def get_file(req: func.HttpRequest) -> func.HttpResponse:
    """Stream an uploaded file's bytes (for hover previews and downloads)."""
    caller = _require_user(req)
    if not caller:
        return _unauthorized()
    comp_id = req.params.get('competition')
    file_id = req.params.get('fileId')
    if not comp_id or not file_id:
        return func.HttpResponse("Missing competition or fileId", status_code=400)
    try:
        entity, folder_path, denied = _require_competition(caller, comp_id, "read")
        if denied:
            return denied
        structure = sh.read_structure(folder_path)
        meta = structure.get("files", {}).get(file_id)
        if not meta or not meta.get("blob"):
            return func.HttpResponse("File not found", status_code=404)
        container = sh.get_container_client()
        blob = container.get_blob_client(meta["blob"])
        if not blob.exists():
            return func.HttpResponse("File not found", status_code=404)
        data = blob.download_blob().readall()
        mime = {
            "pdf": "application/pdf",
            "image": "image/jpeg",
            "xml": "application/xml",
        }.get(meta.get("kind"), "application/octet-stream")
        if meta.get("kind") == "image":
            name = meta.get("filename", "").lower()
            if name.endswith(".png"):
                mime = "image/png"
            elif name.endswith(".gif"):
                mime = "image/gif"
            elif name.endswith(".webp"):
                mime = "image/webp"
        return func.HttpResponse(body=data, status_code=200, mimetype=mime,
                                 headers={"Cache-Control": "private, max-age=60"})
    except Exception as e:
        logging.error(f"Error fetching file: {e}")
        return func.HttpResponse("Internal server error", status_code=500)


@app.route(route="assign_file", auth_level=func.AuthLevel.ANONYMOUS, methods=["POST"])
def assign_file(req: func.HttpRequest) -> func.HttpResponse:
    """Move a file into a slot (drag-and-drop). fileId may be null to clear."""
    caller = _require_user(req)
    if not caller:
        return _unauthorized()
    try:
        body = req.get_json()
        comp_id = body.get('id')
        file_id = body.get('fileId')   # may be None to clear the target slot
        target = body.get('target')
    except ValueError:
        return func.HttpResponse("Invalid JSON body", status_code=400)
    if not comp_id or not target:
        return func.HttpResponse("Missing id or target", status_code=400)
    try:
        entity, folder_path, denied = _require_competition(caller, comp_id, "write")
        if denied:
            return denied
        structure = sh.read_structure(folder_path)
        if file_id and file_id not in structure.get("files", {}):
            return func.HttpResponse("Unknown fileId", status_code=400)
        st.assign_file(structure, target, file_id)
        if target.get("kind") == "totalResults" and file_id:
            cat = st.find_category(structure, target.get("categoryId"))
            if cat:
                _fill_podium_from_results(structure, cat)
            _rematch_after_results(structure, folder_path)
        elif target.get("kind") == "segment" and target.get("role") == "results" and file_id:
            cat = st.find_category(structure, target.get("categoryId"))
            seg = st.find_segment(cat, target.get("segmentId")) if cat else None
            _fill_segment_count_from_results(structure, seg)
        elif target.get("kind") == "categoryTitle" and file_id:
            cat = st.find_category(structure, target.get("categoryId"))
            meta = structure.get("files", {}).get(file_id) or {}
            st.apply_title_discipline(cat, meta.get("filename"))
        if file_id:
            # Moving a file by hand *is* the confirmation an auto-placement was
            # waiting for — including a move back to the tray. A caller that is
            # itself placing files automatically says so in the body.
            meta = structure.get("files", {}).get(file_id)
            if meta is not None:
                if _truthy(body.get("autoAssigned")):
                    meta["autoAssigned"] = True
                else:
                    meta.pop("autoAssigned", None)
        sh.write_structure(folder_path, structure)
        return sh.json_response({"ok": True})
    except KeyError as ke:
        return func.HttpResponse(f"Invalid target: {ke}", status_code=400)
    except Exception as e:
        logging.error(f"Error assigning file: {e}")
        return func.HttpResponse("Internal server error", status_code=500)


@app.route(route="delete_file", auth_level=func.AuthLevel.ANONYMOUS, methods=["DELETE", "POST"])
def delete_file(req: func.HttpRequest) -> func.HttpResponse:
    caller = _require_user(req)
    if not caller:
        return _unauthorized()
    comp_id = req.params.get('competition')
    file_id = req.params.get('fileId')
    if not comp_id or not file_id:
        return func.HttpResponse("Missing competition or fileId", status_code=400)
    try:
        entity, folder_path, denied = _require_competition(caller, comp_id, "write")
        if denied:
            return denied
        structure = sh.read_structure(folder_path)
        meta = structure.get("files", {}).get(file_id)
        if not meta:
            return func.HttpResponse("File not found", status_code=404)
        container = sh.get_container_client()
        if meta.get("blob") and container.get_blob_client(meta["blob"]).exists():
            container.delete_blob(meta["blob"])
        st.clear_file(structure, file_id)
        structure["files"].pop(file_id, None)
        sh.write_structure(folder_path, structure)
        return sh.json_response({"status": "deleted"})
    except Exception as e:
        logging.error(f"Error deleting file: {e}")
        return func.HttpResponse("Internal server error", status_code=500)


# ── schedule + roster ──────────────────────────────────────────────────────────

def _apply_schedule_meta(event: dict, meta: dict) -> None:
    """Auto-fill event fields from the parsed schedule.

    The rink is fill-when-empty (the platform's venue deliberately wins). The dates
    are also overwritten when they were auto-filled (`datesAuto`, e.g. the
    platform's single start date) — the schedule knows the full span, which is the
    better guess. The flag stays set so a re-parse can refine it again; only a user
    save clears it (see `save_event_settings`)."""
    if meta.get("rink") and not (event.get("rink") or "").strip():
        event["rink"] = meta["rink"]
    if meta.get("dates") and (not (event.get("dates") or "").strip()
                              or event.get("datesAuto")):
        event["dates"] = meta["dates"]
        event["datesAuto"] = True


@app.route(route="parse_schedule", auth_level=func.AuthLevel.ANONYMOUS, methods=["POST"])
def parse_schedule(req: func.HttpRequest) -> func.HttpResponse:
    """Parse the competition schedule; build the category/segment structure.

    Two ways in, both ending in `parse_schedule_data` (DT_SCHEDULE XML or a
    schedule PDF, auto-detected):

    * the file as the **request body** — the browser's drop box;
    * a **pool reference** — `poolName` (+ optional `source=upload|fsm`,
      default `upload`) with an empty body, when the schedule is already in the
      platform's shared competition file pool. The bytes are read through
      `_read_pool_file`, i.e. exactly like `import_platform_file`: the folder
      comes from the competition's bound PlatformId (409 `not_bound` without
      one), the name is reduced to a basename, and the pool's own failures
      surface as 503/502/404/413. An unknown `source` is a plain-text 400
      `invalid_source`.

    Either way an existing category list is only rebuilt with `force=true`
    (409 otherwise), the source file is kept as `schedule.xml|pdf` (an FS Manager
    participant file only without its dates of birth, else 422
    `birth_dates_not_removed`), and the parsed rink/dates auto-fill the event.
    A pool parse echoes back which pool file it used in `source`."""
    caller = _require_user(req)
    if not caller:
        return _unauthorized()
    comp_id = req.params.get('competition')
    force = req.params.get('force') in ('1', 'true', 'yes')
    if not comp_id:
        return func.HttpResponse("Missing competition", status_code=400)
    body = req.get_body()
    pool_name = req.params.get('poolName')
    source = req.params.get('source') or 'upload'
    if not body and not pool_name:
        return func.HttpResponse(
            "Missing schedule: send the file (PDF or DT_SCHEDULE XML) as the request "
            "body, or name one in the competition file pool with poolName.",
            status_code=400)
    if not body and source not in ('upload', 'fsm'):
        return func.HttpResponse("invalid_source: source must be 'upload' or 'fsm'",
                                 status_code=400)
    try:
        entity, folder_path, denied = _require_competition(caller, comp_id, "write")
        if denied:
            return denied
        structure = sh.read_structure(folder_path)

        if structure.get("categories") and not force:
            return func.HttpResponse(
                "Competition already has categories. Re-parse with force=true to rebuild.",
                status_code=409)

        # No body = the schedule already sits in the shared competition file pool.
        pool_ref = None
        if not body:
            filename = os.path.basename(pool_name)
            body, err = _read_pool_file(entity, filename, source)
            if err:
                return err
            pool_ref = {"poolName": filename, "source": source}

        # A participant file dropped here by mistake is still never kept with
        # its dates of birth (a real schedule comes back untouched).
        try:
            body = odf_birth_dates.strip_upload(body)
        except odf_birth_dates.BirthDateError as e:
            logging.warning(f"Schedule upload refused: {e}")
            return _birth_dates_not_removed()

        # Keep the source schedule (PDF or DT_SCHEDULE XML) for re-parsing.
        is_xml = body[:2000].lstrip()[:5] == b"<?xml" or b"OdfBody" in body[:2000]
        sh.get_container_client().upload_blob(
            f"{folder_path}/schedule.{'xml' if is_xml else 'pdf'}", body, overwrite=True)

        rows, categories, meta = parse_schedule_data(body)
        structure["schedule"] = rows
        structure["categories"] = categories
        structure["scheduleParsed"] = True
        # Auto-fill event fields the schedule gives us, without clobbering user input.
        _apply_schedule_meta(structure.setdefault("event", {}), meta)
        sh.write_structure(folder_path, structure)
        result = {
            "rows": len(rows),
            "categories": len(structure["categories"]),
            "structure": structure,
        }
        if pool_ref:
            result["source"] = pool_ref
        return sh.json_response(result)
    except Exception as e:
        logging.error(f"Error parsing schedule: {e}")
        return func.HttpResponse("Internal server error", status_code=500)


def _result_rows_for_categories(structure, container, folder_path) -> dict:
    """categoryId -> the placement rows of that category's total-results PDF.

    Roster matching is results-first (see `roster_matching`), so every category
    that already has a Total Results PDF contributes the names it lists. A sheet
    that cannot be read (blob gone, unparsable PDF) simply yields no rows, which
    the matcher treats as "no results for this category yet"."""
    rows_by_cat = {}
    by_blob = {}
    files = structure.get("files", {})
    for cat in structure.get("categories", []):
        file_id = cat.get("totalResultsPdf")
        meta = files.get(file_id) if file_id else None
        if not meta or meta.get("kind") != "pdf" or not meta.get("blob"):
            continue
        path = meta["blob"]
        if path not in by_blob:
            by_blob[path] = []
            try:
                blob = container.get_blob_client(path)
                if blob.exists():
                    by_blob[path] = parse_result_rows(blob.download_blob().readall())
            except Exception as e:
                logging.warning(f"Could not read total results {path} in {folder_path}: {e}")
        if by_blob[path]:
            rows_by_cat[cat["id"]] = by_blob[path]
    return rows_by_cat


def _find_team_anywhere(structure: dict, parsed: dict):
    """`(category, team)` for a parsed DT_PARTIC_TEAMS team's current placement —
    matched on the DT_PARTIC_TEAMS code first, then on the normalised name, across
    *all* categories. `(None, None)` when the team isn't in the structure yet."""
    cats = structure.get("categories") or []
    code = (parsed.get("code") or "").strip()
    if code:
        for cat in cats:
            for team in cat.get("teams", []):
                if (team.get("code") or "").strip() == code:
                    return cat, team
    name = parsed.get("name") or ""
    if name:
        for cat in cats:
            for team in cat.get("teams", []):
                if roster_matching.same_name(team.get("name") or "", name):
                    return cat, team
    return None, None


def _upsert_team(structure: dict, category: dict, parsed: dict) -> bool:
    """Place one parsed team into `category`, wherever it currently sits.

    The lookup is competition-wide, so a re-import never duplicates a team and a
    team the previous pass put in the wrong block is *moved* — keeping its id and
    both photo slots. Roster/code/event are always refreshed; org and name only
    when the XML actually carries them. Returns True when the team moved between
    categories."""
    teams = category.setdefault("teams", [])
    old_cat, team = _find_team_anywhere(structure, parsed)
    moved = False
    if team is None:
        team = st.new_team(parsed.get("org", ""), parsed.get("name", ""))
        teams.append(team)
    elif old_cat is not category:
        old_cat["teams"] = [t for t in old_cat.get("teams", []) if t is not team]
        teams.append(team)
        moved = True
    team["code"] = parsed.get("code", "")
    team["event"] = parsed.get("event", "")
    team["members"] = parsed.get("members", [])
    if parsed.get("org"):
        team["org"] = parsed["org"]
    if parsed.get("name"):
        team["name"] = parsed["name"]
    return moved


def _apply_assignments(structure: dict, report: dict, auto: bool = False):
    """Apply a `roster_matching.match_teams` report; returns
    `(imported, moved, touched_category_ids)`.

    An `auto` pass is the background re-match triggered by a Total Results PDF: it
    may *place* a team that is in no category yet, but it may only move an already
    placed team on a tier-1 exact results hit — a manual placement is never
    overridden by a fuzzy guess."""
    imported, moved, touched = 0, 0, set()
    for assignment in report.get("assignments", []):
        category = st.find_category(structure, assignment.get("categoryId"))
        if not category:
            continue
        parsed = assignment.get("team") or {}
        if auto and assignment.get("method") != "results":
            old_cat, existing = _find_team_anywhere(structure, parsed)
            if existing is not None and old_cat is not category:
                continue
        if _upsert_team(structure, category, parsed):
            moved += 1
        imported += 1
        category["discipline"] = "synchro"
        touched.add(category["id"])
    return imported, moved, touched


def _as_bytes(xml):
    """XML as bytes, whether it came from a JSON body (str) or a blob (bytes)."""
    return xml.encode("utf-8") if isinstance(xml, str) else xml


def _roster_blob(container, folder_path: str, name: str):
    """Blob client for an archived roster XML (`rosters/teams.xml`/`partic.xml`)."""
    return container.get_blob_client(f"{folder_path}/rosters/{name}")


def _run_roster_match(structure, container, folder_path, teams_xml=None, partic_xml=None,
                      pool=None):
    """Match every registered team onto a category and apply the result in place.

    Shared by the `import_rosters` route and the automatic re-match that runs when
    a Total Results PDF arrives. Whichever XML is supplied is archived under
    `rosters/`, which is exactly what lets a later pass run without re-uploading;
    an archived copy is read only for the file this call did *not* bring. So a
    partic-only body (the supported "add the skater names to the roster I already
    imported" case) re-matches the archived TEAMS against the participants the
    user just picked, rather than against the archived ones.

    `auto` stays "no fresh TEAMS file drove this pass" — it is what stops
    `_apply_assignments` moving an already-placed team on a non-exact hit — and is
    therefore keyed on `teams_xml` alone, never on `partic_xml`.

    The structure is mutated (the caller persists it) and the report is stored in
    `structure["rosterImport"]` for the UI. Besides the outcome, `files` records
    the roster files in use — `{teams?, partic?}` → `{at}` (when it was supplied)
    plus `{poolName, source}` when it came from the competition pool (`pool`, same
    shape without `at`) — which is what the UI compares the pool against to offer
    newer files. A file this pass did not supply keeps its entry, so the automatic
    re-match after a results upload never makes an older pool file look imported.
    Returns the summary, or None when there is nothing to match (no archived
    roster, or no teams in the XML).

    Whatever XML this call supplies loses its dates of birth first — inline or
    from the pool (already clean there; stripped again all the same) — and is
    parsed and archived only in that form. odf_birth_dates.BirthDateError, raised
    before anything is written, when they cannot be removed (or the file is not
    XML at all)."""
    auto = teams_xml is None
    fresh_partic = partic_xml is not None
    if not auto:
        teams_xml = odf_birth_dates.strip_birth_dates(_as_bytes(teams_xml))
    if fresh_partic:
        partic_xml = odf_birth_dates.strip_birth_dates(_as_bytes(partic_xml))
    if auto:
        blob = _roster_blob(container, folder_path, "teams.xml")
        if not blob.exists():
            return None
        teams_xml = blob.download_blob().readall()
        if not fresh_partic:
            partic = _roster_blob(container, folder_path, "partic.xml")
            partic_xml = partic.download_blob().readall() if partic.exists() else None

    participants = parse_participants(_as_bytes(partic_xml)) if partic_xml else {}
    teams = parse_team_rosters(_as_bytes(teams_xml), participants)
    if not teams:
        return None

    # Keep the source XML files for the record (not draggable upload chips) and
    # for later automatic re-matches — each one only when this call supplied it,
    # so an archived copy is never rewritten with itself.
    if not auto:
        container.upload_blob(f"{folder_path}/rosters/teams.xml", _as_bytes(teams_xml), overwrite=True)
    if fresh_partic:
        container.upload_blob(f"{folder_path}/rosters/partic.xml", _as_bytes(partic_xml), overwrite=True)

    rows_by_cat = _result_rows_for_categories(structure, container, folder_path)
    report = roster_matching.match_teams(structure, teams, rows_by_cat)
    imported, moved, touched = _apply_assignments(structure, report, auto=auto)
    now = f"{datetime.utcnow().isoformat()}Z"
    pool = pool or {}
    previous = structure.get("rosterImport") or {}
    if "files" in previous:
        files = dict(previous["files"])
    else:
        # A report from before `files`: its `at` is when the archived roster was
        # supplied, and this pass is about to overwrite it.
        files = {}
        if previous.get("at"):
            files["teams"] = {"at": previous["at"]}
            if partic_xml is not None and not fresh_partic:
                files["partic"] = {"at": previous["at"]}
    if not auto:
        files["teams"] = {"at": now, **pool.get("teams", {})}
        # A fresh TEAMS file is never joined with the archived DT_PARTIC.
        files.pop("partic", None)
    if fresh_partic:
        files["partic"] = {"at": now, **pool.get("partic", {})}
    structure["rosterImport"] = {
        "at": now,
        "imported": imported,
        "moved": moved,
        "unmatched": report["unmatched"],
        "withdrawn": report["withdrawn"],
    }
    if files:   # a re-match of a roster imported before `files` existed has none
        structure["rosterImport"]["files"] = files
    return {"imported": imported, "moved": moved, "touched": touched, "report": report}


def _rematch_after_results(structure, folder_path):
    """Re-run roster matching after a Total Results PDF landed: its rows name the
    teams that skated that block, so teams the import could not place (or reported
    withdrawn) may now find their category. Only worth doing while something is
    still open, and never fatal — the upload itself must succeed regardless."""
    previous = structure.get("rosterImport") or {}
    if not (previous.get("unmatched") or previous.get("withdrawn")):
        return
    try:
        _run_roster_match(structure, sh.get_container_client(), folder_path)
    except Exception as e:
        logging.warning(f"Automatic roster re-match failed: {e}")


@app.route(route="import_rosters", auth_level=func.AuthLevel.ANONYMOUS, methods=["POST"])
def import_rosters(req: func.HttpRequest) -> func.HttpResponse:
    """Import synchro team rosters for the *whole competition* from one
    DT_PARTIC_TEAMS file joined with one DT_PARTIC file (XML strings in the body).

    Body: {id, teamsXml?, particXml?, teamsPool?, particPool?} — without a TEAMS
    file the roster archived by an earlier import is re-matched instead (409 when
    nothing was imported yet), against the DT_PARTIC of this call when it carries
    one (that is how skater names are added to a TEAMS-only import) and otherwise
    against the archived one.

    Either file may instead be a reference into the platform's shared competition
    file pool, `{name, source: "upload"|"fsm"}` — FS Manager pushes both over HOVTP
    — read exactly as `import_platform_file` reads (`_read_pool_file`: bound
    `PlatformId`, fixed folder, the same JSON error codes). Inline XML wins over a
    reference to the same file. A referenced file must really be the type it is
    referenced as (`dt_partic.document_type`), or the request is a 400
    `wrong_document_type`: the pool is picked by filename, and a TEAMS file taken
    for DT_PARTIC would import with bare athlete codes for names.

    One TEAMS file spans every synchro event, but teams register per *event* and
    compete per *block*: which block a team skated in shows up only in that block's
    total-results PDF. So placement is results-first (`roster_matching.match_teams`)
    — a team named on a category's result sheet goes there, otherwise its
    registered event decides when it maps to exactly one category. Anything else is
    reported (unmatched with an actionable reason, or withdrawn) instead of guessed,
    and assigning the missing Total Results PDFs re-runs the match automatically.
    """
    caller = _require_user(req)
    if not caller:
        return _unauthorized()
    try:
        body = req.get_json()
        comp_id = body.get('id')
        teams_xml = body.get('teamsXml')
        partic_xml = body.get('particXml')
        refs = {kind: body.get(f'{kind}Pool') for kind in ("teams", "partic")}
    except ValueError:
        return func.HttpResponse("Invalid JSON body", status_code=400)
    if not comp_id:
        return func.HttpResponse("Missing id", status_code=400)
    refs = {kind: ref for kind, ref in refs.items()
            if ref is not None and not (teams_xml if kind == "teams" else partic_xml)}
    for ref in refs.values():
        if not isinstance(ref, dict) or not ref.get('name'):
            return sh.json_response(
                {"error": "missing_parameter",
                 "message": "A competition file reference needs a name"}, 400)
        if (ref.get('source') or 'upload') not in ('upload', 'fsm'):
            return sh.json_response(
                {"error": "invalid_source", "message": "source must be 'upload' or 'fsm'"}, 400)
    try:
        entity, folder_path, denied = _require_competition(caller, comp_id, "write")
        if denied:
            return denied

        pool = {}
        for kind, ref in refs.items():
            filename = os.path.basename(ref['name'])
            source = ref.get('source') or 'upload'
            xml, err = _read_pool_file(entity, filename, source)
            if err:
                return err
            expected = dt_partic.TEAMS if kind == "teams" else dt_partic.PARTIC
            if dt_partic.document_type(xml) != expected:
                return sh.json_response(
                    {"error": "wrong_document_type",
                     "message": f"{filename} is not a {expected} file"}, 400)
            if kind == "teams":
                teams_xml = xml
            else:
                partic_xml = xml
            pool[kind] = {"poolName": filename, "source": source}

        structure = sh.read_structure(folder_path)
        container = sh.get_container_client()

        if not teams_xml and not _roster_blob(container, folder_path, "teams.xml").exists():
            return func.HttpResponse(
                "No team roster has been imported for this competition yet — select the "
                "DT_PARTIC_TEAMS file as well, not just DT_PARTIC.", status_code=409)

        try:
            summary = _run_roster_match(structure, container, folder_path,
                                        teams_xml=teams_xml or None,
                                        partic_xml=partic_xml or None, pool=pool)
        except odf_birth_dates.BirthDateError as e:
            logging.warning(f"Roster import refused: {e}")
            return _birth_dates_not_removed()
        if summary is None:
            return func.HttpResponse(
                "No teams found in the DT_PARTIC_TEAMS file — check that it is the team "
                "export of this competition.", status_code=422)

        sh.write_structure(folder_path, structure)
        return sh.json_response({
            "imported": summary["imported"],
            "moved": summary["moved"],
            "categories": len(summary["touched"]),
            "report": summary["report"],
            "source": pool,
            "structure": structure,
        })
    except Exception as e:
        logging.error(f"Error importing rosters: {e}")
        return func.HttpResponse("Internal server error", status_code=500)


def _drop_file(structure, container, file_id):
    """Delete an uploaded file completely: its blob, every slot referencing it and
    its registry entry (the same trio as delete_file)."""
    meta = structure.get("files", {}).get(file_id)
    if not meta:
        return
    try:
        if meta.get("blob") and container.get_blob_client(meta["blob"]).exists():
            container.delete_blob(meta["blob"])
    except Exception as e:
        logging.warning(f"Could not delete replaced fallback blob: {e}")
    st.clear_file(structure, file_id)
    structure["files"].pop(file_id, None)


@app.route(route="upload_fallback_photos", auth_level=func.AuthLevel.ANONYMOUS, methods=["POST"])
def upload_fallback_photos(req: func.HttpRequest) -> func.HttpResponse:
    """Import one ZIP of accreditation photos as *fallback* team pictures.

    Body: the raw ZIP. Folders loosely name categories, files are
    `Team-Name_Club-Name.jpeg` (see fallback_photos). Every picture is re-encoded
    and registered as a normal uploaded image; those whose team could be
    identified are assigned to that team's `teamPhotoFallback` slot (replacing —
    and deleting — any previous fallback so no orphans accumulate), the rest stay
    unassigned in the Uploads tray and are reported back.
    """
    caller = _require_user(req)
    if not caller:
        return _unauthorized()
    comp_id = req.params.get('competition')
    if not comp_id:
        return func.HttpResponse("Missing competition", status_code=400)

    content_length = req.headers.get('Content-Length')
    if content_length and int(content_length) > sh.MAX_ZIP_UPLOAD_SIZE:
        return func.HttpResponse(f"File too large (max {sh.MAX_ZIP_UPLOAD_SIZE // (1024*1024)} MB).", status_code=413)
    body = req.get_body()
    if len(body) > sh.MAX_ZIP_UPLOAD_SIZE:
        return func.HttpResponse(f"File too large (max {sh.MAX_ZIP_UPLOAD_SIZE // (1024*1024)} MB).", status_code=413)
    if not body:
        return func.HttpResponse("Missing ZIP body", status_code=400)

    try:
        entity, folder_path, denied = _require_competition(caller, comp_id, "write")
        if denied:
            return denied
        structure = sh.read_structure(folder_path)

        try:
            images, rejected = fallback_photos.parse_zip(body)
        except zipfile.BadZipFile:
            return func.HttpResponse("Not a valid ZIP file", status_code=400)
        if not images:
            return func.HttpResponse("No usable pictures in the ZIP", status_code=422)

        container = sh.get_container_client()
        matched = 0
        unmatched_files = []
        for img in images:
            category, team = fallback_photos.match_team(
                structure, img["team_norm"], img["club_norm"],
                fallback_photos.normalize(img["folder"] or ""))

            # Register the picture either way, so an unmatched file is still
            # draggable from the tray.
            file_id = st.new_id("file")
            filename = f"{os.path.splitext(img['filename'])[0]}.jpg"   # re-encoded
            blob_path = f"{folder_path}/uploads/{file_id}_{filename}"
            container.upload_blob(blob_path, img["jpeg"], overwrite=True)
            structure.setdefault("files", {})[file_id] = {
                "filename": filename,
                "kind": "image",
                "size": len(img["jpeg"]),
                "uploadedAt": f"{datetime.utcnow().isoformat()}Z",
                "blob": blob_path,
            }

            if not team:
                unmatched_files.append(img["filename"])
                continue
            # Replace, don't orphan: a re-uploaded ZIP shouldn't grow the registry.
            previous = team.get("photoFallback")
            if previous and previous in structure.get("files", {}):
                _drop_file(structure, container, previous)
            st.assign_file(structure, {"kind": "teamPhotoFallback",
                                       "categoryId": category["id"],
                                       "teamId": team["id"]}, file_id)
            matched += 1

        sh.write_structure(folder_path, structure)
        return sh.json_response({
            "matched": matched,
            "unmatchedFiles": unmatched_files,
            "rejected": rejected,
            "structure": structure,
        })
    except Exception as e:
        logging.error(f"Error importing fallback photos: {e}")
        return func.HttpResponse("Internal server error", status_code=500)


# ── structure editing (manual add/remove/update) ──────────────────────────────

@app.route(route="edit_structure", auth_level=func.AuthLevel.ANONYMOUS, methods=["POST"])
def edit_structure(req: func.HttpRequest) -> func.HttpResponse:
    """Manual structure edits via a small command set so the tool is usable and
    correctable even before/without schedule parsing.

    Body: {id, op, ...}. Ops:
      add_category {name, discipline}
      remove_category {categoryId}
      set_category {categoryId, name?, discipline?, order?, code?,
                    pageEnabled?, nameMode?}
      add_segment {categoryId, name}
      remove_segment {categoryId, segmentId}
      set_segment {categoryId, segmentId, name?, order?, unitCount?}
      add_team {categoryId, org?, name?}
      remove_team {categoryId, teamId}
      set_team {categoryId, teamId, org?, name?, members?,
                pageEnabled?, nameMode?, textFields?}
      set_podium {categoryId, names:[..]}
      set_page_mode {slot:'cover'|'lastPage', mode:'default'}
      set_footer_enabled {enabled}
      set_team_pages {enabled?, nameMode?}
    """
    caller = _require_user(req)
    if not caller:
        return _unauthorized()
    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse("Invalid JSON body", status_code=400)
    comp_id = body.get('id')
    op = body.get('op')
    if not comp_id or not op:
        return func.HttpResponse("Missing id or op", status_code=400)
    try:
        entity, folder_path, denied = _require_competition(caller, comp_id, "write")
        if denied:
            return denied
        structure = sh.read_structure(folder_path)
        cats = structure.setdefault("categories", [])

        def cat():
            c = st.find_category(structure, body.get("categoryId"))
            if not c:
                raise KeyError("category")
            return c

        if op == "add_category":
            order = len(cats)
            cats.append(st.new_category(body.get("name", "New Category"),
                                        body.get("discipline", "single"), order))
        elif op == "remove_category":
            structure["categories"] = [c for c in cats if c["id"] != body.get("categoryId")]
        elif op == "set_category":
            c = cat()
            for k in ("name", "discipline", "order"):
                if k in body:
                    c[k] = body[k]
            if "code" in body:
                c["code"] = str(body["code"] or "").strip()[:32]
            # The category's team-page defaults, tri-state exactly like a team's:
            # an explicit null (or an unknown name mode) returns the category to
            # the competition-wide default.
            if "pageEnabled" in body:
                v = body["pageEnabled"]
                c["pageEnabled"] = None if v is None else bool(v)
            if "nameMode" in body:
                c["nameMode"] = st.coerce_name_mode(body["nameMode"])
        elif op == "add_segment":
            c = cat()
            c["segments"].append(st.new_segment(body.get("name", "Segment"), len(c["segments"])))
        elif op == "remove_segment":
            c = cat()
            c["segments"] = [s for s in c["segments"] if s["id"] != body.get("segmentId")]
        elif op == "set_segment":
            c = cat()
            s = st.find_segment(c, body.get("segmentId"))
            if not s:
                raise KeyError("segment")
            for k in ("name", "order"):
                if k in body:
                    s[k] = body[k]
            if "unitCount" in body:
                s["unitCount"] = _coerce_count(body["unitCount"])
        elif op == "add_team":
            c = cat()
            c.setdefault("teams", []).append(st.new_team(body.get("org", ""), body.get("name", "")))
        elif op == "remove_team":
            c = cat()
            c["teams"] = [t for t in c.get("teams", []) if t["id"] != body.get("teamId")]
        elif op == "set_team":
            c = cat()
            t = st.find_team(c, body.get("teamId"))
            if not t:
                raise KeyError("team")
            for k in ("org", "name", "members"):
                if k in body:
                    t[k] = body[k]
            # Team-page overrides are tri-state: an explicit null (or an unknown
            # name mode) returns the team to its category's default.
            if "pageEnabled" in body:
                v = body["pageEnabled"]
                t["pageEnabled"] = None if v is None else bool(v)
            if "nameMode" in body:
                t["nameMode"] = st.coerce_name_mode(body["nameMode"])
            if "textFields" in body:
                t["textFields"] = st.sanitize_text_fields(body["textFields"])
        elif op == "set_podium":
            c = cat()
            names = (list(body.get("names", [])) + ["", "", ""])[:3]
            c.setdefault("podium", {"photo": None, "names": ["", "", ""]})["names"] = names
        elif op == "set_page_mode":
            slot = body.get("slot")
            if slot in ("coverPage", "lastPage", "cover", "last"):
                key = "coverPage" if slot in ("cover", "coverPage") else "lastPage"
                structure[key] = {"mode": "default", "fileId": None}
        elif op == "set_footer_enabled":
            structure["footerEnabled"] = bool(body.get("enabled", True))
        elif op == "set_team_pages":
            # Competition-wide team-page defaults. No inherit state here, so an
            # unknown name mode falls back to "full" rather than to None.
            tp = structure.setdefault("teamPages", {"enabled": True, "nameMode": "full"})
            if "enabled" in body:
                tp["enabled"] = bool(body.get("enabled", True))
            if "nameMode" in body:
                tp["nameMode"] = st.coerce_name_mode(body["nameMode"]) or "full"
        else:
            return func.HttpResponse(f"Unknown op: {op}", status_code=400)

        sh.write_structure(folder_path, structure)
        return sh.json_response({"structure": structure})
    except KeyError as ke:
        return func.HttpResponse(f"Not found: {ke}", status_code=404)
    except Exception as e:
        logging.error(f"Error editing structure: {e}")
        return func.HttpResponse("Internal server error", status_code=500)


# ── generate ───────────────────────────────────────────────────────────────────

@app.route(route="generate_protocol", auth_level=func.AuthLevel.ANONYMOUS, methods=["POST"])
def generate_protocol(req: func.HttpRequest) -> func.HttpResponse:
    caller = _require_user(req)
    if not caller:
        return _unauthorized()
    try:
        body = req.get_json()
        comp_id = body.get('id')
    except ValueError:
        return func.HttpResponse("Invalid JSON body", status_code=400)
    if not comp_id:
        return func.HttpResponse("Missing id", status_code=400)
    try:
        entity, folder_path, denied = _require_competition(caller, comp_id, "write")
        if denied:
            return denied
        structure = sh.read_structure(folder_path)

        get_bytes = _file_bytes_getter(folder_path, structure)
        pdf_bytes = assemble_protocol(structure, get_bytes)

        # One fixed name per competition, FS Manager style: regenerating
        # replaces the previous PDF, and when it was made is a separate field.
        out_name = sh.protocol_file_name(entity.get("Code"), structure.get("name"))
        generated_at = pc.to_iso_z(datetime.now(timezone.utc))
        blob_name = sh.protocol_blob_name(folder_path, out_name)

        blob_service_client = sh.get_blob_service_client()
        container = blob_service_client.get_container_client(sh.CONTAINER_NAME)
        container.upload_blob(blob_name, pdf_bytes, overwrite=True)

        sh.record_generated_protocol(comp_id, out_name, len(pdf_bytes), generated_at)
        return sh.json_response({"fileName": out_name, "size": len(pdf_bytes),
                                 "generatedAt": generated_at})
    except SanitizeError as e:
        # Fail closed: an uploaded PDF holds content that could not be proven
        # free of hidden metadata, so nothing is published.
        logging.warning(f"Protocol not generated, uncleanable content: {e}")
        return func.HttpResponse(
            "An uploaded PDF contains an image that could not be checked for hidden "
            "metadata. Re-export or re-save that PDF and try again.", status_code=422)
    except Exception as e:
        # Type + stack frames only: the exception message can quote page text
        # (skater/official names) and the protocol's logs must not hold names.
        frames = "".join(traceback.format_tb(e.__traceback__))
        logging.error(f"Error generating protocol: {type(e).__name__}\n{frames}")
        return func.HttpResponse("Error generating protocol. Check server logs.", status_code=500)


@app.route(route="delete_protocol", auth_level=func.AuthLevel.ANONYMOUS, methods=["DELETE", "POST"])
def delete_protocol(req: func.HttpRequest) -> func.HttpResponse:
    """Delete a generated protocol PDF: its blob and its generatedprotocols row.
    Identified by competition id + fileName (the download-link list's fileName)."""
    caller = _require_user(req)
    if not caller:
        return _unauthorized()
    comp_id = req.params.get('competition')
    file_name = req.params.get('fileName')
    if not comp_id or not file_name:
        return func.HttpResponse("Missing competition or fileName", status_code=400)
    file_name = os.path.basename(file_name)  # guard against path traversal
    try:
        entity, folder_path, denied = _require_competition(caller, comp_id, "write")
        if denied:
            return denied

        blob_name = f"{folder_path}/protocols/{file_name}"
        container = sh.get_container_client()
        if container and container.get_blob_client(blob_name).exists():
            container.delete_blob(blob_name)

        try:
            table_client = sh.get_table_client()
            if table_client:
                row_key = file_name.replace('/', '_').replace('\\', '_')
                table_client.delete_entity(partition_key=comp_id, row_key=row_key)
        except ResourceNotFoundError:
            pass
        except Exception as e:
            logging.warning(f"Could not delete protocol table row: {e}")

        return sh.json_response({"status": "deleted"})
    except Exception as e:
        logging.error(f"Error deleting protocol: {e}")
        return func.HttpResponse("Internal server error", status_code=500)
