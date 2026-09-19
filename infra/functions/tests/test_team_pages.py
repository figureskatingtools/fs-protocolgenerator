"""`structure` — resolving the synchro team-page settings and their free-text rows.

Two settings (create the page at all, and how much of the roster to print) live
competition-wide, may be defaulted per category and overridden per team, where
`None` means "inherit" at both lower levels. Nothing is copied down, so moving a
category moves every team that has not spoken for itself. The reason every read
goes through a resolver is backward compatibility: a metadata.json written before
the feature existed carries none of the keys, and must keep rendering exactly as
it did — pages on, full names — without a migration.

The free-text rows ("Theme: Spies") are a flat per-team list printed in stored
order. They used to hang off a segment; rows written then still carry a
`segmentId`, so what the tests pin down is that the leftover key is ignored
rather than a reason to lose the row.
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


def rows(category, *entries):
    """Put `entries` on the category's team and read them back in print order."""
    team = category["teams"][0]
    team["textFields"] = list(entries)
    return st.team_text_rows(team)


# ── resolution ────────────────────────────────────────────────────────────────

def test_a_new_competition_creates_team_pages_with_full_names(comp, category):
    team = category["teams"][0]
    assert st.team_page_enabled(comp, category, team) is True
    assert st.team_name_mode(comp, category, team) == "full"


def test_a_structure_written_before_the_feature_keeps_todays_behaviour(comp, category):
    del comp["teamPages"]
    for key in ("pageEnabled", "nameMode"):
        del category[key]
    team = category["teams"][0]
    for key in ("pageEnabled", "nameMode", "textFields"):
        del team[key]
    assert st.team_page_enabled(comp, category, team) is True
    assert st.team_name_mode(comp, category, team) == "full"
    assert st.team_text_rows(team) == []


def test_a_team_without_overrides_follows_the_competition_default(comp, category):
    comp["teamPages"] = {"enabled": False, "nameMode": "firstNames"}
    team = category["teams"][0]
    assert st.team_page_enabled(comp, category, team) is False
    assert st.team_name_mode(comp, category, team) == "firstNames"


def test_a_team_override_wins_over_the_competition_default(comp, category):
    comp["teamPages"] = {"enabled": True, "nameMode": "full"}
    team = category["teams"][0]
    team["pageEnabled"] = False
    team["nameMode"] = "none"
    assert st.team_page_enabled(comp, category, team) is False
    assert st.team_name_mode(comp, category, team) == "none"


def test_a_team_can_opt_back_in_when_the_competition_default_is_off(comp, category):
    comp["teamPages"] = {"enabled": False, "nameMode": "none"}
    team = category["teams"][0]
    team["pageEnabled"] = True
    team["nameMode"] = "full"
    assert st.team_page_enabled(comp, category, team) is True
    assert st.team_name_mode(comp, category, team) == "full"


def test_an_unknown_name_mode_falls_back_to_the_inherited_one(comp, category):
    comp["teamPages"] = {"enabled": True, "nameMode": "firstNames"}
    team = category["teams"][0]
    team["nameMode"] = "surnames-only"
    assert st.team_name_mode(comp, category, team) == "firstNames"


def test_an_unknown_competition_name_mode_falls_back_to_full(comp):
    comp["teamPages"] = {"enabled": True, "nameMode": ""}
    assert st.team_pages_defaults(comp)["nameMode"] == "full"


def test_coerce_name_mode_reads_anything_unknown_as_inherit():
    assert st.coerce_name_mode("none") == "none"
    assert st.coerce_name_mode("") is None
    assert st.coerce_name_mode(None) is None
    assert st.coerce_name_mode("FULL") is None


# ── the category level ────────────────────────────────────────────────────────

def test_a_new_category_inherits_the_competition_default(comp, category):
    comp["teamPages"] = {"enabled": False, "nameMode": "none"}
    assert st.category_page_enabled(comp, category) is False
    assert st.category_name_mode(comp, category) == "none"


def test_a_category_override_wins_over_the_competition_default(comp, category):
    comp["teamPages"] = {"enabled": True, "nameMode": "full"}
    category["pageEnabled"] = False
    category["nameMode"] = "firstNames"
    assert st.category_page_enabled(comp, category) is False
    assert st.category_name_mode(comp, category) == "firstNames"


def test_a_category_setting_reaches_a_team_that_has_not_overridden(comp, category):
    category["pageEnabled"] = False
    category["nameMode"] = "none"
    team = category["teams"][0]
    assert st.team_page_enabled(comp, category, team) is False
    assert st.team_name_mode(comp, category, team) == "none"


def test_a_team_override_wins_over_its_category(comp, category):
    category["pageEnabled"] = False
    category["nameMode"] = "none"
    team = category["teams"][0]
    team["pageEnabled"] = True
    team["nameMode"] = "full"
    assert st.team_page_enabled(comp, category, team) is True
    assert st.team_name_mode(comp, category, team) == "full"


def test_a_category_can_switch_its_teams_back_on(comp, category):
    # The competition default is off, so only the middle level can produce this.
    comp["teamPages"] = {"enabled": False, "nameMode": "none"}
    category["pageEnabled"] = True
    category["nameMode"] = "full"
    assert st.team_page_enabled(comp, category, category["teams"][0]) is True
    assert st.team_name_mode(comp, category, category["teams"][0]) == "full"


def test_an_unknown_category_name_mode_is_read_as_inherit(comp, category):
    comp["teamPages"] = {"enabled": True, "nameMode": "firstNames"}
    category["nameMode"] = "surnames-only"
    assert st.category_name_mode(comp, category) == "firstNames"


def test_a_category_written_before_the_level_existed_inherits(comp, category):
    del category["pageEnabled"]
    del category["nameMode"]
    comp["teamPages"] = {"enabled": False, "nameMode": "firstNames"}
    assert st.category_page_enabled(comp, category) is False
    assert st.category_name_mode(comp, category) == "firstNames"


# ── free-text rows ────────────────────────────────────────────────────────────

def test_rows_print_in_the_order_they_are_stored(category):
    out = rows(category,
               {"id": "a", "label": "Theme", "value": "Spies"},
               {"id": "b", "label": "Coach", "value": "M. Virta"})
    assert [r["label"] for r in out] == ["Theme", "Coach"]


def test_a_row_comes_back_as_plain_label_and_value(category):
    out = rows(category, {"id": "a", "label": "Theme", "value": "Spies"})
    assert out == [{"label": "Theme", "value": "Spies"}]


def test_a_row_stored_with_a_segment_still_prints(category):
    # Rows written before they became a flat list carry a segmentId — ignored,
    # never a reason to lose the row.
    out = rows(category, {"id": "a", "segmentId": category["segments"][0]["id"],
                          "label": "Theme", "value": "Spies"})
    assert out == [{"label": "Theme", "value": "Spies"}]


def test_a_row_blank_on_both_sides_is_not_printed(category):
    out = rows(category,
               {"id": "a", "label": "  ", "value": ""},
               {"id": "b", "label": "Coach", "value": ""})
    assert [r["label"] for r in out] == ["Coach"]


def test_a_team_with_no_rows_at_all_reads_as_empty():
    assert st.team_text_rows({}) == []
    assert st.team_text_rows(st.new_team("HTK", "Helsinki Finettes")) == []


# ── sanitizing what the client sends ──────────────────────────────────────────

def test_sanitize_mints_an_id_for_a_new_row():
    out = st.sanitize_text_fields([{"label": "Theme", "value": "Spies"}])
    assert out[0]["id"].startswith("fld-")


def test_sanitize_keeps_an_id_the_client_already_has():
    out = st.sanitize_text_fields([{"id": "fld-1234", "label": "Theme", "value": "x"}])
    assert out[0]["id"] == "fld-1234"


def test_sanitize_strips_a_legacy_segment_id():
    out = st.sanitize_text_fields(
        [{"id": "fld-1", "segmentId": "seg-1234", "label": "Theme", "value": "Spies"}])
    assert out == [{"id": "fld-1", "label": "Theme", "value": "Spies"}]


def test_sanitize_trims_and_length_caps_both_sides():
    out = st.sanitize_text_fields([{"label": "  Theme  ", "value": "x" * 200}])
    assert out[0]["label"] == "Theme"
    assert len(out[0]["value"]) == st.TEXT_VALUE_MAX


def test_sanitize_drops_blank_rows_and_caps_the_list():
    sent = [{"label": "", "value": ""}] + \
           [{"label": f"L{i}", "value": "v"} for i in range(st.MAX_TEAM_TEXT_FIELDS + 5)]
    out = st.sanitize_text_fields(sent)
    assert len(out) == st.MAX_TEAM_TEXT_FIELDS
    assert out[0]["label"] == "L0"


def test_sanitize_keeps_the_order_it_was_sent_in():
    out = st.sanitize_text_fields([{"label": "Theme", "value": "Spies"},
                                   {"label": "Coach", "value": "M. Virta"}])
    assert [r["label"] for r in out] == ["Theme", "Coach"]


def test_sanitize_ignores_a_non_dict_row():
    assert st.sanitize_text_fields(["nope", None]) == []
