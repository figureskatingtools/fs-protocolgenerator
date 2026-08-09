"""`podium_page` — how a medallist name is laid out over its pedestal column.

Ice-dance/pair entries carry both skaters ("A / B") and often overrun the narrow
column, so an over-long couple breaks onto a second row at the slash (which stays
at the end of the first row) while a name that fits is left on one line. The
assertions read the drawn text back out of the produced PDF: reportlab writes one
run per `drawString`, so each row comes back as its own extracted line.
"""
import io

import pytest
from pypdf import PdfReader

import branding
import generate_pages

LONG_COUPLE = "HL - Millie COLLING / Emma Aino Elina AALTO"
SHORT_COUPLE = "HL - Ida OJA / A VUO"     # narrow enough for the 1st-place column
SHORT_SINGLE = "SCT - Lotta TERHO"


def podium_lines(names) -> list:
    """The text rows of a rendered podium page, in draw order."""
    pdf = generate_pages.podium_page("Juniorit Ice Dance", None, names, None)
    text = PdfReader(io.BytesIO(pdf)).pages[0].extract_text() or ""
    return [line.strip() for line in text.split("\n") if line.strip()]


@pytest.fixture
def plain(monkeypatch):
    """Force the plain fallback (the brand fonts ship with the repo, so the
    branded path is what runs otherwise)."""
    monkeypatch.setattr(branding, "fonts_available", lambda: False)


def test_the_brand_fonts_are_available_so_the_branded_path_is_the_one_tested():
    assert branding.fonts_available() is True


def test_a_long_couple_breaks_at_the_slash_onto_two_rows():
    lines = podium_lines([LONG_COUPLE, SHORT_SINGLE, SHORT_SINGLE])
    assert "Millie COLLING /" in lines
    assert "Emma Aino Elina AALTO" in lines
    assert lines.index("Millie COLLING /") < lines.index("Emma Aino Elina AALTO")


def test_a_name_that_fits_stays_on_one_row():
    lines = podium_lines([SHORT_SINGLE, SHORT_SINGLE, SHORT_SINGLE])
    assert "Lotta TERHO" in lines
    assert not [l for l in lines if l.endswith("/")]


def test_a_short_couple_keeps_the_slash_inline():
    lines = podium_lines([SHORT_COUPLE, SHORT_SINGLE, SHORT_SINGLE])
    assert "Ida OJA / A VUO" in lines


def test_a_two_row_name_keeps_its_club_and_medallion():
    # Each column draws club, then the name rows, then the rank numeral, so the
    # centre column's run shows the whole block survived the extra row.
    lines = podium_lines([LONG_COUPLE, SHORT_SINGLE, SHORT_SINGLE])
    start = lines.index("Millie COLLING /")
    assert lines[start - 1:start + 3] == ["HL", "Millie COLLING /",
                                          "Emma Aino Elina AALTO", "1"]


def test_the_plain_fallback_also_breaks_a_long_couple_in_two(plain):
    lines = podium_lines([LONG_COUPLE, SHORT_SINGLE, SHORT_SINGLE])
    assert "HL - Millie COLLING /" in lines
    assert "Emma Aino Elina AALTO" in lines


def test_the_plain_fallback_leaves_a_fitting_name_alone(plain):
    lines = podium_lines([SHORT_SINGLE, SHORT_SINGLE, SHORT_SINGLE])
    assert "SCT - Lotta TERHO" in lines
    assert not [l for l in lines if l.endswith("/")]


def test_split_couple_name_only_splits_a_real_pair():
    assert branding.split_couple_name("A NAME / B NAME") == ["A NAME /", "B NAME"]
    assert branding.split_couple_name("Lotta TERHO") == ["Lotta TERHO"]
    assert branding.split_couple_name("/ B NAME") == ["/ B NAME"]
    assert branding.split_couple_name("") == [""]
