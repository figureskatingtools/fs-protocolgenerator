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
import structure as st

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
