"""`results_parser` — placement rows read out of a total-results PDF.

The PDF text layer is stubbed (`_read_text`), so these are pure row-shape tests:
layout extraction glues the place onto the name ("1Shadows …") and the club is the
last non-numeric column. Team and club names are invented.
"""
import results_parser as rp

SHEET = """
                            Tulokkaat, Mupi L1 - Total Results

 Pl.  Name                        Club       Total   SP    FS
   1Blue Herons                   BHK        69.29   1     1
   2Silver Comets                 SCK        61.05   2     2
   3Northern Lights               NLK        55.10   3     3
   4Golden Arrows                 GAK        50.02   4     4
   1Blue Herons                   XXX         0.00   -     -
"""


def _stub(monkeypatch, text):
    monkeypatch.setattr(rp, "_read_text", lambda _pdf: text)


def test_glued_rank_rows_are_split_into_name_and_club(monkeypatch):
    _stub(monkeypatch, SHEET)
    rows = rp.parse_result_rows(b"pdf")
    assert rows[:2] == [{"rank": 1, "name": "Blue Herons", "club": "BHK"},
                        {"rank": 2, "name": "Silver Comets", "club": "SCK"}]


def test_a_repeated_rank_keeps_the_first_occurrence(monkeypatch):
    _stub(monkeypatch, SHEET)
    rows = rp.parse_result_rows(b"pdf")
    assert [r["rank"] for r in rows] == [1, 2, 3, 4]
    assert rows[0]["club"] == "BHK"          # not the stray "XXX" reprint


def test_count_result_rows_counts_the_placement_rows(monkeypatch):
    _stub(monkeypatch, SHEET)
    assert rp.count_result_rows(b"pdf") == 4


def test_parse_top_three_formats_club_dash_name(monkeypatch):
    _stub(monkeypatch, SHEET)
    assert rp.parse_top_three(b"pdf") == ["BHK - Blue Herons", "SCK - Silver Comets",
                                          "NLK - Northern Lights"]


def test_missing_placements_come_back_empty(monkeypatch):
    _stub(monkeypatch, "   1Blue Herons     BHK    69.29  1\n   3Northern Lights   NLK  55.10  3\n")
    assert rp.parse_top_three(b"pdf") == ["BHK - Blue Herons", "", "NLK - Northern Lights"]


COUPLES_SHEET = """
                            Juniorit Ice Dance - Total Results

 Pl.  Name                                              Club   Total   SD    FD
   1Iris LAHTI / Oskari LIEDENPOHJA                      HL   113.03   1     1
   2Millie COLLING / Emma Aino Elina AALTO               HL    98.40   2     2
"""


def test_a_couple_row_keeps_the_slash_between_the_two_names(monkeypatch):
    _stub(monkeypatch, COUPLES_SHEET)
    assert rp.parse_result_rows(b"pdf") == [
        {"rank": 1, "name": "Iris LAHTI / Oskari LIEDENPOHJA", "club": "HL"},
        {"rank": 2, "name": "Millie COLLING / Emma Aino Elina AALTO", "club": "HL"},
    ]


def test_parse_top_three_keeps_the_couple_separator(monkeypatch):
    _stub(monkeypatch, COUPLES_SHEET)
    assert rp.parse_top_three(b"pdf")[0] == "HL - Iris LAHTI / Oskari LIEDENPOHJA"


def test_a_placeholder_segment_rank_is_still_dropped(monkeypatch):
    # The lone "-"/":" stand-ins for a missing rank are scores, not name parts.
    _stub(monkeypatch, "   1Iris LAHTI / Oskari LIEDENPOHJA   HL   113.03   -   -\n")
    assert rp.parse_result_rows(b"pdf") == [
        {"rank": 1, "name": "Iris LAHTI / Oskari LIEDENPOHJA", "club": "HL"}]


def test_a_row_without_a_club_is_all_name(monkeypatch):
    _stub(monkeypatch, "  1 Loners   58.20  1\n")
    assert rp.parse_result_rows(b"pdf") == [{"rank": 1, "name": "Loners", "club": ""}]
    assert rp.parse_top_three(b"pdf") == ["Loners", "", ""]


def test_an_unreadable_pdf_yields_nothing(monkeypatch):
    def boom(_pdf):
        raise ValueError("not a PDF")
    monkeypatch.setattr(rp, "_read_text", boom)
    assert rp.parse_result_rows(b"junk") == []
    assert rp.count_result_rows(b"junk") == 0
    assert rp.parse_top_three(b"junk") == []
