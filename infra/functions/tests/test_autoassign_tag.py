"""The `autoAssigned` tag — how a file placed by filename recognition is marked
as *not yet verified by a human*, and how that mark goes away.

The frontend recognizes an FSM export's filename and uploads it straight into
the slot it belongs in (`slotKind=...&autoAssigned=1`). The chip then renders an
amber "auto" pill until somebody confirms it. Two rules carry the whole feature:
a placement that did **not** happen (a stale category id sends the file to the
tray) must never be tagged, and moving a chip by hand is the confirmation — so a
manual `assign_file`, including a move back to the tray, clears the tag.
"""
import json

import azure.functions as func
import pytest

import function_app as fa
import structure as st

EMAIL = "organizer@example.com"
PDF = b"%PDF-1.4 fake"


@pytest.fixture
def comp(storage):
    """A competition with one category holding one segment."""
    structure = storage.competition()
    cat = st.new_category("Naiset", "single", 0)
    cat["segments"].append(st.new_segment("Lyhytohjelma", 0))
    structure["categories"].append(cat)
    return structure


def category(structure):
    return structure["categories"][0]


def segment(structure):
    return structure["categories"][0]["segments"][0]


def upload(filename="FSKWSINGLES_ISUPanelofJudgesandTechnicalPanel.pdf",
           body=PDF, email=EMAIL, **params):
    query = {"competition": "abc12345", "filename": filename}
    query.update({k: v for k, v in params.items() if v is not None})
    req = func.HttpRequest(
        "POST", "/api/upload_file",
        headers={"x-forwarded-user-email": email} if email else {},
        params=query, body=body)
    return fa.upload_file._function.get_user_function()(req)


def assign(file_id, target, **extra):
    body = {"id": "abc12345", "fileId": file_id, "target": target}
    body.update(extra)
    req = func.HttpRequest(
        "POST", "/api/assign_file",
        headers={"x-forwarded-user-email": EMAIL},
        body=json.dumps(body).encode("utf-8"))
    return fa.assign_file._function.get_user_function()(req)


def payload(response):
    assert response.status_code == 200, response.get_body()
    return json.loads(response.get_body())


def panel_target(structure):
    """The segment's Panel slot — where a recognized ISU panel PDF belongs."""
    return {"kind": "segment", "role": "panel",
            "categoryId": category(structure)["id"],
            "segmentId": segment(structure)["id"]}


def panel_params(structure, **extra):
    params = dict(panel_target(structure), slotKind="segment")
    params.pop("kind")
    params.update(extra)
    return params


# ── upload ───────────────────────────────────────────────────────────────────

def test_a_recognized_upload_fills_its_slot_and_is_tagged(comp):
    body = payload(upload(**panel_params(comp, autoAssigned="1")))
    assert segment(comp)["panelPdf"] == body["fileId"]
    assert comp["files"][body["fileId"]]["autoAssigned"] is True
    assert body["file"]["autoAssigned"] is True


def test_the_tag_survives_the_response_and_the_stored_structure_alike(comp):
    body = payload(upload(**panel_params(comp, autoAssigned="true")))
    assert body["file"] == comp["files"][body["fileId"]]


def test_an_upload_that_falls_back_to_the_tray_is_never_tagged(comp):
    """A stale category id (the structure changed under the planner) must leave
    the file in the tray *untagged* — there is no placement to confirm."""
    params = panel_params(comp, autoAssigned="1")
    params["categoryId"] = "cat-gone"
    body = payload(upload(**params))
    file_id = body["fileId"]
    assert file_id in comp["files"]
    assert file_id not in st.assigned_file_ids(comp)
    assert "autoAssigned" not in comp["files"][file_id]
    assert "autoAssigned" not in body["file"]


def test_a_manual_upload_into_a_slot_carries_no_tag(comp):
    """No `autoAssigned` param = the user dropped the file there themselves."""
    body = payload(upload(**panel_params(comp)))
    assert segment(comp)["panelPdf"] == body["fileId"]
    assert "autoAssigned" not in comp["files"][body["fileId"]]


def test_a_plain_tray_upload_carries_no_tag(comp):
    body = payload(upload())
    assert "autoAssigned" not in comp["files"][body["fileId"]]
    assert st.assigned_file_ids(comp) == set()


def test_an_unrecognized_flag_value_does_not_tag(comp):
    body = payload(upload(**panel_params(comp, autoAssigned="0")))
    assert "autoAssigned" not in comp["files"][body["fileId"]]


# ── manual moves clear the tag ───────────────────────────────────────────────

def test_moving_a_tagged_file_to_another_slot_clears_the_tag(comp):
    file_id = payload(upload(**panel_params(comp, autoAssigned="1")))["fileId"]
    assert payload(assign(file_id, {"kind": "categoryTitle",
                                    "categoryId": category(comp)["id"]})) == {"ok": True}
    assert category(comp)["titlePdf"] == file_id
    assert segment(comp)["panelPdf"] is None
    assert "autoAssigned" not in comp["files"][file_id]


def test_moving_a_tagged_file_back_to_the_tray_clears_the_tag(comp):
    file_id = payload(upload(**panel_params(comp, autoAssigned="1")))["fileId"]
    payload(assign(file_id, {"kind": "tray"}))
    assert st.assigned_file_ids(comp) == set()
    assert "autoAssigned" not in comp["files"][file_id]


def test_clearing_a_slot_leaves_the_other_files_alone(comp):
    """A null fileId clears the target slot; no file's tag is touched."""
    file_id = payload(upload(**panel_params(comp, autoAssigned="1")))["fileId"]
    payload(assign(None, {"kind": "categoryTitle", "categoryId": category(comp)["id"]}))
    assert comp["files"][file_id]["autoAssigned"] is True


# ── the body setter (pool import / future auto-place button) ─────────────────

def test_an_assign_that_declares_itself_automatic_sets_the_tag(comp):
    file_id = payload(upload())["fileId"]
    payload(assign(file_id, panel_target(comp), autoAssigned=True))
    assert segment(comp)["panelPdf"] == file_id
    assert comp["files"][file_id]["autoAssigned"] is True


def test_a_later_manual_move_still_clears_a_body_set_tag(comp):
    file_id = payload(upload())["fileId"]
    payload(assign(file_id, panel_target(comp), autoAssigned=True))
    payload(assign(file_id, {"kind": "categoryTitle", "categoryId": category(comp)["id"]}))
    assert "autoAssigned" not in comp["files"][file_id]


# ── auth ─────────────────────────────────────────────────────────────────────

def test_an_unauthenticated_upload_is_rejected(comp):
    assert upload(email=None).status_code == 401
