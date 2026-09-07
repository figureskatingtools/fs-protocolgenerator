"""`function_app.parse_schedule` — parsing the schedule the organizer never had
to drop on the page.

FS Manager pushes the whole competition file set into the platform's shared pool
over HOVTP, the schedule among them (`DT_SCHEDULE_FSK….xml`, or the
`…_CompetitionSchedule.pdf` when only the print exists). So besides the browser
upload (the file as the request body) the route takes a *pool reference*:
`poolName` + optional `source`, empty body. The bytes then travel exactly the
path `import_platform_file` uses — the folder comes from the competition's bound
PlatformId, never from the client — and everything after that (the
force-to-rebuild gate, keeping `schedule.xml|pdf`, the event auto-fill) is the
same code as an upload.
"""
import json

import azure.functions as func
import pytest

import function_app as fa
import storage_helpers as sh
import structure as st

from conftest import FakeContainerClient

EMAIL = "organizer@example.com"
SCHEDULE_NAME = "DT_SCHEDULE_FSK-------------------------------.xml"


def schedule_xml(category="Naiset", rink="Tikkurilan jäähalli"):
    """The smallest DT_SCHEDULE `parse_schedule_xml` reads: two units sharing a
    category code (so they merge into one category with two segments)."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<OdfBody DocumentType="DT_SCHEDULE">
  <Competition Code="FSK-------------------------------">
    <Unit Code="FSKWSINGLES-----------QUAL000100--" StartDate="2026-01-05T15:00:00">
      <ItemName Value="{category} Short Program" />
      <VenueDescription VenueName="{rink}" />
    </Unit>
    <Unit Code="FSKWSINGLES-----------FNL-000100--" StartDate="2026-01-06T12:00:00">
      <ItemName Value="{category} Free Skating" />
      <VenueDescription VenueName="{rink}" />
    </Unit>
  </Competition>
</OdfBody>
""".encode("utf-8")


POOL_XML = schedule_xml()
FSM_XML = schedule_xml("Miehet", "Oulunkylän jäähalli")


@pytest.fixture
def comp(storage):
    """An empty competition bound to platform competition `p-1`."""
    return storage.competition(PlatformId="p-1")


@pytest.fixture
def pool(monkeypatch):
    """The platform's competition-data container: the same schedule name sits in
    both pool folders, with different contents, so a test can tell them apart."""
    fake = FakeContainerClient({f"p-1/uploads/{SCHEDULE_NAME}": POOL_XML,
                                f"p-1/fsm/{SCHEDULE_NAME}": FSM_XML})
    monkeypatch.setattr(sh, "get_platform_container_client", lambda: fake)
    return fake


def parse(body=b"", comp_id="abc12345", email=EMAIL, **params):
    query = {"competition": comp_id} if comp_id is not None else {}
    query.update({k: v for k, v in params.items() if v is not None})
    req = func.HttpRequest(
        "POST", "/api/parse_schedule",
        headers={"x-forwarded-user-email": email} if email else {},
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
        ["Short Program", "Free Skating"]
    assert comp["scheduleParsed"] is True
    # Kept for re-parsing, under the XML name — same as a body upload.
    assert storage.container.blobs["Spring Trophy 2026-abc12345/schedule.xml"] == POOL_XML
    # The schedule's own auto-fill still runs.
    assert comp["event"]["rink"] == "Tikkurilan jäähalli"
    assert comp["event"]["dates"] == "05.01.2026 – 06.01.2026"


def test_the_fsm_source_reads_the_listener_folder(comp, pool, storage):
    body = payload(parse(poolName=SCHEDULE_NAME, source="fsm"))
    assert body["source"] == {"poolName": SCHEDULE_NAME, "source": "fsm"}
    assert category_names(comp) == ["Miehet"]
    assert storage.container.blobs["Spring Trophy 2026-abc12345/schedule.xml"] == FSM_XML


def test_the_pool_path_is_built_from_the_binding_not_from_the_client(comp, pool):
    payload(parse(poolName=f"../../p-2/fsm/{SCHEDULE_NAME}"))
    assert category_names(comp) == ["Naiset"]


def test_a_missing_pool_file_is_a_404(comp, pool):
    response = parse(poolName="DT_SCHEDULE_NeverPushed.xml")
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
    assert "Spring Trophy 2026-abc12345/schedule.xml" not in storage.container.blobs


def test_force_replaces_the_categories_from_the_pool(comp, pool):
    comp["categories"].append(st.new_category("Hand-made", "single", 0))
    payload(parse(poolName=SCHEDULE_NAME, force="true"))
    assert category_names(comp) == ["Naiset"]


# ── the body upload is untouched ─────────────────────────────────────────────

def test_a_body_upload_still_parses(comp, storage):
    body = payload(parse(body=POOL_XML))
    assert body["categories"] == 1
    assert "source" not in body
    assert category_names(comp) == ["Naiset"]
    assert storage.container.blobs["Spring Trophy 2026-abc12345/schedule.xml"] == POOL_XML


def test_a_body_upload_wins_over_a_pool_name(comp, pool, storage):
    """A dropped file is what the user just chose — the pool reference on the
    same request is ignored rather than second-guessed."""
    payload(parse(body=FSM_XML, poolName=SCHEDULE_NAME))
    assert category_names(comp) == ["Miehet"]


def test_an_empty_request_is_a_bad_request(comp, pool):
    assert parse().status_code == 400


def test_an_unauthenticated_parse_is_rejected(comp, pool):
    assert parse(poolName=SCHEDULE_NAME, email=None).status_code == 401
