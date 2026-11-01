"""The printed time schedule follows the names set in the tool.

The schedule PDF supplies each category's and segment's name, and that is only
the default: renaming a category or segment in the tool must reach the schedule
page as it already reaches the podium page. So every parsed row carries the ids
of the category/segment it was grouped into, and `structure.schedule_rows`
prints their current names. Schedules parsed before the rows carried ids are
linked at generation by re-parsing the kept `schedule.pdf` — or the
`schedule.xml` of a competition parsed while FSM's DT_SCHEDULE XML (with its
standard English names) was still accepted.
"""
import copy

import pytest

import assemble
import function_app as fa
import generate_pages
import schedule_parser as sp
import structure as st

from conftest import schedule_pdf


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


# English names: what an organizer then translates in the tool.
ENGLISH = schedule_pdf(
    "31.10.2026",
    "10:00:0011:30:00  8  Junior Synchronized Skating   Short Program",
    "12:00:0013:30:00  6  Senior Synchronized Skating   Free Skating",
    "01.11.2026",
    "10:00:0011:30:00  8  Junior Synchronized Skating   Free Skating",
)
# The DT_SCHEDULE XML of the same schedule, as an earlier parse kept it.
ENGLISH_XML = schedule_xml(
    ("FSKXSYNCHRONJUNIOR----QUAL000100--", "2026-10-31T10:00:00",
     "Junior Synchronized Skating Short Program"),
    ("FSKXSYNCHRON----------FNL-000100--", "2026-10-31T12:00:00",
     "Senior Synchronized Skating Free Skating"),
    ("FSKXSYNCHRONJUNIOR----FNL-000100--", "2026-11-01T10:00:00",
     "Junior Synchronized Skating Free Skating"),
)


def parsed(source=ENGLISH):
    rows, categories, _meta = (sp.parse_schedule_data(source) if sp.is_pdf(source)
                               else sp.parse_schedule_xml(source))
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

@pytest.mark.parametrize("source", [ENGLISH, ENGLISH_XML], ids=["pdf", "xml"])
def test_rows_carry_their_category_and_segment(source):
    structure = parsed(source)
    for row in structure["schedule"]:
        category = st.find_category(structure, row["categoryId"])
        segment = st.find_segment(category, row["segmentId"])
        assert category["name"] == row["category_name"]
        assert segment["name"] == row["segment_name"]


def test_rows_of_no_category_link_to_nothing():
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


# What an older parse can still identify: an ISU code (XML-parsed categories)
# or a name left as parsed. Never a position — a removal + an addition reuses
# it, and the replacement would print at the deleted item's times.
LEGACY_XML_PRINTED = [
    ("Juniorit", "Short Program"),
    ("Senior Synchronized Skating", "Free Skating"),
    ("Juniorit", "Free Skating"),        # the renamed segment has no identity left
]


def test_a_legacy_xml_schedule_links_renamed_categories_by_code():
    structure = legacy(parsed(ENGLISH_XML))
    rename(structure)
    assert not sp.schedule_rows_linked(structure["schedule"])

    assert sp.link_legacy_schedule(structure, ENGLISH_XML)
    assert sp.schedule_rows_linked(structure["schedule"])
    assert printed(structure) == LEGACY_XML_PRINTED


def test_a_legacy_pdf_schedule_links_only_unrenamed_categories():
    structure = legacy(parsed())
    rename(structure)

    assert sp.link_legacy_schedule(structure, ENGLISH)
    assert printed(structure) == ENGLISH_PRINTED
    senior = by_name(structure, "Senior Synchronized Skating")
    assert [r["categoryId"] for r in structure["schedule"]] == [None, senior["id"], None]


def test_a_source_that_does_not_reproduce_the_rows_links_nothing():
    structure = legacy(parsed())
    rename(structure)
    before = copy.deepcopy(structure["schedule"])
    other = schedule_xml(("FSKWSINGLES-----------FNL-000100--", "2026-10-31T10:00:00",
                          "Women Free Skating"))
    assert not sp.link_legacy_schedule(structure, other)
    assert structure["schedule"] == before
    assert printed(structure) == ENGLISH_PRINTED


@pytest.mark.parametrize("kept", ["xml", "pdf"])
def test_generation_links_a_legacy_schedule_from_the_kept_file(storage, kept):
    source = ENGLISH_XML if kept == "xml" else ENGLISH
    structure = legacy(parsed(source))
    rename(structure)
    folder = "Spring Trophy 2026-abc12345"
    storage.container.blobs[f"{folder}/schedule.{kept}"] = source
    # A stale file from another parse beside the one the rows came from.
    other = "pdf" if kept == "xml" else "xml"
    storage.container.blobs[f"{folder}/schedule.{other}"] = (
        schedule_pdf("31.10.2026", "09:00:0010:00:00  4  Naiset   Vapaaohjelma")
        if other == "pdf" else schedule_xml())

    fa._link_legacy_schedule(folder, structure)
    assert sp.schedule_rows_linked(structure["schedule"])
    assert printed(structure) == (LEGACY_XML_PRINTED if kept == "xml" else ENGLISH_PRINTED)


def test_generation_without_a_kept_file_prints_as_parsed(storage):
    structure = legacy(parsed())
    rename(structure)
    fa._link_legacy_schedule("Spring Trophy 2026-abc12345", structure)
    assert printed(structure) == ENGLISH_PRINTED


def test_a_replaced_last_segment_does_not_inherit_its_times():
    """Free skating removed, an exhibition added: `add_segment` gives it the
    freed position, but not the free skating's schedule row."""
    structure = legacy(parsed(ENGLISH_XML))
    junior = by_name(structure, "Junior Synchronized Skating")
    junior["segments"] = [s for s in junior["segments"] if s["name"] != "Free Skating"]
    junior["segments"].append(st.new_segment("Exhibition", len(junior["segments"])))

    assert sp.link_legacy_schedule(structure, ENGLISH_XML)
    assert printed(structure) == ENGLISH_PRINTED
    assert structure["schedule"][2]["segmentId"] is None


def test_a_replaced_last_category_does_not_inherit_its_times():
    """The senior category removed, a hand-made one with as many segments added
    in its place: it does not take over the senior's schedule row."""
    structure = legacy(parsed())
    senior = by_name(structure, "Senior Synchronized Skating")
    structure["categories"].remove(senior)
    replacement = st.new_category("Hand-made", "synchro", senior["order"])
    replacement["segments"].append(st.new_segment("Free Skating", 0))
    structure["categories"].append(replacement)

    assert sp.link_legacy_schedule(structure, ENGLISH)
    assert printed(structure) == ENGLISH_PRINTED
    assert structure["schedule"][1]["categoryId"] is None
