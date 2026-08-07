"""
Placement extraction from a category's total-results PDF (calibration pending).

When the organizer drops a category's *total results* PDF into its slot, we read
the top three placements and pre-fill the podium name fields in the structure, so
the podium page can be produced without retyping. Names are rendered in the
"<nation/club> - <competitor or team name>" style the podium expects, e.g.
"HTK - Helsinki Finettes" for a synchro team or "FIN - VIRTANEN Anna" for a skater.

This is deliberately heuristic and only fills *empty* podium fields — the operator
can always correct a value. It is calibrated against a real ISU single-skating
result sheet (Tikkurila Trophy), whose rows are

    <Pl.>  <Given FAMILY>  <Nation/Club>  <Total Score>  <SP>  <FS>

e.g. "1 Lotta TERHO SCT 38.53 1" → "SCT - Lotta TERHO". The nation/club is the last
non-numeric column (often a mixed-case club code like "KaTa"/"PoriTa", sometimes an
all-caps code like "SCT"/"HL"), with the name to its left and the scores trailing.
Expected to be refined further against more real exports (e.g. synchro totals).

`parse_result_rows` is the single row extractor every reader is built on: the
podium pre-fill (`parse_top_three`), the information-page tally
(`count_result_rows`) and roster matching (which needs the placed team names of a
block) all read the same rows.
"""
import io
import re
import logging

from pypdf import PdfReader

# A purely numeric/score/time token (scores, start order, segment ranks) — never
# part of a name or a club code.
_NUMERIC = re.compile(r'^[\d.,:/+\-]+$')
# Placement row: a 1-2 digit place, then the rest. Layout extraction often glues
# the place onto the name ("1Lotta TERHO …"), so no separator is required — but the
# remainder must begin with a non-digit (a name) to reject stray numeric lines.
_RANK = re.compile(r'^\s*(\d{1,2})[.)]?\s*(\D.*\S)\s*$')


def _read_text(pdf_bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text(extraction_mode="layout") or "")
        except Exception:
            pages.append(page.extract_text() or "")
    return "\n".join(pages)


def _split_row(rest: str):
    """Split the non-rank remainder of a result row into `(name, club)`.

    Columns are Name | Nation/Club | scores, so once the numeric score columns are
    dropped the last token is the nation/club and everything before it is the name
    ("Given FAMILY" for a skater, a team name for synchro). A lone token is a name
    with no club, and so is a remainder whose name part is pure punctuation."""
    tokens = [t for t in rest.split() if not _NUMERIC.match(t)]
    if not tokens:
        return "", ""
    if len(tokens) == 1:
        return tokens[0], ""
    club = tokens[-1]
    name = " ".join(tokens[:-1]).strip(" -–·")
    if not name:
        return club, ""
    return name, club


def _format_row(name: str, club: str) -> str:
    """'<nation/club> - <name>' as the podium fields expect it."""
    return f"{club} - {name}" if club else name


def parse_result_rows(pdf_bytes) -> list:
    """Read a results PDF into `[{"rank", "name", "club"}, …]`, in page order.

    One row per placement — a competition unit (skater, pair or team) on a
    total-results sheet, a performance on a segment sheet. Only the first
    occurrence of each rank is kept (a repeated place is stray text, not a second
    placement) and rows whose remainder holds no name are dropped. Empty list when
    the PDF can't be read at all."""
    try:
        text = _read_text(pdf_bytes)
    except Exception as e:
        logging.warning(f"Could not read results PDF: {e}")
        return []

    rows, seen = [], set()
    for line in text.split("\n"):
        m = _RANK.match(line)
        if not m:
            continue
        rank = int(m.group(1))
        if not (1 <= rank <= 99) or rank in seen:
            continue
        name, club = _split_row(m.group(2))
        if not name:
            continue
        seen.add(rank)
        rows.append({"rank": rank, "name": name, "club": club})
    return rows


def count_result_rows(pdf_bytes) -> int:
    """Count the placement rows in a results PDF. Used to tally competition-wide
    counts for the information page: a total-results sheet yields the category's
    unit count, a segment-results sheet yields that segment's performance count."""
    return len(parse_result_rows(pdf_bytes))


def parse_top_three(pdf_bytes) -> list:
    """Return ['<code> - <name>', …] for ranks 1-3 (a 3-element list, '' when a
    placement can't be read). Empty list when the PDF can't be parsed at all."""
    found = {r["rank"]: _format_row(r["name"], r["club"])
             for r in parse_result_rows(pdf_bytes) if r["rank"] in (1, 2, 3)}
    if not found:
        return []
    return [found.get(1, ""), found.get(2, ""), found.get(3, "")]
