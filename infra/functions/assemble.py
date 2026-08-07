"""
Protocol assembler.

Walks the competition structure document and emits one merged PDF in the
canonical ISU-protocol order:

  cover (custom or default)
  event-info page
  time-schedule page
  for each category (schedule order):
      [synchro] one team-presentation page per team
      category title PDF
      podium page (photo + names) — when a photo or any name is present
      total category results PDF
      for each segment: results PDF -> panel PDF -> judges-details PDF
  last page (custom or default)

Uploaded PDFs are inserted as-is; photos and missing graphics are rendered by
generate_pages. `get_file_bytes(file_id)` resolves an uploaded file's bytes.
"""
import io
import logging

from pypdf import PdfReader, PdfWriter

import branding
import generate_pages
import results_parser
from structure import sorted_categories, sorted_segments


def _append_pdf_bytes(writer: PdfWriter, pdf_bytes: bytes) -> bool:
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        for page in reader.pages:
            writer.add_page(page)
        return True
    except Exception as e:
        logging.warning(f"Failed to append a PDF: {e}")
        return False


def _file_kind(structure: dict, file_id: str) -> str:
    return (structure.get("files", {}).get(file_id) or {}).get("kind", "")


def _append_file(writer: PdfWriter, structure: dict, file_id, get_file_bytes) -> bool:
    """Append an uploaded file (PDF inserted as-is, image wrapped to a page)."""
    if not file_id:
        return False
    data = get_file_bytes(file_id)
    if not data:
        logging.warning(f"File {file_id} referenced by a slot is missing")
        return False
    kind = _file_kind(structure, file_id)
    if kind == "image":
        return _append_pdf_bytes(writer, generate_pages.image_fullpage(data))
    # default: treat as PDF
    return _append_pdf_bytes(writer, data)


def _photo_bytes(structure: dict, file_id, get_file_bytes):
    if not file_id:
        return None
    if _file_kind(structure, file_id) != "image":
        return None
    return get_file_bytes(file_id)


def _chrome_band(structure: dict, key: str, get_file_bytes):
    """Resolve the bytes of a competition header/footer graphic, if a custom one
    is assigned (image only — drawn as an edge-to-edge band)."""
    ref = structure.get(key) or {}
    if ref.get("mode") == "custom" and ref.get("fileId"):
        return _photo_bytes(structure, ref.get("fileId"), get_file_bytes)
    return None


def _result_pdf_bytes(structure: dict, file_id, get_file_bytes):
    """Bytes of a result PDF slot (skips images and missing files)."""
    if not file_id or _file_kind(structure, file_id) == "image":
        return None
    return get_file_bytes(file_id)


def _segment_count(structure: dict, segment: dict, get_file_bytes):
    """Competition units in a segment: the stored (auto-filled, user-correctable)
    `unitCount` when set, else a live count of the segment's results PDF."""
    n = segment.get("unitCount")
    if isinstance(n, int) and n >= 0:
        return n
    data = _result_pdf_bytes(structure, segment.get("resultsPdf"), get_file_bytes)
    return results_parser.count_result_rows(data) if data else 0


def _competition_stats(structure: dict, get_file_bytes) -> dict:
    """Tally competition-wide counts for the information page: number of
    categories, competition units (skaters/pairs/teams) and performances (one per
    unit per segment).

    Per-segment unit counts are the source of truth (each stored on the segment,
    auto-filled from its results PDF and user-correctable). Performances are their
    sum; a category's units come from its first segment (which always holds the
    full field — later segments hold a subset). When a category has no segment
    counts the units fall back to its total-results sheet or the synchro team
    count, and performances to the unit count."""
    cats = sorted_categories(structure)
    units = performances = 0
    for cat in cats:
        seg_counts = [n for n in (_segment_count(structure, s, get_file_bytes)
                                  for s in sorted_segments(cat)) if n > 0]
        cat_perfs = sum(seg_counts)
        cat_units = seg_counts[0] if seg_counts else 0

        if cat_units == 0:
            total = _result_pdf_bytes(structure, cat.get("totalResultsPdf"), get_file_bytes)
            if total:
                cat_units = results_parser.count_result_rows(total)
            elif cat.get("discipline") == "synchro":
                cat_units = len(cat.get("teams") or [])
        if cat_perfs == 0:
            cat_perfs = cat_units

        units += cat_units
        performances += cat_perfs
    return {"categories": len(cats), "units": units, "performances": performances}


def _append_branded_cover(writer: PdfWriter, structure: dict, event: dict):
    """The approved brand cover (reportlab, dynamic text); falls back to the plain
    cover only if the brand fonts/assets are unavailable."""
    name = event.get("title") or structure.get("name", "")
    org = event.get("organization", "")
    organizer = f"Organized by {org}" if org else ""
    location = event.get("city") or event.get("rink") or ""
    try:
        if branding.fonts_available():
            _append_pdf_bytes(writer, branding.cover_page(
                name=name, dates=event.get("dates", ""),
                location=location, organizer=organizer))
            return
    except Exception as e:
        logging.warning(f"Branded cover failed, using plain cover: {e}")
    _append_pdf_bytes(writer, generate_pages.default_cover_page(name, event.get("dates", "")))


def _append_branded_last_page(writer: PdfWriter):
    """The approved brand last page (pre-rendered PDF); falls back to the plain
    last page only if the bundled asset is missing."""
    try:
        if _append_pdf_bytes(writer, branding.last_page_pdf()):
            return
    except Exception as e:
        logging.warning(f"Branded last page failed, using plain last page: {e}")
    _append_pdf_bytes(writer, generate_pages.default_last_page())


def assemble_protocol(structure: dict, get_file_bytes) -> bytes:
    """Build the merged protocol PDF and return its bytes."""
    writer = PdfWriter()
    event = structure.get("event", {}) or {}

    # Competition-wide page chrome (custom band image or the approved brand band),
    # stamped on every generated interior page. The header also prints the
    # competition name + dates·location; the footer can be toggled off.
    chrome = {
        "header": _chrome_band(structure, "header", get_file_bytes),
        "footer": _chrome_band(structure, "footer", get_file_bytes),
        "footer_enabled": structure.get("footerEnabled", True),
        "name": event.get("title") or structure.get("name", ""),
        "dates": event.get("dates", ""),
        "location": event.get("city") or event.get("rink") or "",
    }

    # 1. Cover page (custom upload, else the branded cover)
    cover = structure.get("coverPage", {}) or {}
    if cover.get("mode") == "custom" and cover.get("fileId"):
        if not _append_file(writer, structure, cover["fileId"], get_file_bytes):
            _append_branded_cover(writer, structure, event)
    else:
        _append_branded_cover(writer, structure, event)

    # 2. Event info page (with competition-wide counts read from the result PDFs)
    stats = _competition_stats(structure, get_file_bytes)
    _append_pdf_bytes(writer, generate_pages.event_info_page(event, chrome, stats))

    # 3. Time schedule page
    _append_pdf_bytes(writer, generate_pages.time_schedule_page(structure.get("schedule") or [], chrome))

    # 4. Categories (schedule order)
    for category in sorted_categories(structure):
        cat_name = category.get("name", "")

        # Synchronized skating: a team-presentation page per team, first.
        if category.get("discipline") == "synchro":
            for team in category.get("teams", []):
                # Competition photo first, then the accreditation fallback picture
                # (imported from the optional ZIP); neither → placeholder box.
                photo = (_photo_bytes(structure, team.get("photo"), get_file_bytes)
                         or _photo_bytes(structure, team.get("photoFallback"), get_file_bytes))
                _append_pdf_bytes(writer, generate_pages.synchro_team_page(team, photo, chrome))

        # Category protocol head page PDF
        _append_file(writer, structure, category.get("titlePdf"), get_file_bytes)

        # Podium page (only when there's a photo or at least one name)
        podium = category.get("podium", {}) or {}
        names = podium.get("names", []) or []
        photo = _photo_bytes(structure, podium.get("photo"), get_file_bytes)
        if photo or any((n or "").strip() for n in names):
            _append_pdf_bytes(writer, generate_pages.podium_page(cat_name, photo, names, chrome))

        # Total category results
        _append_file(writer, structure, category.get("totalResultsPdf"), get_file_bytes)

        # Per segment: results -> panel -> judges details
        for segment in sorted_segments(category):
            _append_file(writer, structure, segment.get("resultsPdf"), get_file_bytes)
            _append_file(writer, structure, segment.get("panelPdf"), get_file_bytes)
            _append_file(writer, structure, segment.get("judgesDetailsPdf"), get_file_bytes)

    # 5. Last page (custom upload, else the branded last page)
    last = structure.get("lastPage", {}) or {}
    if last.get("mode") == "custom" and last.get("fileId"):
        if not _append_file(writer, structure, last["fileId"], get_file_bytes):
            _append_branded_last_page(writer)
    else:
        _append_branded_last_page(writer)

    out = io.BytesIO()
    writer.write(out)
    out.seek(0)
    return out.getvalue()
