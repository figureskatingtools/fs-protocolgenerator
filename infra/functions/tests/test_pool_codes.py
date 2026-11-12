"""`pool_codes` — reading each category's ISU code off the competition's own FS
Manager reports.

The schedule PDF names categories the organizer's way and carries no code; every
other FSM report is *named* by its unit's code and *prints* the category the
schedule's way. Equality after normalisation ties the two together. Every name
here is invented; the shapes are FSM's.
"""
import pool_codes as pc
import structure as st

from conftest import sheet_pdf


def cat(name, *segments, code=""):
    c = st.new_category(name, "single", 0)
    c["code"] = code
    for i, seg in enumerate(segments):
        c["segments"].append(st.new_segment(seg, i))
    return c


def struct(*cats):
    return {"categories": list(cats)}


START_LIST = "FSKXSYNCHRONMLKNOV----FNL-000100--_StartListwithTimes.pdf"


# ── choosing what to read ─────────────────────────────────────────────────────

def test_suffix_of_reads_the_report_kind():
    assert pc.suffix_of(START_LIST) == "StartListwithTimes"
    assert pc.suffix_of("FSKXSYNCHRONADVNOV----------------_Results.pdf") == "Results"
    assert pc.suffix_of("Team-Name_Club.jpg") == ""
    assert pc.suffix_of("FSK-------------------------------_CompetitionSchedule.pdf") == "CompetitionSchedule"


def test_pick_samples_one_report_per_code_best_kind_first():
    names = [
        ("fsm", "FSKXSYNCHRONMLKNOV----FNL-000100--_JudgesSheetAll.pdf"),
        ("fsm", START_LIST),
        ("fsm", "FSKXSYNCHRONMLKNOV----FNL-000100--_RefereeSheet.pdf"),      # not a kind we read
        ("fsm", "FSKXSYNCHRONMLMINO----FNL-000200--_StartListwithTimes.pdf"),
        ("fsm", "FSKXSYNCHRONMLMINO----FNL-000100--_StartListwithTimes.pdf"),  # same code, later
        ("fsm", "FSK-------------------------------_CompetitionSchedule.pdf"),
        ("fsm", "DT_PARTIC_TEAMS_FSK-------------------------------.xml"),
        ("uploads", "FSKXSYNCHRONADVNOV--01------------_ProtocolHeadPage.pdf"),
        ("uploads", "photo.jpg"),
    ]
    picks = pc.pick_samples(names)
    assert picks == {
        "FSKXSYNCHRONMLKNOV": ("fsm", START_LIST),
        "FSKXSYNCHRONMLMINO": ("fsm", "FSKXSYNCHRONMLMINO----FNL-000200--_StartListwithTimes.pdf"),
        "FSKXSYNCHRONADVNOV--01": ("uploads", "FSKXSYNCHRONADVNOV--01------------_ProtocolHeadPage.pdf"),
    }


# ── reading the printed name ──────────────────────────────────────────────────

def test_a_start_list_names_category_and_segment():
    s = struct(cat("Noviisit, Mupi", "Free Skating"), cat("Minorit", "Free Skating 1", "Free Skating 2"))
    lines = pc.page1_lines(sheet_pdf("SPRING TROPHY 2026", "TIME SCHEDULE",
                                     "NOVIISIT, MUPI FREE SKATING", "printed: 01.01.2026"))
    assert pc.match_printed(s, lines)["name"] == "Noviisit, Mupi"


def test_double_spaces_case_and_diacritics_are_folded():
    s = struct(cat("DEBYTANTTI Tytöt", "Free Skating"), cat("TÄHTIDEBYTANTTI Tytöt", "Free Skating"))
    lines = pc.page1_lines(sheet_pdf("TIME SCHEDULE", "DEBYTANTTI TYTÖT  FREE SKATING"))
    assert pc.match_printed(s, lines)["name"] == "DEBYTANTTI Tytöt"


def test_numbered_segments_pick_their_own_segment_but_the_same_category():
    s = struct(cat("Minorit", "Free Skating 1", "Free Skating 2"), cat("Juniorit", "Free Skating"))
    one = pc.page1_lines(sheet_pdf("TIME SCHEDULE", "MINORIT FREE SKATING 1"))
    two = pc.page1_lines(sheet_pdf("TIME SCHEDULE", "MINORIT FREE SKATING 2"))
    assert pc.match_printed(s, one)["name"] == "Minorit"
    assert pc.match_printed(s, two)["name"] == "Minorit"


def test_results_and_head_pages_print_the_category_alone():
    s = struct(cat("SM-Noviisit L1", "Free Skating"), cat("SM-Noviisit L2", "Free Skating"))
    results = pc.page1_lines(sheet_pdf("RESULTS", "SM-NOVIISIT L2", "printed: 01.11.2026 16.30"))
    head = pc.page1_lines(sheet_pdf("SM-NOVIISIT L1", "printed: 01.11.2026 11.19", "Ice Hall"))
    assert pc.match_printed(s, results)["name"] == "SM-Noviisit L2"
    assert pc.match_printed(s, head)["name"] == "SM-Noviisit L1"


def test_a_judges_sheet_header_is_read_past_the_referee_and_competition_lines():
    s = struct(cat("Aikuiset Mupi", "Free Skating"), cat("Aikuiset", "Free Skating"))
    lines = pc.page1_lines(sheet_pdf("Referee Ms. Jane DOE", "Spring Trophy 2026",
                                     "AIKUISET MUPI FREE SKATING", "1 Blue Herons BHK"))
    assert pc.match_printed(s, lines)["name"] == "Aikuiset Mupi"


def test_a_name_two_categories_share_matches_nothing():
    s = struct(cat("Noviisit", "Free Skating"), cat("Noviisit", "Free Skating"))
    lines = pc.page1_lines(sheet_pdf("TIME SCHEDULE", "NOVIISIT FREE SKATING"))
    assert pc.match_printed(s, lines) is None


def test_the_competition_name_and_boilerplate_never_match():
    """Equality, not containment: the competition name, "TIME SCHEDULE" and the
    print stamp are not category names, and a category name inside the
    competition's name ("Juniorit" in "Juniorit Cup") is no match either."""
    s = struct(cat("Juniorit", "Free Skating"), cat("Noviisit", "Free Skating"))
    lines = pc.page1_lines(sheet_pdf("JUNIORIT CUP 2026", "TIME SCHEDULE", "printed: 12.11.2026 17.25",
                                     "Nation Current", "1 Blue Herons BHK"))
    assert pc.match_printed(s, lines) is None


def test_an_unreadable_pdf_yields_no_lines():
    assert pc.page1_lines(b"%PDF-1.4 not really") == []
    assert pc.page1_lines(b"") == []


# ── stamping ──────────────────────────────────────────────────────────────────

def test_learn_codes_stamps_code_and_discipline():
    s = struct(cat("Noviisit, Mupi", "Free Skating"), cat("SM-Noviisit", "Free Skating"))
    learned = pc.learn_codes(s, [
        ("FSKXSYNCHRONMLKNOV", ["TIME SCHEDULE", "NOVIISIT, MUPI FREE SKATING"]),
        ("FSKXSYNCHRONADVNOV", ["TIME SCHEDULE", "SM-NOVIISIT FREE SKATING"]),
        ("FSKXSYNCHRONJUNIOR", ["TIME SCHEDULE", "SM-JUNIORIT FREE SKATING"]),   # no such category
    ])
    by_name = {c["name"]: c for c in s["categories"]}
    assert learned == {by_name["Noviisit, Mupi"]["id"]: "FSKXSYNCHRONMLKNOV",
                       by_name["SM-Noviisit"]["id"]: "FSKXSYNCHRONADVNOV"}
    assert by_name["Noviisit, Mupi"]["code"] == "FSKXSYNCHRONMLKNOV"
    assert by_name["Noviisit, Mupi"]["discipline"] == "synchro"
    assert by_name["SM-Noviisit"]["discipline"] == "synchro"


def test_blocks_printed_distinctly_get_their_own_codes():
    s = struct(cat("SM-Noviisit L1", "Free Skating"), cat("SM-Noviisit L2", "Free Skating"))
    pc.learn_codes(s, [("FSKXSYNCHRONADVNOV--01", ["RESULTS", "SM-NOVIISIT L1"]),
                       ("FSKXSYNCHRONADVNOV", ["RESULTS", "SM-NOVIISIT L2"])])
    assert [c["code"] for c in s["categories"]] == ["FSKXSYNCHRONADVNOV--01", "FSKXSYNCHRONADVNOV"]


def test_blocks_printed_once_stamp_the_bare_code_only():
    """FSM split the start order into blocks the schedule prints as one
    category: the bare code is the one that names them all."""
    s = struct(cat("Tulokkaat", "Free Skating"))
    pc.learn_codes(s, [("FSKXSYNCHRONMLTULO--01", ["TIME SCHEDULE", "TULOKKAAT FREE SKATING"]),
                       ("FSKXSYNCHRONMLTULO--02", ["TIME SCHEDULE", "TULOKKAAT FREE SKATING"]),
                       ("FSKXSYNCHRONMLTULO", ["TIME SCHEDULE", "TULOKKAAT FREE SKATING"])])
    assert s["categories"][0]["code"] == "FSKXSYNCHRONMLTULO"


def test_two_unrelated_codes_printing_one_name_stamp_nothing():
    s = struct(cat("Juniorit", "Free Skating"))
    assert pc.learn_codes(s, [("FSKXSYNCHRONMLKJUN", ["TIME SCHEDULE", "JUNIORIT FREE SKATING"]),
                              ("FSKXSYNCHRONJUNIOR", ["TIME SCHEDULE", "JUNIORIT FREE SKATING"])]) == {}
    assert s["categories"][0]["code"] == ""


def test_learning_never_overwrites_a_conflicting_code():
    s = struct(cat("Juniorit", "Free Skating", code="FSKXSYNCHRONJUNIOR"))
    assert pc.learn_codes(s, [("FSKXSYNCHRONMLKJUN", ["TIME SCHEDULE", "JUNIORIT FREE SKATING"])]) == {}
    assert s["categories"][0]["code"] == "FSKXSYNCHRONJUNIOR"


def test_learning_upgrades_a_bare_code_to_its_block():
    s = struct(cat("SM-Noviisit L1", "Free Skating", code="FSKXSYNCHRONADVNOV"))
    pc.learn_codes(s, [("FSKXSYNCHRONADVNOV--01", ["RESULTS", "SM-NOVIISIT L1"])])
    assert s["categories"][0]["code"] == "FSKXSYNCHRONADVNOV--01"
