"""`assemble` — which synchro teams actually get a presentation page.

Turning team pages off has to remove the page *entirely* — not an empty one, not
a photo-only one — because the reason to turn it off is that the organizer does
not want those pages in the bound protocol at all. The competition-wide setting
is only a default, so a single team can opt back in when everything else is off.

The counterpart matters just as much: a team that gets no page still competed,
so the information page's Competition Units / Performances tallies must not move.
"""
import io

import pytest
from pypdf import PdfReader

import assemble
import structure as st


def competition(**team_pages) -> dict:
    """A synchro competition with two rostered teams and no uploaded files."""
    structure = st.new_structure("comp-1", "Spring Trophy", "01.02.2026",
                                 "o@example.com", "")
    if team_pages:
        structure["teamPages"] = team_pages
    cat = st.new_category("SM-seniorit", "synchro", 0)
    cat["segments"].append(st.new_segment("Free Skating", 0))
    for org, name, member in (("HTK", "Helsinki Finettes", "KORHONEN Anna"),
                              ("RTL", "Rockettes", "VIRTANEN Sofia")):
        team = st.new_team(org, name)
        team["members"] = [member]
        cat["teams"].append(team)
    structure["categories"].append(cat)
    return structure


def protocol_text(structure) -> str:
    pdf = assemble.assemble_protocol(structure, lambda _file_id: None)
    reader = PdfReader(io.BytesIO(pdf))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def teams_of(structure):
    return structure["categories"][0]["teams"]


def test_by_default_every_team_gets_a_page():
    text = protocol_text(competition())
    assert "Helsinki Finettes" in text
    assert "Rockettes" in text


def test_turning_the_pages_off_skips_every_team():
    text = protocol_text(competition(enabled=False, nameMode="full"))
    assert "Helsinki Finettes" not in text
    assert "Rockettes" not in text


def test_a_single_team_can_be_skipped():
    structure = competition()
    teams_of(structure)[0]["pageEnabled"] = False
    text = protocol_text(structure)
    assert "Helsinki Finettes" not in text
    assert "Rockettes" in text


def test_a_team_can_opt_back_in_when_the_default_is_off():
    structure = competition(enabled=False, nameMode="full")
    teams_of(structure)[1]["pageEnabled"] = True
    text = protocol_text(structure)
    assert "Helsinki Finettes" not in text
    assert "Rockettes" in text


def test_the_competition_name_mode_reaches_the_page():
    text = protocol_text(competition(enabled=True, nameMode="firstNames"))
    assert "Anna" in text
    assert "KORHONEN" not in text


def test_a_teams_text_rows_reach_its_page():
    structure = competition()
    seg_id = structure["categories"][0]["segments"][0]["id"]
    teams_of(structure)[0]["textFields"] = [
        {"id": "fld-1", "segmentId": seg_id, "label": "Theme", "value": "Spies"}]
    text = protocol_text(structure)
    assert "Spies" in text
    assert "FREE SKATING" in text


def test_a_skipped_team_still_counts_towards_the_information_page():
    # The units tally comes from the team list, not from the pages produced.
    structure = competition(enabled=False, nameMode="full")
    stats = assemble._competition_stats(structure, lambda _file_id: None)
    assert stats == assemble._competition_stats(competition(), lambda _f: None)
    assert stats["units"] == 2
