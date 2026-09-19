"""`structure` — resolving the synchro team-page settings and their free-text rows.

Two settings (create the page at all, and how much of the roster to print) live
competition-wide and may be overridden per team, where `None` means "inherit".
The reason every read goes through a resolver is backward compatibility: a
metadata.json written before the feature existed carries neither key, and must
keep rendering exactly as it did — pages on, full names — without a migration.

The free-text rows ("Free Skating theme: Spies") are stored on the team but
printed grouped by segment, so ordering and a stale segmentId are what the tests
pin down: a row must never disappear because its segment was renamed away.
"""
import pytest

import structure as st


@pytest.fixture
def category():
    cat = st.new_category("SM-seniorit", "synchro", 0)
    cat["segments"] = [st.new_segment("Free Skating", 1), st.new_segment("Short Program", 0)]
    cat["teams"] = [st.new_team("HTK", "Helsinki Finettes")]
    return cat


@pytest.fixture
def comp(category):
    structure = st.new_structure("abc12345", "Spring Trophy", "", "o@example.com", "")
    structure["categories"].append(category)
    return structure


def seg(category, name):
    return next(s["id"] for s in category["segments"] if s["name"] == name)


def rows(category, *entries):
    """Put `entries` on the category's team and read them back in print order."""
    team = category["teams"][0]
    team["textFields"] = list(entries)
    return st.team_text_rows(category, team)


# ── resolution ────────────────────────────────────────────────────────────────

def test_a_new_competition_creates_team_pages_with_full_names(comp):
    team = comp["categories"][0]["teams"][0]
    assert st.team_page_enabled(comp, team) is True
    assert st.team_name_mode(comp, team) == "full"


def test_a_structure_written_before_the_feature_keeps_todays_behaviour(comp):
    del comp["teamPages"]
    team = comp["categories"][0]["teams"][0]
    for key in ("pageEnabled", "nameMode", "textFields"):
        del team[key]
    assert st.team_page_enabled(comp, team) is True
    assert st.team_name_mode(comp, team) == "full"
    assert st.team_text_rows(comp["categories"][0], team) == []


def test_a_team_without_overrides_follows_the_competition_default(comp):
    comp["teamPages"] = {"enabled": False, "nameMode": "firstNames"}
    team = comp["categories"][0]["teams"][0]
    assert st.team_page_enabled(comp, team) is False
    assert st.team_name_mode(comp, team) == "firstNames"


def test_a_team_override_wins_over_the_competition_default(comp):
    comp["teamPages"] = {"enabled": True, "nameMode": "full"}
    team = comp["categories"][0]["teams"][0]
    team["pageEnabled"] = False
    team["nameMode"] = "none"
    assert st.team_page_enabled(comp, team) is False
    assert st.team_name_mode(comp, team) == "none"


def test_a_team_can_opt_back_in_when_the_competition_default_is_off(comp):
    comp["teamPages"] = {"enabled": False, "nameMode": "none"}
    team = comp["categories"][0]["teams"][0]
    team["pageEnabled"] = True
    team["nameMode"] = "full"
    assert st.team_page_enabled(comp, team) is True
    assert st.team_name_mode(comp, team) == "full"


def test_an_unknown_name_mode_falls_back_to_the_inherited_one(comp):
    comp["teamPages"] = {"enabled": True, "nameMode": "firstNames"}
    team = comp["categories"][0]["teams"][0]
    team["nameMode"] = "surnames-only"
    assert st.team_name_mode(comp, team) == "firstNames"


def test_an_unknown_competition_name_mode_falls_back_to_full(comp):
    comp["teamPages"] = {"enabled": True, "nameMode": ""}
    assert st.team_pages_defaults(comp)["nameMode"] == "full"


def test_coerce_name_mode_reads_anything_unknown_as_inherit():
    assert st.coerce_name_mode("none") == "none"
    assert st.coerce_name_mode("") is None
    assert st.coerce_name_mode(None) is None
    assert st.coerce_name_mode("FULL") is None


# ── free-text rows ────────────────────────────────────────────────────────────

def test_team_level_rows_come_before_the_segment_ones(category):
    free = seg(category, "Free Skating")
    out = rows(category,
               {"id": "a", "segmentId": free, "label": "Theme", "value": "Spies"},
               {"id": "b", "segmentId": None, "label": "Coach", "value": "M. Virta"})
    assert [r["label"] for r in out] == ["Coach", "Theme"]


def test_segment_groups_follow_the_segments_own_order(category):
    # Short Program has order 0, Free Skating order 1 — the list order is the other
    # way round, so only the segment order can produce this result.
    out = rows(category,
               {"id": "a", "segmentId": seg(category, "Free Skating"),
                "label": "Theme", "value": "Spies"},
               {"id": "b", "segmentId": seg(category, "Short Program"),
                "label": "Theme", "value": "Tango"})
    assert [r["value"] for r in out] == ["Tango", "Spies"]
    assert [r["segment"] for r in out] == ["Short Program", "Free Skating"]


def test_rows_of_one_segment_keep_their_insertion_order(category):
    free = seg(category, "Free Skating")
    out = rows(category,
               {"id": "a", "segmentId": free, "label": "Theme", "value": "Spies"},
               {"id": "b", "segmentId": free, "label": "Music", "value": "Goldfinger"})
    assert [r["label"] for r in out] == ["Theme", "Music"]


def test_a_row_pointing_at_a_removed_segment_degrades_to_team_level(category):
    out = rows(category, {"id": "a", "segmentId": "seg-gone",
                          "label": "Theme", "value": "Spies"})
    assert out == [{"segment": "", "label": "Theme", "value": "Spies"}]


def test_a_row_blank_on_both_sides_is_not_printed(category):
    out = rows(category,
               {"id": "a", "segmentId": None, "label": "  ", "value": ""},
               {"id": "b", "segmentId": None, "label": "Coach", "value": ""})
    assert [r["label"] for r in out] == ["Coach"]


# ── sanitizing what the client sends ──────────────────────────────────────────

def test_sanitize_mints_an_id_for_a_new_row(category):
    out = st.sanitize_text_fields([{"label": "Theme", "value": "Spies"}], category)
    assert out[0]["id"].startswith("fld-")
    assert out[0]["segmentId"] is None


def test_sanitize_keeps_an_id_the_client_already_has(category):
    out = st.sanitize_text_fields([{"id": "fld-1234", "label": "Theme", "value": "x"}],
                                  category)
    assert out[0]["id"] == "fld-1234"


def test_sanitize_drops_a_segment_id_the_category_does_not_have(category):
    out = st.sanitize_text_fields(
        [{"segmentId": "seg-gone", "label": "Theme", "value": "Spies"}], category)
    assert out[0]["segmentId"] is None


def test_sanitize_trims_and_length_caps_both_sides(category):
    out = st.sanitize_text_fields(
        [{"label": "  Theme  ", "value": "x" * 200}], category)
    assert out[0]["label"] == "Theme"
    assert len(out[0]["value"]) == st.TEXT_VALUE_MAX


def test_sanitize_drops_blank_rows_and_caps_the_list(category):
    sent = [{"label": "", "value": ""}] + \
           [{"label": f"L{i}", "value": "v"} for i in range(st.MAX_TEAM_TEXT_FIELDS + 5)]
    out = st.sanitize_text_fields(sent, category)
    assert len(out) == st.MAX_TEAM_TEXT_FIELDS
    assert out[0]["label"] == "L0"


def test_sanitize_ignores_a_non_dict_row(category):
    assert st.sanitize_text_fields(["nope", None], category) == []
