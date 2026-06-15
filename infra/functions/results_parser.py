"""
Top-three extraction from a category's total-results PDF (calibration pending).

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


def _format_row(rest: str) -> str:
    """Turn the non-rank remainder of a result row into '<nation/club> - <name>'.

    Columns are Name | Nation/Club | scores, so once the numeric score columns are
    dropped the last token is the nation/club and everything before it is the name
    ("Given FAMILY" for a skater, a team name for synchro)."""
    tokens = [t for t in rest.split() if not _NUMERIC.match(t)]
    if not tokens:
        return ""
    if len(tokens) == 1:
        return tokens[0]
    code = tokens[-1]
    name = " ".join(tokens[:-1]).strip(" -–·")
    return f"{code} - {name}" if name else code


def count_result_rows(pdf_bytes) -> int:
    """Count the placement rows in a results PDF (one row per competition unit —
    skater, pair or team). Used to tally competition-wide counts for the
    information page: a total-results sheet yields the category's unit count, a
    segment-results sheet yields that segment's performance count. Heuristic, same
    row shape as parse_top_three; counts rows whose remainder reads as a name."""
    try:
        text = _read_text(pdf_bytes)
    except Exception as e:
        logging.warning(f"Could not read results PDF for row count: {e}")
        return 0
    count = 0
    for line in text.split("\n"):
        m = _RANK.match(line)
        if not m:
            continue
        if not (1 <= int(m.group(1)) <= 99):
            continue
        if _format_row(m.group(2)):
            count += 1
    return count


def parse_top_three(pdf_bytes) -> list:
    """Return ['<code> - <name>', …] for ranks 1-3 (a 3-element list, '' when a
    placement can't be read). Empty list when the PDF can't be parsed at all."""
    try:
        text = _read_text(pdf_bytes)
    except Exception as e:
        logging.warning(f"Could not read results PDF for top three: {e}")
        return []

    found = {}
    for line in text.split("\n"):
        m = _RANK.match(line)
        if not m:
            continue
        rank = int(m.group(1))
        if rank not in (1, 2, 3) or rank in found:
            continue
        formatted = _format_row(m.group(2))
        if formatted:
            found[rank] = formatted
        if len(found) == 3:
            break

    if not found:
        return []
    return [found.get(1, ""), found.get(2, ""), found.get(3, "")]
