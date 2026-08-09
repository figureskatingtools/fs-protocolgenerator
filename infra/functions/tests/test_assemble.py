"""`assemble` — the empty-page filter applied to uploaded result PDFs.

FSM/ISU exports often spill a trailing page that holds only the repeated title
chrome, the "printed:"/"Page N / N" furniture and the Legend block. These tests
build synthetic PDFs in the shape of real exports (reportlab, one text line per
row) and assert that such pages are dropped while short-but-real sheets, panel
pages and scanned/image pages survive.
"""
import io

import pytest
from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

import assemble

# ── real page shapes (from a validated 80-page protocol) ───────────────────────

JUDGES_DETAILS_PAGE_1 = [
    "JUDGES DETAILS PER SKATER",
    "SM-NOVIISI TYTÖT FREE SKATING",
    "Pl. Name                       Nation  StNr  Total  TES   PCS   Ded.",
    "1 Nea KURJENMÄKI RTL 3 66.94 40.30 26.64 0.00",
    "2 Sofia HAAPANIEMI HL 5 64.12 38.02 26.10 0.00",
    "printed:   09.08.2026 12.30                Page 1 / 2",
]

JUDGES_DETAILS_EMPTY_PAGE = [
    "JUDGES DETAILS PER SKATER",
    "SM-NOVIISI TYTÖT FREE SKATING",
    "Legend:",
    "#   Sequence number   GOE Grade of Execution",
    "b   Bonus Point for jump added to the element score",
    "printed:   09.08.2026 12.30                Page 2 / 2",
]

SHORT_RESULTS_PAGE = [
    "RESULTS",
    "SM-SENIORI MIEHET FREE SKATING",
    "Pl.Name   Nation   Total SegmentScore",
    "1 Matias LINDFORS  PeSal  139.83",
    "2 Makar SUNTSEV  KaTa  123.29",
    "Ms. Tarja RISTANEN, FIN   Ms. Anu NIINIRANTA, FIN",
    "Referee   Technical Controller",
    "Legend:",
    "WD  Withdrawn",
    "printed:   09.08.2026 16.31   Page 1 / 1",
]

PANEL_PAGE = [
    "JUDGES PANEL",
    "SM-JUNIORI NAISET SHORT PROGRAM",
    "Referee   Ms. Tarja RISTANEN   FIN",
    "Technical Controller   Ms. Anu NIINIRANTA   FIN",
    "Judge No.1   Mr. Veikko HAVUKAINEN   FIN",
    "printed:   09.08.2026 16.31   Page 1 / 1",
]


# ── helpers ────────────────────────────────────────────────────────────────────

def make_pdf(pages: list) -> bytes:
    """A PDF from a list of pages, each page a list of text lines (or None for a
    page with nothing drawn on it at all)."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for lines in pages:
        y = A4[1] - 60
        for line in (lines or []):
            c.drawString(40, y, line)
            y -= 16
        c.showPage()
    c.save()
    return buf.getvalue()


def make_image_page_pdf() -> bytes:
    """A one-page PDF holding only a generated bitmap — a stand-in for a scanned
    page, which must never be dropped for having no text."""
    from PIL import Image
    from reportlab.lib.utils import ImageReader

    img = Image.new("RGB", (120, 90), (200, 120, 60))
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.drawImage(ImageReader(img), 40, 400, width=240, height=180)
    c.showPage()
    c.save()
    return buf.getvalue()


def appended_page_count(pdf_bytes: bytes, **kwargs) -> int:
    writer = PdfWriter()
    assert assemble._append_pdf_bytes(writer, pdf_bytes, **kwargs) is True
    return len(writer.pages)


def appended_texts(pdf_bytes: bytes, **kwargs) -> list:
    """The text of every page that made it into the writer."""
    writer = PdfWriter()
    assert assemble._append_pdf_bytes(writer, pdf_bytes, **kwargs) is True
    out = io.BytesIO()
    writer.write(out)
    out.seek(0)
    return [page.extract_text() or "" for page in PdfReader(out).pages]


# ── the filter ─────────────────────────────────────────────────────────────────

def test_trailing_legend_only_page_is_dropped():
    data = make_pdf([JUDGES_DETAILS_PAGE_1, JUDGES_DETAILS_EMPTY_PAGE])
    texts = appended_texts(data, skip_empty=True)
    assert len(texts) == 1
    assert "Nea KURJENMÄKI" in texts[0]      # the content page, not the legend one


def test_a_short_one_page_results_sheet_is_kept():
    assert appended_page_count(make_pdf([SHORT_RESULTS_PAGE]), skip_empty=True) == 1


def test_a_panel_page_is_kept():
    assert appended_page_count(make_pdf([PANEL_PAGE]), skip_empty=True) == 1


def test_a_page_with_nothing_drawn_is_dropped():
    data = make_pdf([JUDGES_DETAILS_PAGE_1, None])
    assert appended_page_count(data, skip_empty=True) == 1


def test_a_textless_page_with_an_image_is_kept():
    assert appended_page_count(make_image_page_pdf(), skip_empty=True) == 1


def test_without_skip_empty_every_page_is_appended():
    """Generated pages (cover, event info, podium, last page) go through the same
    helper and must never be filtered."""
    data = make_pdf([JUDGES_DETAILS_PAGE_1, JUDGES_DETAILS_EMPTY_PAGE, None])
    assert appended_page_count(data) == 3


# ── through the assembler ──────────────────────────────────────────────────────

def _structure_with_judges_details() -> dict:
    return {
        "id": "comp-1",
        "name": "Test Competition",
        "event": {"title": "Test Competition", "dates": "09.08.2026", "city": "Helsinki"},
        "coverPage": {"mode": "default", "fileId": None},
        "lastPage": {"mode": "default", "fileId": None},
        "header": {"mode": "default", "fileId": None},
        "footer": {"mode": "default", "fileId": None},
        "footerEnabled": True,
        "schedule": [],
        "files": {"jd-1": {"filename": "judges.pdf", "kind": "pdf"}},
        "categories": [{
            "id": "cat-1",
            "name": "SM-NOVIISI TYTÖT",
            "discipline": "single",
            "order": 1,
            "titlePdf": None,
            "podium": {"photo": None, "names": ["", "", ""]},
            "totalResultsPdf": None,
            "teams": [],
            "segments": [{
                "id": "seg-1", "name": "FREE SKATING", "order": 1, "unitCount": 2,
                "resultsPdf": None, "panelPdf": None, "judgesDetailsPdf": "jd-1",
            }],
        }],
    }


@pytest.mark.parametrize("pages", [
    [JUDGES_DETAILS_PAGE_1],
    [JUDGES_DETAILS_PAGE_1, JUDGES_DETAILS_EMPTY_PAGE],
])
def test_assemble_protocol_drops_the_trailing_empty_page(pages):
    """The protocol comes out the same length whether or not the uploaded judges
    details carry the trailing legend-only page (counted relatively, since the
    generated pages around it depend on the brand assets being available)."""
    data = make_pdf(pages)
    out = assemble.assemble_protocol(_structure_with_judges_details(), lambda _id: data)
    n = len(PdfReader(io.BytesIO(out)).pages)
    # cover + event info + time schedule + judges details page 1 + last page
    assert n == 5
