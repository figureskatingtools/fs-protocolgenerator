"""
Generated protocol pages (reportlab).

Everything the organizer does *not* supply as a finished PDF is drawn here:
the default cover, the event-info page, the modernised time-schedule page, the
podium page, synchronized-skating team-presentation pages, and the default last
page. Photos (podium, team) are embedded with aspect-preserving fit; when a photo
is missing a neutral placeholder is drawn so a protocol can always be produced.

These are intentionally simple, ISU-protocol-like layouts. The real cover and
last page are placeholders for now and will be replaced with finished designs.
"""
import io
import logging

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

try:
    from PIL import Image, ImageOps
except Exception:  # pragma: no cover
    Image = None
    ImageOps = None

PAGE_W, PAGE_H = A4
MARGIN = 22 * mm

INK = (0.05, 0.12, 0.20)
MUTED = (0.39, 0.47, 0.56)
LINE = (0.78, 0.83, 0.87)
GOLD = (0.69, 0.55, 0.18)


def _new_canvas():
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    return buf, c


def _finish(buf, c) -> bytes:
    c.showPage()
    c.save()
    buf.seek(0)
    return buf.getvalue()


def _wrapped(c, text, x, y, max_width, font, size, leading):
    """Draw left-aligned wrapped text; return the y below the last line."""
    from reportlab.pdfbase.pdfmetrics import stringWidth
    words = (text or "").split()
    line = ""
    for word in words:
        trial = f"{line} {word}".strip()
        if stringWidth(trial, font, size) <= max_width:
            line = trial
        else:
            c.drawString(x, y, line)
            y -= leading
            line = word
    if line:
        c.drawString(x, y, line)
        y -= leading
    return y


def _centered(c, text, y, font, size, color=INK):
    c.setFillColorRGB(*color)
    c.setFont(font, size)
    c.drawCentredString(PAGE_W / 2, y, text)


def _fit_image_box(img_bytes, box_w, box_h):
    """Return (ImageReader, draw_w, draw_h) scaled to fit a box, preserving aspect
    ratio. Falls back gracefully if PIL is unavailable."""
    try:
        if Image is not None:
            im = Image.open(io.BytesIO(img_bytes))
            im = ImageOps.exif_transpose(im)
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            iw, ih = im.size
            out = io.BytesIO()
            im.save(out, format="JPEG", quality=88)
            out.seek(0)
            reader = ImageReader(out)
        else:
            reader = ImageReader(io.BytesIO(img_bytes))
            iw, ih = reader.getSize()
        scale = min(box_w / iw, box_h / ih)
        return reader, iw * scale, ih * scale
    except Exception as e:
        logging.warning(f"Could not load image for embedding: {e}")
        return None, 0, 0


def _placeholder_box(c, x, y, w, h, label):
    c.saveState()
    c.setFillColorRGB(0.93, 0.95, 0.97)
    c.setStrokeColorRGB(*LINE)
    c.rect(x, y, w, h, fill=1, stroke=1)
    c.setFillColorRGB(*MUTED)
    c.setFont("Helvetica", 10)
    c.drawCentredString(x + w / 2, y + h / 2, label)
    c.restoreState()


def _draw_photo(c, img_bytes, x, y, w, h, placeholder_label):
    if img_bytes:
        reader, dw, dh = _fit_image_box(img_bytes, w, h)
        if reader is not None:
            c.drawImage(reader, x + (w - dw) / 2, y + (h - dh) / 2, dw, dh,
                        preserveAspectRatio=True, mask='auto')
            return
    _placeholder_box(c, x, y, w, h, placeholder_label)


# ── pages ──────────────────────────────────────────────────────────────────────

def default_cover_page(title: str, dates: str) -> bytes:
    """White placeholder cover: 'PROTOCOL' + competition title + dates."""
    buf, c = _new_canvas()
    _centered(c, "PROTOCOL", PAGE_H - 95 * mm, "Times-Bold", 30, INK)
    c.setStrokeColorRGB(*GOLD)
    c.setLineWidth(1)
    c.line(PAGE_W / 2 - 30 * mm, PAGE_H - 102 * mm, PAGE_W / 2 + 30 * mm, PAGE_H - 102 * mm)
    if title:
        _centered(c, title, PAGE_H - 120 * mm, "Times-Roman", 18, INK)
    if dates:
        _centered(c, dates, PAGE_H - 132 * mm, "Helvetica", 12, MUTED)
    return _finish(buf, c)


def event_info_page(event: dict) -> bytes:
    """ISU-style event description page (page 2 of a protocol)."""
    buf, c = _new_canvas()
    y = PAGE_H - 70 * mm
    title = event.get("title", "")
    organization = event.get("organization", "")
    authorization = event.get("authorization", "")
    city = event.get("city", "")
    dates = event.get("dates", "")
    rink = event.get("rink", "")

    _centered(c, "Protocol", y, "Times-Roman", 16, MUTED); y -= 9 * mm
    _centered(c, "of the", y, "Helvetica", 11, MUTED); y -= 9 * mm
    _centered(c, title or "—", y, "Times-Bold", 20, INK); y -= 14 * mm

    if organization:
        _centered(c, "organized by", y, "Helvetica", 11, MUTED); y -= 7 * mm
        _centered(c, organization, y, "Times-Roman", 14, INK); y -= 12 * mm
    if authorization:
        _centered(c, "with the authorization of", y, "Helvetica", 11, MUTED); y -= 7 * mm
        _centered(c, authorization, y, "Times-Roman", 14, INK); y -= 12 * mm
    if city or dates:
        _centered(c, "held in", y, "Helvetica", 11, MUTED); y -= 7 * mm
        location = ", ".join(filter(None, [city, dates]))
        _centered(c, location, y, "Times-Roman", 14, INK); y -= 12 * mm
    if rink:
        y -= 4 * mm
        _centered(c, "The events took place at", y, "Helvetica", 11, MUTED); y -= 7 * mm
        _centered(c, rink, y, "Times-Roman", 14, INK)
    return _finish(buf, c)


def time_schedule_page(rows) -> bytes:
    """Modernised time-schedule table: Date · Time · Event."""
    buf, c = _new_canvas()
    x = MARGIN
    y = PAGE_H - MARGIN
    c.setFillColorRGB(*INK)
    c.setFont("Times-Bold", 18)
    c.drawString(x, y, "Time Schedule")
    y -= 12 * mm

    col_date = x
    col_time = x + 45 * mm
    col_event = x + 72 * mm
    event_w = PAGE_W - MARGIN - col_event

    c.setFont("Helvetica-Bold", 8)
    c.setFillColorRGB(*MUTED)
    for label, cx in (("DATE", col_date), ("TIME", col_time), ("EVENT", col_event)):
        c.drawString(cx, y, label)
    y -= 3 * mm
    c.setStrokeColorRGB(*LINE)
    c.line(x, y, PAGE_W - MARGIN, y)
    y -= 6 * mm

    last_date = None
    c.setFont("Helvetica", 9.5)
    for row in rows or []:
        if y < MARGIN + 12 * mm:
            c.showPage()
            y = PAGE_H - MARGIN
            c.setFont("Helvetica", 9.5)
        date_disp = row.get("date_display") or ""
        c.setFillColorRGB(*INK)
        if date_disp and date_disp != last_date:
            c.setFont("Helvetica-Bold", 9.5)
            c.drawString(col_date, y, date_disp)
            c.setFont("Helvetica", 9.5)
            last_date = date_disp
        c.setFillColorRGB(*MUTED)
        c.drawString(col_time, y, row.get("start_time") or "")
        c.setFillColorRGB(*INK)
        y = _wrapped(c, row.get("event_name") or "", col_event, y, event_w,
                     "Helvetica", 9.5, 5.0 * mm)
        y -= 1.5 * mm
    return _finish(buf, c)


def podium_page(category_name: str, photo_bytes, names) -> bytes:
    """Podium photo + 1st/2nd/3rd names, mirroring the ISU podium page."""
    buf, c = _new_canvas()
    _centered(c, "Podium", PAGE_H - 30 * mm, "Times-Bold", 18, INK)
    if category_name:
        _centered(c, category_name, PAGE_H - 39 * mm, "Helvetica", 11, MUTED)

    box_w = PAGE_W - 2 * MARGIN
    box_h = 120 * mm
    box_x = MARGIN
    box_y = PAGE_H - 50 * mm - box_h
    _draw_photo(c, photo_bytes, box_x, box_y, box_w, box_h, "Podium photo (not provided)")

    y = box_y - 16 * mm
    labels = ["1st place", "2nd place", "3rd place"]
    names = (list(names) + ["", "", ""])[:3]
    for label, name in zip(labels, names):
        c.setFillColorRGB(*GOLD)
        c.setFont("Helvetica-Bold", 10)
        c.drawString(MARGIN, y, label)
        c.setFillColorRGB(*INK)
        c.setFont("Times-Roman", 13)
        c.drawString(MARGIN + 28 * mm, y, name or "—")
        y -= 9 * mm
    return _finish(buf, c)


def synchro_team_page(team: dict, photo_bytes) -> bytes:
    """Team-presentation page: organization + team name, photo, and roster."""
    buf, c = _new_canvas()
    name = team.get("name") or "Team"
    org = team.get("org") or ""
    members = team.get("members") or []

    y = PAGE_H - 28 * mm
    c.setFillColorRGB(*INK)
    c.setFont("Times-Bold", 18)
    c.drawString(MARGIN, y, name)
    y -= 8 * mm
    if org:
        c.setFillColorRGB(*MUTED)
        c.setFont("Helvetica", 11)
        c.drawString(MARGIN, y, org)
    y -= 6 * mm

    box_w = PAGE_W - 2 * MARGIN
    box_h = 95 * mm
    box_y = y - box_h
    _draw_photo(c, photo_bytes, MARGIN, box_y, box_w, box_h, "Team photo (not provided)")

    y = box_y - 12 * mm
    c.setFillColorRGB(*MUTED)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(MARGIN, y, "SKATERS")
    y -= 7 * mm

    # Two-column roster
    c.setFillColorRGB(*INK)
    c.setFont("Helvetica", 10)
    col_w = (PAGE_W - 2 * MARGIN) / 2
    half = (len(members) + 1) // 2
    columns = [members[:half], members[half:]]
    start_y = y
    for ci, col in enumerate(columns):
        cy = start_y
        cx = MARGIN + ci * col_w
        for member in col:
            if cy < MARGIN:
                break
            c.drawString(cx, cy, member)
            cy -= 5.5 * mm
    return _finish(buf, c)


def default_last_page() -> bytes:
    """Placeholder last page (to be replaced with a finished design)."""
    buf, c = _new_canvas()
    _centered(c, "the last page placeholder", PAGE_H / 2, "Helvetica", 14, MUTED)
    return _finish(buf, c)


def image_fullpage(img_bytes) -> bytes:
    """Wrap a custom image (e.g. an uploaded cover/last page graphic) into a
    single A4 page, centered and scaled to fit with a small margin."""
    buf, c = _new_canvas()
    inset = 10 * mm
    _draw_photo(c, img_bytes, inset, inset, PAGE_W - 2 * inset, PAGE_H - 2 * inset, "Image")
    return _finish(buf, c)
