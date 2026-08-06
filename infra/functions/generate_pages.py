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
import math

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

import branding

try:
    from PIL import Image, ImageOps
except Exception:  # pragma: no cover
    Image = None
    ImageOps = None

PAGE_W, PAGE_H = A4
MARGIN = 22 * mm

# Competition-wide page chrome (header/footer band). Every *generated* page draws
# these so the protocol's own pages share a consistent frame; uploaded result PDFs
# are inserted untouched. A custom graphic (uploaded per competition) fills the
# band edge-to-edge; without one, a generic placeholder text band is drawn.
HEADER_H = 26 * mm
FOOTER_H = 16 * mm
CONTENT_TOP = PAGE_H - HEADER_H      # top of the usable body area
CONTENT_BOTTOM = FOOTER_H            # bottom of the usable body area

INK = (0.05, 0.12, 0.20)
MUTED = (0.39, 0.47, 0.56)
LINE = (0.78, 0.83, 0.87)
GOLD = (0.69, 0.55, 0.18)


def _new_canvas():
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    return buf, c


def _draw_chrome(c, chrome):
    """Stamp the competition header/footer band on the current page.

    `chrome` carries optional custom band images plus the dynamic header text:
    {"header": bytes|None, "footer": bytes|None, "footer_enabled": bool,
     "name": str, "dates": str, "location": str}. A custom graphic fills its band
    edge-to-edge; otherwise the approved brand band (branding.py) is drawn — the
    header with the competition name + dates·location to the right of its divider.
    The footer is omitted entirely when footer_enabled is False."""
    chrome = chrome or {}

    # Header band (top of page).
    header_bytes = chrome.get("header")
    if header_bytes:
        _draw_photo(c, header_bytes, 0, PAGE_H - HEADER_H, PAGE_W, HEADER_H, "")
    else:
        branding.draw_default_header(c, name=chrome.get("name", ""),
                                     dates=chrome.get("dates", ""),
                                     location=chrome.get("location", ""))

    # Footer band (bottom of page) — only when enabled.
    if chrome.get("footer_enabled", True):
        footer_bytes = chrome.get("footer")
        if footer_bytes:
            _draw_photo(c, footer_bytes, 0, 0, PAGE_W, FOOTER_H, "")
        else:
            branding.draw_default_footer(c)


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


def _placeholder_box(c, x, y, w, h, label, label_y=None):
    """`label_y` overrides the label's baseline (kept inside the box) — the team
    page uses it to put the text at the vertical centre of the page rather than
    of the box."""
    if label_y is None:
        label_y = y + h / 2
    label_y = min(max(label_y, y + 6 * mm), y + h - 6 * mm)
    c.saveState()
    c.setFillColorRGB(0.93, 0.95, 0.97)
    c.setStrokeColorRGB(*LINE)
    c.rect(x, y, w, h, fill=1, stroke=1)
    c.setFillColorRGB(*MUTED)
    c.setFont("Helvetica", 10)
    c.drawCentredString(x + w / 2, label_y, label)
    c.restoreState()


def _draw_photo(c, img_bytes, x, y, w, h, placeholder_label, label_y=None):
    if img_bytes:
        reader, dw, dh = _fit_image_box(img_bytes, w, h)
        if reader is not None:
            c.drawImage(reader, x + (w - dw) / 2, y + (h - dh) / 2, dw, dh,
                        preserveAspectRatio=True, mask='auto')
            return
    _placeholder_box(c, x, y, w, h, placeholder_label, label_y=label_y)


# ── pages ──────────────────────────────────────────────────────────────────────

def default_cover_page(title: str, dates: str) -> bytes:
    """Plain fallback cover (used only if the branded cover can't be drawn)."""
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


def event_info_page(event: dict, chrome=None, stats=None) -> bytes:
    """Competition-information page (page 2 of a protocol). Uses the approved
    brand layout when the brand fonts are available, else a plain fallback.
    `stats` (optional) carries the competition-wide counts drawn as the stat row."""
    if branding.fonts_available():
        try:
            buf, c = _new_canvas()
            _draw_chrome(c, chrome)
            branding.draw_event_info(
                c,
                name=event.get("title", ""),
                organization=event.get("organization", ""),
                authorization=event.get("authorization", ""),
                location=event.get("city", ""),
                venue=event.get("rink", ""),
                dates=event.get("dates", ""),
                stats=stats,
            )
            return _finish(buf, c)
        except Exception as e:
            logging.warning(f"Branded event-info page failed, using plain: {e}")

    buf, c = _new_canvas()
    _draw_chrome(c, chrome)
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


def time_schedule_page(rows, chrome=None) -> bytes:
    """Modernised time-schedule page. Uses the approved brand layout (events
    grouped by day, time · category · segment pill) when the brand fonts are
    available, else the plain Date · Time · Event table below."""
    if branding.fonts_available():
        try:
            buf, c = _new_canvas()
            _draw_chrome(c, chrome)
            branding.draw_schedule(c, rows or [],
                                   new_page=lambda: _draw_chrome(c, chrome))
            return _finish(buf, c)
        except Exception as e:
            logging.warning(f"Branded time-schedule page failed, using plain: {e}")

    buf, c = _new_canvas()
    _draw_chrome(c, chrome)
    x = MARGIN
    y = CONTENT_TOP - 6 * mm
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
        if y < CONTENT_BOTTOM + 12 * mm:
            c.showPage()
            _draw_chrome(c, chrome)
            y = CONTENT_TOP - 6 * mm
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


def podium_page(category_name: str, photo_bytes, names, chrome=None) -> bytes:
    """Podium photo + the top three arranged like a real podium: 1st in the centre
    (highest), 2nd on the left (a step lower), 3rd on the right (lower still). Uses
    the approved brand layout when the brand fonts are available, else a plain
    fallback. When no photo is supplied the photo area is left as white space."""
    if branding.fonts_available():
        try:
            buf, c = _new_canvas()
            _draw_chrome(c, chrome)
            branding.draw_podium(c, category_name=category_name,
                                 photo_bytes=photo_bytes, entries=names)
            return _finish(buf, c)
        except Exception as e:
            logging.warning(f"Branded podium page failed, using plain: {e}")

    buf, c = _new_canvas()
    _draw_chrome(c, chrome)
    _centered(c, "Podium", CONTENT_TOP - 7 * mm, "Times-Bold", 18, INK)
    if category_name:
        _centered(c, category_name, CONTENT_TOP - 15 * mm, "Helvetica", 11, MUTED)

    names = (list(names) + ["", "", ""])[:3]

    # Three pedestals of decreasing height; their tops carry the names.
    avail_w = PAGE_W - 2 * MARGIN
    col_w = avail_w / 3
    ped_w = col_w - 10 * mm
    base_y = CONTENT_BOTTOM + 6 * mm
    # column index -> (place index, pedestal height); 2nd | 1st | 3rd left-to-right
    steps = [(1, 30 * mm), (0, 46 * mm), (2, 20 * mm)]

    # Podium photo fills the space above the tallest pedestal.
    tallest_top = base_y + max(h for _, h in steps)
    box_x = MARGIN
    box_y = tallest_top + 16 * mm
    box_h = (CONTENT_TOP - 22 * mm) - box_y
    # The podium photo is optional: when none was provided, leave the space empty
    # (no placeholder box) rather than drawing a "not provided" panel.
    if photo_bytes and box_h > 30 * mm:
        _draw_photo(c, photo_bytes, box_x, box_y, avail_w, box_h, "")

    from reportlab.pdfbase.pdfmetrics import stringWidth
    labels = ["1st", "2nd", "3rd"]
    for col, (place, height) in enumerate(steps):
        cx = MARGIN + col * col_w + col_w / 2
        px = cx - ped_w / 2
        # Name centred over this column's pedestal, shrunk to fit the column.
        name = names[place] or "—"
        fs = 11.5
        while fs > 7 and stringWidth(name, "Times-Roman", fs) > col_w - 4 * mm:
            fs -= 0.5
        c.setFillColorRGB(*INK)
        c.setFont("Times-Roman", fs)
        c.drawCentredString(cx, base_y + height + 6 * mm, name)
        # Pedestal block.
        c.setFillColorRGB(0.93, 0.95, 0.97)
        c.setStrokeColorRGB(*LINE)
        c.setLineWidth(1)
        c.rect(px, base_y, ped_w, height, fill=1, stroke=1)
        # Place numeral on the pedestal.
        c.setFillColorRGB(*GOLD)
        c.setFont("Helvetica-Bold", 16)
        c.drawCentredString(cx, base_y + height / 2 - 6, labels[place])
    return _finish(buf, c)


# Synchro team page: the roster is laid out *first* (a full 32-skater team must
# always fit above the footer band) and the photo box then takes whatever vertical
# space is left, within sane bounds.
ROSTER_ROW_H = 5.5 * mm
ROSTER_LABEL_H = 19 * mm     # the "SKATERS" label block between photo and names
ROSTER_PAD = 4 * mm          # breathing room under the last roster row
PHOTO_MIN_H = 60 * mm
PHOTO_MAX_H = 130 * mm


def _roster_grid(count: int):
    """(columns, rows) for a roster of `count` names: two columns normally, three
    as a safety valve for the rare oversized list."""
    cols = 3 if count > 44 else 2
    return cols, max(1, math.ceil(count / cols))


def _team_photo_box_h(photo_top: float, rows: int, cap: bool = True) -> float:
    """Height for the team photo box: everything between `photo_top` and the roster
    the page still has to fit, clamped so the box neither collapses nor dominates.
    With `cap=False` (no photo) the box takes all remaining height, so the
    placeholder area — and its page-centred label — fills the page instead of
    leaving dead space under the roster."""
    avail = photo_top - CONTENT_BOTTOM - ROSTER_LABEL_H - rows * ROSTER_ROW_H - ROSTER_PAD
    return max(PHOTO_MIN_H, min(PHOTO_MAX_H, avail) if cap else avail)


def _draw_roster(c, members, cols: int, start_y: float, font: str, size: float):
    """Draw the skater names down `cols` equal columns from `start_y`."""
    if not members:
        return
    c.setFont(font, size)
    per_col = math.ceil(len(members) / cols)
    col_w = (PAGE_W - 2 * MARGIN) / cols
    for ci in range(cols):
        cy = start_y
        cx = MARGIN + ci * col_w
        for member in members[ci * per_col:(ci + 1) * per_col]:
            if cy < CONTENT_BOTTOM:      # last-resort guard
                break
            c.drawString(cx, cy, member)
            cy -= ROSTER_ROW_H


def _draw_branded_team(c, name: str, org: str, members, photo_bytes):
    """Brand-styled team body: eyebrow, team name + gradient rule, club, the photo
    in a rounded box with the gradient hairline (matching the podium page), then the
    roster. Same deterministic sizing as the plain layout."""
    from reportlab.pdfbase.pdfmetrics import stringWidth
    box_w = PAGE_W - 2 * MARGIN

    # Eyebrow.
    y = CONTENT_TOP - 9 * mm
    to = c.beginText(MARGIN, y)
    to.setFont(branding.F_RALEWAY_SEMI, 8)
    to.setCharSpace(2.2)
    c.setFillColor(branding.SLATE)
    to.textLine("TEAM")
    to.setCharSpace(0)  # Tc is page-level text state — don't leak it
    c.drawText(to)

    # Team name, shrunk to one line.
    y -= 8 * mm
    fs = 20
    while fs > 12 and stringWidth(name, branding.F_RALEWAY_BOLD, fs) > box_w:
        fs -= 0.5
    c.setFillColor(branding.INK)
    c.setFont(branding.F_RALEWAY_BOLD, fs)
    c.drawString(MARGIN, y, name)

    # Short gradient rule under the name.
    y -= 4.5 * mm
    branding._grad_h(c, MARGIN, y, 22 * mm, 1.6 * mm)

    # Club.
    y -= 5.5 * mm
    if org:
        c.setFillColor(branding.SLATE)
        c.setFont(branding.F_RALEWAY_MED, 10.5)
        c.drawString(MARGIN, y, org)
        y -= 4 * mm

    # Photo: rounded, gradient hairline; a plain placeholder when none was given.
    photo_top = y - 2 * mm
    cols, rows = _roster_grid(len(members))
    box_h = _team_photo_box_h(photo_top, rows, cap=bool(photo_bytes))
    drawn = 0.0
    if photo_bytes:
        drawn = branding._rounded_fit_image(c, photo_bytes, MARGIN, photo_top,
                                           box_w, box_h, radius=10)
    if not drawn:
        _placeholder_box(c, MARGIN, photo_top - box_h, box_w, box_h,
                         "Team photo (not provided)", label_y=PAGE_H / 2)
        drawn = box_h

    # Roster.
    y = photo_top - drawn - 12 * mm
    to = c.beginText(MARGIN, y)
    to.setFont(branding.F_RALEWAY_SEMI, 8)
    to.setCharSpace(1.5)
    c.setFillColor(branding.MUTED)
    to.textLine("SKATERS")
    to.setCharSpace(0)  # Tc is page-level text state — don't leak it
    c.drawText(to)
    c.setFillColor(branding.INK)
    _draw_roster(c, members, cols, y - 7 * mm, branding.F_RALEWAY_MED, 9.5)


def synchro_team_page(team: dict, photo_bytes, chrome=None) -> bytes:
    """Team-presentation page: organization + team name, photo, and roster. Uses
    the brand fonts/rounded photo when the brand fonts are available, else the plain
    layout below. Both size the photo box from the roster length so a full
    32-skater team always fits above the footer band."""
    name = team.get("name") or "Team"
    org = team.get("org") or ""
    members = team.get("members") or []

    if branding.fonts_available():
        try:
            buf, c = _new_canvas()
            _draw_chrome(c, chrome)
            _draw_branded_team(c, name, org, members, photo_bytes)
            return _finish(buf, c)
        except Exception as e:
            logging.warning(f"Branded team page failed, using plain: {e}")

    buf, c = _new_canvas()
    _draw_chrome(c, chrome)

    y = CONTENT_TOP - 8 * mm
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
    cols, rows = _roster_grid(len(members))
    box_h = _team_photo_box_h(y, rows, cap=bool(photo_bytes))
    box_y = y - box_h
    _draw_photo(c, photo_bytes, MARGIN, box_y, box_w, box_h, "Team photo (not provided)",
                label_y=PAGE_H / 2)

    y = box_y - 12 * mm
    c.setFillColorRGB(*MUTED)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(MARGIN, y, "SKATERS")
    y -= 7 * mm

    c.setFillColorRGB(*INK)
    _draw_roster(c, members, cols, y, "Helvetica", 10)
    return _finish(buf, c)


def default_last_page() -> bytes:
    """Plain fallback last page (used only if the branded last page is missing)."""
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
