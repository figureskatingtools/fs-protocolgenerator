"""`function_app._upsert_team` / `_apply_assignments` — applying a match report.

The upsert is competition-wide on purpose: a re-import must never duplicate a
team, and a team the previous pass put in the wrong block has to *move* while
keeping its id and both photo slots (those are the operator's work). Team names
are invented.
"""
import function_app as fa


def cat(cid, name):
    return {"id": cid, "name": name, "code": "", "discipline": "single", "teams": []}


def struct(*cats):
    return {"categories": list(cats)}


def parsed(name, org="BHK", event="FSKXSYNCHRONMLTULO----", code="SYNCHRO0001",
           members=("AHLBERG Bertil",)):
    return {"code": code, "name": name, "org": org, "event": event,
            "members": list(members)}


def test_upsert_creates_a_team():
    s = struct(cat("c1", "Tulokkaat, Mupi L1"))
    p = parsed("Blue Herons")
    assert fa._upsert_team(s, s["categories"][0], p) is False
    team, = s["categories"][0]["teams"]
    assert (team["name"], team["org"], team["code"]) == ("Blue Herons", "BHK", "SYNCHRO0001")
    assert team["members"] == ["AHLBERG Bertil"]
    assert team["id"].startswith("team-")


def test_re_importing_the_same_team_is_a_no_op():
    s = struct(cat("c1", "Tulokkaat, Mupi L1"))
    c1 = s["categories"][0]
    fa._upsert_team(s, c1, parsed("Blue Herons"))
    team_id = c1["teams"][0]["id"]
    assert fa._upsert_team(s, c1, parsed("Blue Herons")) is False
    assert len(c1["teams"]) == 1
    assert c1["teams"][0]["id"] == team_id


def test_moving_a_team_preserves_its_id_and_photos():
    s = struct(cat("c1", "Tulokkaat, Mupi L1"), cat("c2", "Tulokkaat, Mupi L2"))
    c1, c2 = s["categories"]
    fa._upsert_team(s, c1, parsed("Blue Herons"))
    team = c1["teams"][0]
    team["photo"] = "file-photo"
    team["photoFallback"] = "file-fallback"
    original_id = team["id"]

    assert fa._upsert_team(s, c2, parsed("Blue Herons")) is True
    assert c1["teams"] == []
    moved, = c2["teams"]
    assert moved is team
    assert (moved["id"], moved["photo"], moved["photoFallback"]) == \
        (original_id, "file-photo", "file-fallback")


def test_a_renamed_team_is_still_found_by_its_code():
    s = struct(cat("c1", "Tulokkaat, Mupi L1"), cat("c2", "Tulokkaat, Mupi L2"))
    c1, c2 = s["categories"]
    fa._upsert_team(s, c1, parsed("Blue Herons"))
    original_id = c1["teams"][0]["id"]
    fa._upsert_team(s, c2, parsed("Blue Herons Team", members=("KOSKINEN Aino",)))
    moved, = c2["teams"]
    assert moved["id"] == original_id
    assert moved["name"] == "Blue Herons Team"
    assert moved["members"] == ["KOSKINEN Aino"]


# ── the automatic re-match guard ──────────────────────────────────────────────

def _report(*assignments):
    return {"assignments": [{"team": t, "categoryId": cid, "method": m}
                            for t, cid, m in assignments],
            "unmatched": [], "withdrawn": []}


def test_auto_pass_never_moves_a_placed_team_on_a_guess():
    s = struct(cat("c1", "Tulokkaat, Mupi L1"), cat("c2", "Tulokkaat, Mupi L2"))
    c1, c2 = s["categories"]
    placed = parsed("Blue Herons")
    fa._upsert_team(s, c1, placed)

    for method in ("results-fuzzy", "event"):
        imported, moved, touched = fa._apply_assignments(s, _report((placed, "c2", method)),
                                                         auto=True)
        assert (imported, moved, touched) == (0, 0, set())
        assert [t["name"] for t in c1["teams"]] == ["Blue Herons"]
        assert c2["teams"] == []


def test_auto_pass_applies_an_exact_results_move_and_new_placements():
    s = struct(cat("c1", "Tulokkaat, Mupi L1"), cat("c2", "Tulokkaat, Mupi L2"))
    c1, c2 = s["categories"]
    placed = parsed("Blue Herons")
    fa._upsert_team(s, c1, placed)
    fresh = parsed("Silver Comets", org="SCK", code="SYNCHRO0002")

    imported, moved, touched = fa._apply_assignments(
        s, _report((placed, "c2", "results"), (fresh, "c1", "event")), auto=True)

    assert (imported, moved) == (2, 1)
    assert touched == {"c1", "c2"}
    assert [t["name"] for t in c1["teams"]] == ["Silver Comets"]
    assert [t["name"] for t in c2["teams"]] == ["Blue Herons"]
    # Importing rosters proves the category is synchro, whatever the schedule said.
    assert c1["discipline"] == c2["discipline"] == "synchro"


def test_a_manual_import_pass_moves_on_any_method():
    s = struct(cat("c1", "Tulokkaat, Mupi L1"), cat("c2", "Tulokkaat, Mupi L2"))
    c1, c2 = s["categories"]
    placed = parsed("Blue Herons")
    fa._upsert_team(s, c1, placed)

    imported, moved, touched = fa._apply_assignments(s, _report((placed, "c2", "event")))
    assert (imported, moved, touched) == (1, 1, {"c2"})
    assert c1["teams"] == [] and len(c2["teams"]) == 1
