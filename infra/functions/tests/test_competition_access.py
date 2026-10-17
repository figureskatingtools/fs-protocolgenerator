"""Per-competition access: the platform registry decides who may see or change
a competition, and every route that names one enforces it.

The two platform tables `platform_access.py` reads are faked
(`useridentities`: tid_oid -> UserId; `competitionaccess`: platform guid /
UserId -> Role). Each route is called as a caller with no grant, a read, write
or owner grant, and as an admin:

- no grant (or an unknown identity, an unknown or deleted record, a legacy
  record without PlatformId for a non-admin) -> 404 platform_not_found;
- a read grant on a write route -> 403 platform_read_only;
- access tables unreadable -> 503 platform_unavailable;

and a refused call writes nothing. Competition names are invented.
"""
import copy
import json

import azure.functions as func
import pytest
from azure.core.exceptions import ResourceNotFoundError

import function_app as fa
import platform_access as pa
import platform_clock as pc
import storage_helpers as sh
from conftest import OID, TID, auth_headers, http_routes

PID = "0b5c7e0e-6f3a-4d55-9c1e-2a7d8b9c0d11"
OTHER_PID = "7d1f2e3a-4b5c-4d6e-8f90-a1b2c3d4e5f6"
USER_ID = "c0ffee00-1234-4abc-8def-0123456789ab"
COMP = "abc12345"
REAL_IDENTITIES_CLIENT = pa.get_identities_table_client
REAL_ACCESS_CLIENT = pa.get_access_table_client


# ── fakes ─────────────────────────────────────────────────────────────────────

class FakeAccessTable:
    """`get_entity` over a dict, like the SDK: a missing row raises
    ResourceNotFoundError; `error` makes every read fail."""

    def __init__(self, rows=None, error=None):
        self.rows = dict(rows or {})
        self.error = error
        self.reads = []

    def get_entity(self, partition_key, row_key):
        self.reads.append((partition_key, row_key))
        if self.error is not None:
            raise self.error
        if (partition_key, row_key) not in self.rows:
            raise ResourceNotFoundError("The specified resource does not exist.")
        return dict(self.rows[(partition_key, row_key)])


class Platform:
    def __init__(self, monkeypatch):
        self.identities = FakeAccessTable(
            {("IDENTITY", f"{TID}_{OID}"): {"UserId": USER_ID}})
        self.access = FakeAccessTable()
        monkeypatch.setattr(pa, "get_identities_table_client", lambda: self.identities)
        monkeypatch.setattr(pa, "get_access_table_client", lambda: self.access)

    def grant(self, role, platform_id=PID, user_id=USER_ID):
        self.access.rows[(platform_id, user_id)] = {"Role": role}

    def fail(self, error):
        self.identities.error = error
        self.access.error = error


@pytest.fixture
def platform(monkeypatch):
    # No platform clock in these tests (resolve binds without a date).
    monkeypatch.setattr(pc, "get_platform_table_client", lambda: None)
    return Platform(monkeypatch)


class RegistryTable:
    """The tool's own `competitions` table, for list/resolve: rows by RowKey."""

    def __init__(self, rows):
        self.rows = rows
        self.updates = []
        self.created = []

    def create_table(self):
        pass

    def get_entity(self, partition_key, row_key):
        if row_key not in self.rows:
            raise ResourceNotFoundError(row_key)
        return dict(self.rows[row_key])

    def create_entity(self, entity):
        self.created.append(dict(entity))
        self.rows[entity["RowKey"]] = dict(entity)

    def update_entity(self, entity, mode=None):
        self.updates.append(dict(entity))
        self.rows.setdefault(entity["RowKey"], {}).update(entity)

    def query_entities(self, query):
        rows = [dict(r) for r in self.rows.values()]
        if "PlatformId eq '" in query:
            pid = query.split("PlatformId eq '")[1].rstrip("'")
            rows = [r for r in rows if r.get("PlatformId") == pid]
        return rows


@pytest.fixture
def world(storage, platform, monkeypatch):
    """One bound competition (`COMP` -> PID) in the tool's storage, every write
    path recorded so a refused call can be shown to have changed nothing."""
    storage.competition(COMP, PlatformId=PID)
    registry = RegistryTable(storage.rows)
    monkeypatch.setattr(sh, "get_table_client",
                        lambda table_name="generatedprotocols":
                        registry if table_name == "competitions" else None)
    storage.registry = registry
    storage.deleted = []
    monkeypatch.setattr(fa, "_delete_competition_data",
                        lambda entity, by: storage.deleted.append(entity["RowKey"]) or 0)
    storage.pool_reads = []

    def _pool(entity, filename, source):
        storage.pool_reads.append(filename)
        return None, sh.json_response({"error": "pool_file_not_found"}, 404)

    monkeypatch.setattr(fa, "_read_pool_file", _pool)
    storage.generated = []
    monkeypatch.setattr(fa, "assemble_protocol",
                        lambda structure, get_bytes: storage.generated.append(1) or b"%PDF")
    return storage


def snapshot(world):
    return (copy.deepcopy(world.structures), dict(world.container.blobs),
            copy.deepcopy(world.rows), len(world.registry.updates),
            len(world.registry.created), list(world.deleted), list(world.pool_reads),
            list(world.generated))


# ── requests ──────────────────────────────────────────────────────────────────

def _req(method, route, headers, params=None, body=None):
    if isinstance(body, (dict, list)):
        body = json.dumps(body).encode("utf-8")
    return func.HttpRequest(method, f"/api/{route}", headers=headers,
                            params=params or {}, body=body or b"")


def _calls(comp_id):
    """route name -> request builder(headers) for that competition id."""
    return {
        "get_competition_details": lambda h: _req("GET", "get_competition_details", h, {"id": comp_id}),
        "get_file": lambda h: _req("GET", "get_file", h, {"competition": comp_id, "fileId": "file_x"}),
        "delete_competition": lambda h: _req("POST", "delete_competition", h, {"id": comp_id}),
        "save_event_settings": lambda h: _req("POST", "save_event_settings", h,
                                              body={"id": comp_id, "event": {"title": "Hacked"}}),
        "upload_file": lambda h: _req("POST", "upload_file", h,
                                      {"competition": comp_id, "filename": "a.pdf"}, b"%PDF-1.4"),
        "import_platform_file": lambda h: _req("POST", "import_platform_file", h,
                                               {"competition": comp_id, "name": "a.pdf"}),
        "assign_file": lambda h: _req("POST", "assign_file", h,
                                      body={"id": comp_id, "fileId": None,
                                            "target": {"kind": "cover"}}),
        "delete_file": lambda h: _req("POST", "delete_file", h,
                                      {"competition": comp_id, "fileId": "file_x"}),
        "parse_schedule": lambda h: _req("POST", "parse_schedule", h,
                                         {"competition": comp_id, "poolName": "s.xml"}),
        "import_rosters": lambda h: _req("POST", "import_rosters", h,
                                         body={"id": comp_id, "teamsPool": {"name": "t.xml"}}),
        "upload_fallback_photos": lambda h: _req("POST", "upload_fallback_photos", h,
                                                 {"competition": comp_id}, b"not a zip"),
        "edit_structure": lambda h: _req("POST", "edit_structure", h,
                                         body={"id": comp_id, "op": "add_category",
                                               "name": "Hacked"}),
        "generate_protocol": lambda h: _req("POST", "generate_protocol", h, body={"id": comp_id}),
        "delete_protocol": lambda h: _req("POST", "delete_protocol", h,
                                          {"competition": comp_id, "fileName": "p.pdf"}),
    }


NEEDS = {
    "get_competition_details": "read",
    "get_file": "read",
    "delete_competition": "write",
    "save_event_settings": "write",
    "upload_file": "write",
    "import_platform_file": "write",
    "assign_file": "write",
    "delete_file": "write",
    "parse_schedule": "write",
    "import_rosters": "write",
    "upload_fallback_photos": "write",
    "edit_structure": "write",
    "generate_protocol": "write",
    "delete_protocol": "write",
}

# Routes that do not name a competition record, or are covered separately below.
NOT_IN_MATRIX = {"health", "check_user_permission", "list_competitions", "resolve_competition"}


def call(name, headers, comp_id=COMP):
    handler = getattr(fa, name)._function.get_user_function()
    return handler(_calls(comp_id)[name](headers))


def error_code(response):
    try:
        return json.loads(response.get_body()).get("error")
    except Exception:
        return None


def assert_refused(response, status, code):
    assert response.status_code == status, response.get_body()
    assert error_code(response) == code


def assert_allowed(response):
    """Past the gate: whatever the route then answers, it is not a refusal."""
    assert response.status_code != 401, response.get_body()
    assert error_code(response) not in ("platform_not_found", "platform_read_only",
                                        "platform_unavailable"), response.get_body()


def user(**kw):
    return auth_headers(admin=False, **kw)


def test_every_http_route_is_classified():
    """A new route must be added to the matrix (and given a role need)."""
    assert set(http_routes()) == set(NEEDS) | NOT_IN_MATRIX


# ── the role matrix ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", sorted(NEEDS))
def test_no_grant_is_404_and_writes_nothing(world, name):
    before = snapshot(world)
    assert_refused(call(name, user()), 404, "platform_not_found")
    assert snapshot(world) == before


@pytest.mark.parametrize("name", sorted(NEEDS))
def test_a_grant_on_another_competition_is_404(world, platform, name):
    platform.grant("owner", platform_id=OTHER_PID)
    assert_refused(call(name, user()), 404, "platform_not_found")


@pytest.mark.parametrize("name", sorted(NEEDS))
def test_read_grant(world, platform, name):
    platform.grant("read")
    before = snapshot(world)
    response = call(name, user())
    if NEEDS[name] == "read":
        assert_allowed(response)
    else:
        assert_refused(response, 403, "platform_read_only")
        assert snapshot(world) == before


@pytest.mark.parametrize("role", ["write", "owner"])
@pytest.mark.parametrize("name", sorted(NEEDS))
def test_write_and_owner_grants_pass_everywhere(world, platform, name, role):
    platform.grant(role)
    assert_allowed(call(name, user()))


@pytest.mark.parametrize("name", sorted(NEEDS))
def test_an_admin_passes_without_any_lookup(world, platform, name):
    platform.fail(AssertionError("an admin needs no lookup"))
    assert_allowed(call(name, auth_headers(admin=True)))
    assert platform.identities.reads == [] and platform.access.reads == []


@pytest.mark.parametrize("name", sorted(NEEDS))
def test_an_unknown_identity_is_404(world, platform, name):
    platform.grant("owner")
    other = "99999999-8888-4777-8666-555555555555"
    assert_refused(call(name, user(oid=other)), 404, "platform_not_found")


@pytest.mark.parametrize("name", sorted(NEEDS))
def test_unreadable_access_tables_are_503_and_write_nothing(world, platform, name):
    platform.fail(RuntimeError("network down"))
    before = snapshot(world)
    assert_refused(call(name, user()), 503, "platform_unavailable")
    assert snapshot(world) == before


def test_a_missing_access_table_is_503(world, platform):
    platform.access.error = ResourceNotFoundError("TableNotFound: The table does not exist.")
    assert_refused(call("get_competition_details", user()), 503, "platform_unavailable")


def test_an_unconfigured_platform_account_is_503(world, monkeypatch):
    """Fail closed: without PLATFORM_STORAGE_ACCOUNT nobody but an admin gets in."""
    monkeypatch.setattr(pa, "get_identities_table_client", REAL_IDENTITIES_CLIENT)
    monkeypatch.setattr(pa, "get_access_table_client", REAL_ACCESS_CLIENT)
    monkeypatch.delenv("PLATFORM_STORAGE_ACCOUNT", raising=False)
    assert_refused(call("get_competition_details", user()), 503, "platform_unavailable")
    assert_allowed(call("get_competition_details", auth_headers(admin=True)))


@pytest.mark.parametrize("name", sorted(NEEDS))
def test_an_unknown_competition_is_404(world, platform, name):
    platform.grant("owner")
    assert_refused(call(name, user(), comp_id="nosuch00"), 404, "platform_not_found")


def test_a_soft_deleted_record_is_404_even_for_an_admin(world):
    world.rows[COMP]["Visible"] = False
    assert_refused(call("get_competition_details", auth_headers(admin=True)),
                   404, "platform_not_found")


@pytest.mark.parametrize("name", sorted(NEEDS))
def test_a_legacy_unbound_record_is_admin_only(world, platform, name):
    del world.rows[COMP]["PlatformId"]
    platform.grant("owner")
    before = snapshot(world)
    assert_refused(call(name, user()), 404, "platform_not_found")
    assert snapshot(world) == before
    assert_allowed(call(name, auth_headers(admin=True)))


def test_a_read_grant_sees_the_details(world, platform):
    platform.grant("read")
    response = call("get_competition_details", user())
    assert response.status_code == 200
    assert json.loads(response.get_body())["structure"]["id"] == COMP


def test_a_write_grant_changes_the_structure(world, platform):
    platform.grant("write")
    response = call("edit_structure", user())
    assert response.status_code == 200
    folder = world.rows[COMP]["FolderPath"]
    assert [c["name"] for c in world.structures[folder]["categories"]] == ["Hacked"]


def test_a_write_grant_deletes_the_record(world, platform):
    platform.grant("write")
    assert call("delete_competition", user()).status_code == 200
    assert world.deleted == [COMP]


def test_a_revoked_grant_stops_working_once_the_cache_expires(world, platform):
    platform.grant("write")
    assert_allowed(call("edit_structure", user()))
    del platform.access.rows[(PID, USER_ID)]
    pa.clear_cache()   # stands in for the 30 s expiry
    assert_refused(call("edit_structure", user()), 404, "platform_not_found")


def test_mixed_case_ids_still_match_the_grant(world, platform):
    world.rows[COMP]["PlatformId"] = PID.upper()
    platform.grant("read")
    assert_allowed(call("get_competition_details", user(tid=TID.upper(), oid=OID.upper())))


# ── resolve_competition ───────────────────────────────────────────────────────

def resolve(headers, platform_id=PID, name="Spring Trophy 2026", dates="", venue=""):
    req = _req("POST", "resolve_competition", headers,
               body={"platformId": platform_id, "name": name, "dates": dates, "venue": venue})
    return fa.resolve_competition._function.get_user_function()(req)


@pytest.fixture
def empty(storage, platform, monkeypatch):
    registry = RegistryTable(storage.rows)
    monkeypatch.setattr(sh, "get_table_client",
                        lambda table_name="generatedprotocols":
                        registry if table_name == "competitions" else None)
    storage.registry = registry
    return storage


def test_resolve_without_a_grant_is_404_and_creates_nothing(empty, platform):
    assert_refused(resolve(user()), 404, "platform_not_found")
    assert empty.rows == {} and empty.structures == {}


def test_resolve_with_a_read_grant_creates_an_empty_bound_record(empty, platform):
    platform.grant("read")
    response = resolve(user(email="viewer@example.com"))
    assert response.status_code == 200, response.get_body()
    body = json.loads(response.get_body())
    assert body["created"] is True
    row = empty.rows[body["id"]]
    assert row["PlatformId"] == PID
    assert row["CreatedBy"] == "viewer@example.com"   # audit only


def test_resolve_with_a_read_grant_never_renames_or_backfills(empty, platform):
    empty.competition(COMP, PlatformId=PID)
    platform.grant("read")
    before = snapshot_resolve(empty)
    body = json.loads(resolve(user(), name="Renamed By Viewer", dates="2026-05-01",
                              venue="Somewhere").get_body())
    assert (body["id"], body["name"], body["created"]) == (COMP, "Spring Trophy 2026", False)
    assert snapshot_resolve(empty) == before


def snapshot_resolve(world):
    return copy.deepcopy(world.rows), copy.deepcopy(world.structures), len(world.registry.updates)


def test_resolve_with_a_write_grant_follows_a_rename(empty, platform):
    empty.competition(COMP, PlatformId=PID)
    platform.grant("write")
    body = json.loads(resolve(user(), name="Autumn Trophy 2026").get_body())
    assert body["name"] == "Autumn Trophy 2026"
    assert empty.rows[COMP]["Name"] == "Autumn Trophy 2026"


def test_resolve_never_adopts_an_unbound_record_by_name(empty, platform):
    """The attack the removal closes: name a competition like someone else's
    legacy record and walk off with it."""
    empty.competition("legacy00", name="Spring Trophy 2026")
    platform.grant("owner")
    body = json.loads(resolve(user()).get_body())
    assert body["created"] is True and body["id"] != "legacy00"
    assert "PlatformId" not in empty.rows["legacy00"]


def test_resolve_is_503_when_access_cannot_be_checked(empty, platform):
    platform.fail(RuntimeError("throttled"))
    assert_refused(resolve(user()), 503, "platform_unavailable")
    assert empty.rows == {}


def test_resolve_by_an_admin_needs_no_grant(empty, platform):
    platform.fail(AssertionError("an admin needs no lookup"))
    assert json.loads(resolve(auth_headers(admin=True)).get_body())["created"] is True


# ── list_competitions ─────────────────────────────────────────────────────────

def listed(headers):
    req = _req("GET", "list_competitions", headers)
    response = fa.list_competitions._function.get_user_function()(req)
    return response


@pytest.fixture
def three(empty):
    empty.competition("mine0000", name="Mine", PlatformId=PID)
    empty.competition("other000", name="Other", PlatformId=OTHER_PID)
    empty.competition("legacy00", name="Legacy")
    empty.competition("gone0000", name="Gone", PlatformId=PID, Visible=False)
    return empty


def test_the_list_holds_only_competitions_the_caller_can_read(three, platform):
    platform.grant("read")
    response = listed(user())
    assert response.status_code == 200
    assert [c["id"] for c in json.loads(response.get_body())] == ["mine0000"]


def test_the_list_is_empty_without_grants(three, platform):
    assert json.loads(listed(user()).get_body()) == []


def test_an_admin_lists_everything_visible(three, platform):
    platform.fail(AssertionError("an admin needs no lookup"))
    ids = sorted(c["id"] for c in json.loads(listed(auth_headers(admin=True)).get_body()))
    assert ids == ["legacy00", "mine0000", "other000"]


def test_the_list_is_503_when_access_cannot_be_checked(three, platform):
    platform.fail(RuntimeError("network down"))
    assert_refused(listed(user()), 503, "platform_unavailable")
