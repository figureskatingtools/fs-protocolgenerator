"""
Placing DT_PARTIC_TEAMS teams into the schedule's categories.

Teams register per **event** — one ISU code such as
`FSKXSYNCHRONMLAIKU----------------` ("Aikuiset"/Adult) — but a competition may
run that event as several **blocks**, each of which is its own category in the
schedule ("Aikuiset, Mupi L1" / "Aikuiset, Mupi L2"; DT_SCHEDULE codes
`…MLAIKU--01` vs `…MLAIKU----`). The roster XML carries no block information at
all: which team skated in which block is visible **only in each block's total
results PDF**. So the matcher works results-first and falls back to the event:

  1. the team's name appears in a category's parsed result rows -> that category
     (exact, then a fuzzy word-subset pass for "JääLeidit" vs
     "Helsinki JääLeidit"; the club abbreviation breaks ties);
  2. otherwise the team's event maps to exactly one category -> that category;
  3. an event that maps to several blocks is only decidable from results: when
     every block already has results the team skated in none of them and is
     reported **withdrawn**; when results are still missing it is reported
     unmatched with an actionable reason (assign the Total Results PDFs and the
     team places itself) — never a silent guess into the wrong block.

This module is pure: no Azure imports, no mutation of the structure. The caller
(`function_app.import_rosters`) applies the returned assignments and persists the
report. Name folding is shared with the photo ZIP importer
(`fallback_photos.normalize`) so both features agree on what "the same team" is.
"""
from dt_partic import event_label
from fallback_photos import normalize

# Discipline prefixes an ISU event code may carry before the event token itself.
_EVENT_PREFIXES = ("FSKXSYNCHRON", "FSKSYNCHRON", "SYNCHRON", "FSKX", "FSK")

# A shorter containment hit is noise ("ETK" inside "Jäätähdet"), so only
# fragments/substrings of this length may match by containment.
MIN_FRAGMENT = 4


def _despaced(s: str) -> str:
    return s.replace(" ", "")


# ── name comparison ───────────────────────────────────────────────────────────

def same_name(a: str, b: str) -> bool:
    """Equal normalised names, also tolerating a differing word split
    ("Team Unique" vs "TeamUnique"). An empty name never matches."""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return False
    return na == nb or _despaced(na) == _despaced(nb)


def name_subset(a: str, b: str) -> bool:
    """True when one name is a *plausible short form* of the other: its words are
    a subset of the other's (either direction), or one despaced name contains the
    other. Result sheets abbreviate ("JääLeidit" for the XML's
    "Helsinki JääLeidit") and occasionally do the opposite."""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return False
    wa, wb = set(na.split()), set(nb.split())
    if wa <= wb or wb <= wa:
        return True
    da, db = _despaced(na), _despaced(nb)
    if len(da) >= MIN_FRAGMENT and da in db:
        return True
    if len(db) >= MIN_FRAGMENT and db in da:
        return True
    return False


# ── event codes ───────────────────────────────────────────────────────────────

def strip_event(code: str) -> str:
    """Drop an ISU code's *trailing* dash padding only. Interior dashes carry the
    block suffix, so `…MLAIKU--01` must stay distinct from `…MLAIKU----`."""
    return (code or "").strip().rstrip("-")


def event_token(code: str) -> str:
    """The event part of a code, without discipline prefix or dashes:
    `'FSKXSYNCHRONMLAIKU----'` -> `'MLAIKU'`."""
    token = strip_event(code)
    upper = token.upper()
    for prefix in _EVENT_PREFIXES:
        if upper.startswith(prefix):
            token = token[len(prefix):]
            break
    return token.replace("-", "").upper()


def categories_for_event(structure: dict, event_code: str) -> list:
    """Every category an event could belong to, in structure order.

    Three independent attempts, the first with any hit wins (an event genuinely
    split into blocks yields several categories — that is the caller's cue that
    results are needed to tell them apart):

      1. **code** — the categories a DT_SCHEDULE import stamped with a code that
         is a prefix of, or prefixed by, this event's code;
      2. **name fragment** — the event token's tail matched against the start of a
         category-name word ("MLAIKU" -> "AIKU" -> "Aikuiset, Mupi L1"), longest
         fragment first so a shorter accidental match can never win;
      3. **ISU label** — the English label as a substring of the category name.

    No discipline gate: categories parsed from a schedule *PDF* are all typed
    `single` until an import proves otherwise, so gating on synchro would make
    the name/label passes dead code."""
    cats = structure.get("categories") or []
    ev = strip_event(event_code).casefold()
    if not ev:
        return []

    hits = []
    for cat in cats:
        cc = strip_event(cat.get("code")).casefold()
        if cc and (cc == ev or cc.startswith(ev) or ev.startswith(cc)):
            hits.append(cat)
    if hits:
        return hits

    token = event_token(event_code)
    for size in range(len(token), MIN_FRAGMENT - 1, -1):
        fragment = token[-size:].lower()
        hits = [c for c in cats
                if any(w.startswith(fragment) for w in normalize(c.get("name", "")).split())]
        if hits:
            return hits

    label = event_label(event_code).casefold().strip()
    if label:
        hits = [c for c in cats if label in normalize(c.get("name", ""))]
        if hits:
            return hits
    return []


# ── the matching pipeline ─────────────────────────────────────────────────────

def _report_row(team: dict, reason: str = None) -> dict:
    code = team.get("event") or ""
    entry = {
        "name": team.get("name") or "",
        "org": team.get("org") or "",
        "eventLabel": event_label(code) or code,
    }
    if reason is not None:
        entry["reason"] = reason
    return entry


def match_teams(structure: dict, teams: list, rows_by_cat: dict) -> dict:
    """Match parsed DT_PARTIC_TEAMS teams onto the structure's categories.

    `teams` are `dt_partic.parse_team_rosters` dicts
    (`{code, name, org, event, members}`); `rows_by_cat` maps a categoryId to the
    `results_parser.parse_result_rows` output (`{rank, name, club}`) of that
    category's total-results PDF — a missing/empty entry simply means "no results
    for this category yet".

    Returns `{"assignments": [{"team", "categoryId", "method"}], "unmatched":
    [{"name", "org", "eventLabel", "reason"}], "withdrawn": [{"name", "org",
    "eventLabel"}]}`; `method` is `"results"`, `"results-fuzzy"` or `"event"`.
    Nothing is mutated — the caller decides what to apply."""
    cats = structure.get("categories") or []
    rows_by_cat = rows_by_cat or {}

    # categoryId -> [(normalised row name, club)], only for categories with results.
    parsed_rows = {}
    for cat in cats:
        rows = rows_by_cat.get(cat.get("id")) or []
        if rows:
            parsed_rows[cat["id"]] = [(normalize(r.get("name", "")),
                                       (r.get("club") or "").casefold()) for r in rows]

    assignments, unmatched, withdrawn, leftover = [], [], [], []

    for team in teams:
        tn = normalize(team.get("name", ""))
        org = (team.get("org") or "").casefold()
        method = "results"
        hits = {}
        if tn:
            hits = {cid: [r for r in rows if same_name(r[0], tn)]
                    for cid, rows in parsed_rows.items()}
            hits = {cid: rows for cid, rows in hits.items() if rows}
            if not hits:
                method = "results-fuzzy"
                hits = {cid: [r for r in rows if name_subset(r[0], tn)]
                        for cid, rows in parsed_rows.items()}
                hits = {cid: rows for cid, rows in hits.items() if rows}

        # The same team name can appear in two blocks' sheets (a fuzzy hit
        # especially); the club abbreviation then usually settles it.
        if len(hits) > 1 and org:
            narrowed = {cid: rows for cid, rows in hits.items()
                        if any(club == org for _, club in rows)}
            if narrowed:
                hits = narrowed

        if len(hits) == 1:
            assignments.append({"team": team, "categoryId": next(iter(hits)), "method": method})
        elif len(hits) > 1:
            unmatched.append(_report_row(team, "team name appears in several result sheets"))
        else:
            leftover.append(team)

    # Teams on no sheet: decide from their registered event, one group at a time.
    groups = {}
    for team in leftover:
        groups.setdefault(team.get("event") or "", []).append(team)

    for code, group in groups.items():
        label = event_label(code) or code
        if not code:
            for team in group:
                unmatched.append(_report_row(team, "no registered event in DT_PARTIC_TEAMS"))
            continue
        candidates = categories_for_event(structure, code)
        if len(candidates) == 1:
            for team in group:
                assignments.append({"team": team, "categoryId": candidates[0]["id"],
                                    "method": "event"})
        elif not candidates:
            for team in group:
                unmatched.append(_report_row(team, f"no category matches event {label}"))
        elif all(parsed_rows.get(c["id"]) for c in candidates):
            # Every block of the event has been resulted and the team is on none
            # of those sheets: registered but never skated.
            for team in group:
                withdrawn.append(_report_row(team))
        else:
            reason = (f"event {label} matches {len(candidates)} categories — assign each "
                      "one's Total Results PDF and the team is placed automatically")
            for team in group:
                unmatched.append(_report_row(team, reason))

    return {"assignments": assignments, "unmatched": unmatched, "withdrawn": withdrawn}
