"""Dates of birth never reach this app's storage.

FS Manager's participant files carry each skater's `BirthDate`. The platform
strips it from what FS Manager pushes and users upload to the shared competition
files; this app strips it from everything it stores itself: a direct upload
(`upload_file`), a pool import (`import_platform_file`, already clean, stripped
again), the roster XML `import_rosters` archives under `rosters/` (inline or from
the pool) and a participant file dropped on the schedule box.

`odf_birth_dates.py` is a byte-identical copy of the site's code; the stripping
itself is tested in depth there (infra/hovtp/tests/test_birth_dates.py). The
fixtures below are that suite's, invented — they only reproduce the shape of the
real exports, which carry minors' personal data and must never be committed.
"""
import hashlib
import json
import os

import azure.functions as func
import pytest

import dt_partic
import function_app as fa
import odf_birth_dates as obd
import storage_helpers as sh
import structure as st

from conftest import FakeContainerClient, auth_headers

EMAIL = "organizer@example.com"
COMP_ID = "abc12345"
FOLDER = "Spring Trophy 2026-abc12345"

# ── fixtures (copied from figureskatingtools-site infra/hovtp/tests/fixtures.py) ──

COMPETITION_CODE_RAW = "YL110926HTL"


def odf(attributes: dict, inner: str = "") -> bytes:
    """`<OdfBody …>` with the given attributes and body, as FSM would send it."""
    rendered = " ".join(f'{key}="{value}"' for key, value in attributes.items())
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f"<OdfBody {rendered}>{inner}</OdfBody>\n"
    ).encode("utf-8")


DT_PARTIC_BODY = odf({
    "CompetitionCode": COMPETITION_CODE_RAW,
    "DocumentCode": "FSK-------------------------------",
    "DocumentType": "DT_PARTIC",
    "Version": "1",
    "FeedFlag": "P",
    "Date": "2026-09-01",
    "Time": "180000000",
    "LogicalDate": "2026-09-01",
    "Source": "FSKFSK1",
}, "<Competition><Participant Code=\"1234567\"/></Competition>")

_PARTIC_HEAD = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    f'<OdfBody CompetitionCode="{COMPETITION_CODE_RAW}" '
    'DocumentCode="FSK-------------------------------" DocumentType="DT_PARTIC" '
    'Version="1" FeedFlag="P" Date="2026-09-01" Time="180000000" '
    'LogicalDate="2026-09-01" Source="FSKFSK1">\n'
    '  <Competition>\n'
)

DT_PARTIC_BIRTHDATES_BODY = (
    _PARTIC_HEAD
    + '    <Participant Code="1234567" Parent="1234567" Status="ACTIVE" GivenName="Aino" '
      'FamilyName="Koskinen " PrintName="KOSKINEN Aino" Gender="W" Organisation="FIN" '
      'BirthDate="2012-03-04" Current="true">\n'
      '      <Discipline Code="FSK-------------------------------">\n'
      '        <RegisteredEvent Event="FSKWSINGLES-ADVNOV----------------"/>\n'
      '      </Discipline>\n'
      '    </Participant>\n'
      "    <Participant Code='1234568' GivenName='Bertil' FamilyName='Ahlberg' "
      "birthdate = '2011-12-31' Organisation='SWE'/>\n"
      '    <Participant Code="1234569" GivenName="Cecilia" FamilyName="Lindgren">\n'
      '      <BirthDate>2013-01-15</BirthDate>\n'
      '      <!-- BirthDate="this comment is not a birth date and stays" -->\n'
      '    </Participant>\n'
      '  </Competition>\n'
      '</OdfBody>\n'
).encode("utf-8")

DT_PARTIC_BIRTHDATES_STRIPPED = (
    _PARTIC_HEAD
    + '    <Participant Code="1234567" Parent="1234567" Status="ACTIVE" GivenName="Aino" '
      'FamilyName="Koskinen " PrintName="KOSKINEN Aino" Gender="W" Organisation="FIN" '
      'Current="true">\n'
      '      <Discipline Code="FSK-------------------------------">\n'
      '        <RegisteredEvent Event="FSKWSINGLES-ADVNOV----------------"/>\n'
      '      </Discipline>\n'
      '    </Participant>\n'
      "    <Participant Code='1234568' GivenName='Bertil' FamilyName='Ahlberg' "
      "Organisation='SWE'/>\n"
      '    <Participant Code="1234569" GivenName="Cecilia" FamilyName="Lindgren">\n'
      '      \n'
      '      <!-- BirthDate="this comment is not a birth date and stays" -->\n'
      '    </Participant>\n'
      '  </Competition>\n'
      '</OdfBody>\n'
).encode("utf-8")

_TEAMS_HEAD = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<OdfBody xmlns="http://www.swisstiming.com/odf" xmlns:st="http://www.swisstiming.com/ext" '
    f'CompetitionCode="{COMPETITION_CODE_RAW}" '
    'DocumentCode="FSK-------------------------------" DocumentType="DT_PARTIC_TEAMS" '
    'Version="1" Source="FSKFSK1">\n'
    '  <Competition>\n'
    '    <Team Code="SYNCHRO0001" Organisation="BHK " Name="Blue Herons " Gender="X">\n'
    '      <RegisteredEvent Event="" />\n'
    '      <RegisteredEvent Event="FSKXSYNCHRONMLTULO----------------" />\n'
    '      <Composition>\n'
)

DT_PARTIC_TEAMS_BIRTHDATES_BODY = (
    _TEAMS_HEAD
    + '        <Athlete Code="1234567" Order="1">\n'
      '          <Description GivenName="Aino" FamilyName="Koskinen" BirthDate="2012-03-04" '
      'Gender="W"/>\n'
      '        </Athlete>\n'
      "        <Athlete Code=\"1234568\" Order=\"2\"><Description st:BirthDate='2011-12-31' "
      "GivenName='Bertil'/></Athlete>\n"
      '      </Composition>\n'
      '    </Team>\n'
      '  </Competition>\n'
      '</OdfBody>\n'
).encode("utf-8")

DT_PARTIC_TEAMS_BIRTHDATES_STRIPPED = (
    _TEAMS_HEAD
    + '        <Athlete Code="1234567" Order="1">\n'
      '          <Description GivenName="Aino" FamilyName="Koskinen" '
      'Gender="W"/>\n'
      '        </Athlete>\n'
      "        <Athlete Code=\"1234568\" Order=\"2\"><Description "
      "GivenName='Bertil'/></Athlete>\n"
      '      </Composition>\n'
      '    </Team>\n'
      '  </Competition>\n'
      '</OdfBody>\n'
).encode("utf-8")

# Not a participant file, so not touched even though it (artificially) has one.
DT_SCHEDULE_WITH_BIRTHDATE_BODY = odf({
    "CompetitionCode": COMPETITION_CODE_RAW,
    "DocumentCode": "FSK-------------------------------",
    "DocumentType": "DT_SCHEDULE",
    "Version": "1",
    "Source": "FSKFSK1",
}, '<Competition><Session Code="FSKWSINGLES" BirthDate="2012-03-04"/></Competition>')

# A participant file whose birth dates the byte-level pass cannot reach (UTF-16,
# so no ASCII pattern matches): must be refused, never stored as it came.
DT_PARTIC_UTF16_BIRTHDATES_BODY = (
    '<?xml version="1.0" encoding="utf-16"?>\n'
    f'<OdfBody CompetitionCode="{COMPETITION_CODE_RAW}" '
    'DocumentCode="FSK-------------------------------" DocumentType="DT_PARTIC" '
    'Version="1" Source="FSKFSK1"><Competition>'
    '<Participant Code="1234567" BirthDate="2012-03-04"/></Competition></OdfBody>\n'
).encode("utf-16")

# ── this repo's own ───────────────────────────────────────────────────────────

# A participant file cut short: not XML, so its birth dates cannot be removed.
BROKEN_PARTIC = DT_PARTIC_BIRTHDATES_BODY.replace(b"</Competition>", b"</Competitio>")
PDF = b"%PDF-1.4\n% BirthDate=\"2012-03-04\"\n%%EOF"


def _payload(response):
    return json.loads(response.get_body())


def _refused(response):
    assert response.status_code == 422, response.get_body()
    assert _payload(response)["error"] == "birth_dates_not_removed"


@pytest.fixture
def comp(storage):
    """A bound competition with the synchro block the fixture team registered for."""
    structure = storage.competition(PlatformId="p-1")
    category = st.new_category("Tulokkaat, Mupi L1", "synchro", 0)
    category["code"] = "FSKXSYNCHRONMLTULO--01"
    structure["categories"].append(category)
    return structure


@pytest.fixture
def pool(monkeypatch):
    """The platform's competition-data container for `p-1`."""
    fake = FakeContainerClient({
        "p-1/uploads/DT_PARTIC.xml": DT_PARTIC_BIRTHDATES_BODY,
        "p-1/fsm/DT_PARTIC_TEAMS.xml": DT_PARTIC_TEAMS_BIRTHDATES_BODY,
        "p-1/uploads/utf16.xml": DT_PARTIC_UTF16_BIRTHDATES_BODY,
    })
    monkeypatch.setattr(sh, "get_platform_container_client", lambda: fake)
    return fake


def upload(filename, body):
    req = func.HttpRequest(
        "POST", "/api/upload_file", headers=auth_headers(EMAIL),
        params={"competition": COMP_ID, "filename": filename}, body=body)
    return fa.upload_file._function.get_user_function()(req)


def import_platform_file(name, source="upload"):
    req = func.HttpRequest(
        "POST", "/api/import_platform_file", headers=auth_headers(EMAIL),
        params={"competition": COMP_ID, "name": name, "source": source}, body=b"")
    return fa.import_platform_file._function.get_user_function()(req)


def import_rosters(**body):
    body.setdefault("id", COMP_ID)
    req = func.HttpRequest("POST", "/api/import_rosters", headers=auth_headers(EMAIL),
                           body=json.dumps(body).encode())
    return fa.import_rosters._function.get_user_function()(req)


def parse_schedule(body):
    req = func.HttpRequest("POST", "/api/parse_schedule", headers=auth_headers(EMAIL),
                           params={"competition": COMP_ID}, body=body)
    return fa.parse_schedule._function.get_user_function()(req)


def uploads(storage):
    return {path: data for path, data in storage.container.blobs.items()
            if path.startswith(f"{FOLDER}/uploads/")}


def rosters(storage):
    return {path.rsplit("/", 1)[-1]: data for path, data in storage.container.blobs.items()
            if path.startswith(f"{FOLDER}/rosters/")}


# ── the copy is the site's ────────────────────────────────────────────────────

# SHA-256 of the two sections as they stand in figureskatingtools-site
# infra/functions/function_app.py (commit 0d3488c). After a change there, copy
# them over verbatim, check with FIGURESKATINGTOOLS_SITE=<site checkout> (the
# second test), and put the digest the first test then reports here.
SITE_SECTIONS_SHA256 = "3918c36f413c38db2fdfaac7496879de5449cd0a627b35fcabd0372b5fb51a81"


def shared_sections(text: str) -> str:
    """From the "birth dates out of participant files" heading to the end of the
    "participant files uploaded by users" section, trailing blank lines dropped."""
    first = text.index("# ── birth dates out of participant files ──")
    second = text.index("# ── participant files uploaded by users ──", first)
    end = text.index("\n# ── ", second + 1)
    return text[first:end].rstrip() + "\n"


def test_the_shared_sections_are_the_sites_byte_for_byte():
    """A change made only here (or only on the site) is drift: birth dates would
    then be cut differently through this app's doors than through the pool's."""
    with open(obd.__file__, encoding="utf-8") as handle:
        text = handle.read()

    digest = hashlib.sha256(shared_sections(text).encode("utf-8")).hexdigest()
    assert digest == SITE_SECTIONS_SHA256, f"sections changed: now {digest}"


def test_against_a_site_checkout_when_one_is_named():
    """Optional: FIGURESKATINGTOOLS_SITE=<checkout> compares with the live file."""
    site = os.environ.get("FIGURESKATINGTOOLS_SITE")
    if not site:
        pytest.skip("FIGURESKATINGTOOLS_SITE not set")
    with open(os.path.join(site, "infra", "functions", "function_app.py"),
              encoding="utf-8") as handle:
        theirs = shared_sections(handle.read())
    with open(obd.__file__, encoding="utf-8") as handle:
        assert shared_sections(handle.read()) == theirs


# ── the module ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("body, expected", [
    (DT_PARTIC_BIRTHDATES_BODY, DT_PARTIC_BIRTHDATES_STRIPPED),
    (DT_PARTIC_TEAMS_BIRTHDATES_BODY, DT_PARTIC_TEAMS_BIRTHDATES_STRIPPED),
])
def test_only_the_birth_dates_go(body, expected):
    assert obd.strip_birth_dates(body) == expected
    assert obd.strip_upload(body) == expected


def test_a_file_without_birth_dates_comes_back_as_the_same_bytes():
    assert obd.strip_birth_dates(DT_PARTIC_BODY) is DT_PARTIC_BODY
    assert obd.strip_upload(DT_PARTIC_BODY) is DT_PARTIC_BODY


@pytest.mark.parametrize("body", [
    DT_SCHEDULE_WITH_BIRTHDATE_BODY,
    b'<Root><P BirthDate="2012-03-04"/></Root>',     # not ODF
    PDF,
])
def test_an_upload_that_is_not_a_participant_file_is_untouched(body):
    assert obd.strip_upload(body) is body


@pytest.mark.parametrize("declared, replacement", [
    (b'DocumentType="DT_PARTIC"', b'DocumentType="DT_PARTIC_UPDATE"'),
    (b'DocumentType="DT_PARTIC"', b'DocumentType=" dt_partic "'),
    (b' DocumentType="DT_PARTIC"', b""),      # undeclared: recognised by shape later
])
def test_participant_uploads_are_recognised_by_content(declared, replacement):
    body = DT_PARTIC_BIRTHDATES_BODY.replace(declared, replacement)

    assert obd.strip_upload(body) == DT_PARTIC_BIRTHDATES_STRIPPED.replace(declared, replacement)


@pytest.mark.parametrize("body", [DT_PARTIC_UTF16_BIRTHDATES_BODY, BROKEN_PARTIC])
def test_a_participant_file_that_cannot_be_cleaned_raises(body):
    with pytest.raises(obd.BirthDateError):
        obd.strip_upload(body)


def test_the_parsers_read_the_stripped_files_exactly_as_the_originals():
    """Only the birth dates go: rosters, names and the type check are unchanged."""
    parts = dt_partic.parse_participants(DT_PARTIC_BIRTHDATES_BODY)
    parts_stripped = dt_partic.parse_participants(DT_PARTIC_BIRTHDATES_STRIPPED)
    assert parts_stripped == parts and len(parts) == 3

    teams = dt_partic.parse_team_rosters(DT_PARTIC_TEAMS_BIRTHDATES_BODY, parts)
    teams_stripped = dt_partic.parse_team_rosters(DT_PARTIC_TEAMS_BIRTHDATES_STRIPPED,
                                                  parts_stripped)
    assert teams_stripped == teams
    assert teams[0]["members"] == ["AHLBERG Bertil", "KOSKINEN Aino"]

    for body, stripped in ((DT_PARTIC_BIRTHDATES_BODY, DT_PARTIC_BIRTHDATES_STRIPPED),
                           (DT_PARTIC_TEAMS_BIRTHDATES_BODY,
                            DT_PARTIC_TEAMS_BIRTHDATES_STRIPPED)):
        assert dt_partic.document_type(stripped) == dt_partic.document_type(body)


# ── upload_file ───────────────────────────────────────────────────────────────

def test_an_uploaded_participant_file_is_stored_without_birth_dates(comp, storage):
    response = upload("DT_PARTIC.xml", DT_PARTIC_BIRTHDATES_BODY)

    assert response.status_code == 200, response.get_body()
    assert list(uploads(storage).values()) == [DT_PARTIC_BIRTHDATES_STRIPPED]
    assert _payload(response)["file"]["size"] == len(DT_PARTIC_BIRTHDATES_STRIPPED)


def test_the_content_decides_not_the_name(comp, storage):
    upload("roster.xml", DT_PARTIC_TEAMS_BIRTHDATES_BODY)

    assert list(uploads(storage).values()) == [DT_PARTIC_TEAMS_BIRTHDATES_STRIPPED]


def test_uploads_without_birth_dates_or_not_participant_files_are_stored_as_sent(comp, storage):
    sent = {"DT_PARTIC.xml": DT_PARTIC_BODY,
            "DT_SCHEDULE.xml": DT_SCHEDULE_WITH_BIRTHDATE_BODY,
            "other.xml": b'<Root><P BirthDate="2012-03-04"/></Root>',
            "results.pdf": PDF}
    for name, body in sent.items():
        assert upload(name, body).status_code == 200

    stored = {path.split("_", 1)[-1]: data for path, data in uploads(storage).items()}
    assert stored == sent


@pytest.mark.parametrize("body", [DT_PARTIC_UTF16_BIRTHDATES_BODY, BROKEN_PARTIC])
def test_an_upload_that_cannot_be_cleaned_stores_nothing(comp, storage, body):
    response = upload("DT_PARTIC.xml", body)

    _refused(response)
    assert storage.container.blobs == {}
    assert comp.get("files", {}) == {}


# ── import_platform_file (pool files are clean already: stripped all the same) ──

def test_a_pool_import_is_stripped_too(comp, storage, pool):
    response = import_platform_file("DT_PARTIC.xml")

    assert response.status_code == 200, response.get_body()
    assert list(uploads(storage).values()) == [DT_PARTIC_BIRTHDATES_STRIPPED]


def test_a_pool_file_that_cannot_be_cleaned_is_not_imported(comp, storage, pool):
    _refused(import_platform_file("utf16.xml"))
    assert storage.container.blobs == {}
    assert comp.get("files", {}) == {}


# ── import_rosters ────────────────────────────────────────────────────────────

def test_inline_roster_xml_is_parsed_and_archived_without_birth_dates(comp, storage):
    response = import_rosters(teamsXml=DT_PARTIC_TEAMS_BIRTHDATES_BODY.decode(),
                              particXml=DT_PARTIC_BIRTHDATES_BODY.decode())

    assert response.status_code == 200, response.get_body()
    assert comp["categories"][0]["teams"][0]["members"] == ["AHLBERG Bertil", "KOSKINEN Aino"]
    assert rosters(storage) == {"teams.xml": DT_PARTIC_TEAMS_BIRTHDATES_STRIPPED,
                                "partic.xml": DT_PARTIC_BIRTHDATES_STRIPPED}


def test_a_partic_only_rematch_archives_it_without_birth_dates(comp, storage):
    import_rosters(teamsXml=DT_PARTIC_TEAMS_BIRTHDATES_BODY.decode())

    response = import_rosters(particXml=DT_PARTIC_BIRTHDATES_BODY.decode())

    assert response.status_code == 200, response.get_body()
    assert rosters(storage)["partic.xml"] == DT_PARTIC_BIRTHDATES_STRIPPED


def test_pool_referenced_rosters_are_archived_without_birth_dates(comp, storage, pool):
    response = import_rosters(teamsPool={"name": "DT_PARTIC_TEAMS.xml", "source": "fsm"},
                              particPool={"name": "DT_PARTIC.xml", "source": "upload"})

    assert response.status_code == 200, response.get_body()
    assert rosters(storage) == {"teams.xml": DT_PARTIC_TEAMS_BIRTHDATES_STRIPPED,
                                "partic.xml": DT_PARTIC_BIRTHDATES_STRIPPED}


def test_a_roster_whose_birth_dates_cannot_be_removed_stores_nothing(comp, storage):
    """The TEAMS file is fine, the PARTIC is not: neither is archived."""
    response = import_rosters(teamsXml=DT_PARTIC_TEAMS_BIRTHDATES_BODY.decode(),
                              particXml=BROKEN_PARTIC.decode())

    _refused(response)
    assert storage.container.blobs == {}
    assert "rosterImport" not in comp
    assert comp["categories"][0]["teams"] == []


def test_a_strip_that_would_change_anything_else_stores_nothing(comp, storage, monkeypatch):
    """The re-parse check, through the route: a broken removal is refused."""
    real = obd._remove_birth_date_markup
    monkeypatch.setattr(obd, "_remove_birth_date_markup",
                        lambda data: real(data).replace(b'Name="Blue Herons "', b'Name="X"'))

    _refused(import_rosters(teamsXml=DT_PARTIC_TEAMS_BIRTHDATES_BODY.decode()))
    assert storage.container.blobs == {}


# ── parse_schedule ────────────────────────────────────────────────────────────

def test_a_participant_file_dropped_as_the_schedule_is_kept_without_birth_dates(storage):
    storage.competition()
    parse_schedule(DT_PARTIC_BIRTHDATES_BODY)

    assert storage.container.blobs[f"{FOLDER}/schedule.xml"] == DT_PARTIC_BIRTHDATES_STRIPPED


def test_a_schedule_upload_that_cannot_be_cleaned_keeps_nothing(storage):
    storage.competition()

    _refused(parse_schedule(BROKEN_PARTIC))
    assert storage.container.blobs == {}


def test_a_schedule_is_kept_as_sent(storage):
    storage.competition()
    parse_schedule(DT_SCHEDULE_WITH_BIRTHDATE_BODY)

    assert storage.container.blobs[f"{FOLDER}/schedule.xml"] == DT_SCHEDULE_WITH_BIRTHDATE_BODY
