"""
Metadata-free, right-sized re-encoding of every photo that ends up in a protocol.

Uploaded pictures (podium photos, team photos, fallback accreditation pictures,
custom cover/header/footer graphics) arrive with whatever the camera or editor
wrote into them: EXIF (GPS position, device serial number, photographer,
timestamps), XMP, IPTC, ICC profiles and JPEG comments. None of it is visible on
the page, so none of it may reach the published PDF.

Removing tags is not enough — Pillow, for one, copies a source JPEG's COM comment
into a re-saved file by default — so `clean_jpeg` rebuilds the picture from its
decoded pixels alone (`Image.frombytes` starts with an empty `info`) and encodes
that with no metadata arguments. The EXIF orientation is applied to the pixels
first, so dropping the tag never turns a photo sideways.

Pictures are also sized to where they are printed: given the box a photo is drawn
into (`box=(w_pt, h_pt)`), it is downscaled to `PRINT_DPI` at its drawn size —
never upscaled — so a 48 MP phone photo in a 180 mm box embeds as ~1400 px
rather than 8000. Large JPEGs are scaled down already while decoding
(`Image.draft`), which keeps the worker's memory flat.

HEIC/HEIF (the iPhone default) decodes through pillow-heif's opener, registered
on import; without it those files simply fail to open like any unreadable image.

Without Pillow nothing is embedded: callers get None and draw their placeholder,
because the only alternative would be handing the raw upload to reportlab, which
embeds a JPEG byte-for-byte.
"""
import io
import math

try:
    from PIL import Image, ImageOps
except Exception:  # pragma: no cover - PIL ships in requirements.txt
    Image = None
    ImageOps = None

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover - pillow-heif ships in requirements.txt
    pillow_heif = None

JPEG_QUALITY = 85
# Resolution a photo is embedded at, relative to its printed size. 200 dpi is
# sharp in print for photographs; each doubling roughly quadruples the bytes.
PRINT_DPI = 200

HEIF_EXTS = (".heic", ".heif")
# EXIF orientations that swap width and height (90°/270° rotations).
_TRANSPOSING = (5, 6, 7, 8)


def _upright_size(im) -> tuple[int, int]:
    """The image's size once its EXIF orientation is applied, read from the
    header without decoding the pixels."""
    w, h = im.size
    try:
        if im.getexif().get(0x0112) in _TRANSPOSING:
            return h, w
    except Exception:
        pass
    return w, h


def box_long_edge(size: tuple[int, int], box: tuple[float, float], dpi: int = PRINT_DPI) -> int:
    """Long edge in pixels a `size` picture needs when fitted (aspect kept) into
    a `box` of PDF points at `dpi` — never more than the picture already has."""
    w, h = size
    scale = min(box[0] / w, box[1] / h)          # points per source pixel
    return min(max(w, h), max(1, math.ceil(max(w, h) * scale / 72 * dpi)))


def clean_jpeg(raw: bytes, max_edge: int | None = None, quality: int = JPEG_QUALITY,
               box: tuple[float, float] | None = None, dpi: int = PRINT_DPI):
    """Decode `raw`, apply its EXIF orientation, optionally downscale so the long
    edge is at most `max_edge` (and, with `box`, at most what that box needs at
    `dpi`), and return `(jpeg_bytes, width, height)` holding nothing but the
    pixels. Returns None when Pillow is unavailable; raises on an undecodable
    image (PIL's decompression-bomb guard stays on)."""
    if Image is None:
        return None
    im = Image.open(io.BytesIO(raw))
    upright = _upright_size(im)
    if box and box[0] > 0 and box[1] > 0:
        fit = box_long_edge(upright, box, dpi)
        max_edge = min(max_edge, fit) if max_edge else fit
    if max_edge and max_edge < max(upright):
        # Let the JPEG decoder scale by 1/2…1/8 straight away; draft keeps the
        # result at least the requested size, thumbnail() does the rest.
        f = max_edge / max(upright)
        im.draft(None, (math.ceil(im.width * f), math.ceil(im.height * f)))
    try:
        im = ImageOps.exif_transpose(im)
    except Exception:
        pass
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    if max_edge:
        im.thumbnail((max_edge, max_edge), resample=Image.Resampling.LANCZOS)
    # Pixels only: a fresh image carries no info dict (no exif/xmp/icc/comment/dpi).
    clean = Image.frombytes(im.mode, im.size, im.tobytes())
    out = io.BytesIO()
    clean.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue(), clean.width, clean.height
