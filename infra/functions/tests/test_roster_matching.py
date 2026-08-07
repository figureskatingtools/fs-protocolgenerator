"""`roster_matching.match_teams` — placing registered teams into schedule blocks.

Result rows are injected directly (no PDFs), and every name here is invented:
the real competition's rosters are minors' personal data and must never be
committed.
"""
import copy

import roster_matching as rm


# ── tiny builders ─────────────────────────────────────────────────────────────

def cat(cid, name, code=""):
    return {"id": cid, "name": name, "code": code, "discipline": "single", "teams": []}


def struct(*cats):
    return {"categories": list(cats)}


def team(name, org="", event="", code="t"):
    return {"code": code, "name": name, "org": org, "event": event, "members": []}


def row(rank, name, club=""):
    return {"rank": rank, "name": name, "club": club}


TULO = "FSKXSYNCHRONMLTULO----------------"
AIKU = "FSKXSYNCHRONMLAIKU----------------"


# ── results-first matching ────────────────────────────────────────────────────

def test_exact_result_name_places_team():
    s = struct(cat("c1", "Tulokkaat, Mupi L1"), cat("c2", "Tulokkaat, Mupi L2"))
    parsed = team("Espoo Nightingales", event=TULO)
    report = rm.match_teams(s, [parsed], {"c1": [row(1, "Espoo Nightingales", "ENK")]})
    assert report["assignments"] == [
        {"team": parsed, "categoryId": "c1", "method": "results"}]
    assert not report["unmatched"] and not report["withdrawn"]


def test_result_sheet_short_form_matches_by_word_subset():
    """Result sheets abbreviate: "Nightingales" is the XML's "Espoo Nightingales"."""
    s = struct(cat("c1", "Tulokkaat, Mupi L1"), cat("c2", "Tulokkaat, Mupi L2"))
    report = rm.match_teams(s, [team("Espoo Nightingales", event=TULO)],
                            {"c2": [row(1, "Nightingales", "ENK")]})
    assert [(a["categoryId"], a["method"]) for a in report["assignments"]] == \
        [("c2", "results-fuzzy")]


def test_club_breaks_a_tie_between_two_result_sheets():
    s = struct(cat("c1", "Tulokkaat, Mupi L1"), cat("c2", "Tulokkaat, Mupi L2"))
    report = rm.match_teams(s, [team("Blue Herons", org="BHK", event=TULO)],
                            {"c1": [row(1, "Blue Herons", "XYZ")],
                             "c2": [row(1, "Blue Herons", "BHK")]})
    assert [a["categoryId"] for a in report["assignments"]] == ["c2"]
    assert not report["unmatched"]


def test_same_name_on_two_sheets_without_a_club_is_unmatched():
    s = struct(cat("c1", "Tulokkaat, Mupi L1"), cat("c2", "Tulokkaat, Mupi L2"))
    report = rm.match_teams(s, [team("Blue Herons", event=TULO)],
                            {"c1": [row(1, "Blue Herons", "XYZ")],
                             "c2": [row(1, "Blue Herons", "BHK")]})
    assert not report["assignments"]
    assert len(report["unmatched"]) == 1
    entry = report["unmatched"][0]
    assert "several result sheets" in entry["reason"]
    assert entry["name"] == "Blue Herons" and entry["eventLabel"] == "Tulokkaat"


# ── event fallback ────────────────────────────────────────────────────────────

def test_event_code_matching_a_single_category_places_the_team():
    s = struct(cat("c1", "Tulokkaat, Mupi L1", code="FSKXSYNCHRONMLTULO--01"),
               cat("c2", "Aikuiset, Mupi L1", code="FSKXSYNCHRONMLAIKU--01"))
    report = rm.match_teams(s, [team("Blue Herons", event=AIKU)], {})
    assert [(a["categoryId"], a["method"]) for a in report["assignments"]] == \
        [("c2", "event")]


def test_padded_event_code_spans_every_block_of_that_event():
    """`…MLTULO----` is the whole event, so both of its blocks are candidates —
    which is what makes the "assign the result sheets" report necessary."""
    s = struct(cat("c1", "Tulokkaat, Mupi L1", code="FSKXSYNCHRONMLTULO----"),
               cat("c2", "Tulokkaat, Mupi L2", code="FSKXSYNCHRONMLTULO--01"),
               cat("c3", "Aikuiset, Mupi L1", code="FSKXSYNCHRONMLAIKU--01"))
    assert [c["id"] for c in rm.categories_for_event(s, TULO)] == ["c1", "c2"]


def test_block_specific_event_code_matches_only_its_own_block():
    """Only *trailing* dashes are stripped, so the sibling block suffixes stay
    distinct (stripping every dash would collapse --01 and --02 into one)."""
    s = struct(cat("c1", "Tulokkaat, Mupi L1", code="FSKXSYNCHRONMLTULO--01"),
               cat("c2", "Tulokkaat, Mupi L2", code="FSKXSYNCHRONMLTULO--02"))
    assert rm.strip_event("FSKXSYNCHRONMLTULO--01") == "FSKXSYNCHRONMLTULO--01"
    assert rm.strip_event("FSKXSYNCHRONMLTULO----") == "FSKXSYNCHRONMLTULO"
    assert [c["id"] for c in rm.categories_for_event(s, "FSKXSYNCHRONMLTULO--02")] == ["c2"]


def test_name_fragment_places_a_pdf_parsed_category():
    """Schedule-PDF categories have no code, so the event token's tail is matched
    against the category name: MLTULO -> LTULO -> TULO -> "Tulokkaat…"."""
    s = struct(cat("c1", "Aikuiset, Mupi L1"), cat("c2", "Tulokkaat, Mupi L1"),
               cat("c3", "Noviisit L1"))
    assert [c["id"] for c in rm.categories_for_event(s, TULO)] == ["c2"]
    report = rm.match_teams(s, [team("Blue Herons", event=TULO)], {})
    assert [(a["categoryId"], a["method"]) for a in report["assignments"]] == \
        [("c2", "event")]


def test_two_blocks_without_results_gives_an_actionable_reason():
    s = struct(cat("c1", "Tulokkaat, Mupi L1"), cat("c2", "Tulokkaat, Mupi L2"))
    report = rm.match_teams(s, [team("Blue Herons", event=TULO)], {})
    assert not report["assignments"] and not report["withdrawn"]
    reason = report["unmatched"][0]["reason"]
    assert "matches 2 categories" in reason and "Total Results PDF" in reason


def test_team_on_no_sheet_when_every_block_is_resulted_is_withdrawn():
    s = struct(cat("c1", "Tulokkaat, Mupi L1"), cat("c2", "Tulokkaat, Mupi L2"))
    report = rm.match_teams(s, [team("Silver Comets", org="SCK", event=TULO)],
                            {"c1": [row(1, "Northern Lights", "NLK")],
                             "c2": [row(1, "Golden Arrows", "GAK")]})
    assert not report["assignments"] and not report["unmatched"]
    assert report["withdrawn"] == [
        {"name": "Silver Comets", "org": "SCK", "eventLabel": "Tulokkaat"}]


def test_team_without_a_registered_event_is_reported():
    s = struct(cat("c1", "Tulokkaat, Mupi L1"))
    report = rm.match_teams(s, [team("Blue Herons", event="")], {})
    assert "no registered event" in report["unmatched"][0]["reason"]
    assert report["unmatched"][0]["eventLabel"] == ""


def test_event_matching_no_category_is_reported():
    s = struct(cat("c1", "Noviisit L1"))
    report = rm.match_teams(s, [team("Blue Herons", event=AIKU)], {})
    assert report["unmatched"][0]["reason"] == "no category matches event Aikuiset"


def test_match_teams_does_not_mutate_the_structure():
    s = struct(cat("c1", "Tulokkaat, Mupi L1"), cat("c2", "Tulokkaat, Mupi L2"))
    before = copy.deepcopy(s)
    rm.match_teams(s, [team("Blue Herons", org="BHK", event=TULO)],
                   {"c1": [row(1, "Blue Herons", "BHK")]})
    assert s == before
