"""
The competition *structure* document (stored as metadata.json in each blob folder).

It is the single source of truth for everything the protocol assembler needs:
event details, the cover/last-page choices, a registry of uploaded files, and the
ordered categories with their slots. Uploaded files are referenced by `fileId`;
each file lives in at most one slot. Files not referenced by any slot are
"unassigned" (the UI shows them in a tray to be dragged into place).
"""
from uuid import uuid4

DISCIPLINES = ("single", "pair", "dance", "synchro")

# Per-segment slot roles -> structure keys.
ROLE_KEYS = {
    "results": "resultsPdf",
    "panel": "panelPdf",
    "judgesDetails": "judgesDetailsPdf",
}


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
        "scheduleParsed": False,
        # fileId -> {"filename", "kind" (pdf|image|xml), "size", "uploadedAt"}
        "files": {},
        "categories": [],
    }


def new_category(name: str, discipline: str, order: int) -> dict:
    return {
        "id": new_id("cat"),
        "name": name,
        "code": "",          # ISU event code (from a DT_SCHEDULE import), if any
        "discipline": discipline if discipline in DISCIPLINES else "single",
        "order": order,
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
        "members": [],
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
        for seg in cat.get("segments", []):
            for key in ROLE_KEYS.values():
                if seg.get(key) == file_id:
                    seg[key] = None


def assign_file(structure: dict, target: dict, file_id):
    """Place `file_id` into the slot described by `target` (or clear the slot when
    file_id is None). A file is first removed from any slot it already occupies, so
    assignment also implements drag-and-drop *moves* between slots.

    target = {"kind": "cover"|"lastPage"|"header"|"footer"|"tray"|"categoryTitle"
                       |"totalResults"|"podiumPhoto"|"teamPhoto"|"teamRoster"|"segment", ...ids}
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
        for seg in cat.get("segments", []):
            for key in ROLE_KEYS.values():
                if seg.get(key):
                    ids.add(seg[key])
    return ids
