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

import generate_pages
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


def assemble_protocol(structure: dict, get_file_bytes) -> bytes:
    """Build the merged protocol PDF and return its bytes."""
    writer = PdfWriter()
    event = structure.get("event", {}) or {}

    # 1. Cover page
    cover = structure.get("coverPage", {}) or {}
    if cover.get("mode") == "custom" and cover.get("fileId"):
        if not _append_file(writer, structure, cover["fileId"], get_file_bytes):
            _append_pdf_bytes(writer, generate_pages.default_cover_page(
                event.get("title", structure.get("name", "")), event.get("dates", "")))
    else:
        _append_pdf_bytes(writer, generate_pages.default_cover_page(
            event.get("title", structure.get("name", "")), event.get("dates", "")))

    # 2. Event info page
    _append_pdf_bytes(writer, generate_pages.event_info_page(event))

    # 3. Time schedule page
    _append_pdf_bytes(writer, generate_pages.time_schedule_page(structure.get("schedule") or []))

    # 4. Categories (schedule order)
    for category in sorted_categories(structure):
        cat_name = category.get("name", "")

        # Synchronized skating: a team-presentation page per team, first.
        if category.get("discipline") == "synchro":
            for team in category.get("teams", []):
                photo = _photo_bytes(structure, team.get("photo"), get_file_bytes)
                _append_pdf_bytes(writer, generate_pages.synchro_team_page(team, photo))

        # Category title PDF
        _append_file(writer, structure, category.get("titlePdf"), get_file_bytes)

        # Podium page (only when there's a photo or at least one name)
        podium = category.get("podium", {}) or {}
        names = podium.get("names", []) or []
        photo = _photo_bytes(structure, podium.get("photo"), get_file_bytes)
        if photo or any((n or "").strip() for n in names):
            _append_pdf_bytes(writer, generate_pages.podium_page(cat_name, photo, names))

        # Total category results
        _append_file(writer, structure, category.get("totalResultsPdf"), get_file_bytes)

        # Per segment: results -> panel -> judges details
        for segment in sorted_segments(category):
            _append_file(writer, structure, segment.get("resultsPdf"), get_file_bytes)
            _append_file(writer, structure, segment.get("panelPdf"), get_file_bytes)
            _append_file(writer, structure, segment.get("judgesDetailsPdf"), get_file_bytes)

    # 5. Last page
    last = structure.get("lastPage", {}) or {}
    if last.get("mode") == "custom" and last.get("fileId"):
        if not _append_file(writer, structure, last["fileId"], get_file_bytes):
            _append_pdf_bytes(writer, generate_pages.default_last_page())
    else:
        _append_pdf_bytes(writer, generate_pages.default_last_page())

    out = io.BytesIO()
    writer.write(out)
    out.seek(0)
    return out.getvalue()
