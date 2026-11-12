"""Every FS Manager export placed into a category teaches it its ISU code.

The frontend used to learn a category's code from the Judge Papers table and
`set_category` it — the bare abbreviation only, so two blocks of one event ended
up with the same code. Now the backend reads the full 22-character code off the
filename on the very call that places the file (`upload_file` /
`import_platform_file` with a slot, `assign_file`), and the code decides the
discipline. `set_category code` stays as the manual escape hatch, but can no
longer undo a block code with the bare one.
"""
import json

import azure.functions as func
import pytest

import function_app as fa
import structure as st
from conftest import auth_headers

EMAIL = "organizer@example.com"
PDF = b"%PDF-1.4 fake"


@pytest.fixture
def comp(storage):
    """Two Finnish-named blocks of one event, no codes, both typed single."""
    structure = storage.competition()
    for i, name in enumerate(("SM-Noviisit L1", "SM-Noviisit L2")):
        cat = st.new_category(name, "single", i)
        cat["segments"].append(st.new_segment("Free Skating", 0))
        structure["categories"].append(cat)
    return structure


def upload(filename, structure, slot_kind, **params):
    cat = structure["categories"][0]
    query = {"competition": "abc12345", "filename": filename, "slotKind": slot_kind,
             "categoryId": cat["id"]}
    if slot_kind == "segment":
        query.update(segmentId=cat["segments"][0]["id"], role="results")
    query.update(params)
    req = func.HttpRequest("POST", "/api/upload_file", headers=auth_headers(EMAIL),
                           params=query, body=PDF)
    response = fa.upload_file._function.get_user_function()(req)
    assert response.status_code == 200, response.get_body()
    return json.loads(response.get_body())["fileId"]


def assign(file_id, target):
    req = func.HttpRequest("POST", "/api/assign_file", headers=auth_headers(EMAIL),
                           body=json.dumps({"id": "abc12345", "fileId": file_id,
                                            "target": target}).encode())
    response = fa.assign_file._function.get_user_function()(req)
    assert response.status_code == 200, response.get_body()


def edit(**body):
    body.setdefault("id", "abc12345")
    req = func.HttpRequest("POST", "/api/edit_structure", headers=auth_headers(EMAIL),
                           body=json.dumps(body).encode())
    response = fa.edit_structure._function.get_user_function()(req)
    assert response.status_code == 200, response.get_body()


def test_a_segment_upload_stamps_the_code_and_the_discipline(comp):
    upload("FSKXSYNCHRONADVNOV--01FNL-000100--_SegmentResults.pdf", comp, "segment")
    cat = comp["categories"][0]
    assert cat["code"] == "FSKXSYNCHRONADVNOV--01" and cat["discipline"] == "synchro"
    assert comp["categories"][1]["code"] == ""


def test_a_total_results_upload_stamps_too(comp):
    upload("FSKXSYNCHRONADVNOV----------------_Results.pdf", comp, "totalResults")
    assert comp["categories"][0]["code"] == "FSKXSYNCHRONADVNOV"


def test_a_manual_assign_stamps_the_code(comp):
    file_id = upload("FSKXSYNCHRONADVNOV--01FNL-000100--_ISUPanelofJudgesandTechnicalPanel.pdf",
                     comp, "cover")       # lands nowhere useful first
    cat = comp["categories"][1]
    assign(file_id, {"kind": "segment", "role": "panel", "categoryId": cat["id"],
                     "segmentId": cat["segments"][0]["id"]})
    assert cat["code"] == "FSKXSYNCHRONADVNOV--01" and cat["discipline"] == "synchro"


def test_a_block_file_upgrades_a_bare_code_a_conflicting_one_is_ignored(comp):
    cat = comp["categories"][0]
    cat["code"] = "FSKXSYNCHRONADVNOV"
    upload("FSKXSYNCHRONADVNOV--01FNL-000100--_SegmentResults.pdf", comp, "segment")
    assert cat["code"] == "FSKXSYNCHRONADVNOV--01"
    upload("FSKXSYNCHRONJUNIOR----FNL-000100--_SegmentResults.pdf", comp, "segment")
    assert cat["code"] == "FSKXSYNCHRONADVNOV--01"


def test_a_hand_named_head_page_still_sets_the_discipline(comp):
    upload("FSKXSYNCHRON-head.pdf", comp, "categoryTitle")
    cat = comp["categories"][0]
    assert cat["code"] == "" and cat["discipline"] == "synchro"


def test_a_photo_or_a_plain_pdf_teaches_nothing(comp):
    upload("results_final.pdf", comp, "totalResults")
    cat = comp["categories"][0]
    assert cat["code"] == "" and cat["discipline"] == "single"


def test_set_category_code_decides_the_discipline_and_can_be_cleared(comp):
    cat = comp["categories"][0]
    edit(op="set_category", categoryId=cat["id"], code="fskxsynchronadvnov")
    assert cat["code"] == "FSKXSYNCHRONADVNOV" and cat["discipline"] == "synchro"
    edit(op="set_category", categoryId=cat["id"], code="")
    assert cat["code"] == ""


def test_set_category_code_never_downgrades_a_block_code(comp):
    """The old site stamps the table's bare abbreviation right after the
    backend read `…--01` off the filename; that must not win."""
    cat = comp["categories"][0]
    cat["code"] = "FSKXSYNCHRONADVNOV--01"
    edit(op="set_category", categoryId=cat["id"], code="FSKXSYNCHRONADVNOV")
    assert cat["code"] == "FSKXSYNCHRONADVNOV--01"
    # A different code is a correction, not a downgrade.
    edit(op="set_category", categoryId=cat["id"], code="FSKXSYNCHRONJUNIOR")
    assert cat["code"] == "FSKXSYNCHRONJUNIOR"
