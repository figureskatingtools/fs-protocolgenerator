"""
Approved Figureskatingtools branding for the generated protocol pages.

The designer delivered the cover and last page as print-ready HTML and the header/
footer as transparent PNG bands (see the `final/` brand kit: SPEC.md). Azure
Functions can't run a headless browser at generation time, so:

  * the **cover** carries dynamic text (competition name, dates, location,
    organizer) that reflows by title length, so it is reproduced here in reportlab
    from `cover.html` — the brand type is set in Raleway (bundled in `fonts/`), colours
    and brand gradient;
  * the static **last page** is likewise reproduced in reportlab (geometry lifted
    from the designer's pre-render, which remains at `assets/last_page.pdf` as the
    fonts-missing fallback);
  * the **header band** (`assets/header.png`) is drawn edge-to-edge with the
    competition name + dates·location printed to the right of its divider, and the
    **footer band** (`assets/footer.png`) is drawn as-is (fully baked slogan).

px→pt uses the CSS reference 96 dpi (1px = 0.75pt); the HTML page is A4.
"""
import io
import os
import logging

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.lib.colors import HexColor
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase.pdfmetrics import stringWidth

try:
    from PIL import Image, ImageOps
except Exception:  # pragma: no cover
    Image = None
    ImageOps = None

PAGE_W, PAGE_H = A4
PX = 72.0 / 96.0  # CSS px -> pt

_HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(_HERE, "assets")
FONTS = os.path.join(_HERE, "fonts")

# Brand palette (SPEC §4).
CYAN = HexColor("#15A9E3")
BLUE = HexColor("#2C77C8")
VIOLET = HexColor("#5746A4")
PURPLE = HexColor("#7C3A9B")
INK = HexColor("#16263B")
SLATE = HexColor("#5C6B7D")
MUTED = HexColor("#94A1B0")
HAIR = HexColor("#E4E7EC")

GRAD_COLORS = [CYAN, BLUE, VIOLET, PURPLE]
GRAD_POS = [0.0, 0.30, 0.62, 1.0]

# Font family names registered with reportlab (one per weight).
F_RALEWAY = "Raleway"
F_RALEWAY_MED = "Raleway-Medium"
F_RALEWAY_SEMI = "Raleway-SemiBold"
F_RALEWAY_BOLD = "Raleway-Bold"

_FONTS_READY = False


def _register_fonts():
    global _FONTS_READY
    if _FONTS_READY:
        return
    for name, filename in (
        (F_RALEWAY, "Raleway-Regular.ttf"),
        (F_RALEWAY_MED, "Raleway-Medium.ttf"),
        (F_RALEWAY_SEMI, "Raleway-SemiBold.ttf"),
        (F_RALEWAY_BOLD, "Raleway-Bold.ttf"),
    ):
        try:
            pdfmetrics.registerFont(TTFont(name, os.path.join(FONTS, filename)))
        except Exception as e:
            logging.warning(f"Could not register font {name}: {e}")
    _FONTS_READY = True


def fonts_available() -> bool:
    """True when the bundled brand fonts registered — callers fall back to the
    plain placeholder pages otherwise."""
    _register_fonts()
    return F_RALEWAY_BOLD in pdfmetrics.getRegisteredFontNames()


# ── low-level helpers ────────────────────────────────────────────────────────

def _asset(name: str) -> str:
    return os.path.join(ASSETS, name)


def _grad_h(c, x, y, w, h, colors=GRAD_COLORS, positions=GRAD_POS):
    """Fill a rectangle with the brand gradient, left → right."""
    c.saveState()
    p = c.beginPath()
    p.rect(x, y, w, h)
    c.clipPath(p, stroke=0, fill=0)
    c.linearGradient(x, y, x + w, y, colors, positions, extend=True)
    c.restoreState()


def _grad_circle(c, cx, cy, r):
    c.saveState()
    p = c.beginPath()
    p.circle(cx, cy, r)
    c.clipPath(p, stroke=0, fill=0)
    c.linearGradient(cx - r, cy, cx + r, cy, [CYAN, PURPLE], [0.0, 1.0], extend=True)
    c.restoreState()


def _text(c, x, top, text, font, size, color, char_space=0.0):
    """Draw a single line; `top` is the line-box top (from page top, CSS-style)."""
    c.setFillColor(color)
    # Baseline ≈ top + ascent; ~0.92·size matches Raleway against the HTML.
    to = c.beginText(x, PAGE_H - top - size * 0.92)
    to.setFont(font, size)
    if char_space:
        to.setCharSpace(char_space)
    to.textLine(text)
    if char_space:
        to.setCharSpace(0)  # Tc is page-level text state — don't leak it
    c.drawText(to)


def _text_center(c, cx, top, text, font, size, color, char_space=0.0):
    """Draw a single line centred on `cx` (letter-spacing aware)."""
    w = _str_w(text, font, size, char_space)
    _text(c, cx - w / 2, top, text, font, size, color, char_space)


def _str_w(text, font, size, char_space=0.0):
    return stringWidth(text, font, size) + max(0, len(text) - 1) * char_space


def _layout_lines(text, font, size, max_w, char_space=0.0):
    """Greedy-wrap to `max_w`, then balance the lines (CSS text-wrap: balance)."""
    words = (text or "").split()
    if not words:
        return []

    def greedy(maxw):
        lines, cur = [], ""
        for word in words:
            trial = (cur + " " + word).strip()
            if cur and _str_w(trial, font, size, char_space) > maxw:
                lines.append(cur)
                cur = word
            else:
                cur = trial
        if cur:
            lines.append(cur)
        return lines

    base = greedy(max_w)
    n = len(base)
    if n <= 1:
        return base
    lo, hi, best = 0.0, max_w, base
    for _ in range(24):
        mid = (lo + hi) / 2
        g = greedy(mid)
        if len(g) <= n:
            best, hi = g, mid
        else:
            lo = mid
    return best


def _faint_skate(opacity=0.06):
    if Image is None:
        return ImageReader(_asset("skate_mark.png"))
    im = Image.open(_asset("skate_mark.png")).convert("RGBA")
    alpha = im.split()[3].point(lambda v: int(v * opacity))
    im.putalpha(alpha)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    buf.seek(0)
    return ImageReader(buf)


# ── cover page ───────────────────────────────────────────────────────────────

def draw_cover(c, *, name: str, dates: str = "", location: str = "", organizer: str = ""):
    """Render the branded cover onto canvas `c` (one A4 page; caller showPage()s).

    Reproduces cover.html: gradient hairline, skate lockup top-left, faint
    watermark, 'OFFICIAL PROTOCOL' eyebrow, dynamic competition name (balanced
    wrap), gradient rule, dates, location, and the organizer pinned above a
    hairline divider near the foot. The eyebrow→…→location group is vertically
    centred between the lockup and the organizer block, so it reflows with the
    title length."""
    _register_fonts()
    name = (name or "").strip() or "Competition"

    pad_top, pad_bottom, pad_x = 87 * PX, 69 * PX, 83 * PX
    content_h = PAGE_H - pad_top - pad_bottom

    # Faint watermark (bottom-right, partly off-page) — drawn first (behind).
    try:
        wm_w = 707 * PX
        wm = _faint_skate(0.06)
        iw, ih = wm.getSize()
        wm_h = wm_w * ih / iw
        wm_right = PAGE_W + 155 * PX           # right: -155px
        c.drawImage(wm, wm_right - wm_w, 72 * PX, wm_w, wm_h,
                    preserveAspectRatio=True, mask='auto')
    except Exception as e:
        logging.warning(f"cover watermark failed: {e}")

    # Top gradient hairline (11px).
    _grad_h(c, 0, PAGE_H - 11 * PX, PAGE_W, 11 * PX)

    # Skate lockup, top-left (height 101px).
    mark_h = 101 * PX
    try:
        mk = ImageReader(_asset("skate_mark.png"))
        iw, ih = mk.getSize()
        mark_w = mark_h * iw / ih
        c.drawImage(mk, pad_x, PAGE_H - pad_top - mark_h, mark_w, mark_h,
                    preserveAspectRatio=True, mask='auto')
    except Exception as e:
        logging.warning(f"cover skate mark failed: {e}")

    # Dynamic name lines (Raleway Bold 65px, max-width 606px, balanced).
    name_size = 65 * PX
    name_ls = -1 * PX
    name_lh = 65 * PX * 1.12
    lines = _layout_lines(name, F_RALEWAY_BOLD, name_size, 606 * PX, name_ls)
    n = max(1, len(lines))

    # Block heights (px → pt), mirroring cover.html spacing.
    eyebrow_h = 21 * PX * 1.2
    h1_h = n * name_lh
    rule_top_gap, rule_h, rule_bot_gap = 40 * PX, 9 * PX, 32 * PX
    dates_h = 38 * PX * 1.2
    venue_gap, venue_h = 13 * PX, 25 * PX * 1.2
    name_gap = 25 * PX
    group_h = (eyebrow_h + name_gap + h1_h + rule_top_gap + rule_h + rule_bot_gap
               + dates_h + venue_gap + venue_h)

    meta_h = 1 + 23 * PX + 18 * PX * 1.2     # divider + padding-top + org line

    spacer = max(20 * PX, (content_h - mark_h - group_h - meta_h) / 2)

    # Walk the group from its top (CSS from-page-top coordinates).
    top = pad_top + mark_h + spacer

    # Eyebrow: gradient dot + letter-spaced label.
    dot_r = 7 * PX
    dot_cx = pad_x + dot_r
    dot_cy = PAGE_H - (top + eyebrow_h / 2)
    _grad_circle(c, dot_cx, dot_cy, dot_r)
    _text(c, pad_x + 14 * PX + 16 * PX, top - 2 * PX, "OFFICIAL PROTOCOL",
          F_RALEWAY_SEMI, 21 * PX, SLATE, char_space=6.3 * PX)
    top += eyebrow_h + name_gap

    # Competition name.
    for line in lines:
        _text(c, pad_x, top, line, F_RALEWAY_BOLD, name_size, INK, char_space=name_ls)
        top += name_lh

    # Gradient rule.
    top += rule_top_gap
    _grad_h(c, pad_x, PAGE_H - top - rule_h, 112 * PX, rule_h)
    top += rule_h + rule_bot_gap

    # Dates / location.
    if dates:
        _text(c, pad_x, top, dates, F_RALEWAY_MED, 38 * PX, INK)
    top += dates_h + venue_gap
    if location:
        _text(c, pad_x, top, location, F_RALEWAY_MED, 25 * PX, SLATE)

    # Organizer pinned above a hairline divider near the foot.
    meta_top = PAGE_H - pad_bottom - meta_h
    c.setStrokeColor(HAIR)
    c.setLineWidth(1)
    c.line(pad_x, PAGE_H - meta_top, PAGE_W - pad_x, PAGE_H - meta_top)
    if organizer:
        _text(c, pad_x, meta_top + 1 + 23 * PX, organizer,
              F_RALEWAY_MED, 18 * PX, MUTED, char_space=0.4 * PX)


def cover_page(*, name, dates="", location="", organizer="") -> bytes:
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    draw_cover(c, name=name, dates=dates, location=location, organizer=organizer)
    c.showPage()
    c.save()
    buf.seek(0)
    return buf.getvalue()


# ── last page ─────────────────────────────────────────────────────────────────

def draw_last_page(c):
    """Render the branded 'Thank you' last page onto canvas `c`.

    Reproduces the designer's last page (geometry lifted from the original
    pre-render): gradient hairlines top and bottom, skate lockup top-centre,
    'THANK YOU' eyebrow, the two-line 'Created with Figureskatingtools.com'
    title, a gradient pill rule, the slogan, and a faint watermark running off
    the bottom edge."""
    _register_fonts()
    cx = PAGE_W / 2

    # Faint watermark (bottom-centre, partly off-page) — drawn first (behind).
    try:
        wm_w = 540 * PX
        wm = _faint_skate(0.06)
        iw, ih = wm.getSize()
        wm_h = wm_w * ih / iw
        c.drawImage(wm, cx - wm_w / 2, PAGE_H - 843 * PX - wm_h, wm_w, wm_h,
                    preserveAspectRatio=True, mask='auto')
    except Exception as e:
        logging.warning(f"last page watermark failed: {e}")

    # Gradient hairlines (11px), top and bottom.
    _grad_h(c, 0, PAGE_H - 11 * PX, PAGE_W, 11 * PX)
    _grad_h(c, 0, 0, PAGE_W, 11 * PX)

    # Skate lockup, top-centre (height 166px, top 162px).
    try:
        mk = ImageReader(_asset("skate_mark.png"))
        iw, ih = mk.getSize()
        mark_h = 166 * PX
        mark_w = mark_h * iw / ih
        c.drawImage(mk, cx - mark_w / 2, PAGE_H - 162 * PX - mark_h, mark_w, mark_h,
                    preserveAspectRatio=True, mask='auto')
    except Exception as e:
        logging.warning(f"last page skate mark failed: {e}")

    _text_center(c, cx, 540 * PX, "THANK YOU",
                 F_RALEWAY_SEMI, 21 * PX, SLATE, char_space=6.3 * PX)

    _text_center(c, cx, 588 * PX, "Created with", F_RALEWAY_BOLD, 54 * PX, INK)
    _text_center(c, cx, 649 * PX, "Figureskatingtools.com", F_RALEWAY_BOLD, 54 * PX, INK)

    # Gradient pill rule, centred (99×9px).
    rule_w, rule_h = 99 * PX, 9 * PX
    rule_y = PAGE_H - 752 * PX - rule_h
    c.saveState()
    p = c.beginPath()
    p.roundRect(cx - rule_w / 2, rule_y, rule_w, rule_h, rule_h / 2)
    c.clipPath(p, stroke=0, fill=0)
    c.linearGradient(cx - rule_w / 2, rule_y, cx + rule_w / 2, rule_y,
                     GRAD_COLORS, GRAD_POS, extend=True)
    c.restoreState()

    _text_center(c, cx, 800 * PX, "Supporting Figure Skating Community",
                 F_RALEWAY, 33 * PX, SLATE)


def last_page_pdf() -> bytes:
    """The branded last page as a one-page A4 PDF — rendered live (Raleway) when
    the brand fonts are available, else the designer's original pre-render."""
    if fonts_available():
        from reportlab.pdfgen import canvas
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=A4)
        draw_last_page(c)
        c.showPage()
        c.save()
        return buf.getvalue()
    with open(_asset("last_page.pdf"), "rb") as f:
        return f.read()


# ── header / footer bands ─────────────────────────────────────────────────────

HEADER_H = 26 * mm
FOOTER_H = 16 * mm


def draw_default_header(c, *, name: str = "", dates: str = "", location: str = ""):
    """Draw the brand header band edge-to-edge and print the competition name +
    dates·location to the right of its divider, vertically centred in the band."""
    _register_fonts()
    try:
        img = ImageReader(_asset("header.png"))
        c.drawImage(img, 0, PAGE_H - HEADER_H, PAGE_W, HEADER_H,
                    preserveAspectRatio=False, mask='auto')
    except Exception as e:
        logging.warning(f"header band image failed: {e}")

    # Header.png is 2480×307 px; map design px into the band.
    sx = PAGE_W / 2480.0
    sy = HEADER_H / 307.0
    text_x = 470 * sx                         # right of the divider
    centre_y = PAGE_H - 153 * sy              # band vertical centre (design y=153)

    name = (name or "").strip()
    meta = " · ".join([p for p in (dates, location) if p]).strip()
    if name:
        c.setFillColor(INK)
        c.setFont(F_RALEWAY_SEMI, 11)
        c.drawString(text_x, centre_y + (1.5 if meta else -3), name)
    if meta:
        c.setFillColor(SLATE)
        c.setFont(F_RALEWAY_MED, 7.5)
        c.drawString(text_x, centre_y - 10, meta)


def draw_default_footer(c):
    """Draw the brand footer band (fully baked slogan + wordmark)."""
    try:
        img = ImageReader(_asset("footer.png"))
        c.drawImage(img, 0, 0, PAGE_W, FOOTER_H, preserveAspectRatio=False, mask='auto')
    except Exception as e:
        logging.warning(f"footer band image failed: {e}")


# ── competition-information page (page 2) ──────────────────────────────────────
#
# Reproduces competitionInformation.html: the eyebrow, big competition name with a
# short gradient rule, then a list of label/value detail rows. The designer's art
# is a 440×622 box scaled to fill A4, so design px map to points via SX/SY. The
# running header/footer bands are stamped by the caller (generate_pages._draw_chrome);
# this only draws the body between them.

def draw_event_info(c, *, name="", organization="", authorization="",
                    location="", venue="", dates="", stats=None):
    """Render the branded competition-information body onto canvas `c`.

    Rows are emitted only when their value is present: Organiser, Authorised by
    (the "with the authorization of" party), Held in (City, Country), Venue and
    Dates. When `stats` (categories/units/performances, computed from the uploaded
    result PDFs) carries non-zero counts, a Categories · Competition Units ·
    Performances stat row is drawn beneath."""
    _register_fonts()
    SX = PAGE_W / 440.0
    SY = PAGE_H / 622.0
    pad_l = 40 * SX

    # Faint skate watermark, bottom-right (opacity .05), partly off-page.
    try:
        wm = _faint_skate(0.05)
        iw, ih = wm.getSize()
        wm_w = 230 * SX
        wm_h = wm_w * ih / iw
        c.drawImage(wm, (440 + 50) * SX - wm_w, PAGE_H - 578 * SY, wm_w, wm_h,
                    preserveAspectRatio=True, mask='auto')
    except Exception as e:
        logging.warning(f"event-info watermark failed: {e}")

    # Eyebrow.
    _text(c, pad_l, 96 * SY, "COMPETITION INFORMATION",
          F_RALEWAY_SEMI, 11 * SX, SLATE, char_space=3 * SX)

    # Detail rows (built first — the title size adapts to the space they need).
    rows = []
    if organization:
        rows.append(("ORGANISER", organization))
    if authorization:
        rows.append(("AUTHORISED BY", authorization))
    if location:
        rows.append(("HELD IN", location))
    if venue:
        rows.append(("VENUE", venue))
    if dates:
        rows.append(("DATES", dates))

    stats = stats or {}
    has_stats = bool(stats.get("units") or stats.get("performances"))

    # Vertical budget (design units): the body must clear the footer band. A long
    # competition name wraps to several lines, so shrink it until everything below
    # fits; past the floor, compress the row heights/gaps instead.
    body_bottom = 584
    below_h = 18 + 5 + 24 + len(rows) * 45 + ((26 + 45) if has_stats else 0)
    name_px = 31.0
    name_ls = -0.6 * SX
    while True:
        lines = _layout_lines(name or "—", F_RALEWAY_BOLD, name_px * SX, 360 * SX, name_ls)
        title_h = len(lines) * name_px * 1.1
        if 121 + title_h + below_h <= body_bottom or name_px <= 20:
            break
        name_px -= 1.0
    squeeze = min(1.0, (body_bottom - 121 - title_h) / below_h)

    # Competition name (balanced wrap within the content width).
    top = 121 * SY
    for line in lines:
        _text(c, pad_l, top, line, F_RALEWAY_BOLD, name_px * SX, INK, char_space=name_ls)
        top += name_px * 1.1 * SY

    # Gradient rule.
    top += 18 * squeeze * SY
    _grad_h(c, pad_l, PAGE_H - top - 5 * SY, 58 * SX, 5 * SY)
    top += (5 + 24 * squeeze) * SY

    value_x = pad_l + 120 * SX
    value_w = (PAGE_W - pad_l) - value_x
    row_h = 45 * squeeze * SY
    for label, value in rows:
        _text(c, pad_l, top + 16 * squeeze * SY, label, F_RALEWAY_SEMI, 10 * SX, MUTED,
              char_space=1.2 * SX)
        # Shrink an over-long value to fit its column rather than overflow.
        vfs = 16 * SX
        while vfs > 11 * SX and _str_w(value, F_RALEWAY_MED, vfs) > value_w:
            vfs -= 0.5
        _text(c, value_x, top + 13 * squeeze * SY, value, F_RALEWAY_MED, vfs, INK)
        line_y = PAGE_H - (top + row_h)
        c.setStrokeColor(HexColor("#EEF0F3"))
        c.setLineWidth(1)
        c.line(pad_l, line_y, PAGE_W - pad_l, line_y)
        top += row_h

    # Stat row (Categories · Competition Units · Performances), only when the
    # counts read from the result PDFs are meaningful.
    if has_stats:
        top += 26 * squeeze * SY
        col_w = (PAGE_W - 2 * pad_l) / 3.0
        cells = (
            ("CATEGORIES", stats.get("categories", 0)),
            ("COMPETITION UNITS", stats.get("units", 0)),
            ("PERFORMANCES", stats.get("performances", 0)),
        )
        for i, (label, value) in enumerate(cells):
            cell_x = pad_l + i * col_w
            _text(c, cell_x, top, str(value), F_RALEWAY_BOLD, 30 * SX, INK)
            _text(c, cell_x, top + 30 * SY + 7 * SY, label,
                  F_RALEWAY_SEMI, 7.5 * SX, MUTED, char_space=1.1 * SX)


# ── time-schedule page ─────────────────────────────────────────────────────────
#
# Reproduces schedule.html: a "Time Schedule" title + gradient rule, then the events
# grouped by day (a gradient dot + day label + "N EVENTS" count), each event a row
# of time · category · segment pill separated by hairlines. Fonts are kept a touch
# smaller than the HTML so more rows fit. The running header/footer bands are
# stamped by the caller; pagination is handled here via the `new_page` callback.

def _schedule_pill_style(segment: str):
    """Pill colours for a segment label: Short Program in brand blue, else grey."""
    s = (segment or "").lower()
    if "short" in s:
        return HexColor("#EAF1FB"), BLUE
    return HexColor("#F3F5F8"), SLATE


def draw_schedule(c, rows, *, new_page=None):
    """Render the branded time-schedule body onto canvas `c`.

    `rows` are the flat schedule entries (date_display / start_time /
    category_name / segment_name). `new_page()` is invoked after the canvas page
    break to re-stamp the running chrome; the active day header repeats atop each
    continued page so context is kept."""
    _register_fonts()
    SX = PAGE_W / 440.0
    SY = PAGE_H / 622.0
    pad_x = 34.0
    content_r = (440.0 - 34.0) * SX          # right content edge (pt)
    event_x = (pad_x + 52.0) * SX            # time col (40) + gap (12)
    TOP_PX, BOTTOM_PX, ROW_H = 70.0, 590.0, 15.0

    rows = list(rows or [])
    day_counts = {}
    for r in rows:
        d = r.get("date_display") or ""
        day_counts[d] = day_counts.get(d, 0) + 1

    top = TOP_PX

    # Title + gradient rule.
    _text(c, pad_x * SX, top * SY, "Time Schedule", F_RALEWAY_BOLD, 18 * SX,
          INK, char_space=-0.4 * SX)
    top += 27
    _grad_h(c, pad_x * SX, PAGE_H - (top + 4) * SY, 50 * SX, 4 * SY)
    top += 4 + 14

    def emit_day(label, count):
        nonlocal top
        # Gradient dot + day label + right-aligned "N EVENTS".
        _grad_circle(c, (pad_x + 3.5) * SX, PAGE_H - (top + 7) * SY, 3.5 * SX)
        _text(c, (pad_x + 15) * SX, top * SY, label, F_RALEWAY_SEMI, 11 * SX, INK)
        if count:
            tag = f"{count} EVENT" + ("S" if count != 1 else "")
            tw = _str_w(tag, F_RALEWAY_SEMI, 7 * SX, 0.6 * SX)
            _text(c, content_r - tw, (top + 1) * SY, tag, F_RALEWAY_SEMI,
                  7 * SX, MUTED, char_space=0.6 * SX)
        top += 18

    def page_break(active_day):
        nonlocal top
        c.showPage()
        if new_page:
            new_page()
        top = TOP_PX
        if active_day:
            emit_day(active_day, day_counts.get(active_day, 0))

    cur_day = None
    for row in rows:
        day = row.get("date_display") or ""
        new_day = day and day != cur_day

        # Space needed: a day header (if the day changes) plus one row.
        needed = (18 if new_day else 0) + ROW_H
        if top + needed > BOTTOM_PX:
            page_break(cur_day if not new_day else None)

        if new_day:
            top += 10
            emit_day(day, day_counts.get(day, 0))
            cur_day = day

        # Row hairline (top border).
        c.setStrokeColor(HexColor("#F0F2F5"))
        c.setLineWidth(1)
        ly = PAGE_H - top * SY
        c.line(pad_x * SX, ly, content_r, ly)

        text_top = top + 2.5
        time = row.get("start_time") or ""
        _text(c, pad_x * SX, text_top * SY, time, F_RALEWAY_SEMI, 9.5 * SX, INK)

        # Segment pill, right-aligned; sized to its label.
        seg = row.get("segment_name") or ""
        avail_r = content_r
        if seg:
            seg_fs = 7 * SX
            # stringWidth under-measures the TTF advance by a few percent, so the
            # pill must be widened to the real rendered width or the label spills
            # past the rounded background.
            seg_tw = _str_w(seg, F_RALEWAY_SEMI, seg_fs) * 1.10
            pad_h = 8 * SX
            pw = seg_tw + 2 * pad_h
            ph = 13 * SY
            px = content_r - pw
            py = PAGE_H - (top + 1 + 13) * SY
            bg, fg = _schedule_pill_style(seg)
            c.setFillColor(bg)
            c.roundRect(px, py, pw, ph, ph / 2.0, stroke=0, fill=1)
            # Centre on the *rendered* width (pad_h either side); _text_center
            # would use the un-fudged width and sit the label left of centre.
            _text(c, px + pad_h, (top + 4) * SY, seg, F_RALEWAY_SEMI, seg_fs, fg)
            avail_r = px - 10 * SX

        # Category name, shrunk then ellipsised to clear the pill. stringWidth
        # under-measures these TTFs by a few percent, so a fudge factor keeps a
        # real gap rather than a hairline (borderline names overlapped without it).
        label = row.get("category_name") or row.get("event_name") or ""
        avail = avail_r - event_x

        def _fits(s, fs):
            return _str_w(s, F_RALEWAY_MED, fs) * 1.10 <= avail

        ev_fs = 9.5 * SX
        while ev_fs > 8 * SX and not _fits(label, ev_fs):
            ev_fs -= 0.5
        if not _fits(label, ev_fs):
            while label and not _fits(label + "…", ev_fs):
                label = label[:-1]
            label = (label + "…") if label else ""
        _text(c, event_x, text_top * SY, label, F_RALEWAY_MED, ev_fs, INK)

        top += ROW_H


def schedule_page(rows) -> bytes:
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    draw_schedule(c, rows)
    c.showPage()
    c.save()
    buf.seek(0)
    return buf.getvalue()


# ── podium page ────────────────────────────────────────────────────────────────
#
# Reproduces podium.html: a centred eyebrow + category name, an optional podium
# photo, then the top three on a 2-1-3 rostrum (1st centre/highest on the brand
# gradient, 2nd left, 3rd right). Our data carries each medallist as a single
# "<club> - <name>" string and no scores, so the score pill is omitted. When no
# photo is supplied the photo area is left as plain white space (per design).

def _load_reader(img_bytes):
    """ImageReader for a photo plus its pixel size (EXIF-rotated, RGB)."""
    try:
        if Image is not None:
            im = Image.open(io.BytesIO(img_bytes))
            try:
                im = ImageOps.exif_transpose(im)
            except Exception:
                pass
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            iw, ih = im.size
            out = io.BytesIO()
            im.save(out, format="JPEG", quality=88)
            out.seek(0)
            return ImageReader(out), iw, ih
        reader = ImageReader(io.BytesIO(img_bytes))
        iw, ih = reader.getSize()
        return reader, iw, ih
    except Exception as e:
        logging.warning(f"podium photo load failed: {e}")
        return None, 0, 0


def _rounded_fit_image(c, img_bytes, x, top_y, w, max_h, radius):
    """Draw the *whole* photo (no crop) into a rounded box that hugs the scaled
    image: fit to the available width, capped at `max_h`, centred horizontally.
    `top_y` is the top edge of the area; returns the actual box height drawn."""
    reader, iw, ih = _load_reader(img_bytes)
    if reader is None or iw == 0 or ih == 0:
        return 0.0
    scale = min(w / iw, max_h / ih)
    dw, dh = iw * scale, ih * scale
    bx = x + (w - dw) / 2.0
    by = top_y - dh
    c.saveState()
    p = c.beginPath()
    p.roundRect(bx, by, dw, dh, radius)
    c.clipPath(p, stroke=0, fill=0)
    c.drawImage(reader, bx, by, dw, dh, mask='auto')
    # Gradient hairline along the top; _grad_h clips to the bar and intersects the
    # rounded clip already in effect, so the corners stay rounded.
    bar_h = 5 * (dh / 134.0)
    _grad_h(c, bx, by + dh - bar_h, dw, bar_h)
    c.restoreState()
    return dh


def _split_medallist(entry):
    """Split a "<club> - <name>" results string into (name, club)."""
    entry = (entry or "").strip()
    if " - " in entry:
        club, name = entry.split(" - ", 1)
        return name.strip(), club.strip()
    return entry, ""


def draw_podium(c, *, category_name="", photo_bytes=None, entries=None):
    """Render the branded podium body onto canvas `c`."""
    _register_fonts()
    SX = PAGE_W / 440.0
    SY = PAGE_H / 622.0
    pad = 34 * SX

    _text_center(c, PAGE_W / 2, 90 * SY, "PODIUM",
                 F_RALEWAY_SEMI, 11 * SX, SLATE, char_space=3.5 * SX)
    if category_name:
        # Shrink an over-long category name to fit the content width on one line.
        tfs = 30 * SX
        max_w = PAGE_W - 2 * pad
        while tfs > 16 * SX and _str_w(category_name, F_RALEWAY_BOLD, tfs, -0.6 * SX) > max_w:
            tfs -= 0.5
        _text_center(c, PAGE_W / 2, 111 * SY, category_name,
                     F_RALEWAY_BOLD, tfs, INK, char_space=-0.6 * SX)

    # Podium photo — drawn only when supplied; otherwise the area stays white.
    # Show the whole picture (no crop): fit to the content width, capped so the
    # box stays clear of the rostrum medallions below.
    if photo_bytes:
        box_top = 161 * SY
        _rounded_fit_image(c, photo_bytes, pad, PAGE_H - box_top,
                           PAGE_W - 2 * pad, 225 * SY, radius=14 * SX)

    # Top three: 1st centre/highest, 2nd left, 3rd right.
    ranks = (list(entries or []) + [None, None, None])[:3]
    parsed = [_split_medallist(e) for e in ranks]

    PED_BOTTOM = 580.0  # design-px (from top) of the rostrum's base line
    gap = 16
    # left→right: (rank, pedestal h, column w, circle d, name size, name font,
    #              circle colour, numeral size)
    cols = [
        (1, 60, 118, 30, 13, F_RALEWAY_SEMI, HexColor("#B8C0C9"), 15),
        (0, 86, 124, 36, 14, F_RALEWAY_BOLD, HexColor("#E3B23C"), 18),
        (2, 46, 118, 30, 13, F_RALEWAY_SEMI, HexColor("#C58A5B"), 15),
    ]
    x_cursor = (440 - (118 + 124 + 118 + 2 * gap)) / 2.0
    for rank, ped_h, col_w, circ_d, name_fs, name_font, circ_color, num_fs in cols:
        center_px = x_cursor + col_w / 2.0
        cx = center_px * SX
        name, club = parsed[rank]

        # Pedestal block (1st on the brand gradient, others a soft grey).
        ped_x = (center_px - col_w / 2.0) * SX
        ped_y = PAGE_H - PED_BOTTOM * SY
        ped_w, ped_h_pt = col_w * SX, ped_h * SY
        if rank == 0:
            c.saveState()
            p = c.beginPath()
            p.roundRect(ped_x, ped_y, ped_w, ped_h_pt, 9 * SX)
            c.clipPath(p, stroke=0, fill=0)
            c.linearGradient(ped_x, ped_y, ped_x + ped_w, ped_y + ped_h_pt,
                             GRAD_COLORS, GRAD_POS, extend=True)
            c.restoreState()
        else:
            c.setFillColor(HexColor("#E7EAEF"))
            c.roundRect(ped_x, ped_y, ped_w, ped_h_pt, 9 * SX, stroke=0, fill=1)

        # Club, then name, stacked above the pedestal.
        club_bottom_px = (PED_BOTTOM - ped_h) - 12
        if club:
            club_h_px = 9.5 * 1.3
            club_top_px = club_bottom_px - club_h_px
            _text_center(c, cx, club_top_px * SY, club, F_RALEWAY_MED, 9.5 * SX, SLATE)
            name_bottom_px = club_top_px - 3
        else:
            name_bottom_px = club_bottom_px
        name_top_px = name_bottom_px - name_fs * 1.2
        fs = name_fs * SX
        label = name or "—"
        while fs > 8 and _str_w(label, name_font, fs) > (col_w - 4) * SX:
            fs -= 0.5
        _text_center(c, cx, name_top_px * SY, label, name_font, fs, INK)

        # Rank medallion above the name.
        circ_top_px = (name_top_px - 9) - circ_d
        circ_cy = PAGE_H - (circ_top_px + circ_d / 2.0) * SY
        c.setFillColor(circ_color)
        c.circle(cx, circ_cy, circ_d / 2.0 * SX, stroke=0, fill=1)
        c.setFillColor(HexColor("#FFFFFF"))
        c.setFont(F_RALEWAY_BOLD, num_fs * SX)
        c.drawCentredString(cx, circ_cy - num_fs * SX * 0.35, str(rank + 1))

        x_cursor += col_w + gap
