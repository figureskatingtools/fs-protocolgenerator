"""
Learning each category's ISU code from the competition's own FS Manager files.

A schedule parsed from the PDF knows the categories only by the names the
organizer printed ("SM-Noviisit", "Aikuiset Mupi") — no ISU code, hence no
discipline and nothing a DT_PARTIC_TEAMS roster (codes only) can be matched on.
Every other FSM export, though, carries both: its filename is the unit's RSC code
(`FSKXSYNCHRONMLKJUN----FNL-000100--_StartListwithTimes.pdf`, see
`structure.code_from_filename`) and its first page prints the category exactly
as the schedule PDF does — a start list says "TIME SCHEDULE" / "JUNIORIT FREE
SKATING", a results sheet "RESULTS" / "JUNIORIT", a head page "JUNIORIT". Read
one such file per code, match the printed line against the schedule's category
(+ segment) names, and the code is known.

The match is strict equality after `fallback_photos.normalize` (case, diacritics,
punctuation and spacing folded): "DEBYTANTTI TYTÖT  FREE SKATING" is
"DEBYTANTTI Tytöt" + "Free Skating", and nothing shorter or longer matches. A
printed name two categories share is reported, never guessed between. Verified
on real synchro, singles and mixed competitions (every code resolved) — but all
test data here is invented.

Pure module: bytes in, decisions out. `function_app._learn_codes_from_pool` does
the listing and downloading.
"""
import io
import logging

from pypdf import PdfReader

from fallback_photos import normalize
import structure as st

# The report kinds whose first page names the category, most useful first: a
# start list exists for every segment as soon as the draw is done, long before
# any result sheet.
SAMPLE_PREFERENCE = ("StartListwithTimes", "JudgesSheetAll", "Results", "ProtocolHeadPage")
# Page-1 header lines to look at (competition name, "TIME SCHEDULE", the title…).
SCAN_LINES = 8
# A sample larger than this is not a one-page start list; skip it.
MAX_SAMPLE_BYTES = 5 * 1024 * 1024


def suffix_of(filename) -> str:
    """`"StartListwithTimes"` from `…_StartListwithTimes.pdf`; `""` when the
    name is not `<code>_<Report>.pdf`."""
    name = str(filename or "").rsplit("/", 1)[-1]
    if "_" not in name or not name.lower().endswith(".pdf"):
        return ""
    return name.rsplit("_", 1)[1][:-4]


def _rank(suffix: str) -> int:
    return SAMPLE_PREFERENCE.index(suffix) if suffix in SAMPLE_PREFERENCE else len(SAMPLE_PREFERENCE)


def pick_samples(names) -> dict:
    """`{code: (folder, filename)}` — one file to read per distinct category code
    out of `(folder, filename)` pairs in listing order: the best report kind by
    `SAMPLE_PREFERENCE`, the first listed on a tie. Files that are not coded FSM
    reports of those kinds are ignored."""
    picks = {}
    for folder, filename in names:
        code = st.code_from_filename(filename)
        suffix = suffix_of(filename)
        if not code or suffix not in SAMPLE_PREFERENCE:
            continue
        current = picks.get(code)
        if current is None or _rank(suffix) < _rank(suffix_of(current[1])):
            picks[code] = (folder, filename)
    return picks


def page1_lines(pdf_bytes) -> list:
    """The first `SCAN_LINES` non-empty lines of page 1, in both of pypdf's
    extraction modes (plain first, then layout) — the header lines come out in
    slightly different order in the two, and equality matching makes the extra
    candidates harmless. `[]` when the PDF cannot be read."""
    lines = []
    try:
        page = PdfReader(io.BytesIO(pdf_bytes)).pages[0]
    except Exception:
        return []
    for mode in (None, "layout"):
        try:
            text = page.extract_text(extraction_mode=mode) if mode else page.extract_text()
        except Exception:
            continue
        found = [l.strip() for l in (text or "").splitlines() if l.strip()][:SCAN_LINES]
        lines.extend(l for l in found if l not in lines)
    return lines


def printed_index(structure: dict) -> dict:
    """`normalized printed text -> [category…]`: every category by its name, and by
    its name followed by each of its segment names — the two shapes the FSM
    reports print."""
    index = {}
    for cat in structure.get("categories") or []:
        name = (cat.get("name") or "").strip()
        if not name:
            continue
        keys = {normalize(name)}
        for seg in cat.get("segments") or []:
            seg_name = (seg.get("name") or "").strip()
            if seg_name:
                keys.add(normalize(f"{name} {seg_name}"))
        for key in keys:
            if key:
                index.setdefault(key, []).append(cat)
    return index


def match_printed(structure: dict, lines, index=None):
    """The one category a sample's header lines name, or None. The first line
    that equals (normalized) a category name or a category + segment name wins;
    a line that two categories could have printed matches nothing."""
    index = printed_index(structure) if index is None else index
    for line in lines or []:
        hits = index.get(normalize(line))
        if not hits:
            continue
        distinct = {id(c): c for c in hits}
        if len(distinct) == 1:
            return next(iter(distinct.values()))
        return None
    return None


def learn_codes(structure: dict, samples) -> dict:
    """Apply what the samples teach: `samples` are `(code, page1 lines)` pairs,
    one per code. Returns `{categoryId: code}` for every code stamped (through
    `structure.apply_code`, so an existing code is only ever made more specific).

    When several codes resolve to one category — FSM split the start order
    into blocks the schedule prints once — only the code that is a dash-prefix
    of all the others (the bare one) is stamped; if there is none, none is."""
    index = printed_index(structure)
    by_category = {}
    for code, lines in samples:
        code = st.strip_code(code)
        cat = match_printed(structure, lines, index) if code else None
        if cat is not None:
            by_category.setdefault(cat["id"], {"category": cat, "codes": set()})["codes"].add(code)

    learned = {}
    for cat_id, entry in by_category.items():
        codes = entry["codes"]
        if len(codes) == 1:
            chosen = next(iter(codes))
        else:
            bare = [c for c in codes if all(o == c or o.startswith(c + "-") for o in codes)]
            if len(bare) != 1:
                logging.info("Pool code learning: %d codes print the same category name, none stamped",
                             len(codes))
                continue
            chosen = bare[0]
        if st.apply_code(entry["category"], chosen) or st.strip_code(entry["category"].get("code")) == chosen:
            learned[cat_id] = chosen
    return learned
