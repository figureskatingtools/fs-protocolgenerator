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


def test_a_bare_event_is_the_bare_coded_block_when_one_exists():
    """FS Manager registers a team to its block's exact code: with the blocks
    drawn, `…MLTULO----` *is* the bare-coded block and `…MLTULO--01` the other,
    whether the stored codes are padded or not."""
    s = struct(cat("c1", "Tulokkaat, Mupi L2", code="FSKXSYNCHRONMLTULO----"),
               cat("c2", "Tulokkaat, Mupi L1", code="FSKXSYNCHRONMLTULO--01"),
               cat("c3", "Aikuiset, Mupi L1", code="FSKXSYNCHRONMLAIKU--01"))
    assert [c["id"] for c in rm.categories_for_event(s, TULO)] == ["c1"]
    assert [c["id"] for c in rm.categories_for_event(s, "FSKXSYNCHRONMLTULO--01")] == ["c2"]
    report = rm.match_teams(s, [team("Blue Herons", event=TULO),
                                team("Silver Comets", event="FSKXSYNCHRONMLTULO--01")], {})
    assert [(a["team"]["name"], a["categoryId"], a["method"]) for a in report["assignments"]] == \
        [("Blue Herons", "c1", "event"), ("Silver Comets", "c2", "event")]
    assert not report["unmatched"]


def test_a_bare_event_spans_the_blocks_when_none_carries_it():
    """A roster exported before the blocks were drawn registers every team to
    the whole event, so both `--01`/`--02` blocks are candidates — which is what
    makes the "assign the result sheets" report necessary."""
    s = struct(cat("c1", "Tulokkaat, Mupi L1", code="FSKXSYNCHRONMLTULO--01"),
               cat("c2", "Tulokkaat, Mupi L2", code="FSKXSYNCHRONMLTULO--02"),
               cat("c3", "Aikuiset, Mupi L1", code="FSKXSYNCHRONMLAIKU--01"))
    assert [c["id"] for c in rm.categories_for_event(s, TULO)] == ["c1", "c2"]
    report = rm.match_teams(s, [team("Blue Herons", event=TULO)], {})
    assert "matches 2 categories" in report["unmatched"][0]["reason"]


def test_block_specific_event_code_matches_only_its_own_block():
    """Only *trailing* dashes are stripped, so the sibling block suffixes stay
    distinct (stripping every dash would collapse --01 and --02 into one)."""
    s = struct(cat("c1", "Tulokkaat, Mupi L1", code="FSKXSYNCHRONMLTULO--01"),
               cat("c2", "Tulokkaat, Mupi L2", code="FSKXSYNCHRONMLTULO--02"))
    assert rm.strip_event("FSKXSYNCHRONMLTULO--01") == "FSKXSYNCHRONMLTULO--01"
    assert rm.strip_event("FSKXSYNCHRONMLTULO----") == "FSKXSYNCHRONMLTULO"
    assert [c["id"] for c in rm.categories_for_event(s, "FSKXSYNCHRONMLTULO--02")] == ["c2"]


def test_a_block_registration_without_codes_still_matches_by_token():
    """`…MLTULO--01` on a code-less schedule: the block suffix is not part of
    the event token (otherwise "MLTULO01" would find nothing), and the
    unmatched report names the block."""
    s = struct(cat("c1", "Aikuiset, Mupi L1"), cat("c2", "Tulokkaat, Mupi L1"),
               cat("c3", "Tulokkaat, Mupi L2"))
    assert rm.event_token("FSKXSYNCHRONMLTULO--01") == "MLTULO"
    assert [c["id"] for c in rm.categories_for_event(s, "FSKXSYNCHRONMLTULO--01")] == ["c2", "c3"]
    report = rm.match_teams(s, [team("Blue Herons", event="FSKXSYNCHRONMLTULO--01")], {})
    assert report["unmatched"][0]["eventLabel"] == "Tulokkaat (block 1)"


def test_finnish_federation_events_find_their_categories_by_label():
    """The organizer prints the platform's names ("Juniorit", "Noviisit, Mupi",
    "Seniorit", "Minorit") for the federation's levels and the championship
    names for the ISU events: "SM-Juniorit" is Junior, "SM-Noviisit" Advanced
    Novice — so no code is needed to tell "Juniorit" and "SM-Juniorit" apart."""
    s = struct(cat("nm", "Noviisit, Mupi"), cat("mi", "Minorit"), cat("ju", "Juniorit"),
               cat("se", "Seniorit"), cat("smn", "SM-Noviisit"), cat("smj", "SM-Juniorit"))
    assert [c["id"] for c in rm.categories_for_event(s, "FSKXSYNCHRONMLKJUN----------------")] == ["ju"]
    assert [c["id"] for c in rm.categories_for_event(s, "FSKXSYNCHRONMLKSEN----------------")] == ["se"]
    assert [c["id"] for c in rm.categories_for_event(s, "FSKXSYNCHRONMLKNOV----------------")] == ["nm"]
    assert [c["id"] for c in rm.categories_for_event(s, "FSKXSYNCHRONMLMINO----------------")] == ["mi"]
    assert [c["id"] for c in rm.categories_for_event(s, JUNIOR)] == ["smj"]
    assert [c["id"] for c in rm.categories_for_event(s, "FSKXSYNCHRONADVNOV----------------")] == ["smn"]
    report = rm.match_teams(s, [team("Blue Herons", event="FSKXSYNCHRONADVNOV----------------")], {})
    assert report["unmatched"] == [] and report["withdrawn"] == []


def test_a_championship_event_split_into_blocks_finds_every_block():
    s = struct(cat("smn1", "SM-Noviisit L1"), cat("smn2", "SM-Noviisit L2"),
               cat("nm", "Noviisit, Mupi"))
    assert [c["id"] for c in rm.categories_for_event(s, "FSKXSYNCHRONADVNOV----------------")] == ["smn1", "smn2"]


def test_a_learned_code_still_beats_the_championship_name():
    s = struct(cat("smj", "SM-Juniorit"), cat("x", "Juniorit, kansallinen", code="FSKXSYNCHRONJUNIOR"))
    assert [c["id"] for c in rm.categories_for_event(s, JUNIOR)] == ["x"]


def test_a_championship_name_of_another_discipline_is_passed_over():
    """Singles and synchro juniors side by side: "SM-Juniorit Naiset" carries the
    championship name, but only the synchro category may take a synchro team."""
    s = struct(cat("sn", "SM-Juniorit Naiset"), cat("sm", "SM-JUNIORI Muodostelma"))
    assert [c["id"] for c in rm.categories_for_event(s, JUNIOR)] == ["sm"]


def test_codes_settle_what_names_cannot():
    """The same competition once its codes are learned from the FSM files."""
    s = struct(cat("ju", "Juniorit", code="FSKXSYNCHRONMLKJUN"),
               cat("smj", "SM-Juniorit", code="FSKXSYNCHRONJUNIOR"),
               cat("smn", "SM-Noviisit", code="FSKXSYNCHRONADVNOV"))
    assert [c["id"] for c in rm.categories_for_event(s, JUNIOR)] == ["smj"]
    assert [c["id"] for c in rm.categories_for_event(s, "FSKXSYNCHRONADVNOV----------------")] == ["smn"]
    assert [c["id"] for c in rm.categories_for_event(s, "FSKXSYNCHRONMLKJUN----------------")] == ["ju"]


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


# ── senior vs junior synchro ──────────────────────────────────────────────────

SENIOR = "FSKXSYNCHRON----------------------"
JUNIOR = "FSKXSYNCHRONJUNIOR----------------"


def test_senior_code_is_not_a_prefix_match_for_junior():
    """Senior is the bare `FSKXSYNCHRON`, a string prefix of `FSKXSYNCHRONJUNIOR`;
    each event must still map only onto its own category."""
    s = struct(cat("sen", "SM-seniorit", code="FSKXSYNCHRON----------"),
               cat("jun", "SM-juniorit", code="FSKXSYNCHRONJUNIOR----"))
    assert [c["id"] for c in rm.categories_for_event(s, SENIOR)] == ["sen"]
    assert [c["id"] for c in rm.categories_for_event(s, JUNIOR)] == ["jun"]
    report = rm.match_teams(s, [team("Blue Herons", event=SENIOR),
                                team("Silver Comets", event=JUNIOR)], {})
    assert [(a["team"]["name"], a["categoryId"], a["method"])
            for a in report["assignments"]] == \
        [("Blue Herons", "sen", "event"), ("Silver Comets", "jun", "event")]
    assert not report["unmatched"]


def test_senior_event_finds_a_codeless_senior_category_by_label():
    s = struct(cat("jun", "SM-juniorit"), cat("sen", "SM-seniorit"))
    assert [c["id"] for c in rm.categories_for_event(s, SENIOR)] == ["sen"]
    assert [c["id"] for c in rm.categories_for_event(s, JUNIOR)] == ["jun"]


def test_mixed_singles_and_synchro_competition_prefers_the_synchro_category():
    """A schedule-PDF competition with singles and synchro under one level name:
    "junior" hits all three JUNIORI categories, but rosters are synchro only."""
    s = struct(cat("jn", "SM-JUNIORI Naiset"), cat("jm", "SM-JUNIORI Miehet"),
               cat("sm", "SM-SENIORI Miehet"), cat("sn", "SM-SENIORI Naiset"),
               dict(cat("js", "SM-JUNIORI Muodostelma"), discipline="synchro"),
               dict(cat("ss", "SM-SENIORI Muodostelma"), discipline="synchro"))
    assert [c["id"] for c in rm.categories_for_event(s, JUNIOR)] == ["js"]
    assert [c["id"] for c in rm.categories_for_event(s, SENIOR)] == ["ss"]
    report = rm.match_teams(s, [team("Blue Herons", event=SENIOR),
                                team("Silver Comets", event=JUNIOR)], {})
    assert [(a["team"]["name"], a["categoryId"]) for a in report["assignments"]] == \
        [("Blue Herons", "ss"), ("Silver Comets", "js")]
    assert not report["unmatched"]


def test_synchro_named_category_is_preferred_even_while_typed_single():
    s = struct(cat("jn", "SM-JUNIORI Naiset"), cat("js", "SM-JUNIORI Muodostelma"))
    assert [c["id"] for c in rm.categories_for_event(s, JUNIOR)] == ["js"]


def test_partial_import_does_not_collapse_sibling_blocks():
    """Placing a team marks its category synchro while its untouched sibling
    block stays typed single; the stored discipline must not make the first
    resulted block swallow every team still waiting for its own sheet."""
    s = struct(dict(cat("c1", "Tulokkaat, Mupi L1"), discipline="synchro"),
               cat("c2", "Tulokkaat, Mupi L2"))
    assert [c["id"] for c in rm.categories_for_event(s, TULO)] == ["c1", "c2"]
    report = rm.match_teams(s, [team("Blue Herons", event=TULO)],
                            {"c1": [row(1, "Northern Lights", "NLK")]})
    assert not report["assignments"] and not report["withdrawn"]
    assert "matches 2 categories" in report["unmatched"][0]["reason"]
