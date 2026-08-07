"""`function_app.resolve_competition` — binding the site's shared competition
selector to this tool's registry.

The route is the only way the new UI reaches a competition, so the awkward cases
matter: a soft-deleted row keeps its PlatformId but its blobs are gone (it must
never be resurrected), rows created before this feature carry no PlatformId at
all (adopted by name), and the platform id goes straight into a table filter
(single quotes have to be escaped). The registry is faked with a dict-backed
table client; competition names are invented.

The platform also supplies the competition's start date (ISO) and venue, which
seed `event.dates` (rendered Finnish, flagged `datesAuto`) and `event.rink`
without ever overwriting a value that is already there — plus the flag handshake
that lets a later schedule parse replace an auto-filled date span but never a
user-saved one (`_apply_schedule_meta` / `save_event_settings`).
"""
import json

import azure.functions as func
import pytest
from azure.core.exceptions import ResourceNotFoundError

import function_app as fa
import storage_helpers as sh
import structure as st


# ── a dict-backed stand-in for the `competitions` table ───────────────────────

def _parse_filter(query):
    """Understand exactly the filters the backend emits: `Prop eq 'value'`
    clauses joined by ` and `, where '' is an escaped single quote."""
    terms = {}
    for clause in query.split(" and "):
        key, _, literal = clause.partition(" eq ")
        terms[key.strip()] = literal.strip()[1:-1].replace("''", "'")
    return terms


class FakeTable:
    def __init__(self, *entities):
        self.rows = {e["RowKey"]: dict(e) for e in entities}
        self.queries = []

    def create_table(self):
        pass

    def get_entity(self, partition_key, row_key):
        if row_key not in self.rows:
            raise ResourceNotFoundError(row_key)
        return dict(self.rows[row_key])

    def create_entity(self, entity):
        self.rows[entity["RowKey"]] = dict(entity)

    def update_entity(self, entity, mode=None):
        self.rows.setdefault(entity["RowKey"], {}).update(entity)

    def upsert_entity(self, entity):
        self.update_entity(entity)

    def query_entities(self, query):
        self.queries.append(query)
        terms = _parse_filter(query)
        return [dict(row) for row in self.rows.values()
                if all(row.get(key) == value for key, value in terms.items())]


def entity(comp_id, name, created="2026-05-01T10:00:00Z", **extra):
    row = {
        "PartitionKey": "GLOBAL",
        "RowKey": comp_id,
        "Name": name,
        "FolderPath": f"{name}-{comp_id}",
        "Visible": True,
        "CreatedBy": "organizer@example.com",
        "CreatedDate": created,
    }
    row.update(extra)
    return row


@pytest.fixture
def table(monkeypatch):
    """An empty registry table wired under `get_table_client('competitions')`,
    with metadata.json faked as an in-memory folder_path -> structure dict
    (`table.written`); `table.writes` records every write, so a test can assert
    that an unchanged structure is *not* persisted."""
    fake = FakeTable()
    fake.written = {}
    fake.writes = []

    def _get_table_client(table_name="generatedprotocols"):
        assert table_name == "competitions"
        return fake

    def _write_structure(folder_path, structure):
        fake.writes.append(folder_path)
        fake.written[folder_path] = structure

    monkeypatch.setattr(sh, "get_table_client", _get_table_client)
    monkeypatch.setattr(sh, "write_structure", _write_structure)
    monkeypatch.setattr(sh, "read_structure", lambda folder_path: fake.written.get(folder_path))
    monkeypatch.setattr(sh, "get_competition_entity",
                        lambda comp_id: fake.rows.get(comp_id))
    return fake


def resolve(platform_id="p-1", name="Spring Trophy 2026", dates="2026-05-01",
            email="organizer@example.com", venue=None):
    body = {"platformId": platform_id, "name": name, "dates": dates}
    if venue is not None:
        body["venue"] = venue
    req = func.HttpRequest(
        "POST", "/api/resolve_competition",
        headers={"x-forwarded-user-email": email} if email else {},
        body=json.dumps(body).encode("utf-8"))
    return fa.resolve_competition._function.get_user_function()(req)


def payload(response):
    assert response.status_code == 200, response.get_body()
    return json.loads(response.get_body())


# ── lookup by platform id ─────────────────────────────────────────────────────

def test_a_bound_competition_is_returned_as_is(table):
    table.rows["abc12345"] = entity("abc12345", "Spring Trophy 2026", PlatformId="p-1")
    assert payload(resolve()) == {"id": "abc12345", "name": "Spring Trophy 2026",
                                 "created": False}
    assert table.queries[0] == "PartitionKey eq 'GLOBAL' and PlatformId eq 'p-1'"


def test_the_newest_of_several_bound_rows_wins(table):
    table.rows["old00000"] = entity("old00000", "Spring Trophy 2026",
                                    created="2026-04-01T09:00:00Z", PlatformId="p-1")
    table.rows["new00000"] = entity("new00000", "Spring Trophy 2026",
                                    created="2026-04-30T08:00:00Z", PlatformId="p-1")
    assert payload(resolve())["id"] == "new00000"


def test_a_soft_deleted_row_is_never_resurrected(table):
    """Deletion clears the blobs but keeps PlatformId, and the name no longer
    matches anything visible — so the route has to create a fresh competition."""
    table.rows["gone0000"] = entity("gone0000", "Spring Trophy 2026",
                                    Visible=False, PlatformId="p-1")
    body = payload(resolve())
    assert body["created"] is True
    assert body["id"] != "gone0000"
    assert table.rows["gone0000"]["Visible"] is False


def test_a_legacy_row_without_visible_still_counts_as_visible(table):
    row = entity("legacy00", "Spring Trophy 2026", PlatformId="p-1")
    del row["Visible"]
    table.rows["legacy00"] = row
    assert payload(resolve())["id"] == "legacy00"


def test_a_quoted_platform_id_is_escaped_in_the_filter(table):
    table.rows["quoted00"] = entity("quoted00", "Spring Trophy 2026",
                                    PlatformId="p-1' or Visible eq true--")
    body = payload(resolve(platform_id="p-1' or Visible eq true--"))
    assert body["id"] == "quoted00"
    assert table.queries[0] == (
        "PartitionKey eq 'GLOBAL' and PlatformId eq "
        "'p-1'' or Visible eq true--'")


# ── adoption of pre-binding competitions ─────────────────────────────────────

def test_an_unbound_competition_is_adopted_by_name_and_stamped(table):
    table.rows["adopt000"] = entity("adopt000", "Spring Trophy 2026")
    assert payload(resolve(name="Spring  Trophy, 2026!")) == {
        "id": "adopt000", "name": "Spring Trophy 2026", "created": False}
    assert table.rows["adopt000"]["PlatformId"] == "p-1"
    assert table.written == {}


def test_adoption_skips_deleted_and_already_bound_rows(table):
    table.rows["deleted0"] = entity("deleted0", "Spring Trophy 2026", Visible=False)
    table.rows["other000"] = entity("other000", "Spring Trophy 2026", PlatformId="p-2")
    body = payload(resolve())
    assert body["created"] is True
    assert body["id"] not in ("deleted0", "other000")
    assert table.rows["other000"]["PlatformId"] == "p-2"


# ── create fallback ──────────────────────────────────────────────────────────

def test_an_unknown_platform_competition_is_created_and_bound(table):
    body = payload(resolve())
    assert (body["created"], body["name"]) == (True, "Spring Trophy 2026")
    row = table.rows[body["id"]]
    assert row["PlatformId"] == "p-1"
    assert row["Visible"] is True
    assert row["CreatedBy"] == "organizer@example.com"
    assert row["Name"] == "Spring Trophy 2026"
    assert row["FolderPath"] == f"Spring Trophy 2026-{body['id']}"
    assert row["DeletionDate"] > row["CreatedDate"]

    structure = table.written[row["FolderPath"]]
    assert structure["id"] == body["id"]
    assert structure["platformId"] == "p-1"
    assert structure["event"]["dates"] == "01.05.2026"

    # A second call for the same platform id now finds the record it just made.
    again = payload(resolve())
    assert (again["id"], again["created"]) == (body["id"], False)


def test_the_platform_date_and_venue_seed_the_event(table):
    """The platform knows the start date (ISO) and the ice rink; the structure
    stores the date Finnish and remembers that nobody typed it (`datesAuto`)."""
    body = payload(resolve(dates="2025-01-25", venue="Vierumäki Ice Hall"))
    event = table.written[table.rows[body["id"]]["FolderPath"]]["event"]
    assert event["dates"] == "25.01.2025"
    assert event["datesAuto"] is True
    assert event["rink"] == "Vierumäki Ice Hall"


def test_a_non_iso_platform_date_is_stored_verbatim(table):
    """Older platform rows / hand-typed spans are not ISO — keep them as-is
    rather than mangling them."""
    body = payload(resolve(dates="1.-2.5.2026"))
    event = table.written[table.rows[body["id"]]["FolderPath"]]["event"]
    assert (event["dates"], event["datesAuto"]) == ("1.-2.5.2026", True)


def test_a_missing_platform_date_leaves_the_field_unflagged(table):
    body = payload(resolve(dates=""))
    event = table.written[table.rows[body["id"]]["FolderPath"]]["event"]
    assert event["dates"] == ""
    assert "datesAuto" not in event


def test_the_legacy_create_route_stays_unbound(table):
    req = func.HttpRequest(
        "POST", "/api/create_competition",
        headers={"x-forwarded-user-email": "organizer@example.com"},
        params={"name": "Spring Trophy 2026", "dates": "1.-2.5.2026"}, body=b"")
    body = payload(fa.create_competition._function.get_user_function()(req))
    assert body["name"] == "Spring Trophy 2026"
    assert "PlatformId" not in table.rows[body["id"]]
    assert "platformId" not in table.written[table.rows[body["id"]]["FolderPath"]]


# ── platform event backfill on hit / adoption ─────────────────────────────────

def seed_structure(table, comp_id, **event):
    """Give an existing registry row a metadata.json with the given event fields."""
    row = table.rows[comp_id]
    structure = st.new_structure(comp_id, row["Name"], event.pop("dates", ""),
                                 "organizer@example.com", "2026-05-01T10:00:00Z")
    structure["event"].update(event)
    table.written[row["FolderPath"]] = structure
    table.writes.clear()
    return structure


def test_a_bound_competition_gets_its_empty_event_fields_backfilled(table):
    table.rows["abc12345"] = entity("abc12345", "Spring Trophy 2026", PlatformId="p-1")
    structure = seed_structure(table, "abc12345")
    assert payload(resolve(dates="2025-01-25", venue="Vierumäki Ice Hall"))["id"] == "abc12345"
    assert structure["event"]["dates"] == "25.01.2025"
    assert structure["event"]["datesAuto"] is True
    assert structure["event"]["rink"] == "Vierumäki Ice Hall"
    assert table.writes == ["Spring Trophy 2026-abc12345"]


def test_backfill_never_overwrites_existing_event_fields(table):
    """A date span already computed from the schedule (or typed by the user) and a
    rink already set must survive every re-resolve — and nothing is written."""
    table.rows["abc12345"] = entity("abc12345", "Spring Trophy 2026", PlatformId="p-1")
    structure = seed_structure(table, "abc12345",
                               dates="25.01.2025 – 26.01.2025", rink="Oulunkylä")
    resolve(dates="2025-01-25", venue="Vierumäki Ice Hall")
    assert structure["event"]["dates"] == "25.01.2025 – 26.01.2025"
    assert structure["event"]["rink"] == "Oulunkylä"
    assert "datesAuto" not in structure["event"]
    assert table.writes == []


def test_backfill_writes_once_for_a_partial_fill(table):
    table.rows["abc12345"] = entity("abc12345", "Spring Trophy 2026", PlatformId="p-1")
    structure = seed_structure(table, "abc12345", rink="Oulunkylä")
    resolve(dates="2025-01-25", venue="Vierumäki Ice Hall")
    assert (structure["event"]["rink"], structure["event"]["dates"]) == ("Oulunkylä", "25.01.2025")
    assert table.writes == ["Spring Trophy 2026-abc12345"]


def test_a_raw_iso_date_left_by_the_old_route_is_repaired(table):
    """Competitions bound before this fix stored the platform's ISO date verbatim —
    nobody types that, so it is rewritten Finnish and flagged for the schedule."""
    table.rows["abc12345"] = entity("abc12345", "Spring Trophy 2026", PlatformId="p-1")
    structure = seed_structure(table, "abc12345", dates="2025-01-25")
    resolve(dates="2025-01-25")
    assert structure["event"]["dates"] == "25.01.2025"
    assert structure["event"]["datesAuto"] is True
    assert table.writes == ["Spring Trophy 2026-abc12345"]


def test_an_adopted_competition_is_backfilled_too(table):
    table.rows["adopt000"] = entity("adopt000", "Spring Trophy 2026")
    structure = seed_structure(table, "adopt000")
    assert payload(resolve(venue="Vierumäki Ice Hall"))["id"] == "adopt000"
    assert structure["event"]["rink"] == "Vierumäki Ice Hall"


def test_a_competition_without_metadata_still_binds(table):
    """Blobs can be gone (or the read can fail) — binding must not depend on it."""
    table.rows["abc12345"] = entity("abc12345", "Spring Trophy 2026", PlatformId="p-1")
    assert payload(resolve(venue="Vierumäki Ice Hall"))["id"] == "abc12345"
    assert table.writes == []


# ── Finnish date rendering ───────────────────────────────────────────────────

@pytest.mark.parametrize("raw, expected", [
    ("2025-01-25", "25.01.2025"),
    ("2026-5-1", "01.05.2026"),                       # sloppy ISO
    ("2025-01-25T00:00:00Z", "25.01.2025"),           # datetime from the platform
    ("2025-01-25 – 2025-01-26", "25.01.2025 – 26.01.2025"),   # ISO span
    ("1.-2.5.2026", "1.-2.5.2026"),                   # already Finnish: untouched
    ("2025-13-45", "2025-13-45"),                     # not a real date: untouched
    ("", ""),
    (None, ""),
])
def test_platform_dates_render_finnish(raw, expected):
    assert fa._fi_date(raw) == expected


# ── schedule meta vs. the auto flag ──────────────────────────────────────────

def test_schedule_meta_fills_empty_event_fields():
    event = {"dates": "", "rink": ""}
    fa._apply_schedule_meta(event, {"dates": "25.01.2025 – 26.01.2025", "rink": "Vierumäki"})
    assert event == {"dates": "25.01.2025 – 26.01.2025", "rink": "Vierumäki",
                     "datesAuto": True}


def test_schedule_meta_replaces_an_auto_filled_date():
    """The platform seeded only the start date; the schedule knows the full span."""
    event = {"dates": "25.01.2025", "datesAuto": True, "rink": "Vierumäki"}
    fa._apply_schedule_meta(event, {"dates": "25.01.2025 – 26.01.2025", "rink": "Other Hall"})
    assert event["dates"] == "25.01.2025 – 26.01.2025"
    assert event["datesAuto"] is True          # a re-parse may refine it again
    assert event["rink"] == "Vierumäki"        # the platform's venue wins


def test_schedule_meta_keeps_a_user_reviewed_date():
    event = {"dates": "25.-26.1.2025", "rink": ""}
    fa._apply_schedule_meta(event, {"dates": "25.01.2025 – 26.01.2025"})
    assert event["dates"] == "25.-26.1.2025"
    assert "datesAuto" not in event


def test_saving_the_event_form_clears_the_auto_flag(table):
    table.rows["abc12345"] = entity("abc12345", "Spring Trophy 2026", PlatformId="p-1")
    structure = seed_structure(table, "abc12345", dates="25.01.2025", datesAuto=True)
    req = func.HttpRequest(
        "POST", "/api/save_event_settings",
        headers={"x-forwarded-user-email": "organizer@example.com"},
        body=json.dumps({"id": "abc12345",
                         "event": {"dates": "25.-26.1.2025"}}).encode("utf-8"))
    body = payload(fa.save_event_settings._function.get_user_function()(req))
    assert body["event"]["dates"] == "25.-26.1.2025"
    assert "datesAuto" not in body["event"]
    assert "datesAuto" not in structure["event"]

    # A later schedule re-parse must now leave the reviewed value alone.
    fa._apply_schedule_meta(structure["event"], {"dates": "25.01.2025 – 26.01.2025"})
    assert structure["event"]["dates"] == "25.-26.1.2025"


# ── request validation ───────────────────────────────────────────────────────

def test_a_missing_platform_id_is_a_bad_request(table):
    assert resolve(platform_id="  ").status_code == 400


def test_a_name_that_sanitizes_away_is_a_bad_request(table):
    assert resolve(name="///").status_code == 400
    assert table.rows == {}


def test_an_unauthenticated_request_is_rejected(table):
    assert resolve(email=None).status_code == 401
