"""`function_app.edit_structure` — the `set_category` op's optional fields.

Every field is optional and only applied when the key is actually present, so
the distinction that matters is *absent* (leave whatever is there alone) versus
*present but empty* (a deliberate clear). `code` — the ISU/FSM event-code
abbreviation a category carries for re-import matching — follows that rule too,
trimmed and length-capped on the way in.
"""
import json

import azure.functions as func
import pytest

import function_app as fa
import structure as st

EMAIL = "organizer@example.com"


@pytest.fixture
def comp(storage):
    """A competition with one category to edit."""
    structure = storage.competition()
    structure["categories"].append(st.new_category("Naiset", "single", 0))
    return structure


def edit(comp_id="abc12345", email=EMAIL, **body):
    body.setdefault("id", comp_id)
    req = func.HttpRequest(
        "POST", "/api/edit_structure",
        headers={"x-forwarded-user-email": email} if email else {},
        body=json.dumps(body).encode())
    return fa.edit_structure._function.get_user_function()(req)


def category(response, index=0):
    assert response.status_code == 200, response.get_body()
    return json.loads(response.get_body())["structure"]["categories"][index]


def test_set_category_stamps_a_code(comp):
    cat_id = comp["categories"][0]["id"]
    assert category(edit(op="set_category", categoryId=cat_id, code="FSKWSINGLES")) \
        ["code"] == "FSKWSINGLES"
    assert comp["categories"][0]["code"] == "FSKWSINGLES"


def test_a_code_is_trimmed_and_capped(comp):
    cat_id = comp["categories"][0]["id"]
    long_code = "  " + "A" * 40 + "  "
    assert category(edit(op="set_category", categoryId=cat_id, code=long_code))["code"] == "A" * 32


def test_an_edit_without_a_code_leaves_the_existing_one_alone(comp):
    cat = comp["categories"][0]
    cat["code"] = "FSKXPAIRS"
    edited = category(edit(op="set_category", categoryId=cat["id"], name="Pariluistelu"))
    assert edited["name"] == "Pariluistelu"
    assert edited["code"] == "FSKXPAIRS"


def test_an_explicit_empty_code_clears_it(comp):
    cat = comp["categories"][0]
    cat["code"] = "FSKXPAIRS"
    assert category(edit(op="set_category", categoryId=cat["id"], code=""))["code"] == ""


def test_setting_a_code_on_an_unknown_category_is_a_404(comp):
    assert edit(op="set_category", categoryId="cat-gone", code="FSKXPAIRS").status_code == 404


def test_an_unauthenticated_edit_is_rejected(comp):
    cat_id = comp["categories"][0]["id"]
    assert edit(op="set_category", categoryId=cat_id, code="FSKXPAIRS", email=None).status_code == 401
