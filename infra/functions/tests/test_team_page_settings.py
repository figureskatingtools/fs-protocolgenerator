"""`function_app.edit_structure` — the team-page settings ops.

`set_team_pages` carries the competition-wide default, `set_category` a
category's, `set_team` a team's overrides and its free-text rows. The distinction
the tests pin down is the tri-state the two lower levels share: they store `None`
to mean "inherit", so an explicit `null` has to be a way of *clearing* an
override, not a way of turning the page off. The competition-wide setting has no
inherit state, so a value it cannot use falls back to "full" instead.

`textFields` is a whole-list replace, like `members`, so the route is also what
assigns a new row its id — and what strips the `segmentId` rows carried while
they were still segment-attached.
"""
import json

import azure.functions as func
import pytest

import function_app as fa
import structure as st

EMAIL = "organizer@example.com"


@pytest.fixture
def comp(storage):
    """A synchro category with one team and one segment."""
    structure = storage.competition()
    cat = st.new_category("SM-seniorit", "synchro", 0)
    cat["segments"].append(st.new_segment("Free Skating", 0))
    cat["teams"].append(st.new_team("HTK", "Helsinki Finettes"))
    structure["categories"].append(cat)
    return structure


def edit(comp_id="abc12345", email=EMAIL, **body):
    body.setdefault("id", comp_id)
    req = func.HttpRequest(
        "POST", "/api/edit_structure",
        headers={"x-forwarded-user-email": email} if email else {},
        body=json.dumps(body).encode())
    return fa.edit_structure._function.get_user_function()(req)


def saved(response) -> dict:
    assert response.status_code == 200, response.get_body()
    return json.loads(response.get_body())["structure"]


def cat_of(structure) -> dict:
    return structure["categories"][0]


def team_of(structure) -> dict:
    return structure["categories"][0]["teams"][0]


def set_category(comp, **fields):
    return saved(edit(op="set_category", categoryId=cat_of(comp)["id"], **fields))


def set_team(comp, **fields):
    cat = cat_of(comp)
    return saved(edit(op="set_team", categoryId=cat["id"],
                      teamId=cat["teams"][0]["id"], **fields))


# ── the competition-wide default ──────────────────────────────────────────────

def test_set_team_pages_turns_the_pages_off(comp):
    assert saved(edit(op="set_team_pages", enabled=False))["teamPages"]["enabled"] is False


def test_set_team_pages_stores_a_name_mode(comp):
    out = saved(edit(op="set_team_pages", nameMode="firstNames"))
    assert out["teamPages"]["nameMode"] == "firstNames"


def test_set_team_pages_leaves_the_key_it_was_not_given_alone(comp):
    saved(edit(op="set_team_pages", nameMode="none"))
    out = saved(edit(op="set_team_pages", enabled=False))
    assert out["teamPages"] == {"enabled": False, "nameMode": "none"}


def test_an_unusable_name_mode_falls_back_to_full(comp):
    # No inherit state at competition level, so None is not an option here.
    out = saved(edit(op="set_team_pages", nameMode="surnames-only"))
    assert out["teamPages"]["nameMode"] == "full"


def test_set_team_pages_works_on_a_structure_written_before_the_feature(comp):
    del comp["teamPages"]
    out = saved(edit(op="set_team_pages", enabled=False))
    assert out["teamPages"] == {"enabled": False, "nameMode": "full"}


# ── the category default ──────────────────────────────────────────────────────

def test_a_category_can_skip_all_of_its_teams(comp):
    out = cat_of(set_category(comp, pageEnabled=False))
    assert out["pageEnabled"] is False


def test_a_category_can_carry_its_own_name_mode(comp):
    assert cat_of(set_category(comp, nameMode="firstNames"))["nameMode"] == "firstNames"


def test_an_explicit_null_returns_the_category_to_the_competition_default(comp):
    set_category(comp, pageEnabled=False, nameMode="none")
    out = set_category(comp, pageEnabled=None, nameMode=None)
    assert cat_of(out)["pageEnabled"] is None
    assert cat_of(out)["nameMode"] is None
    assert st.category_page_enabled(out, cat_of(out)) is True


def test_an_unknown_category_name_mode_is_read_as_inherit(comp):
    assert cat_of(set_category(comp, nameMode="surnames-only"))["nameMode"] is None


def test_setting_the_category_default_leaves_its_other_fields_alone(comp):
    out = cat_of(set_category(comp, pageEnabled=False))
    assert out["name"] == "SM-seniorit"
    assert out["discipline"] == "synchro"
    assert len(out["teams"]) == 1


def test_a_category_default_reaches_a_team_that_has_not_overridden(comp):
    out = set_category(comp, pageEnabled=False, nameMode="none")
    assert st.team_page_enabled(out, cat_of(out), team_of(out)) is False
    assert st.team_name_mode(out, cat_of(out), team_of(out)) == "none"


def test_a_team_override_survives_a_category_change(comp):
    set_team(comp, pageEnabled=True, nameMode="full")
    out = set_category(comp, pageEnabled=False, nameMode="none")
    assert st.team_page_enabled(out, cat_of(out), team_of(out)) is True
    assert st.team_name_mode(out, cat_of(out), team_of(out)) == "full"


# ── per-team overrides ────────────────────────────────────────────────────────

def test_a_team_can_be_skipped(comp):
    assert team_of(set_team(comp, pageEnabled=False))["pageEnabled"] is False


def test_a_team_can_carry_its_own_name_mode(comp):
    assert team_of(set_team(comp, nameMode="none"))["nameMode"] == "none"


def test_an_explicit_null_returns_the_team_to_the_default(comp):
    set_team(comp, pageEnabled=False, nameMode="none")
    out = set_team(comp, pageEnabled=None, nameMode=None)
    assert team_of(out)["pageEnabled"] is None
    assert team_of(out)["nameMode"] is None
    assert st.team_page_enabled(out, cat_of(out), team_of(out)) is True


def test_an_unknown_team_name_mode_is_read_as_inherit(comp):
    assert team_of(set_team(comp, nameMode="surnames-only"))["nameMode"] is None


def test_setting_one_override_leaves_the_roster_alone(comp):
    set_team(comp, members=["KORHONEN Anna"])
    assert team_of(set_team(comp, pageEnabled=False))["members"] == ["KORHONEN Anna"]


# ── free-text rows ────────────────────────────────────────────────────────────

def test_a_new_text_row_comes_back_with_an_id(comp):
    rows = team_of(set_team(comp, textFields=[{"label": "Theme", "value": "Spies"}]))["textFields"]
    assert rows[0]["id"].startswith("fld-")
    assert rows[0]["label"] == "Theme"


def test_a_stored_row_carries_no_segment(comp):
    seg_id = cat_of(comp)["segments"][0]["id"]
    rows = team_of(set_team(comp, textFields=[
        {"segmentId": seg_id, "label": "Theme", "value": "Spies"}]))["textFields"]
    assert "segmentId" not in rows[0]


def test_the_rows_keep_the_order_they_were_sent_in(comp):
    rows = team_of(set_team(comp, textFields=[
        {"label": "Theme", "value": "Spies"},
        {"label": "Coach", "value": "M. Virta"}]))["textFields"]
    assert [r["label"] for r in rows] == ["Theme", "Coach"]


def test_sending_text_rows_replaces_the_whole_list(comp):
    set_team(comp, textFields=[{"label": "Theme", "value": "Spies"}])
    rows = team_of(set_team(comp, textFields=[{"label": "Coach", "value": "M. Virta"}]))["textFields"]
    assert [r["label"] for r in rows] == ["Coach"]


def test_an_empty_row_is_not_stored(comp):
    rows = team_of(set_team(comp, textFields=[{"label": " ", "value": ""}]))["textFields"]
    assert rows == []


# ── the usual route guards ────────────────────────────────────────────────────

def test_an_unknown_team_is_a_404(comp):
    cat = cat_of(comp)
    res = edit(op="set_team", categoryId=cat["id"], teamId="team-gone", nameMode="none")
    assert res.status_code == 404


def test_an_unknown_category_is_a_404(comp):
    assert edit(op="set_category", categoryId="cat-gone",
                pageEnabled=False).status_code == 404


def test_the_ops_need_a_proxy_identity(comp):
    assert edit(op="set_team_pages", enabled=False, email=None).status_code == 401


def test_a_roster_re_import_keeps_a_teams_overrides(comp):
    # `_upsert_team` refreshes only the imported fields, so the organizer's
    # team-page choices must survive re-importing the DT_PARTIC pair.
    cat = cat_of(comp)
    set_team(comp, pageEnabled=False, nameMode="none",
             textFields=[{"label": "Theme", "value": "Spies"}])
    fa._upsert_team(comp, cat, {"code": "T1", "name": "Helsinki Finettes",
                                "org": "HTK", "event": "MLSENI----",
                                "members": ["KORHONEN Anna"]})
    team = cat["teams"][0]
    assert team["pageEnabled"] is False
    assert team["nameMode"] == "none"
    assert [r["label"] for r in team["textFields"]] == ["Theme"]
    assert team["members"] == ["KORHONEN Anna"]
