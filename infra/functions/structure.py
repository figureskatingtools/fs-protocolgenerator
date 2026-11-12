"""
The competition *structure* document (stored as metadata.json in each blob folder).

It is the single source of truth for everything the protocol assembler needs:
event details, the cover/last-page choices, a registry of uploaded files, and the
ordered categories with their slots. Uploaded files are referenced by `fileId`;
each file lives in at most one slot. Files not referenced by any slot are
"unassigned" (the UI shows them in a tray to be dragged into place).
"""
import re
from uuid import uuid4

DISCIPLINES = ("single", "pair", "dance", "synchro")

# Per-segment slot roles -> structure keys.
ROLE_KEYS = {
    "results": "resultsPdf",
    "panel": "panelPdf",
    "judgesDetails": "judgesDetailsPdf",
}

# How much of a synchro roster the team page prints. "none" keeps the page (photo,
# team name, free-text rows) but drops the skater list entirely.
NAME_MODES = ("full", "firstNames", "none")

# Free-text rows a team page may carry ("Theme: Spies"). The cap is exactly what a
# team page can print, so nothing the user types is stored and then silently
# dropped: `generate_pages._text_block` starts its block at `TEXT_GAP` (8 mm) and
# spends `TEXT_ROW_H` (5.5 mm) per row inside a `TEXT_MAX_H` (45 mm) budget, which
# admits 6 rows (8 + 6 x 5.5 = 41 mm) and breaks before the 7th. Change either side
# and change the other — tests/test_synchro_team_page.py pins them together.
MAX_TEAM_TEXT_FIELDS = 6
TEXT_LABEL_MAX = 60
TEXT_VALUE_MAX = 120


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:6]}"


def new_structure(comp_id: str, name: str, dates: str, created_by: str, created_date: str) -> dict:
    return {
        "id": comp_id,
        "name": name,
        "createdBy": created_by,
        "createdDate": created_date,
        "event": {
            "title": name,
            "organization": "",
            "authorization": "",
            "city": "",
            "rink": "",
            "dates": dates or "",
        },
        "coverPage": {"mode": "default", "fileId": None},
        "lastPage": {"mode": "default", "fileId": None},
        # Optional competition-wide page chrome, stamped on every generated page.
        # When no custom graphic is uploaded a generic placeholder band is drawn.
        "header": {"mode": "default", "fileId": None},
        "footer": {"mode": "default", "fileId": None},
        "footerEnabled": True,   # draw the footer band on every page (toggleable)
        # Synchro team-presentation pages: whether they are produced at all and how
        # skater names are printed. Each team may override both (see new_team).
        "teamPages": {"enabled": True, "nameMode": "full"},
        "scheduleParsed": False,
        # fileId -> {"filename", "kind" (pdf|image|xml), "size", "uploadedAt"}
        "files": {},
        "categories": [],
    }


def new_category(name: str, discipline: str, order: int) -> dict:
    return {
        "id": new_id("cat"),
        "name": name,
        "code": "",          # ISU category code (see "ISU codes" below): learned from the FSM files, if any
        "discipline": discipline if discipline in DISCIPLINES else "single",
        "order": order,
        # Team-page defaults for this category's teams; None = inherit the
        # competition-wide teamPages setting. Mirrors the per-team overrides.
        "pageEnabled": None,
        "nameMode": None,
        "titlePdf": None,
        "podium": {"photo": None, "names": ["", "", ""]},
        "totalResultsPdf": None,
        "teams": [],
        "segments": [],
    }


def new_segment(name: str, order: int) -> dict:
    return {
        "id": new_id("seg"),
        "name": name,
        "order": order,
        # Competition units (skaters/pairs/teams) that performed this segment.
        # Auto-filled from the segment's results PDF, user-correctable; None = unknown.
        # Drives the information page's Competition Units / Performances counts.
        "unitCount": None,
        "resultsPdf": None,
        "panelPdf": None,
        "judgesDetailsPdf": None,
    }


def new_team(org: str = "", name: str = "") -> dict:
    return {
        "id": new_id("team"),
        "code": "",          # DT_PARTIC_TEAMS Team Code (for re-import matching)
        "event": "",         # ISU event code the team is registered in
        "org": org,
        "name": name,
        "photo": None,
        # Accreditation photo imported from the optional fallback-pictures ZIP;
        # used at generation only when the team has no competition photo.
        "photoFallback": None,
        "members": [],
        # Team-page overrides; None = inherit the category's setting (which in
        # turn inherits the competition-wide teamPages one).
        "pageEnabled": None,
        "nameMode": None,
        # Free-typed rows printed on the team page: {"id", "label", "value"}.
        "textFields": [],
    }


def discipline_signal(name: str):
    """Discipline indicated by a (Finnish or English) category name, or None when
    the name carries no clear signal (caller may then apply a competition-level
    default, e.g. an all-synchro competition)."""
    n = (name or "").lower()
    if any(k in n for k in ("synchro", "synchron", "muodostelma", "sysc", "syncro")):
        return "synchro"
    if any(k in n for k in ("ice dance", "rhythm dance", "free dance", "pattern dance",
                            "jäätanssi", "jaatanssi", "tanssi", "rytmitanssi")):
        return "dance"
    if any(k in n for k in ("pairs", "pair", "pariluistelu", "pari")):
        return "pair"
    if any(k in n for k in ("tytöt", "tyttö", "naiset", "miehet", "pojat", "poika",
                            "girls", "ladies", "women", "men", "boys",
                            "singles", "yksinluistelu")):
        return "single"
    return None


def detect_discipline(name: str) -> str:
    """Best-effort discipline; defaults to 'single' when the name is ambiguous."""
    return discipline_signal(name) or "single"


# ── ISU codes ─────────────────────────────────────────────────────────────────
#
# FS Manager names every export after the ISU RSC code of its unit:
# `<22-char category code><phase>_<ReportTitle>.pdf`, e.g.
# `FSKXSYNCHRONADVNOV--01FNL-000100--_StartListwithTimes.pdf`. The first 22
# characters are the category: the discipline/event abbreviation
# (`FSKXSYNCHRONADVNOV`, `FSKWSINGLES-DEBYTW`, bare `FSKXSYNCHRON` for senior
# synchro) padded with dashes, plus an optional `--NN` block suffix when the event
# is run in blocks. That code — never the printed name — is what identifies a
# category across the schedule, the FSM PDFs and the DT_PARTIC_TEAMS roster, and
# its prefix is what tells the discipline.

CODE_DISCIPLINES = (
    ("FSKWSINGLES", "single"),
    ("FSKMSINGLES", "single"),
    ("FSKXSYNCHRON", "synchro"),
    ("FSKXICEDANCE", "dance"),
    ("FSKMSOLDANCE", "dance"),
    ("FSKWSOLDANCE", "dance"),
    ("FSKXPAIRS", "pair"),
)
# Kept under the old name for callers that still read head-page prefixes.
TITLE_PDF_PREFIXES = CODE_DISCIPLINES

RSC_CODE_LEN = 22
_RSC_STEM = re.compile(r"[A-Z]{3}[A-Z0-9-]+")


def strip_code(code) -> str:
    """The canonical form of an ISU category code: upper-case, trailing dash
    padding removed, interior block suffix kept (`FSKWSINGLES-DEBYTW----` and
    `FSKWSINGLES-DEBYTW` are the same code; `…ADVNOV--01` is not `…ADVNOV`)."""
    return str(code or "").strip().upper().rstrip("-")


def code_from_filename(filename) -> str:
    """The category code an FSM export filename carries, canonical, or `""` when
    the name is not an FSM export (a photo, a hand-named PDF)."""
    name = str(filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if "_" not in name:
        return ""          # FSM always separates the code from the report title
    stem = name.rsplit("_", 1)[0].upper()
    if len(stem) < RSC_CODE_LEN or not _RSC_STEM.fullmatch(stem):
        return ""
    return strip_code(stem[:RSC_CODE_LEN])


def discipline_from_code(code):
    """Discipline encoded in an ISU code's (or FSM filename's) prefix, or None
    when it does not follow the ISU convention."""
    n = str(code or "").strip().upper()
    return next((d for prefix, d in CODE_DISCIPLINES if n.startswith(prefix)), None)


# The head-page helper's old name: a filename is read exactly like a code.
discipline_from_title_filename = discipline_from_code


def apply_code(category: dict, code) -> bool:
    """Record an ISU code on a category and let it decide the discipline.

    The code comes from an FSM filename or from the competition's pool; the
    policy keeps the most specific truth and never destroys one:

      * no code yet -> set it;
      * the same code (padded or not) -> nothing to store, the discipline is
        still (re)asserted;
      * the new code extends the old by a dash-led block (`…ADVNOV` ->
        `…ADVNOV--01`) -> replace: the bare abbreviation was a guess at the block;
      * a downgrade (`…ADVNOV--01` -> `…ADVNOV`) or any other conflict -> ignored.

    Whenever a code is accepted its prefix overrides the stored discipline (as
    authoritative as the head page always was). Returns True when the category
    changed."""
    new = strip_code(code)
    if not category or not new:
        return False
    old = strip_code(category.get("code"))
    changed = False
    if old and old != new:
        if not new.startswith(old + "-"):
            return False
        category["code"] = new
        changed = True
    elif not old:
        category["code"] = new
        changed = True
    discipline = discipline_from_code(new)
    if discipline and category.get("discipline") != discipline:
        category["discipline"] = discipline
        changed = True
    return changed


def apply_title_discipline(category: dict, filename: str) -> bool:
    """Set the category's discipline from an ISU head-page PDF filename. The
    prefix is authoritative ISU coding, so it overrides any name-based guess —
    even on a hand-named head page that carries no full code."""
    discipline = discipline_from_code(filename)
    if not category or not discipline or category.get("discipline") == discipline:
        return False
    category["discipline"] = discipline
    return True


def category_needs_code(category: dict) -> bool:
    return not strip_code((category or {}).get("code"))


def categories_needing_codes(structure: dict) -> bool:
    """True when some category has no code, or two share one (a bare
    abbreviation stamped on both blocks of a split event) — either way the
    competition's own FSM files may know better."""
    seen = set()
    for category in structure.get("categories") or []:
        code = strip_code(category.get("code"))
        if not code or code in seen:
            return True
        seen.add(code)
    return False


# ── lookups ───────────────────────────────────────────────────────────────────

def find_category(structure: dict, category_id: str):
    return next((c for c in structure.get("categories", []) if c["id"] == category_id), None)


def find_segment(category: dict, segment_id: str):
    return next((s for s in category.get("segments", []) if s["id"] == segment_id), None)


def find_team(category: dict, team_id: str):
    return next((t for t in category.get("teams", []) if t["id"] == team_id), None)


def sorted_categories(structure: dict):
    return sorted(structure.get("categories", []), key=lambda c: (c.get("order", 0), c.get("name", "")))


def sorted_segments(category: dict):
    return sorted(category.get("segments", []), key=lambda s: (s.get("order", 0), s.get("name", "")))


def schedule_rows(structure: dict):
    """The time-schedule rows as printed. A row linked to a category (and segment)
    carries their *current* names, so a category renamed in the tool reads the
    same on the schedule page as everywhere else; the schedule's own wording is
    only the default. A row with no link, or whose category is gone, prints as
    parsed. Copies — the stored rows are left alone."""
    out = []
    for row in structure.get("schedule") or []:
        row = dict(row)
        category = find_category(structure, row.get("categoryId")) if row.get("categoryId") else None
        if category and (category.get("name") or "").strip():
            row["category_name"] = category["name"].strip()
            segment = find_segment(category, row.get("segmentId")) if row.get("segmentId") else None
            if segment and (segment.get("name") or "").strip():
                row["segment_name"] = segment["name"].strip()
            row["event_name"] = f"{row['category_name']} {row.get('segment_name') or ''}".strip()
        out.append(row)
    return out


# ── team pages ────────────────────────────────────────────────────────────────
#
# Two settings — create the page at all, and how much of the roster to print —
# resolve through three levels: the team's own override, else its category's,
# else the competition-wide `teamPages`. The two upper levels are *defaults*, not
# copies: None means "inherit", so changing a category moves every team that has
# not overridden it. Every read goes through these resolvers, so a metadata.json
# written before the feature existed resolves to today's behaviour (pages on,
# full names) without a migration.

def coerce_name_mode(value):
    """A client-supplied name mode, or None when it is absent/blank/unknown — which
    the resolvers below read as "inherit"."""
    return value if value in NAME_MODES else None


def team_pages_defaults(structure: dict) -> dict:
    """The competition-wide team-page settings, filled in for structures written
    before the setting existed."""
    tp = structure.get("teamPages") or {}
    return {
        "enabled": tp.get("enabled", True) is not False,
        "nameMode": coerce_name_mode(tp.get("nameMode")) or "full",
    }


def category_page_enabled(structure: dict, category: dict) -> bool:
    """What this category hands its teams: its own override when it has one, else
    the competition default. Only an explicit True/False overrides."""
    override = (category or {}).get("pageEnabled")
    if override is None:
        return team_pages_defaults(structure)["enabled"]
    return bool(override)


def category_name_mode(structure: dict, category: dict) -> str:
    """The roster mode this category hands its teams."""
    return (coerce_name_mode((category or {}).get("nameMode"))
            or team_pages_defaults(structure)["nameMode"])


def team_page_enabled(structure: dict, category: dict, team: dict) -> bool:
    """Whether this team gets a presentation page: its own override when it has
    one, else what its category resolves to. Only an explicit True/False overrides."""
    override = (team or {}).get("pageEnabled")
    if override is None:
        return category_page_enabled(structure, category)
    return bool(override)


def team_name_mode(structure: dict, category: dict, team: dict) -> str:
    """'full' | 'firstNames' | 'none' for this team's roster."""
    return (coerce_name_mode((team or {}).get("nameMode"))
            or category_name_mode(structure, category))


def team_text_rows(team: dict) -> list:
    """The team's free-text rows in stored order, which is the order they print in.
    Rows blank on both sides are dropped.

    Each row comes back as {"label", "value"} — plain data, so the page renderer
    needs no structure vocabulary. Rows stored while the feature still attached
    them to segments carry a `segmentId`; it is ignored, never a reason to drop
    the row."""
    out = []
    for row in (team or {}).get("textFields") or []:
        if not isinstance(row, dict):
            continue
        label = str(row.get("label") or "").strip()
        value = str(row.get("value") or "").strip()
        if not label and not value:
            continue
        out.append({"label": label, "value": value})
    return out


def sanitize_text_fields(rows) -> list:
    """Normalize a client-sent `textFields` list before it is stored: mint missing
    ids, trim and length-cap label and value, drop rows blank on both sides, and cap
    the list at MAX_TEAM_TEXT_FIELDS. A legacy `segmentId` is dropped rather than
    kept, so rows written while they were segment-attached normalise on the next
    save."""
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        label = str(row.get("label") or "").strip()[:TEXT_LABEL_MAX]
        value = str(row.get("value") or "").strip()[:TEXT_VALUE_MAX]
        if not label and not value:
            continue
        out.append({
            "id": str(row.get("id") or "").strip() or new_id("fld"),
            "label": label,
            "value": value,
        })
        if len(out) >= MAX_TEAM_TEXT_FIELDS:
            break
    return out


# ── slot assignment ────────────────────────────────────────────────────────────

def clear_file(structure: dict, file_id: str):
    """Remove a fileId from every slot it currently occupies."""
    if structure["coverPage"].get("fileId") == file_id:
        structure["coverPage"] = {"mode": "default", "fileId": None}
    if structure["lastPage"].get("fileId") == file_id:
        structure["lastPage"] = {"mode": "default", "fileId": None}
    for chrome_key in ("header", "footer"):
        if (structure.get(chrome_key) or {}).get("fileId") == file_id:
            structure[chrome_key] = {"mode": "default", "fileId": None}
    for cat in structure.get("categories", []):
        if cat.get("titlePdf") == file_id:
            cat["titlePdf"] = None
        if cat.get("totalResultsPdf") == file_id:
            cat["totalResultsPdf"] = None
        podium = cat.get("podium") or {}
        if podium.get("photo") == file_id:
            podium["photo"] = None
        for team in cat.get("teams", []):
            if team.get("photo") == file_id:
                team["photo"] = None
            if team.get("photoFallback") == file_id:
                team["photoFallback"] = None
        for seg in cat.get("segments", []):
            for key in ROLE_KEYS.values():
                if seg.get(key) == file_id:
                    seg[key] = None


def assign_file(structure: dict, target: dict, file_id):
    """Place `file_id` into the slot described by `target` (or clear the slot when
    file_id is None). A file is first removed from any slot it already occupies, so
    assignment also implements drag-and-drop *moves* between slots.

    target = {"kind": "cover"|"lastPage"|"header"|"footer"|"tray"|"categoryTitle"
                       |"totalResults"|"podiumPhoto"|"teamPhoto"|"teamPhotoFallback"
                       |"segment", ...ids}
    """
    if file_id is not None:
        clear_file(structure, file_id)

    kind = target.get("kind")
    if kind == "tray":
        return  # clearing above already unassigned it
    if kind == "cover":
        structure["coverPage"] = {"mode": "custom" if file_id else "default", "fileId": file_id}
        return
    if kind == "lastPage":
        structure["lastPage"] = {"mode": "custom" if file_id else "default", "fileId": file_id}
        return
    if kind in ("header", "footer"):
        structure[kind] = {"mode": "custom" if file_id else "default", "fileId": file_id}
        return

    cat = find_category(structure, target.get("categoryId"))
    if not cat:
        raise KeyError("category not found")

    if kind == "categoryTitle":
        cat["titlePdf"] = file_id
    elif kind == "totalResults":
        cat["totalResultsPdf"] = file_id
    elif kind == "podiumPhoto":
        cat.setdefault("podium", {"photo": None, "names": ["", "", ""]})["photo"] = file_id
    elif kind == "teamPhoto":
        team = find_team(cat, target.get("teamId"))
        if not team:
            raise KeyError("team not found")
        team["photo"] = file_id
    elif kind == "teamPhotoFallback":
        team = find_team(cat, target.get("teamId"))
        if not team:
            raise KeyError("team not found")
        team["photoFallback"] = file_id
    elif kind == "segment":
        seg = find_segment(cat, target.get("segmentId"))
        if not seg:
            raise KeyError("segment not found")
        role = target.get("role")
        if role not in ROLE_KEYS:
            raise KeyError("invalid segment role")
        seg[ROLE_KEYS[role]] = file_id
    else:
        raise KeyError(f"unknown slot kind: {kind}")


def assigned_file_ids(structure: dict) -> set:
    """All fileIds currently referenced by a slot."""
    ids = set()
    for ref in (structure["coverPage"].get("fileId"), structure["lastPage"].get("fileId"),
                (structure.get("header") or {}).get("fileId"),
                (structure.get("footer") or {}).get("fileId")):
        if ref:
            ids.add(ref)
    for cat in structure.get("categories", []):
        for ref in (cat.get("titlePdf"), cat.get("totalResultsPdf"), (cat.get("podium") or {}).get("photo")):
            if ref:
                ids.add(ref)
        for team in cat.get("teams", []):
            if team.get("photo"):
                ids.add(team["photo"])
            if team.get("photoFallback"):
                ids.add(team["photoFallback"])
        for seg in cat.get("segments", []):
            for key in ROLE_KEYS.values():
                if seg.get(key):
                    ids.add(seg[key])
    return ids
