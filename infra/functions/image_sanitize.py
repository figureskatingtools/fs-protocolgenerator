"""
Metadata-free re-encoding of every photo that ends up in a protocol.

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

Without Pillow nothing is embedded: callers get None and draw their placeholder,
because the only alternative would be handing the raw upload to reportlab, which
embeds a JPEG byte-for-byte.
"""
import io

try:
    from PIL import Image, ImageOps
except Exception:  # pragma: no cover - PIL ships in requirements.txt
    Image = None
    ImageOps = None

JPEG_QUALITY = 88


def clean_jpeg(raw: bytes, max_edge: int | None = None, quality: int = JPEG_QUALITY):
    """Decode `raw`, apply its EXIF orientation, optionally downscale so the long
    edge is at most `max_edge`, and return `(jpeg_bytes, width, height)` holding
    nothing but the pixels. Returns None when Pillow is unavailable; raises on an
    undecodable image (PIL's decompression-bomb guard stays on)."""
    if Image is None:
        return None
    im = Image.open(io.BytesIO(raw))
    try:
        im = ImageOps.exif_transpose(im)
    except Exception:
        pass
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    if max_edge:
        im.thumbnail((max_edge, max_edge))
    # Pixels only: a fresh image carries no info dict (no exif/xmp/icc/comment/dpi).
    clean = Image.frombytes(im.mode, im.size, im.tobytes())
    out = io.BytesIO()
    clean.save(out, format="JPEG", quality=quality)
    return out.getvalue(), clean.width, clean.height
