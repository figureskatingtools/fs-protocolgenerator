import structure as st


def test_title_filename_prefixes_map_to_disciplines():
    cases = {
        "FSKWSINGLES-JUNIOR-0001.pdf": "single",
        "FSKMSINGLES-SENIOR-0001.pdf": "single",
        "FSKXSYNCHRON--------------SM-0001.pdf": "synchro",
        "FSKXICEDANCE-0001.pdf": "dance",
        "FSKMSOLDANCE-0001.pdf": "dance",
        "FSKWSOLDANCE-0001.pdf": "dance",
        "FSKXPAIRS-0001.pdf": "pair",
    }
    for filename, expected in cases.items():
        assert st.discipline_from_title_filename(filename) == expected, filename


def test_title_filename_prefix_is_case_insensitive():
    assert st.discipline_from_title_filename("fskxsynchron-sm.pdf") == "synchro"


def test_title_filename_without_isu_prefix_gives_none():
    assert st.discipline_from_title_filename("head_page.pdf") is None
    assert st.discipline_from_title_filename("") is None
    assert st.discipline_from_title_filename(None) is None
    # Prefix must be at the start, not merely contained.
    assert st.discipline_from_title_filename("copy_of_FSKXPAIRS.pdf") is None


def test_apply_title_discipline_overrides_guess():
    cat = st.new_category("Aikuiset", "single", 0)
    assert st.apply_title_discipline(cat, "FSKXSYNCHRON-0001.pdf") is True
    assert cat["discipline"] == "synchro"


def test_apply_title_discipline_leaves_category_alone_without_signal():
    cat = st.new_category("Aikuiset", "synchro", 0)
    assert st.apply_title_discipline(cat, "custom_head_page.pdf") is False
    assert cat["discipline"] == "synchro"
    assert st.apply_title_discipline(None, "FSKXPAIRS-0001.pdf") is False


# ── ISU codes ─────────────────────────────────────────────────────────────────

def test_code_from_filename_reads_the_rsc_field():
    cases = {
        "FSKXSYNCHRONADVNOV--01FNL-000100--_StartListwithTimes.pdf": "FSKXSYNCHRONADVNOV--01",
        "FSKXSYNCHRONMLMINO----FNL-000200--_JudgesSheetAll.pdf": "FSKXSYNCHRONMLMINO",
        "FSKXSYNCHRON----------FNL-000100--_SegmentResults.pdf": "FSKXSYNCHRON",
        "FSKXSYNCHRON----------------------_ProtocolHeadPage.pdf": "FSKXSYNCHRON",
        "FSKWSINGLES-DEBYTW----QUAL000100--_RefereeSheet.pdf": "FSKWSINGLES-DEBYTW",
        "fskwsingles-debytw----fnl-000100--_x.pdf": "FSKWSINGLES-DEBYTW",
        "uploads/FSKXICEDANCEBASNOV----QUAL0001PD--_StartListwithTimes.pdf": "FSKXICEDANCEBASNOV",
    }
    for filename, expected in cases.items():
        assert st.code_from_filename(filename) == expected, filename


def test_code_from_filename_ignores_anything_that_is_not_an_fsm_export():
    for filename in ("photo.jpg", "Team-Name_Club.jpg", "FSKXSYNCHRON-0001.pdf",
                     "FSKXSYNCHRON--------------SM-0001.pdf", "head page_final.pdf",
                     "FSK-------------------------------_CompetitionSchedule.pdf"[:10] + "_x.pdf",
                     "", None):
        assert st.code_from_filename(filename) == "", filename


def test_strip_code_folds_padding_and_case_but_keeps_the_block():
    assert st.strip_code("FSKWSINGLES-DEBYTW----") == "FSKWSINGLES-DEBYTW"
    assert st.strip_code(" fskxsynchron--01 ") == "FSKXSYNCHRON--01"
    assert st.strip_code(None) == ""


def test_discipline_from_code_reads_the_prefix():
    assert st.discipline_from_code("FSKXSYNCHRONADVNOV--01") == "synchro"
    assert st.discipline_from_code("FSKWSINGLES-DEBYTW") == "single"
    assert st.discipline_from_code("FSKXICEDANCEJUNIOR") == "dance"
    assert st.discipline_from_code("FSKXPAIRS") == "pair"
    assert st.discipline_from_code("hand-made") is None


def test_apply_code_sets_a_missing_code_and_the_discipline():
    cat = st.new_category("SM-Noviisit", "single", 0)
    assert st.apply_code(cat, "FSKXSYNCHRONADVNOV") is True
    assert cat["code"] == "FSKXSYNCHRONADVNOV" and cat["discipline"] == "synchro"


def test_apply_code_treats_a_padded_code_as_the_same_code():
    """A DT_SCHEDULE-era code is stored padded; it is not rewritten, only its
    discipline is (re)asserted."""
    cat = st.new_category("Debytantit", "synchro", 0)
    cat["code"] = "FSKWSINGLES-DEBYTW----"
    assert st.apply_code(cat, "FSKWSINGLES-DEBYTW") is True       # discipline fixed
    assert cat["code"] == "FSKWSINGLES-DEBYTW----" and cat["discipline"] == "single"
    assert st.apply_code(cat, "FSKWSINGLES-DEBYTW") is False


def test_apply_code_upgrades_a_bare_code_to_its_block_but_never_the_reverse():
    cat = st.new_category("SM-Noviisit L1", "single", 0)
    st.apply_code(cat, "FSKXSYNCHRONADVNOV")
    assert st.apply_code(cat, "FSKXSYNCHRONADVNOV--01") is True
    assert cat["code"] == "FSKXSYNCHRONADVNOV--01"
    assert st.apply_code(cat, "FSKXSYNCHRONADVNOV") is False
    assert cat["code"] == "FSKXSYNCHRONADVNOV--01"


def test_apply_code_never_overwrites_a_conflicting_code():
    cat = st.new_category("Juniorit", "synchro", 0)
    cat["code"] = "FSKXSYNCHRONMLKJUN--01"
    for other in ("FSKXSYNCHRONJUNIOR", "FSKXSYNCHRONMLKJUN--02", "FSKWSINGLES-JUNIOR",
                  "FSKXSYNCHRONMLKJUN"):
        assert st.apply_code(cat, other) is False, other
    assert cat["code"] == "FSKXSYNCHRONMLKJUN--01" and cat["discipline"] == "synchro"


def test_apply_code_ignores_empty_input():
    cat = st.new_category("Juniorit", "synchro", 0)
    assert st.apply_code(cat, "") is False and st.apply_code(None, "FSKXSYNCHRON") is False
    assert cat["code"] == ""


def test_categories_needing_codes_flags_missing_and_shared_codes():
    a = st.new_category("A", "single", 0)
    b = st.new_category("B", "single", 1)
    assert st.categories_needing_codes({"categories": [a, b]}) is True
    a["code"], b["code"] = "FSKXSYNCHRONADVNOV", "FSKXSYNCHRONADVNOV----"
    assert st.categories_needing_codes({"categories": [a, b]}) is True     # the same code twice
    b["code"] = "FSKXSYNCHRONADVNOV--01"
    assert st.categories_needing_codes({"categories": [a, b]}) is False
    assert st.categories_needing_codes({"categories": []}) is False
