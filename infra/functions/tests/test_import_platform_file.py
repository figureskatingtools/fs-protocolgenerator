"""`function_app.import_platform_file` — pulling a file out of the platform's
shared competition file pool into this tool's competition.

The pool lives in the platform's storage account, in two folders this Function
App may only *read*: `competition-data/<platform guid>/uploads/<name>` for what
people uploaded and `.../fsm/<name>` for what the HOVTP listener pushed — the
`source` query param picks between them. The route is
the only bridge, so the awkward cases are the interesting ones: the client names
a file and nothing else (the folder comes from the competition's bound
PlatformId, so a traversal attempt collapses to a basename), the feature is off
until the deployment sets `PLATFORM_STORAGE_ACCOUNT` (503, so the frontend can
fall back to a direct upload), and an unbound competition has no pool at all
(409). Once the bytes are in hand the file is registered exactly like a browser
upload — same slot params, same `autoAssigned` tag, plus `poolName` so the UI
can tell which pool files are already imported.
"""
import json

import azure.functions as func
import pytest
from azure.core.exceptions import ResourceNotFoundError

import function_app as fa
import storage_helpers as sh
import structure as st

from conftest import FakeContainerClient

EMAIL = "organizer@example.com"
POOL_NAME = "FSKWSINGLES-----------QUAL000100--_SegmentResults.pdf"
PDF = b"%PDF-1.4 pooled"
FSM_PDF = b"%PDF-1.4 pushed by the listener"


@pytest.fixture
def comp(storage):
    """A competition bound to platform competition `p-1`, with one category and
    one segment to import into."""
    structure = storage.competition(PlatformId="p-1")
    cat = st.new_category("Naiset", "single", 0)
    cat["segments"].append(st.new_segment("Lyhytohjelma", 0))
    structure["categories"].append(cat)
    return structure


@pytest.fixture
def pool(monkeypatch):
    """The platform's competition-data container, holding one file for `p-1`."""
    fake = FakeContainerClient({f"p-1/uploads/{POOL_NAME}": PDF,
                                f"p-1/fsm/{POOL_NAME}": FSM_PDF})
    monkeypatch.setattr(sh, "get_platform_container_client", lambda: fake)
    return fake


def import_file(name=POOL_NAME, comp_id="abc12345", email=EMAIL, **params):
    query = {k: v for k, v in {"competition": comp_id, "name": name}.items() if v is not None}
    query.update(params)
    req = func.HttpRequest(
        "POST", "/api/import_platform_file",
        headers={"x-forwarded-user-email": email} if email else {},
        params=query, body=b"")
    return fa.import_platform_file._function.get_user_function()(req)


def payload(response):
    assert response.status_code == 200, response.get_body()
    return json.loads(response.get_body())


def error(response, status, code):
    assert response.status_code == status, response.get_body()
    assert json.loads(response.get_body())["error"] == code


def results_params(structure, **extra):
    cat = structure["categories"][0]
    params = {"slotKind": "segment", "role": "results", "categoryId": cat["id"],
              "segmentId": cat["segments"][0]["id"]}
    params.update(extra)
    return params


# ── the happy path ───────────────────────────────────────────────────────────

def test_a_pool_file_is_registered_like_an_upload(comp, pool, storage):
    body = payload(import_file())
    file_id, meta = body["fileId"], body["file"]
    assert meta["filename"] == POOL_NAME
    assert meta["kind"] == "pdf"
    assert meta["size"] == len(PDF)
    assert meta["poolName"] == POOL_NAME
    assert comp["files"][file_id] == meta
    # The bytes are copied into this tool's own container, under the file id.
    assert storage.container.blobs[meta["blob"]] == PDF
    assert meta["blob"] == f"Spring Trophy 2026-abc12345/uploads/{file_id}_{POOL_NAME}"


def test_an_imported_file_can_land_straight_in_a_slot(comp, pool):
    body = payload(import_file(**results_params(comp, autoAssigned="1")))
    segment = comp["categories"][0]["segments"][0]
    assert segment["resultsPdf"] == body["fileId"]
    assert comp["files"][body["fileId"]]["autoAssigned"] is True


def test_an_import_without_the_flag_is_not_tagged(comp, pool):
    body = payload(import_file(**results_params(comp)))
    assert "autoAssigned" not in comp["files"][body["fileId"]]


def test_a_failed_slot_assignment_leaves_the_import_in_the_tray(comp, pool):
    body = payload(import_file(**results_params(comp, categoryId="cat-gone", autoAssigned="1")))
    assert body["fileId"] in comp["files"]
    assert st.assigned_file_ids(comp) == set()
    assert "autoAssigned" not in body["file"]


def test_the_pool_path_is_built_from_the_binding_not_from_the_client(comp, pool):
    """A name carrying a path (or a foreign competition's GUID) is reduced to
    its basename and looked up under *this* competition's platform folder."""
    body = payload(import_file(name=f"../../p-2/uploads/{POOL_NAME}"))
    assert body["file"]["filename"] == POOL_NAME
    assert body["file"]["poolName"] == POOL_NAME


def test_the_default_source_is_the_uploads_folder(comp, pool, storage):
    """No `source` = the folder people upload into, even though the same name
    also sits in the FSM folder."""
    body = payload(import_file())
    assert storage.container.blobs[body["file"]["blob"]] == PDF


def test_an_fsm_source_resolves_the_listener_folder(comp, pool, storage):
    body = payload(import_file(source="fsm"))
    assert storage.container.blobs[body["file"]["blob"]] == FSM_PDF
    assert body["file"]["poolName"] == POOL_NAME


def test_an_unknown_source_is_rejected(comp, pool):
    """Only the two folder names the pool actually has — no client-supplied
    path fragment ever reaches the blob name."""
    error(import_file(source="../uploads"), 400, "invalid_source")
    error(import_file(source="archive"), 400, "invalid_source")


def test_a_missing_pool_file_is_a_404(comp, pool):
    error(import_file(name="NeverPushed.pdf"), 404, "pool_file_not_found")
    error(import_file(name="NeverPushed.pdf", source="fsm"), 404, "pool_file_not_found")


# ── the feature's off switches ───────────────────────────────────────────────

def test_an_unbound_competition_cannot_import(storage, pool):
    storage.competition()          # no PlatformId
    error(import_file(), 409, "not_bound")


def test_an_unconfigured_platform_account_reports_itself(comp, monkeypatch):
    """`PLATFORM_STORAGE_ACCOUNT` unset = the pool feature is simply off."""
    monkeypatch.setattr(sh, "get_platform_container_client", lambda: None)
    error(import_file(), 503, "platform_not_configured")


def test_a_storage_failure_is_a_bad_gateway(comp, monkeypatch):
    class Broken:
        def get_blob_client(self, path):
            raise RuntimeError("connection reset")

    monkeypatch.setattr(sh, "get_platform_container_client", lambda: Broken())
    error(import_file(), 502, "platform_unavailable")


def test_a_client_creation_failure_is_unavailable_not_unconfigured(comp, monkeypatch):
    """A transient failure while building the pool client must not masquerade
    as the deliberate feature-off 503 — the frontend treats 503 as "stop
    trying the pool", while 502 keeps the pool in play."""
    def boom():
        raise RuntimeError("credential blew up")

    monkeypatch.setattr(sh, "get_platform_container_client", boom)
    error(import_file(), 502, "platform_unavailable")


def test_a_blob_that_vanishes_between_check_and_read_is_a_404(comp, monkeypatch):
    class Vanishing:
        def get_blob_client(self, path):
            class Blob:
                def exists(self_inner):
                    return True

                def download_blob(self_inner):
                    raise ResourceNotFoundError(path)
            return Blob()

    monkeypatch.setattr(sh, "get_platform_container_client", lambda: Vanishing())
    error(import_file(), 404, "pool_file_not_found")


# ── the tool's own limits still apply ────────────────────────────────────────

def test_an_oversized_pool_file_is_rejected(comp, pool, monkeypatch):
    monkeypatch.setattr(sh, "MAX_UPLOAD_SIZE", len(PDF) - 1)
    error(import_file(), 413, "file_too_large")
    assert comp["files"] == {}


def test_an_unsupported_file_type_is_rejected(comp, pool):
    error(import_file(name="notes.txt"), 400, "unsupported_type")


def test_a_missing_parameter_is_a_bad_request(comp, pool):
    error(import_file(name=None), 400, "missing_parameter")


def test_an_unknown_competition_is_a_404(comp, pool):
    error(import_file(comp_id="nosuch00"), 404, "competition_not_found")


def test_an_unauthenticated_import_is_rejected(comp, pool):
    assert import_file(email=None).status_code == 401
