"""`fallback_photos` — name folding and image→team matching.

Accreditation ZIP filenames are ASCII-folded and use `-`/`_` for spaces, while the
roster XML keeps the diacritics, so `normalize` has to bridge both. All names are
invented.
"""
from fallback_photos import match_team, normalize


# ── normalisation ─────────────────────────────────────────────────────────────

def test_diacritics_and_separators_fold_together():
    assert normalize("Crème de Ments") == normalize("Creme-de-Ments") == "creme de ments"
    assert normalize("JääLeidit") == normalize("JaaLeidit") == "jaaleidit"
    assert normalize("Helsinki JääLeidit") == normalize("Helsinki-JaaLeidit")


def test_case_punctuation_and_whitespace_are_dropped():
    assert normalize("  Blue   Herons!  ") == "blue herons"
    assert normalize("Team_Unique (2026)") == "team unique 2026"
    assert normalize(None) == ""


# ── image -> team ─────────────────────────────────────────────────────────────

def team(name, org, photo=None, fallback=None):
    return {"id": f"team-{name}", "name": name, "org": org,
            "photo": photo, "photoFallback": fallback}


def struct(*cats):
    return {"categories": [{"id": cid, "name": name, "teams": list(teams)}
                           for cid, name, teams in cats]}


def test_team_name_is_a_required_gate():
    s = struct(("c1", "Tulokkaat, Mupi L1", [team("Blue Herons", "BHK")]))
    cat, t = match_team(s, normalize("Blue-Herons"), normalize("Whatever"), normalize("Nowhere"))
    assert (cat["id"], t["name"]) == ("c1", "Blue Herons")
    # A club/folder hit can never stand in for the name.
    assert match_team(s, normalize("Golden-Arrows"), normalize("BHK"),
                      normalize("Tulokkaat, Mupi L1")) == (None, None)


def test_folder_name_breaks_a_tie_between_two_categories():
    s = struct(("c1", "Tulokkaat, Mupi L1", [team("Blue Herons", "BHK")]),
               ("c2", "Aikuiset, Mupi L1", [team("Blue Herons", "BHK")]))
    cat, _ = match_team(s, normalize("Blue-Herons"), "", normalize("Aikuiset"))
    assert cat["id"] == "c2"


def test_club_outranks_the_folder_hint():
    s = struct(("c1", "Tulokkaat, Mupi L1", [team("Blue Herons", "BHK")]),
               ("c2", "Aikuiset, Mupi L1", [team("Blue Herons", "XYZ")]))
    cat, t = match_team(s, normalize("Blue-Herons"), normalize("BHK"), normalize("Aikuiset"))
    assert (cat["id"], t["org"]) == ("c1", "BHK")


def test_two_equally_good_teams_stay_unresolved():
    s = struct(("c1", "Tulokkaat, Mupi L1", [team("Blue Herons", "BHK")]),
               ("c2", "Aikuiset, Mupi L1", [team("Blue Herons", "BHK")]))
    assert match_team(s, normalize("Blue-Herons"), "", "") == (None, None)
