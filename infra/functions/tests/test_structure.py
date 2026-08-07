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
