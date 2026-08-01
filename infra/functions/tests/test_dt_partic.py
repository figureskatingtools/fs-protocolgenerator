"""`dt_partic` — DT_PARTIC_TEAMS/DT_PARTIC parsing.

The XML below is invented (real exports carry minors' names); it only reproduces
the *shape* of the real files, including their untidiness: padded attributes, a
leading empty `RegisteredEvent` and an athlete with no participant record.
"""
from dt_partic import event_label, parse_participants, parse_team_rosters

TEAMS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<OdfBody DocumentType="DT_PARTIC_TEAMS">
  <Competition>
    <Team Code="SYNCHRO0001" Organisation="BHK " Name="Blue Herons ">
      <RegisteredEvent Event="" />
      <RegisteredEvent Event="FSKXSYNCHRONMLTULO----------------" />
      <Composition>
        <Athlete Code="A0002" Order="1" />
        <Athlete Code="A0001" Order="2" />
        <Athlete Code="A9999" Order="3" />
      </Composition>
    </Team>
    <Team Code="SYNCHRO0002" Organisation="SCK" Name="Silver Comets">
      <RegisteredEvent Event="FSKXSYNCHRONMLAIKU----------------" />
      <Composition>
        <Athlete Code="A0003" Order="1" />
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
    <Participant Code="A0003" GivenName="" FamilyName="" PrintName="C. Lindgren" />
  </Competition>
</OdfBody>
"""


def _teams():
    return parse_team_rosters(TEAMS_XML.encode(), parse_participants(PARTIC_XML.encode()))


def test_name_and_organisation_are_stripped():
    herons = _teams()[0]
    assert herons["name"] == "Blue Herons"
    assert herons["org"] == "BHK"
    assert herons["code"] == "SYNCHRO0001"


def test_first_non_empty_registered_event_wins():
    assert _teams()[0]["event"] == "FSKXSYNCHRONMLTULO----------------"


def test_members_are_family_given_sorted_with_unknown_codes_passed_through():
    teams = _teams()
    # Sorted alphabetically; an athlete with no DT_PARTIC record keeps its code.
    assert teams[0]["members"] == ["A9999", "AHLBERG Bertil", "KOSKINEN Aino"]
    # No given/family name at all -> the PrintName fallback.
    assert teams[1]["members"] == ["C. Lindgren"]


def test_empty_event_code_has_no_label():
    """A placeholder label would substring-match every category name."""
    assert event_label("") == ""
    assert event_label("FSKXSYNCHRON----") == ""


def test_event_label_known_and_unknown_tokens():
    assert event_label("FSKXSYNCHRONMLTULO----------------") == "Tulokkaat"
    assert event_label("FSKXSYNCHRONADVNOV----") == "Advanced Novice"
    assert event_label("FSKXSYNCHRONWIDGET----") == "Widget"
