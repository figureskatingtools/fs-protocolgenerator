"""`function_app.parse_schedule` — parsing the schedule the organizer never had
to drop on the page.

FS Manager pushes the whole competition file set into the platform's shared pool
over HOVTP, the schedule print among them (`…_CompetitionSchedule.pdf`; its
`DT_SCHEDULE_FSK….xml` is not used — it carries FSM's standard English names, which
FSM's custom name file rewrites in the PDFs only). So besides the browser
upload (the file as the request body) the route takes a *pool reference*:
`poolName` + optional `source`, empty body. The bytes then travel exactly the
path `import_platform_file` uses — the folder comes from the competition's bound
PlatformId, never from the client — and everything after that (the
force-to-rebuild gate, keeping `schedule.pdf`, the event auto-fill) is the same
code as an upload. Anything but a PDF is refused either way.
"""
import json

import azure.functions as func
import pytest

import function_app as fa
import storage_helpers as sh
import structure as st

from conftest import FakeContainerClient, auth_headers, schedule_pdf

EMAIL = "organizer@example.com"
SCHEDULE_NAME = "FSK-------------------------------_CompetitionSchedule.pdf"
XML_NAME = "DT_SCHEDULE_FSK-------------------------------.xml"
KEPT = "Spring Trophy 2026-abc12345/schedule.pdf"
KEPT_XML = "Spring Trophy 2026-abc12345/schedule.xml"


def schedule(category="Naiset"):
    """One category over two days: its two rows merge into one category with
    two segments."""
    return schedule_pdf(
        "05.01.2026", f"15:00:0016:21:00  12  {category}   Lyhytohjelma",
        "06.01.2026", f"12:00:0014:00:00  12  {category}   Vapaaohjelma")


POOL_PDF = schedule()
FSM_PDF = schedule("Miehet")
# FSM's XML of the same schedule, in its standard English.
SCHEDULE_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<OdfBody DocumentType="DT_SCHEDULE">
  <Competition Code="FSK-------------------------------">
    <Unit Code="FSKWSINGLES-----------QUAL000100--" StartDate="2026-01-05T15:00:00">
      <ItemName Value="Women Short Program" />
    </Unit>
  </Competition>
</OdfBody>
"""


@pytest.fixture
def comp(storage):
    """An empty competition bound to platform competition `p-1`."""
    return storage.competition(PlatformId="p-1")


@pytest.fixture
def pool(monkeypatch):
    """The platform's competition-data container: the same schedule name sits in
    both pool folders, with different contents, so a test can tell them apart."""
    fake = FakeContainerClient({f"p-1/uploads/{SCHEDULE_NAME}": POOL_PDF,
                                f"p-1/fsm/{SCHEDULE_NAME}": FSM_PDF,
                                f"p-1/fsm/{XML_NAME}": SCHEDULE_XML})
    monkeypatch.setattr(sh, "get_platform_container_client", lambda: fake)
    return fake


def parse(body=b"", comp_id="abc12345", email=EMAIL, **params):
    query = {"competition": comp_id} if comp_id is not None else {}
    query.update({k: v for k, v in params.items() if v is not None})
    req = func.HttpRequest(
        "POST", "/api/parse_schedule",
        headers=auth_headers(email) if email else {},
        params=query, body=body)
    return fa.parse_schedule._function.get_user_function()(req)


def payload(response):
    assert response.status_code == 200, response.get_body()
    return json.loads(response.get_body())


def category_names(structure):
    return [c["name"] for c in structure.get("categories", [])]


# ── the pool reference ───────────────────────────────────────────────────────

def test_a_pool_schedule_builds_the_structure(comp, pool, storage):
    body = payload(parse(poolName=SCHEDULE_NAME))
    assert body["categories"] == 1
    assert body["rows"] == 2
    # The route says which pool file it used, so the UI can label the parse.
    assert body["source"] == {"poolName": SCHEDULE_NAME, "source": "upload"}
    assert category_names(comp) == ["Naiset"]
    assert [s["name"] for s in comp["categories"][0]["segments"]] == \
        ["Lyhytohjelma", "Vapaaohjelma"]
    assert comp["scheduleParsed"] is True
    # Kept for re-parsing — same as a body upload.
    assert storage.container.blobs[KEPT] == POOL_PDF
    # The schedule's own auto-fill still runs.
    assert comp["event"]["dates"] == "05.01.2026 – 06.01.2026"


def test_the_fsm_source_reads_the_listener_folder(comp, pool, storage):
    body = payload(parse(poolName=SCHEDULE_NAME, source="fsm"))
    assert body["source"] == {"poolName": SCHEDULE_NAME, "source": "fsm"}
    assert category_names(comp) == ["Miehet"]
    assert storage.container.blobs[KEPT] == FSM_PDF


def test_the_pool_path_is_built_from_the_binding_not_from_the_client(comp, pool):
    payload(parse(poolName=f"../../p-2/fsm/{SCHEDULE_NAME}"))
    assert category_names(comp) == ["Naiset"]


def test_a_missing_pool_file_is_a_404(comp, pool):
    response = parse(poolName="NeverPushed_CompetitionSchedule.pdf")
    assert response.status_code == 404
    assert json.loads(response.get_body())["error"] == "pool_file_not_found"
    assert comp.get("categories") == []


def test_an_unknown_source_is_rejected(comp, pool):
    for bad in ("archive", "../uploads"):
        response = parse(poolName=SCHEDULE_NAME, source=bad)
        assert response.status_code == 400
        assert b"invalid_source" in response.get_body()
    assert comp.get("categories") == []


def test_an_unbound_competition_has_no_pool(storage, pool):
    structure = storage.competition()          # no PlatformId
    response = parse(poolName=SCHEDULE_NAME)
    assert response.status_code == 409
    assert json.loads(response.get_body())["error"] == "not_bound"
    assert structure.get("categories") == []


# ── the force gate is the same for both inputs ───────────────────────────────

def test_existing_categories_block_a_pool_parse(comp, pool, storage):
    comp["categories"].append(st.new_category("Hand-made", "single", 0))
    response = parse(poolName=SCHEDULE_NAME)
    assert response.status_code == 409
    assert category_names(comp) == ["Hand-made"]
    assert KEPT not in storage.container.blobs


def test_force_replaces_the_categories_from_the_pool(comp, pool):
    comp["categories"].append(st.new_category("Hand-made", "single", 0))
    payload(parse(poolName=SCHEDULE_NAME, force="true"))
    assert category_names(comp) == ["Naiset"]


# ── the body upload is untouched ─────────────────────────────────────────────

def test_a_body_upload_still_parses(comp, storage):
    body = payload(parse(body=POOL_PDF))
    assert body["categories"] == 1
    assert "source" not in body
    assert category_names(comp) == ["Naiset"]
    assert storage.container.blobs[KEPT] == POOL_PDF


def test_a_body_upload_wins_over_a_pool_name(comp, pool, storage):
    """A dropped file is what the user just chose — the pool reference on the
    same request is ignored rather than second-guessed."""
    payload(parse(body=FSM_PDF, poolName=SCHEDULE_NAME))
    assert category_names(comp) == ["Miehet"]


def test_an_empty_request_is_a_bad_request(comp, pool):
    assert parse().status_code == 400


def test_an_unauthenticated_parse_is_rejected(comp, pool):
    assert parse(poolName=SCHEDULE_NAME, email=None).status_code == 401


# ── the PDF only ─────────────────────────────────────────────────────────────

def not_pdf(response):
    assert response.status_code == 400, response.get_body()
    assert json.loads(response.get_body())["error"] == "schedule_not_pdf"


def test_a_dt_schedule_xml_upload_is_refused(comp, storage):
    not_pdf(parse(body=SCHEDULE_XML))
    assert comp.get("categories") == []
    assert storage.container.blobs == {}


def test_a_dt_schedule_xml_in_the_pool_is_refused(comp, pool, storage):
    not_pdf(parse(poolName=XML_NAME, source="fsm"))
    assert comp.get("categories") == []
    assert storage.container.blobs == {}


def test_a_pdf_parse_drops_the_xml_an_earlier_parse_kept(comp, storage):
    storage.container.blobs[KEPT_XML] = SCHEDULE_XML
    payload(parse(body=POOL_PDF))
    assert KEPT_XML not in storage.container.blobs
    assert storage.container.blobs[KEPT] == POOL_PDF
