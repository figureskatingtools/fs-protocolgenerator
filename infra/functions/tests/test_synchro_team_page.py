"""`synchro_team_page` — the name-display modes and the free-text rows.

Organizers cannot always publish a full roster (these are often minors), so the
page prints either "FAMILY Given", the given names alone, or no names at all.
The given-name mode reverses `dt_partic._format_member`'s "FAMILY Given"
convention by dropping the leading all-caps tokens; anything that does not follow
the convention — a hand-edited entry — has to come back whole rather than blank.

The other half of the file guards the page's one hard invariant: a full
32-skater roster must still fit above the footer band once free-text rows have
taken their share of the page. The text block is therefore capped (a flat list of
"Label  Value" lines, no headings), and the photo box is sized from whatever is
left.
"""
import io

import pytest
from pypdf import PdfReader

import branding
import generate_pages as gp
import structure as st

ROSTER = ["KORHONEN Anna Maria", "VAN DER BERG Aino", "MÄKI-LUOTO Sofia"]
TEAM = {"name": "Helsinki Finettes", "org": "HTK", "members": ROSTER}


def page_text(team=None, name_mode="full", text_rows=None) -> str:
    """All text of a rendered team page, as one string."""
    pdf = gp.synchro_team_page(team or TEAM, None, None, name_mode, text_rows)
    return PdfReader(io.BytesIO(pdf)).pages[0].extract_text() or ""


@pytest.fixture
def plain(monkeypatch):
    """Force the plain fallback (the brand fonts ship with the repo, so the
    branded path is what runs otherwise)."""
    monkeypatch.setattr(branding, "fonts_available", lambda: False)


# ── name modes ────────────────────────────────────────────────────────────────

def test_the_default_call_still_prints_the_full_roster():
    # Regression guard: the assembler's old call site passed neither new argument.
    text = gp.synchro_team_page(TEAM, None, None)
    text = PdfReader(io.BytesIO(text)).pages[0].extract_text() or ""
    assert "KORHONEN Anna Maria" in text
    assert "SKATERS" in text


def test_given_names_mode_drops_the_family_part():
    text = page_text(name_mode="firstNames")
    assert "Anna Maria" in text
    assert "KORHONEN" not in text


def test_given_names_mode_drops_a_multi_word_family_name():
    text = page_text(name_mode="firstNames")
    assert "Aino" in text
    assert "VAN DER BERG" not in text


def test_no_names_mode_removes_the_roster_and_its_heading():
    text = page_text(name_mode="none")
    assert "KORHONEN" not in text
    assert "Anna" not in text
    assert "SKATERS" not in text


def test_no_names_mode_keeps_the_team_identity():
    text = page_text(name_mode="none")
    assert "Helsinki Finettes" in text
    assert "HTK" in text


def test_a_team_whose_roster_is_not_imported_yet_keeps_the_heading():
    # "no names yet" is not the same as "names switched off" — the heading stays
    # as the hint that the DT_PARTIC import is still pending.
    assert "SKATERS" in page_text({"name": "Team", "org": "", "members": []})


# ── the given-name split itself ───────────────────────────────────────────────

def test_a_hyphenated_family_name_is_still_recognised():
    assert gp._given_names("MÄKI-LUOTO Sofia") == "Sofia"


def test_an_apostrophe_family_name_is_still_recognised():
    assert gp._given_names("O'BRIEN Íñigo") == "Íñigo"


def test_a_hand_edited_name_without_a_caps_lead_is_left_alone():
    assert gp._given_names("Anna Korhonen") == "Anna Korhonen"


def test_an_all_upper_case_entry_is_left_alone():
    # Nothing would be left, and a full name beats a blank roster row.
    assert gp._given_names("ANNA KORHONEN") == "ANNA KORHONEN"


def test_an_empty_entry_stays_empty():
    assert gp._given_names("") == ""
    assert gp._display_members(["", "KORHONEN Anna"], "firstNames") == ["Anna"]


# ── free-text rows ────────────────────────────────────────────────────────────

THEME = [{"label": "Theme", "value": "Spies"}]


def test_a_text_row_prints_its_label_and_value():
    text = page_text(text_rows=THEME)
    assert "Theme" in text
    assert "Spies" in text


def test_the_rows_print_in_the_order_they_are_given():
    rows = THEME + [{"label": "Music", "value": "Goldfinger"}]
    text = page_text(text_rows=rows)
    assert text.index("Theme") < text.index("Music")


def test_an_over_long_row_still_renders():
    # The label is capped at 60 chars by the route, but a label that wide can leave
    # the value less room than the ellipsis it would shrink to — which must not hang.
    rows = [{"label": "L" * 60, "value": "V" * 120}]
    assert "L" * 20 in page_text(text_rows=rows)


def test_a_row_left_over_from_the_segment_days_still_prints():
    # `structure.team_text_rows` strips the key, but the renderer must not care
    # either: it reads only label and value.
    rows = [{"segmentId": "seg-1234", "label": "Coach", "value": "M. Virta"}]
    text = page_text(text_rows=rows)
    assert "Coach" in text and "M. Virta" in text


def test_text_rows_print_alongside_a_roster():
    text = page_text(text_rows=THEME)
    assert "Spies" in text and "KORHONEN Anna Maria" in text


def test_text_rows_print_with_the_names_switched_off():
    text = page_text(name_mode="none", text_rows=THEME)
    assert "Spies" in text
    assert "SKATERS" not in text


# ── layout invariants ─────────────────────────────────────────────────────────

def test_the_stored_cap_is_exactly_what_the_block_can_draw():
    """The one assertion that stops the two constants drifting apart: every row
    `structure.sanitize_text_fields` is willing to store must survive `_text_block`
    unchanged, and one row more must not — otherwise a user types a row, the
    backend keeps it, and the page silently never prints it."""
    rows = [{"label": f"Row {i}", "value": "x"}
            for i in range(st.MAX_TEAM_TEXT_FIELDS)]
    lines, height = gp._text_block(rows)
    assert len(lines) == st.MAX_TEAM_TEXT_FIELDS
    assert height <= gp.TEXT_MAX_H
    # And the cap is not needlessly low either — the 7th row is what the budget
    # genuinely cannot draw.
    one_more = rows + [{"label": "Over", "value": "x"}]
    assert len(gp._text_block(one_more)[0]) == st.MAX_TEAM_TEXT_FIELDS


def test_the_text_block_is_capped_so_the_roster_keeps_its_space():
    many = [{"label": f"Row {i}", "value": "x"} for i in range(30)]
    lines, height = gp._text_block(many)
    assert height <= gp.TEXT_MAX_H
    assert len(lines) < len(many)


def test_every_row_the_cap_admits_costs_the_same():
    # Without segment headings the block is one row height per line, so the budget
    # is simply TEXT_GAP plus n rows — the arithmetic the TEXT_MAX_H comment states.
    lines, height = gp._text_block([{"label": f"Row {i}", "value": "x"}
                                    for i in range(30)])
    assert height == pytest.approx(gp.TEXT_GAP + len(lines) * gp.TEXT_ROW_H)


def test_no_text_rows_means_no_reserved_space():
    assert gp._text_block(None) == ([], 0.0)
    assert gp._text_block([]) == ([], 0.0)


def test_a_full_roster_with_a_full_text_block_stays_above_the_footer():
    # The branded header stack leaves photo_top at roughly 238 mm; 32 names fill
    # two columns of 16 rows. Everything below the photo must still end above the
    # footer band, which is what the TEXT_MAX_H budget buys.
    photo_top = 238 * gp.mm
    _, text_h = gp._text_block(
        [{"label": f"Row {i}", "value": "x"} for i in range(30)])
    cols, rows = gp._roster_grid(32, tight=True)
    roster_h = gp._roster_block_h(rows, True)
    box_h = gp._team_photo_box_h(photo_top, roster_h, text_h, gp.PHOTO_MAX_H)
    assert photo_top - box_h - text_h - roster_h >= gp.CONTENT_BOTTOM


def test_all_32_names_are_printed_next_to_a_text_block():
    # `_draw_roster` silently drops rows that fall below CONTENT_BOTTOM, so the
    # names coming back out of the PDF are the real end-to-end check.
    roster = [f"SUKUNIMI{i:02d} Etunimi{i:02d}" for i in range(32)]
    rows = [{"label": f"Row {i}", "value": "x"} for i in range(30)]
    text = page_text({"name": "Team", "org": "HTK", "members": roster}, text_rows=rows)
    assert all(name in text for name in roster)


def test_switching_the_names_off_lets_the_photo_grow():
    photo_top = 238 * gp.mm
    _, rows = gp._roster_grid(32)
    with_names = gp._team_photo_box_h(photo_top, gp._roster_block_h(rows, True),
                                      0.0, gp.PHOTO_MAX_H)
    without = gp._team_photo_box_h(photo_top, gp._roster_block_h(0, False),
                                   0.0, gp.PHOTO_MAX_H_SOLO)
    assert without > with_names


def test_the_three_column_valve_pulls_earlier_when_there_are_text_rows():
    assert gp._roster_grid(40)[0] == 2
    assert gp._roster_grid(40, tight=True)[0] == 3


# ── the plain fallback ────────────────────────────────────────────────────────

def test_the_plain_fallback_honours_the_given_names_mode(plain):
    text = page_text(name_mode="firstNames")
    assert "Anna Maria" in text
    assert "KORHONEN" not in text


def test_the_plain_fallback_drops_the_roster_for_no_names(plain):
    text = page_text(name_mode="none")
    assert "SKATERS" not in text
    assert "Helsinki Finettes" in text


def test_the_plain_fallback_prints_the_text_rows(plain):
    text = page_text(text_rows=THEME)
    assert "Theme" in text and "Spies" in text
