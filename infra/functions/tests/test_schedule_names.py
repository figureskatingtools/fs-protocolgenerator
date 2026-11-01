"""The printed time schedule follows the names set in the tool.

The schedule (DT_SCHEDULE XML or the schedule PDF) supplies each category's and
segment's name — whatever language FS Manager exported it in — and that is only
the default: renaming a category or segment in the tool must reach the schedule
page as it already reaches the podium page. So every parsed row carries the ids
of the category/segment it was grouped into, and `structure.schedule_rows`
prints their current names. Schedules parsed before the rows carried ids are
linked at generation by re-parsing the kept `schedule.xml|pdf`.
"""
import copy

import assemble
import function_app as fa
import generate_pages
import schedule_parser as sp
import structure as st


def schedule_xml(*units):
    body = "\n".join(
        f'    <Unit Code="{code}" StartDate="{start}">\n'
        f'      <ItemName Value="{item}" />\n'
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


# An English export: the names an organizer then translates in the tool.
ENGLISH = schedule_xml(
    ("FSKXSYNCHRONJUNIOR----QUAL000100--", "2026-10-31T10:00:00",
     "Junior Synchronized Skating Short Program"),
    ("FSKXSYNCHRON----------FNL-000100--", "2026-10-31T12:00:00",
     "Senior Synchronized Skating Free Skating"),
    ("FSKXSYNCHRONJUNIOR----FNL-000100--", "2026-11-01T10:00:00",
     "Junior Synchronized Skating Free Skating"),
)


def parsed(source=ENGLISH):
    rows, categories, _meta = sp.parse_schedule_data(source)
    return {"schedule": rows, "categories": categories}


def by_name(structure, name):
    return next(c for c in structure["categories"] if c["name"] == name)


def printed(structure):
    return [(r["category_name"], r["segment_name"]) for r in st.schedule_rows(structure)]


def rename(structure):
    """What the organizer did in the tool: Finnish names for the junior category
    and its free skating, the senior category left as exported."""
    junior = by_name(structure, "Junior Synchronized Skating")
    junior["name"] = "Juniorit"
    next(s for s in junior["segments"] if s["name"] == "Free Skating")["name"] = "Vapaaohjelma"


ENGLISH_PRINTED = [
    ("Junior Synchronized Skating", "Short Program"),
    ("Senior Synchronized Skating", "Free Skating"),
    ("Junior Synchronized Skating", "Free Skating"),
]
RENAMED_PRINTED = [
    ("Juniorit", "Short Program"),
    ("Senior Synchronized Skating", "Free Skating"),
    ("Juniorit", "Vapaaohjelma"),
]


# ── parsing links every row ───────────────────────────────────────────────────

def test_xml_rows_carry_their_category_and_segment():
    structure = parsed()
    for row in structure["schedule"]:
        category = st.find_category(structure, row["categoryId"])
        segment = st.find_segment(category, row["segmentId"])
        assert category["name"] == row["category_name"]
        assert segment["name"] == row["segment_name"]


def test_pdf_rows_carry_their_category_and_segment():
    rows = [
        {"date": "20261031", "start_time": "09:00", "event_name": "Arvonta",
         "category_name": "Arvonta", "segment_name": "Results"},
        {"date": "20261031", "start_time": "10:00", "event_name": "Naiset Lyhytohjelma",
         "category_name": "Naiset", "segment_name": "Lyhytohjelma"},
        {"date": "20261101", "start_time": "10:00", "event_name": "Naiset Vapaaohjelma",
         "category_name": "Naiset", "segment_name": "Vapaaohjelma"},
    ]
    categories = sp.build_categories_from_schedule(rows)
    structure = {"schedule": rows, "categories": categories}

    # The draw is no category: its row is linked to nothing, explicitly.
    assert rows[0]["categoryId"] is None and rows[0]["segmentId"] is None
    naiset = by_name(structure, "Naiset")
    assert {r["categoryId"] for r in rows[1:]} == {naiset["id"]}
    assert [st.find_segment(naiset, r["segmentId"])["name"] for r in rows[1:]] == [
        "Lyhytohjelma", "Vapaaohjelma"]


# ── the printed schedule ──────────────────────────────────────────────────────

def test_schedule_names_are_the_default():
    assert printed(parsed()) == ENGLISH_PRINTED


def test_schedule_follows_names_changed_in_the_tool():
    structure = parsed()
    rename(structure)
    assert printed(structure) == RENAMED_PRINTED
    # …without touching the stored rows: the schedule's wording stays the default.
    assert [r["category_name"] for r in structure["schedule"]][0] == "Junior Synchronized Skating"


def test_a_removed_or_blank_category_prints_as_parsed():
    structure = parsed()
    junior = by_name(structure, "Junior Synchronized Skating")
    by_name(structure, "Senior Synchronized Skating")["name"] = "   "
    structure["categories"].remove(junior)
    assert printed(structure) == ENGLISH_PRINTED


def test_the_protocol_prints_the_renamed_schedule(monkeypatch):
    structure = parsed()
    rename(structure)
    seen = []
    real = generate_pages.time_schedule_page
    monkeypatch.setattr(generate_pages, "time_schedule_page",
                        lambda rows, chrome=None: seen.append(rows) or real(rows, chrome))
    assemble.assemble_protocol(structure, lambda _file_id: None)
    assert [(r["category_name"], r["segment_name"]) for r in seen[0]] == RENAMED_PRINTED


# ── schedules parsed before rows carried links ────────────────────────────────

def legacy(structure):
    """The same structure as an older parse stored it: rows without links."""
    for row in structure["schedule"]:
        del row["categoryId"], row["segmentId"]
    return structure


def test_a_legacy_schedule_is_linked_by_code_from_its_source():
    structure = legacy(parsed())
    rename(structure)
    # Reordered too: the code, not the position, says which category is which.
    for c in structure["categories"]:
        c["order"] = 5 - c["order"]
    assert not sp.schedule_rows_linked(structure["schedule"])

    assert sp.link_legacy_schedule(structure, ENGLISH)
    assert sp.schedule_rows_linked(structure["schedule"])
    assert printed(structure) == RENAMED_PRINTED


def test_a_legacy_schedule_without_codes_is_linked_by_position():
    structure = legacy(parsed())
    rename(structure)
    for c in structure["categories"]:
        c["code"] = ""
    assert sp.link_legacy_schedule(structure, ENGLISH)
    assert printed(structure) == RENAMED_PRINTED


def test_a_source_that_does_not_reproduce_the_rows_links_nothing():
    structure = legacy(parsed())
    rename(structure)
    before = copy.deepcopy(structure["schedule"])
    other = schedule_xml(("FSKWSINGLES-----------FNL-000100--", "2026-10-31T10:00:00",
                          "Women Free Skating"))
    assert not sp.link_legacy_schedule(structure, other)
    assert structure["schedule"] == before
    assert printed(structure) == ENGLISH_PRINTED


def test_generation_links_a_legacy_schedule_from_the_kept_file(storage):
    structure = legacy(parsed())
    rename(structure)
    folder = "Spring Trophy 2026-abc12345"
    # A stale PDF from an earlier parse beside the XML the rows came from.
    storage.container.blobs[f"{folder}/schedule.pdf"] = b"%PDF-1.4 not this one"
    storage.container.blobs[f"{folder}/schedule.xml"] = ENGLISH

    fa._link_legacy_schedule(folder, structure)
    assert printed(structure) == RENAMED_PRINTED


def test_generation_without_a_kept_file_prints_as_parsed(storage):
    structure = legacy(parsed())
    rename(structure)
    fa._link_legacy_schedule("Spring Trophy 2026-abc12345", structure)
    assert printed(structure) == ENGLISH_PRINTED
