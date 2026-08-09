import azure.functions as func
import logging
import os
import io
import json
import re
import unicodedata
import zipfile
from datetime import date, datetime, timedelta, timezone

from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import UpdateMode

import storage_helpers as sh
import structure as st
from schedule_parser import parse_schedule_data
from dt_partic import parse_participants, parse_team_rosters
import fallback_photos
import roster_matching
from results_parser import parse_top_three, count_result_rows, parse_result_rows
from assemble import assemble_protocol

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")


# ── small helpers ─────────────────────────────────────────────────────────────

def _require_user(req):
    return sh.get_user_email_from_header(req)


def _resolve(comp_id):
    """Return (entity, folder_path) for a competition id, or (None, None)."""
    entity = sh.get_competition_entity(comp_id)
    if not entity:
        return None, None
    return entity, entity.get("FolderPath", entity["RowKey"])


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

@app.route(route="check_user_permission", auth_level=func.AuthLevel.ANONYMOUS)
def check_user_permission(req: func.HttpRequest) -> func.HttpResponse:
    email = _require_user(req)
    if not email:
        return sh.json_response({"allowed": False, "email": None}, 401)
    return sh.json_response({"allowed": True, "email": email})


@app.route(route="list_competitions", auth_level=func.AuthLevel.ANONYMOUS)
def list_competitions(req: func.HttpRequest) -> func.HttpResponse:
    if not _require_user(req):
        return func.HttpResponse("Unauthorized", status_code=401)
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


def _create_competition_record(comp_table, email, safe_name, dates, platform_id=None,
                               rink="", dates_auto=False):
    """Seed a new competition — unique id, metadata.json structure document and
    registry entity — and return its id. Shared by `create_competition` (the
    legacy standalone flow) and `resolve_competition` (platform binding, which
    also seeds the venue and flags the dates as auto-filled)."""
    new_id = sh.generate_competition_id(comp_table)
    folder_path = f"{safe_name}-{new_id}"
    now = datetime.utcnow()
    created_date = f"{now.isoformat()}Z"

    structure = st.new_structure(new_id, safe_name, dates, email, created_date)
    if rink:
        structure["event"]["rink"] = rink
    if dates and dates_auto:
        structure["event"]["datesAuto"] = True
    if platform_id:
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
        "DeletionDate": f"{(now + timedelta(days=sh.DELETION_RETENTION_DAYS)).isoformat()}Z",
    }
    if platform_id:
        entity["PlatformId"] = platform_id
    comp_table.create_entity(entity)
    return new_id


@app.route(route="create_competition", auth_level=func.AuthLevel.ANONYMOUS, methods=["GET", "POST"])
def create_competition(req: func.HttpRequest) -> func.HttpResponse:
    email = _require_user(req)
    if not email:
        return func.HttpResponse("Unauthorized", status_code=401)

    name = req.params.get('name')
    dates = req.params.get('dates', '')
    if not name:
        return func.HttpResponse("Missing name parameter", status_code=400)
    safe_name = sh.sanitize_name(name)
    if not safe_name:
        return func.HttpResponse("Invalid name", status_code=400)

    try:
        comp_table = _competitions_table()
        if not comp_table:
            return func.HttpResponse("Storage configuration invalid", status_code=500)
        new_id = _create_competition_record(comp_table, email, safe_name, dates)
        return sh.json_response({"id": new_id, "name": safe_name})
    except Exception as e:
        logging.error(f"Error creating competition: {e}")
        return func.HttpResponse("Internal server error", status_code=500)


# ── platform binding (the site's shared competition selector) ──────────────────

def _normalized_name(value) -> str:
    """Casefolded, diacritic-folded, alnum-only form of a competition name,
    mirroring the site's `normalizeCompetitionCode` (NFD + mark stripping), used
    to adopt pre-binding records — "Kevät Cup" and "Kevat Cup" must collapse."""
    decomposed = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(c for c in decomposed
                   if c.isalnum() and not unicodedata.combining(c)).casefold()


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


def _adopt_competition_by_name(comp_table, platform_id, name):
    """Bind a competition created before platform binding existed: a visible,
    unbound entity whose normalized name matches the platform one gets stamped
    with PlatformId (merge) and returned. Newest wins; None when nothing fits."""
    target = _normalized_name(name)
    if not target:
        return None
    candidates = [e for e in comp_table.query_entities("PartitionKey eq 'GLOBAL'")
                  if e.get("Visible") is not False and not e.get("PlatformId")
                  and _normalized_name(e.get("Name")) == target]
    if not candidates:
        return None
    entity = _newest(candidates)
    comp_table.update_entity({
        "PartitionKey": "GLOBAL", "RowKey": entity["RowKey"], "PlatformId": platform_id,
    }, mode=UpdateMode.MERGE)
    entity["PlatformId"] = platform_id
    return entity


@app.route(route="resolve_competition", auth_level=func.AuthLevel.ANONYMOUS, methods=["POST"])
def resolve_competition(req: func.HttpRequest) -> func.HttpResponse:
    """Map the site's active platform competition to this tool's competition,
    creating it on first use:
    {platformId, name, dates?, venue?} -> {id, name, created}.

    The platform's date (ISO) and venue seed `event.dates` (as Finnish dd.MM.yyyy,
    flagged `datesAuto` so the schedule's full span may later replace it) and
    `event.rink`. On a hit or an adoption the same values only *backfill* empty
    fields."""
    email = _require_user(req)
    if not email:
        return func.HttpResponse("Unauthorized", status_code=401)
    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse("Invalid JSON body", status_code=400)
    if not isinstance(body, dict):
        return func.HttpResponse("Invalid JSON body", status_code=400)

    platform_id = (body.get('platformId') or '').strip()
    name = (body.get('name') or '').strip()
    dates = body.get('dates') or ''
    venue = (body.get('venue') or '').strip()
    if not platform_id:
        return func.HttpResponse("Missing platformId", status_code=400)
    safe_name = sh.sanitize_name(name)
    if not safe_name:
        return func.HttpResponse("Invalid name", status_code=400)

    try:
        comp_table = _competitions_table()
        if not comp_table:
            return func.HttpResponse("Storage configuration invalid", status_code=500)

        entity = (_find_bound_competition(comp_table, platform_id)
                  or _adopt_competition_by_name(comp_table, platform_id, name))
        if entity:
            if dates or venue:
                folder_path = entity.get("FolderPath", entity["RowKey"])
                try:
                    structure = sh.read_structure(folder_path)
                    if structure and _backfill_platform_event(structure, dates, venue):
                        sh.write_structure(folder_path, structure)
                except Exception as e:
                    # Backfill is a nicety; never fail the binding over it.
                    logging.warning(f"Platform event backfill failed for {folder_path}: {e}")
            return sh.json_response({
                "id": entity["RowKey"],
                "name": entity.get("Name", entity["RowKey"]),
                "created": False,
            })

        new_id = _create_competition_record(
            comp_table, email, safe_name, _fi_date(dates), platform_id,
            rink=venue, dates_auto=bool(dates))
        return sh.json_response({"id": new_id, "name": safe_name, "created": True})
    except Exception as e:
        logging.error(f"Error resolving competition: {e}")
        return func.HttpResponse("Internal server error", status_code=500)


def _delete_competition_data(entity, deleted_by):
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
        "DeletedDate": f"{datetime.utcnow().isoformat()}Z", "DeletedBy": deleted_by,
    }, mode=UpdateMode.MERGE)
    return count


@app.route(route="delete_competition", auth_level=func.AuthLevel.ANONYMOUS, methods=["GET", "POST"])
def delete_competition(req: func.HttpRequest) -> func.HttpResponse:
    email = _require_user(req)
    if not email:
        return func.HttpResponse("Unauthorized", status_code=401)
    comp_id = req.params.get('id')
    if not comp_id:
        return func.HttpResponse("Missing id parameter", status_code=400)
    try:
        entity = sh.get_competition_entity(comp_id)
        if not entity:
            return func.HttpResponse("Competition not found", status_code=404)
        count = _delete_competition_data(entity, email)
        return sh.json_response({"deleted": count})
    except Exception as e:
        logging.error(f"Error deleting competition: {e}")
        return func.HttpResponse("Internal server error", status_code=500)


@app.route(route="extend_competition_deletion", auth_level=func.AuthLevel.ANONYMOUS, methods=["POST", "GET"])
def extend_competition_deletion(req: func.HttpRequest) -> func.HttpResponse:
    email = _require_user(req)
    if not email:
        return func.HttpResponse("Unauthorized", status_code=401)
    comp_id = req.params.get('id')
    if not comp_id:
        return func.HttpResponse("Missing id parameter", status_code=400)
    try:
        comp_table = sh.get_table_client("competitions")
        entity = sh.get_competition_entity(comp_id)
        if not entity or entity.get("Visible") is False:
            return func.HttpResponse("Competition not found", status_code=404)
        sh.ensure_deletion_date(comp_table, entity)
        now = datetime.now(timezone.utc)
        current = sh.parse_iso_utc(entity.get("DeletionDate")) or now
        new_deletion = max(current, now) + timedelta(days=sh.DELETION_EXTENSION_DAYS)
        new_str = f"{new_deletion.replace(tzinfo=None).isoformat()}Z"
        comp_table.update_entity({
            "PartitionKey": "GLOBAL", "RowKey": comp_id, "DeletionDate": new_str,
        }, mode=UpdateMode.MERGE)
        return sh.json_response({"deletionDate": new_str})
    except Exception as e:
        logging.error(f"Error extending deletion date: {e}")
        return func.HttpResponse("Internal server error", status_code=500)


@app.timer_trigger(schedule="0 0 3 * * *", arg_name="timer", run_on_startup=False)
def auto_delete_expired_competitions(timer: func.TimerRequest) -> None:
    try:
        comp_table = sh.get_table_client("competitions")
        if not comp_table:
            return
        now = datetime.now(timezone.utc)
        for entity in list(comp_table.query_entities("PartitionKey eq 'GLOBAL'")):
            if entity.get("Visible") is False or "FolderPath" not in entity:
                continue
            sh.ensure_deletion_date(comp_table, entity)
            deletion = sh.parse_iso_utc(entity.get("DeletionDate"))
            if deletion is None or deletion > now:
                continue
            try:
                _delete_competition_data(entity, sh.AUTO_CLEANUP_ACTOR)
            except Exception as e:
                logging.error(f"Auto-deletion failed for {entity['RowKey']}: {e}")
    except Exception as e:
        logging.error(f"Auto-deletion sweep error: {e}")


# ── details / event settings ──────────────────────────────────────────────────

@app.route(route="get_competition_details", auth_level=func.AuthLevel.ANONYMOUS)
def get_competition_details(req: func.HttpRequest) -> func.HttpResponse:
    if not _require_user(req):
        return func.HttpResponse("Unauthorized", status_code=401)
    comp_id = req.params.get('id')
    if not comp_id:
        return func.HttpResponse("Missing id parameter", status_code=400)
    try:
        entity, folder_path = _resolve(comp_id)
        if not entity:
            return func.HttpResponse("Competition not found", status_code=404)
        structure = sh.read_structure(folder_path)
        if structure is None:
            return func.HttpResponse("Competition data missing", status_code=404)

        assigned = st.assigned_file_ids(structure)
        unassigned = [fid for fid in structure.get("files", {}) if fid not in assigned]

        # Generated protocol download links
        generated = []
        try:
            table_client = sh.get_table_client()
            if table_client:
                safe_pk = comp_id.replace("'", "''")
                for ent in table_client.query_entities(f"PartitionKey eq '{safe_pk}'"):
                    generated.append({
                        "fileName": ent.get("FileName"),
                        "url": ent.get("Url"),
                        "description": ent.get("Description"),
                        "expiration": ent.get("ExpirationDate"),
                        "size": ent.get("FileSize"),
                    })
        except Exception as e:
            logging.warning(f"Could not fetch generated links: {e}")

        return sh.json_response({
            "structure": structure,
            "unassigned": unassigned,
            "generatedFiles": generated,
            # The site UI has no competition list anymore; retention (auto-delete
            # date + extend) is surfaced in the detail view instead.
            "deletionDate": entity.get("DeletionDate"),
        })
    except Exception as e:
        logging.error(f"Error getting details: {e}")
        return func.HttpResponse("Internal server error", status_code=500)


@app.route(route="save_event_settings", auth_level=func.AuthLevel.ANONYMOUS, methods=["POST"])
def save_event_settings(req: func.HttpRequest) -> func.HttpResponse:
    if not _require_user(req):
        return func.HttpResponse("Unauthorized", status_code=401)
    try:
        body = req.get_json()
        comp_id = body.get('id')
        event = body.get('event', {})
    except ValueError:
        return func.HttpResponse("Invalid JSON body", status_code=400)
    if not comp_id:
        return func.HttpResponse("Missing id", status_code=400)
    try:
        entity, folder_path = _resolve(comp_id)
        if not entity:
            return func.HttpResponse("Competition not found", status_code=404)
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

def _register_upload(structure, folder_path, filename, body, params):
    """Store an uploaded file's bytes, register it in the structure and — when
    the caller named a slot — assign it there straight away. Returns
    (file_id, meta); persisting the structure is the caller's job.

    `params` is a plain dict of the request's query params, so the browser
    upload and the competition-pool import describe a target identically:
    `slotKind` (+ `categoryId`/`segmentId`/`teamId`/`role`) and `autoAssigned`.
    A target the structure does not have (a stale category id) leaves the file
    in the tray rather than failing the upload."""
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
    if not _require_user(req):
        return func.HttpResponse("Unauthorized", status_code=401)
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
        entity, folder_path = _resolve(comp_id)
        if not entity:
            return func.HttpResponse("Competition not found", status_code=404)
        structure = sh.read_structure(folder_path)
        file_id, meta = _register_upload(structure, folder_path, filename, body, dict(req.params))
        sh.write_structure(folder_path, structure)
        return sh.json_response({"fileId": file_id, "file": meta})
    except Exception as e:
        logging.error(f"Error uploading file: {e}")
        return func.HttpResponse("Internal server error", status_code=500)


@app.route(route="import_platform_file", auth_level=func.AuthLevel.ANONYMOUS, methods=["POST"])
def import_platform_file(req: func.HttpRequest) -> func.HttpResponse:
    """Copy a file out of the platform's shared competition file pool into this
    competition, registering it exactly like a browser upload.

    The pool folder is derived server-side from the competition's bound
    PlatformId — the client only names a file, never a path or a GUID. Errors
    carry a machine-readable code so the frontend can fall back to a direct
    upload when the feature is off or the competition is unbound."""
    if not _require_user(req):
        return func.HttpResponse("Unauthorized", status_code=401)
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

    try:
        entity, folder_path = _resolve(comp_id)
        if not entity:
            return sh.json_response(
                {"error": "competition_not_found", "message": "Competition not found"}, 404)
        platform_id = entity.get("PlatformId")
        if not platform_id:
            return sh.json_response(
                {"error": "not_bound",
                 "message": "This competition is not linked to a platform competition"}, 409)
        container = sh.get_platform_container_client()
        if container is None:
            return sh.json_response(
                {"error": "platform_not_configured",
                 "message": "The shared competition file pool is not configured"}, 503)

        pool_path = f"{platform_id}/uploads/{filename}"
        try:
            blob = container.get_blob_client(pool_path)
            if not blob.exists():
                return sh.json_response(
                    {"error": "pool_file_not_found",
                     "message": "File not found in the competition files"}, 404)
            body = blob.download_blob().readall()
        except ResourceNotFoundError:
            return sh.json_response(
                {"error": "pool_file_not_found",
                 "message": "File not found in the competition files"}, 404)
        except Exception as e:
            logging.error(f"Platform pool read failed for {pool_path}: {e}")
            return sh.json_response(
                {"error": "platform_unavailable",
                 "message": "The shared competition files are unavailable"}, 502)

        if len(body) > sh.MAX_UPLOAD_SIZE:
            return sh.json_response(
                {"error": "file_too_large",
                 "message": f"File too large (max {sh.MAX_UPLOAD_SIZE // (1024*1024)} MB)."}, 413)

        structure = sh.read_structure(folder_path)
        file_id, meta = _register_upload(structure, folder_path, filename, body, dict(req.params))
        # Which pool file this copy came from, so the UI can mark it imported.
        meta["poolName"] = filename
        sh.write_structure(folder_path, structure)
        return sh.json_response({"fileId": file_id, "file": meta})
    except Exception as e:
        logging.error(f"Error importing platform file: {e}")
        return sh.json_response({"error": "internal_error", "message": "Internal server error"}, 500)


@app.route(route="get_file", auth_level=func.AuthLevel.ANONYMOUS, methods=["GET"])
def get_file(req: func.HttpRequest) -> func.HttpResponse:
    """Stream an uploaded file's bytes (for hover previews and downloads)."""
    if not _require_user(req):
        return func.HttpResponse("Unauthorized", status_code=401)
    comp_id = req.params.get('competition')
    file_id = req.params.get('fileId')
    if not comp_id or not file_id:
        return func.HttpResponse("Missing competition or fileId", status_code=400)
    try:
        entity, folder_path = _resolve(comp_id)
        if not entity:
            return func.HttpResponse("Competition not found", status_code=404)
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
    if not _require_user(req):
        return func.HttpResponse("Unauthorized", status_code=401)
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
        entity, folder_path = _resolve(comp_id)
        if not entity:
            return func.HttpResponse("Competition not found", status_code=404)
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
    if not _require_user(req):
        return func.HttpResponse("Unauthorized", status_code=401)
    comp_id = req.params.get('competition')
    file_id = req.params.get('fileId')
    if not comp_id or not file_id:
        return func.HttpResponse("Missing competition or fileId", status_code=400)
    try:
        entity, folder_path = _resolve(comp_id)
        if not entity:
            return func.HttpResponse("Competition not found", status_code=404)
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
    """Upload + parse the schedule PDF; build the category/segment structure."""
    if not _require_user(req):
        return func.HttpResponse("Unauthorized", status_code=401)
    comp_id = req.params.get('competition')
    force = req.params.get('force') in ('1', 'true', 'yes')
    if not comp_id:
        return func.HttpResponse("Missing competition", status_code=400)
    body = req.get_body()
    if not body:
        return func.HttpResponse("Missing schedule PDF body", status_code=400)
    try:
        entity, folder_path = _resolve(comp_id)
        if not entity:
            return func.HttpResponse("Competition not found", status_code=404)
        structure = sh.read_structure(folder_path)

        if structure.get("categories") and not force:
            return func.HttpResponse(
                "Competition already has categories. Re-parse with force=true to rebuild.",
                status_code=409)

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
        return sh.json_response({
            "rows": len(rows),
            "categories": len(structure["categories"]),
            "structure": structure,
        })
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


def _run_roster_match(structure, container, folder_path, teams_xml=None, partic_xml=None):
    """Match every registered team onto a category and apply the result in place.

    Shared by the `import_rosters` route and the automatic re-match that runs when
    a Total Results PDF arrives. A fresh `teams_xml` (+ optional `partic_xml`) is
    archived under `rosters/`, which is exactly what lets the re-match run later
    without re-uploading: without `teams_xml` the archived copy is used and the
    pass is treated as automatic (see `_apply_assignments`).

    The structure is mutated (the caller persists it) and the report is stored in
    `structure["rosterImport"]` for the UI. Returns the summary, or None when
    there is nothing to match (no archived roster, or no teams in the XML)."""
    auto = teams_xml is None
    if auto:
        blob = _roster_blob(container, folder_path, "teams.xml")
        if not blob.exists():
            return None
        teams_xml = blob.download_blob().readall()
        partic = _roster_blob(container, folder_path, "partic.xml")
        partic_xml = partic.download_blob().readall() if partic.exists() else None

    participants = parse_participants(_as_bytes(partic_xml)) if partic_xml else {}
    teams = parse_team_rosters(_as_bytes(teams_xml), participants)
    if not teams:
        return None

    if not auto:
        # Keep the source XML files for the record (not draggable upload chips)
        # and for later automatic re-matches.
        container.upload_blob(f"{folder_path}/rosters/teams.xml", _as_bytes(teams_xml), overwrite=True)
        if partic_xml:
            container.upload_blob(f"{folder_path}/rosters/partic.xml", _as_bytes(partic_xml), overwrite=True)

    rows_by_cat = _result_rows_for_categories(structure, container, folder_path)
    report = roster_matching.match_teams(structure, teams, rows_by_cat)
    imported, moved, touched = _apply_assignments(structure, report, auto=auto)
    structure["rosterImport"] = {
        "at": f"{datetime.utcnow().isoformat()}Z",
        "imported": imported,
        "moved": moved,
        "unmatched": report["unmatched"],
        "withdrawn": report["withdrawn"],
    }
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

    Body: {id, teamsXml?, particXml?} — without `teamsXml` the roster archived by
    an earlier import is re-matched instead (409 when nothing was imported yet).

    One TEAMS file spans every synchro event, but teams register per *event* and
    compete per *block*: which block a team skated in shows up only in that block's
    total-results PDF. So placement is results-first (`roster_matching.match_teams`)
    — a team named on a category's result sheet goes there, otherwise its
    registered event decides when it maps to exactly one category. Anything else is
    reported (unmatched with an actionable reason, or withdrawn) instead of guessed,
    and assigning the missing Total Results PDFs re-runs the match automatically.
    """
    if not _require_user(req):
        return func.HttpResponse("Unauthorized", status_code=401)
    try:
        body = req.get_json()
        comp_id = body.get('id')
        teams_xml = body.get('teamsXml')
        partic_xml = body.get('particXml')
    except ValueError:
        return func.HttpResponse("Invalid JSON body", status_code=400)
    if not comp_id:
        return func.HttpResponse("Missing id", status_code=400)
    try:
        entity, folder_path = _resolve(comp_id)
        if not entity:
            return func.HttpResponse("Competition not found", status_code=404)
        structure = sh.read_structure(folder_path)
        container = sh.get_container_client()

        if not teams_xml and not _roster_blob(container, folder_path, "teams.xml").exists():
            return func.HttpResponse("No roster files imported yet", status_code=409)

        summary = _run_roster_match(structure, container, folder_path,
                                    teams_xml=teams_xml or None, partic_xml=partic_xml or None)
        if summary is None:
            return func.HttpResponse("No teams found in DT_PARTIC_TEAMS", status_code=422)

        sh.write_structure(folder_path, structure)
        return sh.json_response({
            "imported": summary["imported"],
            "moved": summary["moved"],
            "categories": len(summary["touched"]),
            "report": summary["report"],
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
    if not _require_user(req):
        return func.HttpResponse("Unauthorized", status_code=401)
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
        entity, folder_path = _resolve(comp_id)
        if not entity:
            return func.HttpResponse("Competition not found", status_code=404)
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
      set_category {categoryId, name?, discipline?, order?}
      add_segment {categoryId, name}
      remove_segment {categoryId, segmentId}
      set_segment {categoryId, segmentId, name?, order?, unitCount?}
      add_team {categoryId, org?, name?}
      remove_team {categoryId, teamId}
      set_team {categoryId, teamId, org?, name?, members?}
      set_podium {categoryId, names:[..]}
      set_page_mode {slot:'cover'|'lastPage', mode:'default'}
    """
    if not _require_user(req):
        return func.HttpResponse("Unauthorized", status_code=401)
    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse("Invalid JSON body", status_code=400)
    comp_id = body.get('id')
    op = body.get('op')
    if not comp_id or not op:
        return func.HttpResponse("Missing id or op", status_code=400)
    try:
        entity, folder_path = _resolve(comp_id)
        if not entity:
            return func.HttpResponse("Competition not found", status_code=404)
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
    email = _require_user(req)
    if not email:
        return func.HttpResponse("Unauthorized", status_code=401)
    try:
        body = req.get_json()
        comp_id = body.get('id')
    except ValueError:
        return func.HttpResponse("Invalid JSON body", status_code=400)
    if not comp_id:
        return func.HttpResponse("Missing id", status_code=400)
    try:
        entity, folder_path = _resolve(comp_id)
        if not entity:
            return func.HttpResponse("Competition not found", status_code=404)
        structure = sh.read_structure(folder_path)

        get_bytes = _file_bytes_getter(folder_path, structure)
        pdf_bytes = assemble_protocol(structure, get_bytes)

        safe_name = sh.sanitize_name(structure.get("name", "protocol")).replace(" ", "_") or "protocol"
        stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        out_name = f"protocol_{safe_name}_{stamp}.pdf"
        blob_name = f"{folder_path}/protocols/{out_name}"

        blob_service_client = sh.get_blob_service_client()
        container = blob_service_client.get_container_client(sh.CONTAINER_NAME)
        container.upload_blob(blob_name, pdf_bytes, overwrite=True)

        sh.create_and_store_sas_link(blob_service_client, blob_name, comp_id, out_name, len(pdf_bytes))
        return sh.json_response({"fileName": out_name, "size": len(pdf_bytes)})
    except Exception as e:
        logging.error(f"Error generating protocol: {e}", exc_info=True)
        return func.HttpResponse("Error generating protocol. Check server logs.", status_code=500)


@app.route(route="delete_protocol", auth_level=func.AuthLevel.ANONYMOUS, methods=["DELETE", "POST"])
def delete_protocol(req: func.HttpRequest) -> func.HttpResponse:
    """Delete a generated protocol PDF: its blob and its generatedprotocols row.
    Identified by competition id + fileName (the download-link list's fileName)."""
    if not _require_user(req):
        return func.HttpResponse("Unauthorized", status_code=401)
    comp_id = req.params.get('competition')
    file_name = req.params.get('fileName')
    if not comp_id or not file_name:
        return func.HttpResponse("Missing competition or fileName", status_code=400)
    file_name = os.path.basename(file_name)  # guard against path traversal
    try:
        entity, folder_path = _resolve(comp_id)
        if not entity:
            return func.HttpResponse("Competition not found", status_code=404)

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
