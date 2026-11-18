"""`assemble` — which synchro teams actually get a presentation page.

Turning team pages off has to remove the page *entirely* — not an empty one, not
a photo-only one — because the reason to turn it off is that the organizer does
not want those pages in the bound protocol at all. The competition-wide setting
and the category one are only defaults, so a single team can opt back in when
everything above it is off, and a category can opt its whole field back in.

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


def category_of(structure):
    return structure["categories"][0]


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


def test_a_category_can_skip_its_whole_field():
    structure = competition()
    category_of(structure)["pageEnabled"] = False
    text = protocol_text(structure)
    assert "Helsinki Finettes" not in text
    assert "Rockettes" not in text


def test_a_category_can_opt_its_field_back_in():
    structure = competition(enabled=False, nameMode="full")
    category_of(structure)["pageEnabled"] = True
    text = protocol_text(structure)
    assert "Helsinki Finettes" in text
    assert "Rockettes" in text


def test_the_category_name_mode_reaches_the_page():
    structure = competition()
    category_of(structure)["nameMode"] = "firstNames"
    text = protocol_text(structure)
    assert "Anna" in text
    assert "KORHONEN" not in text


def test_a_team_override_beats_its_category():
    structure = competition()
    category_of(structure)["pageEnabled"] = False
    teams_of(structure)[1]["pageEnabled"] = True
    text = protocol_text(structure)
    assert "Helsinki Finettes" not in text
    assert "Rockettes" in text


def test_a_teams_text_rows_reach_its_page():
    structure = competition()
    teams_of(structure)[0]["textFields"] = [
        {"id": "fld-1", "label": "Theme", "value": "Spies"}]
    text = protocol_text(structure)
    assert "Theme" in text
    assert "Spies" in text


def test_a_text_row_stored_with_a_segment_still_reaches_the_page():
    # Rows saved before they became a flat list carry a segmentId; it is ignored
    # on the way to the page rather than costing the organizer the row.
    structure = competition()
    seg_id = category_of(structure)["segments"][0]["id"]
    teams_of(structure)[0]["textFields"] = [
        {"id": "fld-1", "segmentId": seg_id, "label": "Theme", "value": "Spies"}]
    assert "Spies" in protocol_text(structure)


def test_a_skipped_team_still_counts_towards_the_information_page():
    # The units tally comes from the team list, not from the pages produced.
    structure = competition(enabled=False, nameMode="full")
    stats = assemble._competition_stats(structure, lambda _file_id: None)
    assert stats == assemble._competition_stats(competition(), lambda _f: None)
    assert stats["units"] == 2


def _one_page_pdf(*lines) -> bytes:
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    y = 800
    for line in lines:
        c.drawString(40, y, line)
        y -= 14
    c.save()
    return buf.getvalue()


def test_the_head_page_opens_the_category_before_its_team_pages():
    """The protocol head page is what names the category, so it comes first:
    head page -> one page per team -> podium -> results. Team pages used to
    open the category, which put the rosters ahead of the page that says which
    category they belong to."""
    structure = competition()
    structure["files"]["head-1"] = {"filename": "FSKXSYNCHRON-head.pdf", "kind": "pdf",
                                    "size": 1, "blob": "x"}
    category_of(structure)["titlePdf"] = "head-1"
    category_of(structure)["podium"]["names"] = ["HTK - Helsinki Finettes", "", ""]
    head = _one_page_pdf("PROTOCOL HEAD PAGE MARKER", "SM-seniorit")

    pdf = assemble.assemble_protocol(structure, lambda fid: head if fid == "head-1" else None)
    pages = [(page.extract_text() or "") for page in PdfReader(io.BytesIO(pdf)).pages]

    def page_of(marker):
        return next(i for i, text in enumerate(pages) if marker in text)

    head_at = page_of("PROTOCOL HEAD PAGE MARKER")
    finettes_at = page_of("Helsinki Finettes")
    rockettes_at = page_of("Rockettes")
    podium_at = max(i for i, text in enumerate(pages) if "Helsinki Finettes" in text)
    assert head_at < finettes_at < rockettes_at < podium_at
