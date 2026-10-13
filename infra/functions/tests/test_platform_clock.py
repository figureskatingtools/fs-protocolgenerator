"""Retention follows the platform's clock (`platform_clock.py`).

The site's competition registry owns each competition's deletion date (created
+ 60 days, extended a week at a time on the site). A record here that is bound
to a platform competition (`PlatformId`) follows that date; an unbound one
keeps its own 60-day clock. The one rule that must never bend: when the
platform cannot be read, nothing is deleted early.

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
        self.reads = []

    def get_entity(self, partition_key, row_key):
        self.reads.append((partition_key, row_key))
        if self.error:
            raise self.error
        if row_key not in self.rows:
            raise ResourceNotFoundError(row_key)
        return dict(self.rows[row_key])


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
        headers={"x-forwarded-user-email": email} if email else {},
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
    ({"deletionDate": pc.parse_utc(PAST), "status": "active", "purged": False}, True),
    ({"deletionDate": NOW, "status": "active", "purged": False}, True),
    ({"deletionDate": pc.parse_utc(FUTURE), "status": "deleted", "purged": False}, True),
    ({"deletionDate": pc.parse_utc(FUTURE), "status": "deleted", "purged": True}, True),
    ({"deletionDate": None, "status": "active", "purged": False}, False),
])
def test_platform_says_delete(clock, expected):
    assert pc.platform_says_delete(clock, NOW) is expected


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


def test_details_show_the_platform_date(world):
    world.record(PlatformId=PID, DeletionDate=PAST)
    world.platform_row(deletion=FUTURE)
    body = details()
    assert (body["deletionDate"], body["retentionManagedBy"]) == (FUTURE, "platform")


def test_details_of_an_unbound_record_show_its_own_clock(world):
    world.record(DeletionDate=PAST)
    body = details()
    assert (body["deletionDate"], body["retentionManagedBy"]) == (PAST, "tool")


def test_details_fall_back_to_the_last_synced_date(world):
    world.record(PlatformId=PID, DeletionDate=FUTURE)
    world.platform.error = HttpResponseError("throttled")
    body = details()
    assert (body["deletionDate"], body["retentionManagedBy"]) == (FUTURE, "platform")


def test_details_of_a_record_the_platform_does_not_know(world):
    world.record(PlatformId=PID, DeletionDate=FUTURE)
    assert details()["retentionManagedBy"] == "tool"


# ── extend_competition_deletion ──────────────────────────────────────────────

def extend():
    return _call(fa.extend_competition_deletion, "POST", params={"id": "abc12345"})


def test_extending_a_bound_record_only_syncs_the_platform_date(world):
    world.record(PlatformId=PID, DeletionDate=PAST)
    world.platform_row(deletion="2026-12-08T00:00:00Z")
    body = _json(extend())
    assert body == {"deletionDate": "2026-12-08T00:00:00Z", "retentionManagedBy": "platform"}
    assert world.row()["DeletionDate"] == "2026-12-08T00:00:00Z"


def test_extending_an_unbound_record_adds_a_week(world):
    world.record(DeletionDate=FUTURE)
    body = _json(extend())
    assert body["retentionManagedBy"] == "tool"
    assert pc.parse_utc(body["deletionDate"]) == pc.parse_utc(FUTURE) + timedelta(days=7)
    assert world.row()["DeletionDate"] == body["deletionDate"]


def test_extending_when_the_platform_is_unreadable_changes_nothing(world):
    world.record(PlatformId=PID, DeletionDate=FUTURE)
    world.platform.error = HttpResponseError("throttled")
    assert _json(extend(), 502)["error"] == "platform_unavailable"
    assert world.row()["DeletionDate"] == FUTURE
    assert world.comp.updates == []


# ── the nightly sweep ────────────────────────────────────────────────────────

def sweep():
    fa._run_auto_delete(NOW)


def test_a_bound_record_is_deleted_on_the_platform_date(world):
    world.record(PlatformId=PID, DeletionDate=FUTURE)
    world.platform_row(deletion=PAST)
    sweep()
    row = world.row()
    assert row["Visible"] is False
    assert (row["CreatedBy"], row["DeletedBy"]) == ("", "auto-cleanup")
    assert world.container.blobs == {}


def test_the_platform_date_wins_over_an_expired_local_one(world):
    world.record(PlatformId=PID, DeletionDate=PAST)
    world.platform_row(deletion=FUTURE)
    sweep()
    assert world.row()["Visible"] is True
    assert world.row()["DeletionDate"] == FUTURE      # synced for the fallback
    assert world.container.blobs != {}


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


def test_a_record_the_platform_does_not_know_keeps_its_own_clock(world):
    world.record(PlatformId=PID, DeletionDate=PAST)
    sweep()
    assert world.row()["Visible"] is False


def test_a_platform_row_without_a_date_leaves_the_local_clock_in_charge(world):
    """The platform backfills dates on its own nightly run; until then the
    record must not lose the clock it already had."""
    world.record("expired0", PlatformId=PID, DeletionDate=PAST)
    world.platform_row(deletion=None)
    sweep()
    assert world.row("expired0")["Visible"] is False


def test_unbound_records_keep_the_local_clock(world):
    world.record("expired0", DeletionDate=PAST)
    world.record("current0", DeletionDate=FUTURE)
    sweep()
    assert world.row("expired0")["Visible"] is False
    assert world.row("current0")["Visible"] is True


def test_the_feature_off_keeps_every_record_on_its_own_clock(world, monkeypatch):
    monkeypatch.setattr(pc, "get_platform_table_client", lambda: None)
    world.record("expired0", PlatformId=PID, DeletionDate=PAST)
    world.record("current0", PlatformId=PID, DeletionDate=FUTURE)
    sweep()
    assert world.row("expired0")["Visible"] is False
    assert world.row("current0")["Visible"] is True


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
