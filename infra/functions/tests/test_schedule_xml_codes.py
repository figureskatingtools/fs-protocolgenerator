"""DT_SCHEDULE → category codes.

The ISU RSC a schedule unit carries is the *same* code FS Manager stamps on
every PDF it exports for that unit, so the category code this parser stores is
what the Protocol Generator frontend matches those filenames against
(`rscCodeHits` in `@figureskatingtools/shared-ui`'s `protocol-auto-assign.ts`).
These tests pin that contract with the codes seen in competition YL110926HTL.
"""
import schedule_parser as sp


def schedule_xml(*units):
    body = "\n".join(
        f'    <Unit Code="{code}" StartDate="{start}">\n'
        f'      <ItemName Value="{item}" />\n'
        f'      <VenueDescription VenueName="Hyvinkään jäähalli" />\n'
        f'    </Unit>'
        for code, start, item in units
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<OdfBody DocumentType="DT_SCHEDULE">\n'
        '  <Competition Code="FSK-------------------------------">\n'
        f'{body}\n'
        '  </Competition>\n'
        '</OdfBody>\n'
    ).encode("utf-8")


YL110926HTL = schedule_xml(
    ("FSKWSINGLES-ADVNOV----QUAL000100--", "2026-09-11T15:00:00", "SM-NOVIISI Tytöt Lyhytohjelma"),
    ("FSKWSINGLES-ADVNOV----FNL-000100--", "2026-09-12T13:00:00", "SM-NOVIISI Tytöt Vapaaohjelma"),
    ("FSKWSINGLES-DEBYTW----FNL-000100--", "2026-09-12T10:30:00", "DEBYTANTTI Tytöt"),
)


def test_category_code_is_the_rsc_prefix_the_pdfs_carry():
    _rows, categories, _meta = sp.parse_schedule_xml(YL110926HTL)
    by_name = {c["name"]: c for c in categories}

    assert by_name["DEBYTANTTI Tytöt"]["code"] == "FSKWSINGLES-DEBYTW----"
    assert by_name["SM-NOVIISI Tytöt"]["code"] == "FSKWSINGLES-ADVNOV----"
    # …and it is exactly the prefix of the exported filenames
    assert "FSKWSINGLES-DEBYTW----FNL-000100--_SegmentResults.pdf".startswith(
        by_name["DEBYTANTTI Tytöt"]["code"]
    )


def test_units_of_one_category_merge_into_its_segments():
    _rows, categories, _meta = sp.parse_schedule_xml(YL110926HTL)
    by_name = {c["name"]: c for c in categories}

    assert [s["name"] for s in by_name["SM-NOVIISI Tytöt"]["segments"]] == [
        "Lyhytohjelma", "Vapaaohjelma",
    ]
    # An ItemName without a segment falls back to the RSC phase field (FNL)
    assert [s["name"] for s in by_name["DEBYTANTTI Tytöt"]["segments"]] == ["Free Skating"]


def test_categories_are_ordered_by_first_unit():
    _rows, categories, _meta = sp.parse_schedule_xml(YL110926HTL)
    assert [c["name"] for c in categories] == ["SM-NOVIISI Tytöt", "DEBYTANTTI Tytöt"]
