"""
Schedule-PDF parser for the Protocol Generator.

Calibrated against the Finnish "COMPETITION SCHEDULE" export. Each data row is:

    <start HH:MM:SS><finish HH:MM:SS>  <entries>  <Category>   <Segment>   <resurf> <perf…>

Notes from real exports:
  * the clock-time separator depends on the exporting computer's region settings —
    it can be a colon (15:00:00) or a dot (15.00.00); both are accepted;
  * start and finish times are often concatenated with no space
    (15:00:0016:21:00 / 15.00.0016.21.00);
  * Category and Segment sit in separate columns separated by 2+ spaces;
  * Category may contain spaces/commas ("Tähtijuniorit, Naiset", "Taitajat ei axel");
  * the same category can appear on two days (Short Program + Free Skating) and is
    merged into one category with two segments;
  * the discipline often isn't in the category name — a synchro competition only
    says so in the document title ("MUODOSTELMALUISTELUN…"), so we detect a
    competition-level synchro default and apply it to ambiguous names.
"""
import io
import re
import logging
import xml.etree.ElementTree as ET
from datetime import datetime
from pypdf import PdfReader

from structure import new_category, new_segment, discipline_signal

# A data row: start finish? entries  <category+segment...>  [trailing HH:MM:SS times]
# Clock times may use ':' or '.' as the separator (Finnish exports write "15.00.00").
_ROW = re.compile(
    r'^\s*(\d{1,2}[:.]\d{2}[:.]\d{2})'    # start time
    r'\s*(\d{1,2}[:.]\d{2}[:.]\d{2})?'    # finish time (optional, may be glued on)
    r'\s+(\d+)\s+'                         # number of entries
    r'(.+?)'                              # category + segment (lazy)
    r'(?:\s+\d{1,2}[:.]\d{2}[:.]\d{2}\b.*)?$'  # trailing resurfacing/performance times
)
_DATE_LINE = re.compile(r'^\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s*$')

# Known segment names (used as a fallback when columns aren't 2-space separated).
_SEGMENTS = (
    "Short Program", "Free Skating", "Free Program", "Rhythm Dance", "Free Dance",
    "Pattern Dance", "Short Dance", "Lyhytohjelma", "Vapaaohjelma",
)

_NON_COMPETITION = (
    "meeting", "practice", "draw", "dinner", "ceremony", "opening", "banquet",
    "kokous", "harjoitus", "arvonta", "palkinto",
)


def _norm_time(t: str) -> str:
    parts = re.split(r'[:.]', t)
    return f"{parts[0].zfill(2)}:{parts[1] if len(parts) > 1 else '00'}"


def _split_cat_segment(blob: str):
    """Split the 'Category   Segment' column blob into (category, segment)."""
    parts = re.split(r'\s{2,}', blob.strip())
    if len(parts) >= 2:
        return " ".join(parts[:-1]).strip(), parts[-1].strip()
    # Fallback: peel a known segment off the end.
    text = blob.strip()
    for seg in _SEGMENTS:
        if text.lower().endswith(seg.lower()):
            return text[:-len(seg)].strip(), text[-len(seg):].strip()
    return text, ""


def _read_text(pdf_path_or_stream) -> str:
    reader = PdfReader(pdf_path_or_stream)
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text(extraction_mode="layout") or "")
        except Exception:
            pages.append(page.extract_text() or "")
    return "\n".join(pages)


def _rows_from_text(text: str):
    rows = []
    current_date = current_display = None
    for line in text.split('\n'):
        if not line.strip():
            continue
        date_match = _DATE_LINE.match(line)
        if date_match:
            day, month, year = date_match.groups()
            current_date = f"{year}{month.zfill(2)}{day.zfill(2)}"
            current_display = f"{day}.{month}.{year}"
            continue
        m = _ROW.match(line)
        if not m:
            continue
        start_raw, finish_raw, count, blob = m.groups()
        category, segment = _split_cat_segment(blob)
        if not category and not segment:
            continue
        rows.append({
            "date": current_date,
            "date_display": current_display,
            "start_time": _norm_time(start_raw),
            "end_time": _norm_time(finish_raw) if finish_raw else "",
            "entries": int(count),
            "event_name": f"{category} {segment}".strip(),
            "category_name": category,
            "segment_name": segment or "Results",
        })
    return rows


def parse_schedule_entries(pdf_path_or_stream):
    """Parse a schedule PDF into a flat list of row dicts (for the time-schedule
    page). See module docstring for the row shape."""
    try:
        return _rows_from_text(_read_text(pdf_path_or_stream))
    except Exception as e:
        logging.error(f"Error reading schedule PDF: {e}")
        return []


def build_categories_from_schedule(rows, default_discipline=None):
    """Group parsed rows into ordered category dicts (structure.py shape). Rows are
    grouped by category column; ordering follows earliest (date, start_time).
    Ambiguous categories fall back to `default_discipline` (e.g. a synchro
    competition), then to 'single'."""
    grouped = {}
    for row in rows:
        cat_name = (row.get("category_name") or "").strip()
        seg_name = (row.get("segment_name") or "").strip()
        if not cat_name:
            continue
        if any(word in row.get("event_name", "").lower() for word in _NON_COMPETITION):
            continue
        order_key = (row.get("date") or "", row.get("start_time") or "")
        bucket = grouped.setdefault(cat_name, {"order_key": order_key, "segments": {}})
        if order_key < bucket["order_key"]:
            bucket["order_key"] = order_key
        segs = bucket["segments"]
        if seg_name not in segs or order_key < segs[seg_name]:
            segs[seg_name] = order_key

    categories = []
    for c_index, (cat_name, info) in enumerate(
            sorted(grouped.items(), key=lambda kv: kv[1]["order_key"])):
        discipline = discipline_signal(cat_name) or default_discipline or "single"
        category = new_category(cat_name, discipline, c_index)
        for s_index, (seg_name, _key) in enumerate(
                sorted(info["segments"].items(), key=lambda kv: kv[1])):
            category["segments"].append(new_segment(seg_name, s_index))
        categories.append(category)
    return categories


def parse_schedule(pdf_path_or_stream):
    """High-level PDF parse: returns (rows, categories). Detects a synchro
    competition from the document text and applies it as the default discipline."""
    try:
        text = _read_text(pdf_path_or_stream)
    except Exception as e:
        logging.error(f"Error reading schedule PDF: {e}")
        return [], []
    rows = _rows_from_text(text)
    default = "synchro" if re.search(r'muodostelma|synchron', text, re.I) else None
    return rows, build_categories_from_schedule(rows, default)


# ── DT_SCHEDULE XML (ISU OdfBody) — the preferred, structured input ──────────────

def _local(tag: str) -> str:
    return tag.split('}')[-1] if isinstance(tag, str) else ""


def _cat_seg_from_item(item: str):
    """Split an ItemName like 'Junior Synchronized Skating Free Skating' into
    ('Junior Synchronized Skating', 'Free Skating')."""
    text = (item or "").strip()
    for seg in sorted(_SEGMENTS, key=len, reverse=True):
        if text.lower().endswith(seg.lower()):
            return text[:-len(seg)].strip(), seg
    return text, ""


_PHASE_SEGMENT = {"QUAL": "Short Program", "FNL": "Free Skating",
                  "SP": "Short Program", "FS": "Free Skating",
                  "RD": "Rhythm Dance", "FD": "Free Dance"}


def _phase_segment(code: str) -> str:
    token = (code[22:26] if len(code) >= 26 else "").strip("-")
    return _PHASE_SEGMENT.get(token, "")


def _roman(n: int) -> str:
    return {2: "II", 3: "III", 4: "IV", 5: "V"}.get(n, str(n))


def parse_schedule_xml(xml_bytes):
    """Parse a DT_SCHEDULE OdfBody XML. Returns (rows, categories, meta) where
    meta carries auto-fill hints (rink, dates). Units are grouped into categories
    by the category portion of their ISU Unit Code (so e.g. Advanced Novice L1 and
    L2 stay distinct while a category's Short Program + Free Skating merge)."""
    try:
        root = ET.fromstring(xml_bytes)
    except Exception as e:
        logging.error(f"Error parsing DT_SCHEDULE XML: {e}")
        return [], [], {}

    rows = []
    groups = {}          # category-code -> bucket
    date_set = set()
    rink = ""

    for u in root.iter():
        if _local(u.tag) != "Unit":
            continue
        code = u.get("Code") or ""
        item = ""
        venue_name = ""
        for ch in u:
            if _local(ch.tag) == "ItemName" and ch.get("Value"):
                item = ch.get("Value")
            elif _local(ch.tag) == "VenueDescription" and ch.get("VenueName"):
                venue_name = ch.get("VenueName")

        try:
            dt = datetime.fromisoformat(u.get("StartDate") or "")
            ymd, disp, hhmm = dt.strftime("%Y%m%d"), dt.strftime("%d.%m.%Y"), dt.strftime("%H:%M")
        except Exception:
            ymd = disp = hhmm = ""

        cat_name, seg_name = _cat_seg_from_item(item)
        if not seg_name:
            seg_name = _phase_segment(code)
        if not cat_name:
            cat_name = item

        rows.append({
            "date": ymd, "date_display": disp, "start_time": hhmm, "end_time": "",
            "entries": 0, "event_name": item or f"{cat_name} {seg_name}".strip(),
            "category_name": cat_name, "segment_name": seg_name or "Results",
        })
        if disp:
            date_set.add((ymd, disp))
        if venue_name and not rink:
            rink = venue_name

        key = code[:22] if len(code) >= 22 else (cat_name or code)
        order_key = (ymd, hhmm)
        bucket = groups.setdefault(key, {
            "order_key": order_key, "name": cat_name,
            "discipline": discipline_signal(item) or "single",
            "code": code[:22] if len(code) >= 22 else "", "segments": {},
        })
        if order_key < bucket["order_key"]:
            bucket["order_key"] = order_key
        segs = bucket["segments"]
        if seg_name not in segs or order_key < segs[seg_name]:
            segs[seg_name] = order_key

    rows.sort(key=lambda r: (r["date"], r["start_time"]))

    categories = []
    name_counts = {}
    for i, (_key, bucket) in enumerate(sorted(groups.items(), key=lambda kv: kv[1]["order_key"])):
        name = bucket["name"] or "Category"
        name_counts[name] = name_counts.get(name, 0) + 1
        disp_name = name if name_counts[name] == 1 else f"{name} ({_roman(name_counts[name])})"
        category = new_category(disp_name, bucket["discipline"], i)
        category["code"] = bucket["code"]
        for j, (seg, _k) in enumerate(sorted(bucket["segments"].items(), key=lambda kv: kv[1])):
            category["segments"].append(new_segment(seg, j))
        categories.append(category)

    meta = {"rink": rink}
    if date_set:
        ds = sorted(date_set)
        meta["dates"] = ds[0][1] if ds[0][1] == ds[-1][1] else f"{ds[0][1]} – {ds[-1][1]}"
    return rows, categories, meta


def parse_schedule_data(data: bytes):
    """Format-detecting entry point. Accepts DT_SCHEDULE XML (preferred) or a
    schedule PDF. Returns (rows, categories, meta)."""
    head = data[:2000].lstrip()
    if head[:5] == b"<?xml" or b"DT_SCHEDULE" in head or b"OdfBody" in head:
        return parse_schedule_xml(data)
    rows, categories = parse_schedule(io.BytesIO(data))
    return rows, categories, _meta_from_rows(rows)


def _meta_from_rows(rows):
    """Derive auto-fill hints (currently just the date range) from parsed PDF rows."""
    dates = sorted({(r["date"], r["date_display"]) for r in rows
                    if r.get("date") and r.get("date_display")})
    if not dates:
        return {}
    span = dates[0][1] if dates[0][1] == dates[-1][1] else f"{dates[0][1]} – {dates[-1][1]}"
    return {"dates": span}
