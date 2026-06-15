"""
Approved Figureskatingtools branding for the generated protocol pages.

The designer delivered the cover and last page as print-ready HTML and the header/
footer as transparent PNG bands (see the `final/` brand kit: SPEC.md). Azure
Functions can't run a headless browser at generation time, so:

  * the fully-static **last page** is the designer's HTML pre-rendered once to
    `assets/last_page.pdf` (exact, fonts embedded) and inserted as-is;
  * the **cover** carries dynamic text (competition name, dates, location,
    organizer) that reflows by title length, so it is reproduced here in reportlab
    from `cover.html` — same fonts (Outfit/Manrope, bundled in `fonts/`), colours
    and brand gradient;
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
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None

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
F_OUTFIT = "Outfit"
F_OUTFIT_MED = "Outfit-Medium"
F_OUTFIT_SEMI = "Outfit-SemiBold"
F_OUTFIT_BOLD = "Outfit-Bold"
F_MANROPE_MED = "Manrope-Medium"
F_MANROPE_SEMI = "Manrope-SemiBold"

_FONTS_READY = False


def _register_fonts():
    global _FONTS_READY
    if _FONTS_READY:
        return
    for name, filename in (
        (F_OUTFIT, "Outfit-Regular.ttf"),
        (F_OUTFIT_MED, "Outfit-Medium.ttf"),
        (F_OUTFIT_SEMI, "Outfit-SemiBold.ttf"),
        (F_OUTFIT_BOLD, "Outfit-Bold.ttf"),
        (F_MANROPE_MED, "Manrope-Medium.ttf"),
        (F_MANROPE_SEMI, "Manrope-SemiBold.ttf"),
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
    return F_OUTFIT_BOLD in pdfmetrics.getRegisteredFontNames()


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
    # Baseline ≈ top + ascent; ~0.92·size matches Outfit/Manrope against the HTML.
    to = c.beginText(x, PAGE_H - top - size * 0.92)
    to.setFont(font, size)
    if char_space:
        to.setCharSpace(char_space)
    to.textLine(text)
    c.drawText(to)


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

    # Dynamic name lines (Outfit Bold 65px, max-width 606px, balanced).
    name_size = 65 * PX
    name_ls = -1 * PX
    name_lh = 65 * PX * 1.12
    lines = _layout_lines(name, F_OUTFIT_BOLD, name_size, 606 * PX, name_ls)
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
          F_MANROPE_SEMI, 21 * PX, SLATE, char_space=6.3 * PX)
    top += eyebrow_h + name_gap

    # Competition name.
    for line in lines:
        _text(c, pad_x, top, line, F_OUTFIT_BOLD, name_size, INK, char_space=name_ls)
        top += name_lh

    # Gradient rule.
    top += rule_top_gap
    _grad_h(c, pad_x, PAGE_H - top - rule_h, 112 * PX, rule_h)
    top += rule_h + rule_bot_gap

    # Dates / location.
    if dates:
        _text(c, pad_x, top, dates, F_OUTFIT_MED, 38 * PX, INK)
    top += dates_h + venue_gap
    if location:
        _text(c, pad_x, top, location, F_MANROPE_MED, 25 * PX, SLATE)

    # Organizer pinned above a hairline divider near the foot.
    meta_top = PAGE_H - pad_bottom - meta_h
    c.setStrokeColor(HAIR)
    c.setLineWidth(1)
    c.line(pad_x, PAGE_H - meta_top, PAGE_W - pad_x, PAGE_H - meta_top)
    if organizer:
        _text(c, pad_x, meta_top + 1 + 23 * PX, organizer,
              F_MANROPE_MED, 18 * PX, MUTED, char_space=0.4 * PX)


def cover_page(*, name, dates="", location="", organizer="") -> bytes:
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    draw_cover(c, name=name, dates=dates, location=location, organizer=organizer)
    c.showPage()
    c.save()
    buf.seek(0)
    return buf.getvalue()


# ── last page (bundled, pre-rendered) ─────────────────────────────────────────

def last_page_pdf() -> bytes:
    """The designer's last page, pre-rendered to a self-contained A4 PDF."""
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
        c.setFont(F_OUTFIT_SEMI, 11)
        c.drawString(text_x, centre_y + (1.5 if meta else -3), name)
    if meta:
        c.setFillColor(SLATE)
        c.setFont(F_MANROPE_MED, 7.5)
        c.drawString(text_x, centre_y - 10, meta)


def draw_default_footer(c):
    """Draw the brand footer band (fully baked slogan + wordmark)."""
    try:
        img = ImageReader(_asset("footer.png"))
        c.drawImage(img, 0, 0, PAGE_W, FOOTER_H, preserveAspectRatio=False, mask='auto')
    except Exception as e:
        logging.warning(f"footer band image failed: {e}")
