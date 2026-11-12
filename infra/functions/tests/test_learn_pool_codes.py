"""Learning category codes from the FSM reports in the competition pool —
`function_app._learn_codes_from_pool` and its three triggers: the schedule
parse, a roster import on code-less categories, and the `learn_pool_codes` route
the site calls when a competition still has code-less categories.

A Finnish-only schedule PDF gives names and no codes; the start lists FS Manager
pushes for every segment give the same names *with* their codes. The teams below
register by code, so without the learning step they could not be placed at all.
Everything here is invented.
"""
import json

import azure.functions as func
import pytest

import function_app as fa
import storage_helpers as sh
import structure as st

from conftest import FakeContainerClient, auth_headers, schedule_pdf, sheet_pdf

EMAIL = "organizer@example.com"
FOLDER = "Spring Trophy 2026-abc12345"
SCHEDULE_NAME = "FSK-------------------------------_CompetitionSchedule.pdf"

SCHEDULE = schedule_pdf(
    "15.11.2026",
    "11:00:0011:50:00  5  Noviisit, Mupi   Free Skating",
    "11:50:0012:34:00  4  Aikuiset Mupi   Free Skating",
    "12:34:0013:41:00  7  Minorit   Free Skating 1",
    "13:41:0014:43:00  7  Minorit   Free Skating 2",
    "14:43:0015:39:00  6  Juniorit   Free Skating",
    "16:11:0017:19:00  8  SM-Noviisit   Free Skating",
    "17:19:0017:37:00  3  SM-Juniorit   Free Skating")

START_LISTS = {
    "FSKXSYNCHRONMLKNOV----FNL-000100--_StartListwithTimes.pdf": "NOVIISIT, MUPI FREE SKATING",
    "FSKXSYNCHRONMLAIKU----FNL-000100--_StartListwithTimes.pdf": "AIKUISET MUPI FREE SKATING",
    "FSKXSYNCHRONMLMINO----FNL-000100--_StartListwithTimes.pdf": "MINORIT FREE SKATING 1",
    "FSKXSYNCHRONMLMINO----FNL-000200--_StartListwithTimes.pdf": "MINORIT FREE SKATING 2",
    "FSKXSYNCHRONMLKJUN----FNL-000100--_StartListwithTimes.pdf": "JUNIORIT FREE SKATING",
    "FSKXSYNCHRONADVNOV----FNL-000100--_StartListwithTimes.pdf": "SM-NOVIISIT FREE SKATING",
    "FSKXSYNCHRONJUNIOR----FNL-000100--_StartListwithTimes.pdf": "SM-JUNIORIT FREE SKATING",
}

EXPECTED = {
    "Noviisit, Mupi": "FSKXSYNCHRONMLKNOV", "Aikuiset Mupi": "FSKXSYNCHRONMLAIKU",
    "Minorit": "FSKXSYNCHRONMLMINO", "Juniorit": "FSKXSYNCHRONMLKJUN",
    "SM-Noviisit": "FSKXSYNCHRONADVNOV", "SM-Juniorit": "FSKXSYNCHRONJUNIOR",
}


def team(code, name, org, event):
    return (f'    <Team Code="{code}" Organisation="{org}" Name="{name}">'
            f'<RegisteredEvent Event="{event}" /><Composition /></Team>')


TEAMS_XML = "\n".join([
    '<?xml version="1.0" encoding="UTF-8"?>',
    '<OdfBody DocumentType="DT_PARTIC_TEAMS"><Competition>',
    team("T1", "Blue Herons", "BHK", "FSKXSYNCHRONMLKJUN----------------"),
    team("T2", "Silver Comets", "SCK", "FSKXSYNCHRONADVNOV----------------"),
    team("T3", "Northern Lights", "NLK", "FSKXSYNCHRONJUNIOR----------------"),
    team("T4", "Golden Arrows", "GAK", "FSKXSYNCHRONMLKNOV----------------"),
    team("T5", "Ice Owls", "IOK", "FSKXSYNCHRONMLMINO----------------"),
    team("T6", "River Swans", "RSK", "FSKXSYNCHRONMLAIKU----------------"),
    '</Competition></OdfBody>',
])
TEAMS_NAME = "DT_PARTIC_TEAMS_FSK-------------------------------.xml"


@pytest.fixture
def comp(storage):
    return storage.competition(PlatformId="p-1")


@pytest.fixture
def pool(monkeypatch):
    blobs = {f"p-1/fsm/{SCHEDULE_NAME}": SCHEDULE, f"p-1/fsm/{TEAMS_NAME}": TEAMS_XML.encode()}
    for name, line in START_LISTS.items():
        blobs[f"p-1/fsm/{name}"] = sheet_pdf("SPRING TROPHY 2026", "TIME SCHEDULE", line,
                                             "printed: 12.11.2026 17.25")
    fake = FakeContainerClient(blobs)
    monkeypatch.setattr(sh, "get_platform_container_client", lambda: fake)
    return fake


def post(route, body=None, params=None, email=EMAIL):
    req = func.HttpRequest("POST", f"/api/{route}", headers=auth_headers(email) if email else {},
                           params=params or {}, body=json.dumps(body).encode() if body else b"")
    return getattr(fa, route)._function.get_user_function()(req)


def payload(response):
    assert response.status_code == 200, response.get_body()
    return json.loads(response.get_body())


def codes(structure):
    return {c["name"]: c["code"] for c in structure["categories"]}


def disciplines(structure):
    return {c["discipline"] for c in structure["categories"]}


# ── the schedule parse ────────────────────────────────────────────────────────

def test_a_pool_schedule_parse_learns_codes_and_disciplines(comp, pool):
    body = payload(post("parse_schedule", params={"competition": "abc12345",
                                                  "poolName": SCHEDULE_NAME, "source": "fsm"}))
    assert body["learnedCodes"] == 6
    assert codes(comp) == EXPECTED
    assert disciplines(comp) == {"synchro"}
    # Two start lists of "Minorit" (segments 1 and 2) carry one code.
    assert comp["categories"][2]["code"] == "FSKXSYNCHRONMLMINO"


def test_a_body_upload_on_a_bound_competition_learns_too(comp, pool):
    req = func.HttpRequest("POST", "/api/parse_schedule", headers=auth_headers(EMAIL),
                           params={"competition": "abc12345"}, body=SCHEDULE)
    body = payload(fa.parse_schedule._function.get_user_function()(req))
    assert body["learnedCodes"] == 6 and codes(comp) == EXPECTED


def test_an_unbound_competition_learns_nothing(storage, pool):
    structure = storage.competition()        # no PlatformId
    req = func.HttpRequest("POST", "/api/parse_schedule", headers=auth_headers(EMAIL),
                           params={"competition": "abc12345"}, body=SCHEDULE)
    body = payload(fa.parse_schedule._function.get_user_function()(req))
    assert body["learnedCodes"] == 0
    assert set(codes(structure).values()) == {""}
    assert disciplines(structure) == {"single"}


def test_pool_trouble_never_fails_the_parse(comp, pool, monkeypatch):
    def broken(name_starts_with=""):
        raise RuntimeError("listing failed")
    monkeypatch.setattr(pool, "list_blobs", broken)
    body = payload(post("parse_schedule", params={"competition": "abc12345",
                                                  "poolName": SCHEDULE_NAME, "source": "fsm"}))
    assert body["categories"] == 6 and body["learnedCodes"] == 0


def test_an_unreadable_report_is_skipped_not_fatal(comp, pool):
    pool.blobs["p-1/fsm/FSKXSYNCHRONMLKSEN----FNL-000100--_StartListwithTimes.pdf"] = b"%PDF-1.4 junk"
    body = payload(post("parse_schedule", params={"competition": "abc12345",
                                                  "poolName": SCHEDULE_NAME, "source": "fsm"}))
    assert body["learnedCodes"] == 6


def test_an_oversized_report_is_not_read(comp, pool, monkeypatch):
    import pool_codes
    monkeypatch.setattr(pool_codes, "MAX_SAMPLE_BYTES", 10)
    body = payload(post("parse_schedule", params={"competition": "abc12345",
                                                  "poolName": SCHEDULE_NAME, "source": "fsm"}))
    assert body["learnedCodes"] == 0


# ── the roster import ─────────────────────────────────────────────────────────

def test_a_roster_import_learns_the_codes_it_needs_first(comp, pool):
    """The schedule was parsed before the start lists existed; the import still
    places every team because it learns the codes on the way in."""
    rows, cats, _meta = fa.parse_schedule_data(SCHEDULE)
    comp["categories"], comp["schedule"] = cats, rows
    assert set(codes(comp).values()) == {""}
    data = payload(post("import_rosters", {"id": "abc12345",
                                           "teamsPool": {"name": TEAMS_NAME, "source": "fsm"}}))
    assert data["imported"] == 6 and data["report"]["unmatched"] == []
    placed = {t["name"]: c["name"] for c in comp["categories"] for t in c["teams"]}
    assert placed == {"Blue Herons": "Juniorit", "Silver Comets": "SM-Noviisit",
                      "Northern Lights": "SM-Juniorit", "Golden Arrows": "Noviisit, Mupi",
                      "Ice Owls": "Minorit", "River Swans": "Aikuiset Mupi"}
    assert codes(comp) == EXPECTED


def test_a_roster_import_with_codes_in_place_reads_no_report(comp, pool, monkeypatch):
    rows, cats, _meta = fa.parse_schedule_data(SCHEDULE)
    comp["categories"], comp["schedule"] = cats, rows
    for cat in comp["categories"]:
        cat["code"] = EXPECTED[cat["name"]]
    reads = []
    original = pool.get_blob_client
    monkeypatch.setattr(pool, "get_blob_client",
                        lambda path: reads.append(path) or original(path))
    payload(post("import_rosters", {"id": "abc12345",
                                    "teamsPool": {"name": TEAMS_NAME, "source": "fsm"}}))
    assert reads == [f"p-1/fsm/{TEAMS_NAME}"]


# ── the route ─────────────────────────────────────────────────────────────────

def test_the_route_learns_and_rematches_the_unplaced_teams(comp, pool, storage):
    rows, cats, _meta = fa.parse_schedule_data(SCHEDULE)
    comp["categories"], comp["schedule"] = cats, rows
    # An import that ran before the start lists were pushed: names only, so
    # "Advanced Novice" and the Finnish-federation events found nothing.
    storage.container.upload_blob(f"{FOLDER}/rosters/teams.xml", TEAMS_XML.encode())
    comp["rosterImport"] = {"at": "2026-11-12T15:37:06Z", "imported": 0, "moved": 0,
                            "unmatched": [{"name": "Silver Comets", "org": "SCK",
                                           "eventLabel": "Advanced Novice",
                                           "reason": "no category matches event Advanced Novice"}],
                            "withdrawn": []}
    body = payload(post("learn_pool_codes", {"id": "abc12345"}))
    assert len(body["learned"]) == 6
    assert codes(comp) == EXPECTED
    assert comp["rosterImport"]["unmatched"] == []
    assert comp["rosterImport"]["imported"] == 6
    assert body["structure"]["categories"][0]["discipline"] == "synchro"


def test_the_route_answers_an_empty_map_when_there_is_nothing_to_learn(comp, pool):
    rows, cats, _meta = fa.parse_schedule_data(SCHEDULE)
    comp["categories"] = cats
    for cat in comp["categories"]:
        cat["code"] = EXPECTED[cat["name"]]
    assert payload(post("learn_pool_codes", {"id": "abc12345"}))["learned"] == {}


def test_the_route_guards(comp, pool):
    assert post("learn_pool_codes", {"id": "abc12345"}, email=None).status_code == 401
    assert post("learn_pool_codes", {}).status_code == 400
    assert post("learn_pool_codes", {"id": "nosuchid"}).status_code == 404
