"""Retention follows the platform's clock (`platform_clock.py`).

The site's competition registry owns each competition's deletion date (created
+ 60 days, extended a week at a time on the site only) and mirrors it into its
`retentionclock` table, which is all this tool reads. This tool keeps no clock
of its own: every record is bound to a platform competition (`PlatformId`, a
lowercase GUID), its `DeletionDate` is just a cached copy of the platform's,
and its data is deleted only on the platform's verdict (deleted or purged) —
never on a date alone. Only legacy rows that were never bound still go on
their stored date. The one rule that must never bend: when the platform cannot
be read, is not configured or has no clock row, nothing is deleted.

Deleted rows keep no e-mail address — neither the ones deleted from now on nor
the ones deleted before (the nightly scrub). Both tables are faked with dicts;
ids, names and addresses are invented.
"""
import json
from datetime import datetime, timedelta, timezone

import azure.functions as func
import pytest
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

import function_app as fa
import platform_clock as pc
import storage_helpers as sh
import structure as st
from conftest import auth_headers

PID = "6f1c2a9e-3b4d-4e5f-8a7b-9c0d1e2f3a4b"
NOW = datetime(2026, 10, 13, 3, 0, tzinfo=timezone.utc)
PAST = "2026-10-01T00:00:00Z"
FUTURE = "2026-12-01T00:00:00Z"
ADDRESS = "organizer@example.com"


# ── dict-backed tables ────────────────────────────────────────────────────────

def _parse_filter(query):
    terms = {}
    for clause in query.split(" and "):
        key, _, literal = clause.partition(" eq ")
        terms[key.strip()] = literal.strip()[1:-1].replace("''", "'")
    return terms


class FakeTable:
    def __init__(self):
        self.rows = {}
        self.updates = []

    def create_table(self):
        pass

    def get_entity(self, partition_key, row_key):
        if (partition_key, row_key) not in self.rows:
            raise ResourceNotFoundError(row_key)
        return dict(self.rows[(partition_key, row_key)])

    def create_entity(self, entity):
        self.rows[(entity["PartitionKey"], entity["RowKey"])] = dict(entity)

    def update_entity(self, entity, mode=None):
        self.updates.append(dict(entity))
        self.rows.setdefault((entity["PartitionKey"], entity["RowKey"]), {}).update(entity)

    def query_entities(self, query):
        terms = _parse_filter(query)
        return [dict(row) for row in self.rows.values()
                if all(row.get(key) == value for key, value in terms.items())]

    def delete_entity(self, partition_key, row_key):
        self.rows.pop((partition_key, row_key), None)


class FakePlatformTable:
    """The site's `competitions` table as this app sees it: read-only rows
    keyed by the lowercase platform GUID — or a table that cannot be read."""

    def __init__(self):
        self.rows = {}
        self.error = None
        self.query_error = None
        self.reads = []
        self.queries = []

    def get_entity(self, partition_key, row_key):
        self.reads.append((partition_key, row_key))
        if self.error:
            raise self.error
        if row_key not in self.rows:
            raise ResourceNotFoundError(row_key)
        return dict(self.rows[row_key])

    def query_entities(self, query, select=None, **kwargs):
        self.queries.append(query)
        if self.query_error or self.error:
            raise self.query_error or self.error
        return [dict(row) for row in self.rows.values()
                if f"PartitionKey eq '{row['PartitionKey']}'" == query]


class FakeBlob:
    def __init__(self, name):
        self.name = name


class FakeContainer:
    def __init__(self):
        self.blobs = {}

    def list_blobs(self, name_starts_with=""):
        return [FakeBlob(n) for n in list(self.blobs) if n.startswith(name_starts_with)]

    def delete_blob(self, name):
        self.blobs.pop(name, None)


class World:
    def __init__(self):
        self.comp = FakeTable()
        self.protocols = FakeTable()
        self.platform = FakePlatformTable()
        self.container = FakeContainer()
        self.structures = {}

    def record(self, comp_id="abc12345", name="Spring Trophy 2026", **extra):
        row = {
            "PartitionKey": "GLOBAL", "RowKey": comp_id, "Name": name,
            "FolderPath": f"{name}-{comp_id}", "Visible": True,
            "CreatedBy": ADDRESS, "CreatedDate": "2026-08-01T10:00:00Z",
            "DeletionDate": FUTURE,
        }
        row.update(extra)
        row = {key: value for key, value in row.items() if value is not None}
        self.comp.rows[("GLOBAL", comp_id)] = row
        self.container.blobs[f"{row['FolderPath']}/metadata.json"] = b"{}"
        self.structures[row["FolderPath"]] = st.new_structure(
            comp_id, name, "", ADDRESS, row["CreatedDate"])
        return row

    def platform_row(self, deletion=FUTURE, status="active", purged=None, pid=PID):
        row = {"PartitionKey": "COMPETITION", "RowKey": pid, "Status": status}
        if deletion is not None:
            row["DeletionDate"] = deletion
        if purged:
            row["PurgedUtc"] = purged
        self.platform.rows[pid] = row
        return row

    def row(self, comp_id="abc12345"):
        return self.comp.rows[("GLOBAL", comp_id)]


@pytest.fixture
def world(monkeypatch):
    w = World()

    def _get_table_client(table_name="generatedprotocols"):
        return w.comp if table_name == "competitions" else w.protocols

    monkeypatch.setattr(sh, "get_table_client", _get_table_client)
    monkeypatch.setattr(sh, "get_container_client", lambda: w.container)
    monkeypatch.setattr(sh, "get_competition_entity",
                        lambda comp_id: w.comp.rows.get(("GLOBAL", comp_id)))
    monkeypatch.setattr(sh, "read_structure", lambda path: w.structures.get(path))
    monkeypatch.setattr(sh, "write_structure",
                        lambda path, structure: w.structures.__setitem__(path, structure))
    monkeypatch.setattr(pc, "get_platform_table_client", lambda: w.platform)
    return w


def _call(route, method="GET", params=None, body=None, email=ADDRESS):
    req = func.HttpRequest(
        method, "/api/route",
        headers=auth_headers(email) if email else {},
        params=params or {},
        body=json.dumps(body).encode("utf-8") if body is not None else b"")
    return route._function.get_user_function()(req)


def _json(response, status=200):
    assert response.status_code == status, response.get_body()
    return json.loads(response.get_body())


def resolve(name="Spring Trophy 2026", platform_id=PID):
    return _call(fa.resolve_competition, "POST",
                 body={"platformId": platform_id, "name": name, "dates": ""})


# ── platform_clock.py ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, expected", [
    ("2026-12-01T00:00:00Z", datetime(2026, 12, 1, tzinfo=timezone.utc)),
    ("2026-12-01T00:00:00", datetime(2026, 12, 1, tzinfo=timezone.utc)),
    ("2026-12-01T02:00:00+02:00", datetime(2026, 12, 1, tzinfo=timezone.utc)),
    ("2026-12-01T00:00:00.123456Z", datetime(2026, 12, 1, 0, 0, 0, 123456,
                                             tzinfo=timezone.utc)),
    (datetime(2026, 12, 1), datetime(2026, 12, 1, tzinfo=timezone.utc)),
    ("not a date", None),
    ("", None),
    (None, None),
])
def test_parse_utc(raw, expected):
    assert pc.parse_utc(raw) == expected


def test_to_iso_z_is_the_platform_format():
    assert pc.to_iso_z(datetime(2026, 12, 1, 2, tzinfo=timezone(timedelta(hours=2)))) \
        == "2026-12-01T00:00:00Z"


def test_the_clock_is_read_from_the_lowercase_guid_row(world):
    world.platform_row(deletion=FUTURE)
    clock = pc.read_platform_clock(PID.upper())
    assert clock == {"deletionDate": datetime(2026, 12, 1, tzinfo=timezone.utc),
                     "status": "active", "purged": False}
    assert world.platform.reads == [("COMPETITION", PID)]


def test_a_purged_row_reads_as_purged(world):
    world.platform_row(status="deleted", purged="2026-10-02T03:00:00Z")
    clock = pc.read_platform_clock(PID)
    assert (clock["status"], clock["purged"]) == ("deleted", True)


@pytest.mark.parametrize("platform_id", ["", None, "p-1", "abc12345", f"{PID}' or ''='"])
def test_a_non_guid_platform_id_is_never_looked_up(world, platform_id):
    assert pc.read_platform_clock(platform_id) is None
    assert world.platform.reads == []


def test_an_unknown_platform_row_is_none(world):
    assert pc.read_platform_clock(PID) is None


@pytest.mark.parametrize("raw, expected", [
    (PID, PID),
    (f"  {PID.upper()} ", PID),
    ("p-1", None),
    (f"{PID}' or ''='", None),
    ("", None),
    (None, None),
])
def test_normalize_platform_id(raw, expected):
    assert pc.normalize_platform_id(raw) == expected


def test_a_missing_clock_table_is_unavailable_not_absent(world):
    """A not-yet-provisioned table must not read as "no such competition"."""
    world.platform.error = ResourceNotFoundError(
        "The table specified does not exist. ErrorCode:TableNotFound")
    with pytest.raises(pc.PlatformClockUnavailable):
        pc.read_platform_clock(PID)


def test_the_clock_table_is_the_platform_mirror(monkeypatch):
    monkeypatch.setenv("PLATFORM_STORAGE_ACCOUNT", "stfsplattest")
    monkeypatch.delenv("PLATFORM_CLOCK_TABLE", raising=False)
    assert pc.get_platform_table_client().table_name == "retentionclock"


def test_an_unreadable_platform_raises(world):
    world.platform.error = HttpResponseError("403 AuthorizationPermissionMismatch")
    with pytest.raises(pc.PlatformClockUnavailable):
        pc.read_platform_clock(PID)


def test_a_failing_client_constructor_raises(monkeypatch):
    def _boom():
        raise ValueError("no credential")

    monkeypatch.setattr(pc, "get_platform_table_client", _boom)
    with pytest.raises(pc.PlatformClockUnavailable):
        pc.read_platform_clock(PID)


def test_the_feature_is_off_without_a_platform_account(monkeypatch):
    monkeypatch.delenv("PLATFORM_STORAGE_ACCOUNT", raising=False)
    assert pc.get_platform_table_client() is None
    assert pc.read_platform_clock(PID) is None


def test_the_platform_date_wins_over_the_local_one():
    platform = datetime(2026, 12, 1, tzinfo=timezone.utc)
    assert pc.effective_deletion(PAST, {"deletionDate": platform}) == platform
    assert pc.effective_deletion(PAST, {"deletionDate": None}) == pc.parse_utc(PAST)
    assert pc.effective_deletion(PAST, None) == pc.parse_utc(PAST)


@pytest.mark.parametrize("clock, expected", [
    (None, False),
    ({"deletionDate": pc.parse_utc(FUTURE), "status": "active", "purged": False}, False),
    # The date alone is never the verdict: the platform may be in dry-run, or
    # an overdue competition may just have been extended.
    ({"deletionDate": pc.parse_utc(PAST), "status": "active", "purged": False}, False),
    ({"deletionDate": None, "status": "active", "purged": False}, False),
    ({"deletionDate": pc.parse_utc(FUTURE), "status": "deleted", "purged": False}, True),
    ({"deletionDate": pc.parse_utc(FUTURE), "status": "deleted", "purged": True}, True),
    ({"deletionDate": pc.parse_utc(FUTURE), "status": "active", "purged": True}, True),
])
def test_platform_says_delete(clock, expected):
    assert pc.platform_says_delete(clock) is expected


# ── resolve_competition ──────────────────────────────────────────────────────

def test_resolve_returns_and_stores_the_platform_date(world):
    world.record(PlatformId=PID, DeletionDate="2026-10-05T11:22:33.000001Z")
    world.platform_row(deletion=FUTURE)
    body = _json(resolve())
    assert (body["id"], body["deletionDate"]) == ("abc12345", FUTURE)
    assert world.row()["DeletionDate"] == FUTURE


def test_resolve_does_not_rewrite_a_date_already_in_sync(world):
    world.record(PlatformId=PID, DeletionDate=FUTURE)
    world.platform_row(deletion=FUTURE)
    assert _json(resolve())["deletionDate"] == FUTURE
    assert world.comp.updates == []


def test_a_competition_the_platform_does_not_know_is_not_bound(world):
    """A record bound to a nonexistent platform competition would never be
    deleted, so neither a create nor an adoption happens."""
    world.record("adopt000", name="Spring Trophy 2026")      # unbound, same name
    assert _json(resolve(), 404)["error"] == "platform_not_found"
    assert set(world.comp.rows) == {("GLOBAL", "adopt000")}
    assert "PlatformId" not in world.row("adopt000")


def test_a_non_guid_platform_id_is_a_bad_request(world):
    assert _json(resolve(platform_id="p-1"), 400)["error"] == "invalid_platform_id"
    assert world.platform.reads == []
    assert world.comp.rows == {}


def test_a_differently_cased_guid_resolves_to_the_same_record(world):
    world.platform_row(deletion=FUTURE)
    first = _json(resolve(platform_id=PID.upper()))
    again = _json(resolve(platform_id=PID))
    assert (again["id"], again["created"]) == (first["id"], False)
    assert world.row(first["id"])["PlatformId"] == PID
    assert {key for (_, key) in world.platform.reads} == {PID}


def test_a_missing_clock_table_blocks_a_new_binding(world):
    world.platform.error = ResourceNotFoundError("ErrorCode:TableNotFound")
    assert _json(resolve(), 503)["error"] == "platform_unavailable"
    assert world.comp.rows == {}


def test_no_new_binding_while_the_platform_is_unreadable(world):
    world.record("adopt000", name="Spring Trophy 2026")
    world.platform.error = HttpResponseError("throttled")
    assert _json(resolve(), 503)["error"] == "platform_unavailable"
    assert set(world.comp.rows) == {("GLOBAL", "adopt000")}
    assert "PlatformId" not in world.row("adopt000")


def test_a_failing_platform_client_blocks_a_new_binding(world, monkeypatch):
    def _boom():
        raise ValueError("no credential")

    monkeypatch.setattr(pc, "get_platform_table_client", _boom)
    assert _json(resolve(), 503)["error"] == "platform_unavailable"
    assert world.comp.rows == {}


def test_without_a_platform_a_new_binding_has_no_date(world, monkeypatch):
    """Local dev: no PLATFORM_STORAGE_ACCOUNT, so nothing to confirm against."""
    monkeypatch.setattr(pc, "get_platform_table_client", lambda: None)
    body = _json(resolve())
    assert (body["created"], body["deletionDate"]) == (True, None)
    assert "DeletionDate" not in world.row(body["id"])


def test_a_new_record_takes_the_platform_date(world):
    world.platform_row(deletion=FUTURE)
    body = _json(resolve())
    assert body["created"] is True
    assert body["deletionDate"] == FUTURE
    assert world.row(body["id"])["DeletionDate"] == FUTURE


@pytest.mark.parametrize("status, purged", [("deleted", None),
                                            ("deleted", "2026-10-02T03:00:00Z"),
                                            ("active", "2026-10-02T03:00:00Z")])
def test_a_deleted_platform_competition_is_gone(world, status, purged):
    world.platform_row(status=status, purged=purged)
    body = _json(resolve(), 410)
    assert body["error"] == "platform_deleted"
    assert world.comp.rows == {}


def test_a_deleted_platform_competition_is_gone_even_with_a_record(world):
    world.record(PlatformId=PID)
    world.platform_row(status="deleted")
    assert _json(resolve(), 410)["error"] == "platform_deleted"


def test_an_unreadable_platform_still_resolves_with_the_local_date(world, caplog):
    world.record(PlatformId=PID, DeletionDate=PAST)
    world.platform.error = HttpResponseError("throttled")
    body = _json(resolve())
    assert (body["id"], body["deletionDate"]) == ("abc12345", PAST)
    assert world.row()["DeletionDate"] == PAST
    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_an_unknown_platform_row_keeps_the_local_date(world):
    world.record(PlatformId=PID, DeletionDate=PAST)
    assert _json(resolve())["deletionDate"] == PAST


# ── get_competition_details ──────────────────────────────────────────────────

def details():
    return _json(_call(fa.get_competition_details, params={"id": "abc12345"}))


def test_details_show_and_cache_the_platform_date(world):
    world.record(PlatformId=PID, DeletionDate=PAST)
    world.platform_row(deletion=FUTURE)
    body = details()
    assert body["deletionDate"] == FUTURE
    assert "retentionManagedBy" not in body
    assert world.row()["DeletionDate"] == FUTURE


def test_details_fall_back_to_the_cached_date(world):
    world.record(PlatformId=PID, DeletionDate=FUTURE)
    world.platform.error = HttpResponseError("throttled")
    assert details()["deletionDate"] == FUTURE
    assert world.comp.updates == []


def test_details_of_a_record_without_a_platform_date_show_none(world):
    world.record(PlatformId=PID, DeletionDate=None)
    assert details()["deletionDate"] is None


def test_details_of_a_legacy_unbound_record_show_its_stored_date(world):
    world.record(DeletionDate=PAST)
    assert details()["deletionDate"] == PAST


def test_there_is_no_extend_route():
    """Extending is the platform's alone (POST /api/competitions/{id}/extend)."""
    assert not hasattr(fa, "extend_competition_deletion")


# ── the nightly sweep ────────────────────────────────────────────────────────

def sweep():
    fa._run_auto_delete(NOW)


def test_a_deleted_platform_competition_is_deleted_here(world):
    world.record(PlatformId=PID, DeletionDate=FUTURE)
    world.platform_row(deletion=PAST, status="deleted")
    sweep()
    row = world.row()
    assert row["Visible"] is False
    assert (row["CreatedBy"], row["DeletedBy"]) == ("", "auto-cleanup")
    assert world.container.blobs == {}


def test_a_passed_date_alone_deletes_nothing(world):
    """Only the platform's verdict counts — it may be in dry-run, or the
    competition may have been extended since the date passed."""
    world.record(PlatformId=PID, DeletionDate=PAST)
    world.platform_row(deletion=PAST)
    sweep()
    assert world.row()["Visible"] is True
    assert world.container.blobs != {}


def test_the_sweep_caches_the_platform_date(world):
    world.record(PlatformId=PID, DeletionDate=PAST)
    world.platform_row(deletion=FUTURE)
    sweep()
    assert world.row()["Visible"] is True
    assert world.row()["DeletionDate"] == FUTURE


def test_the_sweep_runs_after_the_platform_sweep():
    """The platform sweeps at 03:00 UTC; following it at 04:00 acts on its
    verdicts the same night."""
    trigger = next(t for t in fa.auto_delete_expired_competitions._function.get_bindings()
                   if t.type == "timerTrigger")
    assert trigger.schedule == "0 0 4 * * *"


def test_a_purged_platform_competition_is_deleted_at_once(world):
    world.record(PlatformId=PID, DeletionDate=FUTURE)
    world.platform_row(deletion=FUTURE, status="deleted", purged="2026-10-12T03:00:00Z")
    sweep()
    assert world.row()["Visible"] is False


def test_an_unreadable_platform_never_deletes_early(world, caplog):
    world.record(PlatformId=PID, DeletionDate=PAST)
    world.platform.error = HttpResponseError("throttled")
    sweep()
    assert world.row()["Visible"] is True
    assert world.container.blobs != {}
    assert any("skipped" in r.getMessage() and r.levelname == "ERROR"
               for r in caplog.records)


def test_a_record_without_a_clock_row_is_kept(world, caplog):
    """No clock row: no verdict, so no deletion — whatever the cached date —
    while the record is under 60 days old (the orphan rule, below)."""
    world.platform_row(pid="99999999-2222-4333-8444-555555555555")
    world.record("expired0", PlatformId=PID, DeletionDate=PAST,
                 CreatedDate="2026-09-01T10:00:00Z")
    sweep()
    assert world.row("expired0")["Visible"] is True
    assert any(r.levelname == "WARNING" and "expired0" in r.getMessage()
               for r in caplog.records)


def test_a_missing_clock_table_deletes_nothing(world, caplog):
    world.record(PlatformId=PID, DeletionDate=PAST)
    world.platform.error = ResourceNotFoundError("ErrorCode:TableNotFound")
    sweep()
    assert world.row()["Visible"] is True
    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_one_platform_client_serves_the_whole_sweep(world, monkeypatch):
    made = []

    def _client():
        made.append(1)
        return world.platform

    monkeypatch.setattr(pc, "get_platform_table_client", _client)
    world.record("first000", PlatformId=PID)
    world.record("second00", PlatformId=PID)
    sweep()
    assert len(made) == 1
    assert len(world.platform.reads) == 2


def test_a_record_with_no_date_at_all_is_kept(world):
    """A bound record the platform has not dated yet has no clock here, and the
    legacy backfill must not hand it one."""
    world.record(PlatformId=PID, DeletionDate=None)
    sweep()
    assert world.row()["Visible"] is True
    assert "DeletionDate" not in world.row()


def test_a_platform_row_without_a_date_is_not_deleted(world):
    """The platform alone decides for a row it knows: no date there, no
    deletion here — the cached date does not take over."""
    world.record(PlatformId=PID, DeletionDate=PAST)
    world.platform_row(deletion=None)
    sweep()
    assert world.row()["Visible"] is True


def test_legacy_unbound_records_go_on_their_stored_date(world):
    world.record("expired0", DeletionDate=PAST)
    world.record("current0", DeletionDate=FUTURE)
    world.record("undated0", DeletionDate=None)      # backfilled to the legacy date
    sweep()
    assert world.row("expired0")["Visible"] is False
    assert world.row("current0")["Visible"] is True
    assert world.row("undated0")["Visible"] is False


def test_an_unconfigured_platform_deletes_nothing_bound(world, monkeypatch, caplog):
    monkeypatch.setattr(pc, "get_platform_table_client", lambda: None)
    world.record("expired0", PlatformId=PID, DeletionDate=PAST)
    world.record("legacy00", DeletionDate=PAST)
    sweep()
    assert world.row("expired0")["Visible"] is True
    assert world.row("legacy00")["Visible"] is False
    assert any("skipped 1" in r.getMessage() and r.levelname == "ERROR"
               for r in caplog.records)


def test_a_failing_platform_client_deletes_nothing_bound(world, monkeypatch):
    def _boom():
        raise ValueError("no credential")

    monkeypatch.setattr(pc, "get_platform_table_client", _boom)
    world.record(PlatformId=PID, DeletionDate=PAST)
    sweep()
    assert world.row()["Visible"] is True


# ── no e-mail address outlives the data ──────────────────────────────────────

def test_a_user_delete_records_no_address(world):
    world.record()
    _json(_call(fa.delete_competition, "POST", params={"id": "abc12345"}))
    row = world.row()
    assert row["Visible"] is False
    assert (row["CreatedBy"], row["DeletedBy"]) == ("", "user")
    assert ADDRESS not in json.dumps(row)


def test_the_sweep_scrubs_rows_deleted_before(world):
    world.record("olddel01", Visible=False, DeletedBy="someone@example.com")
    world.record("olddel02", Visible=False, DeletedBy="auto-cleanup")
    world.record("olddel03", Visible=False, CreatedBy="-", DeletedBy="user")
    world.record("visible0")
    sweep()
    assert (world.row("olddel01")["CreatedBy"], world.row("olddel01")["DeletedBy"]) == ("", "")
    assert (world.row("olddel02")["CreatedBy"], world.row("olddel02")["DeletedBy"]) \
        == ("", "auto-cleanup")
    assert world.row("olddel03")["CreatedBy"] == "-"
    assert world.row("visible0")["CreatedBy"] == ADDRESS     # live records keep it
    scrubbed = {u["RowKey"] for u in world.comp.updates}
    assert scrubbed == {"olddel01", "olddel02"}


def test_the_scrub_leaves_clean_rows_alone(world):
    world.record("olddel01", Visible=False, CreatedBy="", DeletedBy="auto-cleanup")
    assert fa._scrub_hidden_rows(world.comp) == 0
    assert world.comp.updates == []


def test_the_scrub_is_idempotent(world):
    world.record("olddel01", Visible=False, DeletedBy="someone@example.com")
    assert fa._scrub_hidden_rows(world.comp) == 1
    assert fa._scrub_hidden_rows(world.comp) == 0


# ── orphans: no platform competition behind the record ──────────────────────

OTHER = "99999999-2222-4333-8444-555555555555"
OLD = pc.to_iso_z(NOW - timedelta(days=61))
YOUNG = pc.to_iso_z(NOW - timedelta(days=59))


@pytest.fixture
def purge(monkeypatch):
    monkeypatch.setenv("ORPHAN_PURGE_ENABLED", "true")


def _orphan(world, comp_id, created, **extra):
    row = world.record(comp_id, name=f"Cup {comp_id}", CreatedDate=created, **extra)
    world.container.blobs[f"{row['FolderPath']}/results.pdf"] = b"%PDF"
    world.protocols.rows[(comp_id, "protocol.pdf")] = {
        "PartitionKey": comp_id, "RowKey": "protocol.pdf", "FileName": "protocol.pdf"}
    return row


def _assert_scrubbed(world, comp_id):
    row = world.row(comp_id)
    assert row["Visible"] is False
    assert (row["CreatedBy"], row["DeletedBy"]) == ("", "auto-cleanup")
    assert ADDRESS not in json.dumps(row)
    # The row stays as history (name, dates).
    assert row["Name"] == f"Cup {comp_id}" and row["CreatedDate"]
    assert not [b for b in world.container.blobs if b.startswith(f"Cup {comp_id}-")]
    assert not [k for k in world.protocols.rows if k[0] == comp_id]


def test_an_old_record_without_platform_id_is_deleted_and_scrubbed(world, purge):
    world.platform_row(pid=OTHER)
    _orphan(world, "orphan01", OLD)          # its stored date is still ahead
    stats = fa._run_auto_delete(NOW)
    assert stats["orphaned"] == 1 and stats["deleted"] == 0
    _assert_scrubbed(world, "orphan01")


def test_an_old_record_whose_clock_row_is_not_found_is_deleted(world, purge):
    world.platform_row(pid=OTHER)            # the table is healthy
    _orphan(world, "orphan02", OLD, PlatformId=PID)
    assert fa._run_auto_delete(NOW)["orphaned"] == 1
    _assert_scrubbed(world, "orphan02")


def test_a_platform_id_that_is_not_a_guid_counts_as_orphaned(world, purge):
    world.platform_row(pid=OTHER)
    _orphan(world, "orphan03", OLD, PlatformId="not-a-guid")
    assert fa._run_auto_delete(NOW)["orphaned"] == 1
    _assert_scrubbed(world, "orphan03")
    assert world.platform.reads == []


@pytest.mark.parametrize("platform_id", [None, PID])
def test_an_orphan_younger_than_60_days_is_untouched(world, purge, platform_id):
    world.platform_row(pid=OTHER)
    _orphan(world, "young001", YOUNG, PlatformId=platform_id)
    stats = fa._run_auto_delete(NOW)
    assert stats["orphaned"] == 0 and stats["orphan_kept"] == 1
    assert world.row("young001")["Visible"] is True
    assert world.row("young001")["CreatedBy"] == ADDRESS


def test_without_a_created_date_the_table_timestamp_counts(world, purge, monkeypatch):
    class Entity(dict):
        def __init__(self, row, stamp):
            super().__init__(row)
            self.metadata = {"timestamp": stamp} if stamp else {}

    world.platform_row(pid=OTHER)
    _orphan(world, "nodate01", "-", PlatformId=PID)
    _orphan(world, "nodate02", "-", PlatformId=PID)
    _orphan(world, "nodate03", "-", PlatformId=PID)
    stamps = {"nodate01": NOW - timedelta(days=61), "nodate02": NOW - timedelta(days=2),
              "nodate03": None}
    plain = world.comp.query_entities
    monkeypatch.setattr(world.comp, "query_entities",
                        lambda q: [Entity(r, stamps.get(r["RowKey"])) for r in plain(q)])
    stats = fa._run_auto_delete(NOW)
    assert stats["orphaned"] == 1 and stats["orphan_kept"] == 2
    assert world.row("nodate01")["Visible"] is False
    assert world.row("nodate02")["Visible"] is True
    assert world.row("nodate03")["Visible"] is True


def test_a_clock_read_error_is_never_not_found(world, purge, monkeypatch):
    world.platform_row(pid=OTHER)
    _orphan(world, "bound001", OLD, PlatformId=PID)
    original = world.platform.get_entity

    def throttled(partition_key, row_key):
        raise HttpResponseError("throttled")

    monkeypatch.setattr(world.platform, "get_entity", throttled)
    stats = fa._run_auto_delete(NOW)
    assert stats["orphaned"] == 0 and stats["skipped"] == 1
    assert world.row("bound001")["Visible"] is True
    assert world.row("bound001")["CreatedBy"] == ADDRESS
    assert original  # the probe read the table fine


def test_an_empty_clock_table_aborts_the_orphan_step(world, purge, caplog):
    _orphan(world, "bound001", OLD, PlatformId=PID)
    _orphan(world, "unbound1", OLD)
    stats = fa._run_auto_delete(NOW)
    assert stats["orphaned"] == 0 and stats["orphan_skipped"] == 2
    assert world.row("bound001")["Visible"] is True
    assert world.row("unbound1")["Visible"] is True
    assert any("orphan step aborted" in r.getMessage() and "empty" in r.getMessage()
               for r in caplog.records)


def test_an_empty_clock_table_is_fine_when_nothing_is_bound(world, purge):
    _orphan(world, "unbound1", OLD)
    assert fa._run_auto_delete(NOW)["orphaned"] == 1


@pytest.mark.parametrize("error", [HttpResponseError("throttled"),
                                   ResourceNotFoundError("ErrorCode:TableNotFound")])
def test_an_unreadable_clock_table_aborts_the_orphan_step(world, purge, error):
    world.platform_row(pid=OTHER)
    world.platform.query_error = error
    _orphan(world, "unbound1", OLD)
    _orphan(world, "bound001", OLD, PlatformId=PID)
    stats = fa._run_auto_delete(NOW)
    assert stats["orphaned"] == 0 and stats["orphan_skipped"] == 2
    assert world.row("unbound1")["Visible"] is True


def test_without_a_platform_configured_the_orphan_step_is_aborted(world, purge, monkeypatch):
    monkeypatch.setattr(pc, "get_platform_table_client", lambda: None)
    _orphan(world, "unbound1", OLD)
    stats = fa._run_auto_delete(NOW)
    assert stats["orphaned"] == 0 and stats["orphan_skipped"] == 1


def test_a_live_bound_record_is_untouched(world, purge):
    world.platform_row(deletion=FUTURE)
    _orphan(world, "bound001", OLD, PlatformId=PID)
    stats = fa._run_auto_delete(NOW)
    assert stats["orphaned"] == 0 and stats["deleted"] == 0
    assert world.row("bound001")["Visible"] is True
    assert world.row("bound001")["CreatedBy"] == ADDRESS
    assert world.protocols.rows


def test_the_orphan_kill_switch_only_logs(world, monkeypatch, caplog):
    monkeypatch.setenv("ORPHAN_PURGE_ENABLED", "false")
    world.platform_row(pid=OTHER)
    _orphan(world, "orphan01", OLD, PlatformId=PID)
    stats = fa._run_auto_delete(NOW)
    assert stats["orphaned"] == 0 and stats["orphan_dry_run"] == 1
    assert world.row("orphan01")["Visible"] is True
    assert any("would delete" in r.getMessage() for r in caplog.records)


def test_the_sweep_logs_the_orphan_counts(world, purge, caplog):
    import logging
    world.platform_row(pid=OTHER)
    _orphan(world, "orphan01", OLD, PlatformId=PID)
    with caplog.at_level(logging.INFO):
        fa.auto_delete_expired_competitions._function.get_user_function()(None)
    assert world.row("orphan01")["Visible"] is False
    assert any("Orphans: deleted 1" in r.getMessage() for r in caplog.records)
