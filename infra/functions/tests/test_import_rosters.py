"""`function_app.import_rosters` — the route's request contract.

The body carries two *optional* XML strings, and the interesting case is the
asymmetry between them: a DT_PARTIC_TEAMS file is what rosters are built from,
while a body without one is the supported "re-match" — the teams archived under
`rosters/` by an earlier import are matched again, which is how a competition
picks up teams once the missing Total Results PDFs arrive. That only works when
there *is* an archive, hence the 409, whose text the UI shows to the organizer
verbatim.

The XML below is invented: real DT_PARTIC exports carry minors' personal data
and must never be committed. It only reproduces the shape of the real files.
"""
import json

import azure.functions as func
import pytest

import function_app as fa
import storage_helpers as sh
import structure as st

from conftest import FakeContainerClient

EMAIL = "organizer@example.com"
COMP_ID = "abc12345"
FOLDER = "Spring Trophy 2026-abc12345"

TULO = "FSKXSYNCHRONMLTULO----------------"

TEAMS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<OdfBody DocumentType="DT_PARTIC_TEAMS">
  <Competition>
    <Team Code="SYNCHRO0001" Organisation="BHK " Name="Blue Herons ">
      <RegisteredEvent Event="FSKXSYNCHRONMLTULO----------------" />
      <Composition>
        <Athlete Code="A0001" Order="1" />
        <Athlete Code="A0002" Order="2" />
      </Composition>
    </Team>
  </Competition>
</OdfBody>
"""

PARTIC_XML = """<?xml version="1.0" encoding="UTF-8"?>
<OdfBody DocumentType="DT_PARTIC">
  <Competition>
    <Participant Code="A0001" GivenName="Aino" FamilyName="Koskinen " />
    <Participant Code="A0002" GivenName="Bertil" FamilyName="Ahlberg" />
  </Competition>
</OdfBody>
"""

EMPTY_TEAMS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<OdfBody DocumentType="DT_PARTIC_TEAMS"><Competition /></OdfBody>
"""

# The same two athletes with a corrected given name, to tell a freshly supplied
# DT_PARTIC apart from the archived one.
CORRECTED_PARTIC_XML = PARTIC_XML.replace('GivenName="Aino"', 'GivenName="Aino Maria"')


@pytest.fixture
def comp(storage):
    """A competition with the one synchro block the sample team registered for."""
    structure = storage.competition()
    category = st.new_category("Tulokkaat, Mupi L1", "synchro", 0)
    category["code"] = "FSKXSYNCHRONMLTULO--01"
    structure["categories"].append(category)
    return structure


def import_rosters(comp_id=COMP_ID, email=EMAIL, **body):
    body.setdefault("id", comp_id)
    req = func.HttpRequest(
        "POST", "/api/import_rosters",
        headers={"x-forwarded-user-email": email} if email else {},
        body=json.dumps(body).encode())
    return fa.import_rosters._function.get_user_function()(req)


def payload(response):
    assert response.status_code == 200, response.get_body()
    return json.loads(response.get_body())


def teams_of(structure, index=0):
    return structure["categories"][index]["teams"]


# ── a full import ─────────────────────────────────────────────────────────────

def test_a_teams_file_places_its_teams_and_archives_both_xmls(comp, storage):
    data = payload(import_rosters(teamsXml=TEAMS_XML, particXml=PARTIC_XML))
    assert data["imported"] == 1 and data["categories"] == 1
    team = teams_of(comp)[0]
    assert team["name"] == "Blue Herons" and team["org"] == "BHK"
    assert team["members"] == ["AHLBERG Bertil", "KOSKINEN Aino"]
    # Archived so a later re-match needs no re-upload.
    assert f"{FOLDER}/rosters/teams.xml" in storage.container.blobs
    assert f"{FOLDER}/rosters/partic.xml" in storage.container.blobs


def test_a_teams_file_without_a_partic_file_falls_back_to_athlete_codes(comp, storage):
    assert payload(import_rosters(teamsXml=TEAMS_XML))["imported"] == 1
    assert teams_of(comp)[0]["members"] == ["A0001", "A0002"]
    assert f"{FOLDER}/rosters/partic.xml" not in storage.container.blobs


# ── the partic-only re-match ──────────────────────────────────────────────────

def test_a_partic_only_body_rematches_the_archived_roster(comp, storage):
    storage.container.upload_blob(f"{FOLDER}/rosters/teams.xml", TEAMS_XML.encode())
    data = payload(import_rosters(particXml=PARTIC_XML))
    assert data["imported"] == 1
    assert teams_of(comp)[0]["name"] == "Blue Herons"


def test_a_partic_only_body_uses_the_participants_it_supplied(comp, storage):
    """The point of the partic-only import: the archive holds TEAMS but no names,
    so the members must come out of the file the organizer just picked. Falling
    back to the archive (there is none) would leave bare athlete codes."""
    storage.container.upload_blob(f"{FOLDER}/rosters/teams.xml", TEAMS_XML.encode())
    payload(import_rosters(particXml=PARTIC_XML))
    assert teams_of(comp)[0]["members"] == ["AHLBERG Bertil", "KOSKINEN Aino"]


def test_a_partic_only_body_archives_the_partic_it_supplied(comp, storage):
    # Archived like a full pair import archives it, so the next re-match — the
    # automatic one after a Total Results PDF included — keeps the names.
    storage.container.upload_blob(f"{FOLDER}/rosters/teams.xml", TEAMS_XML.encode())
    payload(import_rosters(particXml=PARTIC_XML))
    assert storage.container.blobs[f"{FOLDER}/rosters/partic.xml"] == PARTIC_XML.encode()


def test_a_supplied_partic_wins_over_the_archived_one(comp, storage):
    """The archive is read only for what the call did not bring, so re-importing a
    corrected DT_PARTIC actually corrects the names."""
    storage.container.upload_blob(f"{FOLDER}/rosters/teams.xml", TEAMS_XML.encode())
    storage.container.upload_blob(f"{FOLDER}/rosters/partic.xml", PARTIC_XML.encode())
    payload(import_rosters(particXml=CORRECTED_PARTIC_XML))
    assert teams_of(comp)[0]["members"] == ["AHLBERG Bertil", "KOSKINEN Aino Maria"]
    assert storage.container.blobs[f"{FOLDER}/rosters/partic.xml"] == \
        CORRECTED_PARTIC_XML.encode()


def test_a_body_with_no_xml_at_all_rematches_the_archived_roster(comp, storage):
    storage.container.upload_blob(f"{FOLDER}/rosters/teams.xml", TEAMS_XML.encode())
    storage.container.upload_blob(f"{FOLDER}/rosters/partic.xml", PARTIC_XML.encode())
    assert payload(import_rosters())["imported"] == 1
    assert teams_of(comp)[0]["members"] == ["AHLBERG Bertil", "KOSKINEN Aino"]


def test_a_partic_only_body_without_an_archive_is_a_409(comp):
    response = import_rosters(particXml=PARTIC_XML)
    assert response.status_code == 409
    # Shown to the organizer verbatim, so it has to name the missing file.
    assert "DT_PARTIC_TEAMS" in response.get_body().decode()


def test_a_teams_file_holding_no_teams_is_a_422(comp):
    response = import_rosters(teamsXml=EMPTY_TEAMS_XML)
    assert response.status_code == 422
    assert "DT_PARTIC_TEAMS" in response.get_body().decode()


# ── guards ────────────────────────────────────────────────────────────────────

def test_an_unauthenticated_call_is_a_401(comp):
    assert import_rosters(email=None, teamsXml=TEAMS_XML).status_code == 401


def test_a_body_without_an_id_is_a_400(comp):
    req = func.HttpRequest(
        "POST", "/api/import_rosters",
        headers={"x-forwarded-user-email": EMAIL},
        body=json.dumps({"teamsXml": TEAMS_XML}).encode())
    assert fa.import_rosters._function.get_user_function()(req).status_code == 400


def test_an_unknown_competition_is_a_404(comp):
    assert import_rosters(comp_id="nosuchid", teamsXml=TEAMS_XML).status_code == 404


# ── from the shared competition files ─────────────────────────────────────────
# FS Manager pushes both roster files into the platform pool over HOVTP
# (`<guid>/fsm/DT_PARTIC_TEAMS_<code>.xml`, `DT_PARTIC_<code>.xml`), and people can
# upload them there too. A body may reference either file by pool name instead
# of carrying it.

TEAMS_NAME = "DT_PARTIC_TEAMS_FSK-------------------------------.xml"
PARTIC_NAME = "DT_PARTIC_FSK-------------------------------.xml"


@pytest.fixture
def bound(storage):
    """`comp`, bound to platform competition `p-1`."""
    structure = storage.competition(PlatformId="p-1")
    category = st.new_category("Tulokkaat, Mupi L1", "synchro", 0)
    category["code"] = "FSKXSYNCHRONMLTULO--01"
    structure["categories"].append(category)
    return structure


@pytest.fixture
def pool(monkeypatch):
    """The platform's competition-data container: FSM's pair in `fsm/`, and in
    `uploads/` a TEAMS file under the PARTIC name (picked by name, wrong inside)
    plus a corrected DT_PARTIC."""
    fake = FakeContainerClient({
        f"p-1/fsm/{TEAMS_NAME}": TEAMS_XML.encode(),
        f"p-1/fsm/{PARTIC_NAME}": PARTIC_XML.encode(),
        f"p-1/uploads/{PARTIC_NAME}": TEAMS_XML.encode(),
        "p-1/uploads/corrected.xml": CORRECTED_PARTIC_XML.encode(),
    })
    monkeypatch.setattr(sh, "get_platform_container_client", lambda: fake)
    return fake


def fsm(name):
    return {"name": name, "source": "fsm"}


def error_of(response):
    return json.loads(response.get_body())["error"]


def test_both_files_can_come_from_the_pool(bound, storage, pool):
    data = payload(import_rosters(teamsPool=fsm(TEAMS_NAME), particPool=fsm(PARTIC_NAME)))
    assert data["imported"] == 1
    assert teams_of(bound)[0]["members"] == ["AHLBERG Bertil", "KOSKINEN Aino"]
    assert data["source"] == {"teams": {"poolName": TEAMS_NAME, "source": "fsm"},
                              "partic": {"poolName": PARTIC_NAME, "source": "fsm"}}
    # Archived like an upload, so the automatic re-match works the same.
    assert storage.container.blobs[f"{FOLDER}/rosters/teams.xml"] == TEAMS_XML.encode()
    assert storage.container.blobs[f"{FOLDER}/rosters/partic.xml"] == PARTIC_XML.encode()


def test_the_report_records_which_pool_files_are_in_use(bound, pool):
    payload(import_rosters(teamsPool=fsm(TEAMS_NAME), particPool=fsm(PARTIC_NAME)))
    files = bound["rosterImport"]["files"]
    assert files["teams"]["poolName"] == TEAMS_NAME and files["teams"]["source"] == "fsm"
    assert files["partic"]["poolName"] == PARTIC_NAME
    assert files["teams"]["at"] == bound["rosterImport"]["at"]


def test_a_teams_only_pool_import_falls_back_to_athlete_codes(bound, pool):
    payload(import_rosters(teamsPool=fsm(TEAMS_NAME)))
    assert teams_of(bound)[0]["members"] == ["A0001", "A0002"]
    assert "partic" not in bound["rosterImport"]["files"]


def test_a_pool_partic_adds_names_to_the_archived_roster(bound, storage, pool):
    payload(import_rosters(teamsPool=fsm(TEAMS_NAME)))
    teams_entry = bound["rosterImport"]["files"]["teams"]
    payload(import_rosters(particPool={"name": "corrected.xml"}))   # source defaults to upload
    assert teams_of(bound)[0]["members"] == ["AHLBERG Bertil", "KOSKINEN Aino Maria"]
    files = bound["rosterImport"]["files"]
    # The re-matched TEAMS is still the one from FSM, imported when it was.
    assert files["teams"] == teams_entry
    assert files["partic"]["poolName"] == "corrected.xml"
    assert files["partic"]["source"] == "upload"


def test_a_fresh_teams_file_drops_the_partic_it_is_not_joined_with(bound, pool):
    payload(import_rosters(teamsPool=fsm(TEAMS_NAME), particPool=fsm(PARTIC_NAME)))
    payload(import_rosters(teamsXml=TEAMS_XML))
    assert set(bound["rosterImport"]["files"]) == {"teams"}
    assert "poolName" not in bound["rosterImport"]["files"]["teams"]


def test_the_automatic_rematch_keeps_the_file_record(bound, storage, pool):
    """A Total Results PDF re-runs the match without supplying any file; the
    record of what was imported (and when) must survive, or an older pool file
    would look newer than the import and the UI would offer it again."""
    payload(import_rosters(teamsPool=fsm(TEAMS_NAME), particPool=fsm(PARTIC_NAME)))
    files = bound["rosterImport"]["files"]
    fa._run_roster_match(bound, storage.container, FOLDER)
    assert bound["rosterImport"]["files"] == files


def test_inline_xml_wins_over_a_pool_reference(bound, pool):
    payload(import_rosters(teamsPool=fsm(TEAMS_NAME), particXml=CORRECTED_PARTIC_XML,
                           particPool=fsm(PARTIC_NAME)))
    assert teams_of(bound)[0]["members"] == ["AHLBERG Bertil", "KOSKINEN Aino Maria"]
    assert "poolName" not in bound["rosterImport"]["files"]["partic"]


def test_a_file_of_the_other_type_is_refused(bound, storage, pool):
    response = import_rosters(teamsPool=fsm(TEAMS_NAME), particPool={"name": PARTIC_NAME})
    assert response.status_code == 400 and error_of(response) == "wrong_document_type"
    assert teams_of(bound) == []
    assert f"{FOLDER}/rosters/teams.xml" not in storage.container.blobs


def test_the_pool_name_cannot_leave_the_competition_folder(bound, pool):
    # Another competition's TEAMS file holds no teams: reading it would be a 422.
    pool.blobs["p-2/fsm/" + TEAMS_NAME] = EMPTY_TEAMS_XML.encode()
    data = payload(import_rosters(teamsPool=fsm("../../p-2/fsm/" + TEAMS_NAME)))
    assert data["imported"] == 1
    assert data["source"]["teams"]["poolName"] == TEAMS_NAME


def test_a_missing_pool_file_is_a_404(bound, pool):
    response = import_rosters(teamsPool=fsm("DT_PARTIC_TEAMS_other.xml"))
    assert response.status_code == 404 and error_of(response) == "pool_file_not_found"


def test_an_invalid_source_is_a_400(bound, pool):
    response = import_rosters(teamsPool={"name": TEAMS_NAME, "source": "elsewhere"})
    assert response.status_code == 400 and error_of(response) == "invalid_source"


def test_a_reference_without_a_name_is_a_400(bound, pool):
    response = import_rosters(teamsPool={"source": "fsm"})
    assert response.status_code == 400 and error_of(response) == "missing_parameter"


def test_an_unbound_competition_cannot_read_the_pool(comp, pool):
    response = import_rosters(teamsPool=fsm(TEAMS_NAME))
    assert response.status_code == 409 and error_of(response) == "not_bound"


def test_an_unconfigured_platform_is_a_503(bound, monkeypatch):
    monkeypatch.setattr(sh, "get_platform_container_client", lambda: None)
    response = import_rosters(teamsPool=fsm(TEAMS_NAME))
    assert response.status_code == 503 and error_of(response) == "platform_not_configured"


def test_a_rematch_of_an_older_import_records_no_files(bound, storage):
    """Imported before `files` existed: the automatic re-match has nothing to
    record, and leaving the key out lets the UI fall back on `at`."""
    storage.container.upload_blob(f"{FOLDER}/rosters/teams.xml", TEAMS_XML.encode())
    bound["rosterImport"] = {"at": "2026-01-01T00:00:00Z", "imported": 1, "moved": 0,
                             "unmatched": [], "withdrawn": []}
    fa._run_roster_match(bound, storage.container, FOLDER)
    assert "files" not in bound["rosterImport"]
