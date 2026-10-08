"""Photos are embedded at the size they are printed, repeated images once, and
HEIC/HEIF uploads (the iPhone default) are accepted.

A protocol used to carry every uploaded photo at its original pixel size — a
phone's 12–48 MP — although the team page prints it in a ~180 mm box, and every
generated page carried its own copy of the header/footer band. All inputs here
are synthetic; never use real skaters' photos.
"""
import hashlib
import io
import zipfile

import azure.functions as func
import pikepdf
import pytest
from PIL import ExifTags, Image
from reportlab.lib.units import mm

import assemble
import fallback_photos
import function_app as fa
import generate_pages
import image_sanitize
import structure as st

EMAIL = "organizer@example.com"


def photo(size=(4000, 3000), fmt="JPEG", orientation=None, **save):
    """A noisy picture (so the encoder cannot cheat on flat colour)."""
    im = Image.effect_noise(size, 60).convert("RGB")
    exif = Image.Exif()
    exif[ExifTags.Base.Artist] = "SECRET-PHOTOGRAPHER"
    if orientation:
        exif[ExifTags.Base.Orientation] = orientation
    buf = io.BytesIO()
    im.save(buf, fmt, exif=exif, **save)
    return buf.getvalue()


def at_dpi(pt):
    return pt / 72 * image_sanitize.PRINT_DPI


# ── sizing ────────────────────────────────────────────────────────────────────

def test_a_photo_is_downscaled_to_its_box_at_print_dpi():
    box = (180 * mm, 110 * mm)
    jpeg, w, h = image_sanitize.clean_jpeg(photo(), box=box)
    # 4:3 into 180×110 mm is height-bound: 110 mm tall, 146.7 mm wide.
    assert abs(h - at_dpi(box[1])) <= 2
    assert abs(w - at_dpi(box[1]) * 4 / 3) <= 2
    assert Image.open(io.BytesIO(jpeg)).size == (w, h)


def test_a_small_photo_is_never_upscaled():
    _, w, h = image_sanitize.clean_jpeg(photo((300, 200)), box=(180 * mm, 110 * mm))
    assert (w, h) == (300, 200)


def test_box_sizing_uses_the_upright_orientation():
    # Stored landscape, EXIF says "rotate 90°": upright it is a 3000×4000 portrait,
    # so a portrait box is width-bound, not height-bound.
    box = (100 * mm, 200 * mm)
    _, w, h = image_sanitize.clean_jpeg(photo(orientation=6), box=box)
    assert h > w
    assert abs(w - at_dpi(box[0])) <= 2


def test_max_edge_still_caps_without_a_box():
    _, w, h = image_sanitize.clean_jpeg(photo(), max_edge=2000)
    assert (w, h) == (2000, 1500)


def test_fit_image_box_embeds_the_box_size():
    reader, dw, dh = generate_pages._fit_image_box(photo(), 180 * mm, 110 * mm)
    iw, ih = reader.getSize()
    assert abs(ih - at_dpi(110 * mm)) <= 2
    assert dh == pytest.approx(110 * mm) and dw <= 180 * mm


# ── the assembled protocol ────────────────────────────────────────────────────

def synchro_structure(n_teams=6):
    s = st.new_structure("comp-size", "Kokotesti 2099", "01.02.2099",
                         "creator@example.invalid", "2099-01-01T00:00:00Z")
    s["files"] = {"f-header": {"filename": "header.png", "kind": "image"}}
    s["header"] = {"mode": "custom", "fileId": "f-header"}
    cat = st.new_category("Testi Muodostelma", "synchro", 0)
    for i in range(n_teams):
        team = st.new_team("TSK", f"Testi Tiimi {i}")
        team["members"] = [f"SUKUNIMI{i} Etu{j}" for j in range(16)]
        team["photo"] = f"f-team-{i}"
        s["files"][f"f-team-{i}"] = {"filename": f"team{i}.jpg", "kind": "image"}
        cat["teams"].append(team)
    s["categories"].append(cat)
    return s


@pytest.fixture(scope="module")
def protocol():
    files = {"f-header": photo((2480, 300), fmt="PNG")}
    for i in range(6):
        files[f"f-team-{i}"] = photo()
    return assemble.assemble_protocol(synchro_structure(), files.get)


def _image_xobjects(doc):
    seen = {}
    for obj in doc.objects:
        if isinstance(obj, pikepdf.Stream) and obj.get("/Subtype") == "/Image" \
                and obj.get("/ImageMask") is None and obj.get("/ColorSpace") != "/DeviceGray":
            seen[obj.objgen] = obj
    return list(seen.values())


def _is_jpeg(xobj):
    f = xobj.get("/Filter")
    return "/DCTDecode" in ([str(n) for n in f] if isinstance(f, pikepdf.Array) else [str(f)])


def _content_key(xobj):
    """An image's bytes plus its soft mask's: two watermarks differing only in
    opacity are different images."""
    smask = xobj.get("/SMask")
    return hashlib.sha256(xobj.read_raw_bytes()
                          + (smask.read_raw_bytes() if smask is not None else b"")).hexdigest()


def test_repeated_images_are_stored_once(protocol):
    with pikepdf.open(io.BytesIO(protocol)) as doc:
        images = _image_xobjects(doc)
        digests = [_content_key(x) for x in images]
        assert len(digests) == len(set(digests))
        # One custom header band (a JPEG) and one brand footer band (a PNG),
        # each shared by every generated page.
        headers = [x for x in images if _is_jpeg(x) and int(x.Width) / int(x.Height) > 6]
        footers = [x for x in images if not _is_jpeg(x) and int(x.Width) / int(x.Height) > 6]
        assert len(footers) == 1
        assert len(headers) == 1
        assert len(doc.pages) > 6


def test_team_photos_are_embedded_at_print_size(protocol):
    with pikepdf.open(io.BytesIO(protocol)) as doc:
        photos = [x for x in _image_xobjects(doc)
                  if _is_jpeg(x) and int(x.Width) / int(x.Height) < 2]
        assert len(photos) == 6
        # A team photo box is at most a page wide (≈1650 px at 200 dpi).
        assert all(int(x.Width) <= at_dpi(210 * mm) for x in photos)
    # Six full-size 12 MP noise photos would be well above 20 MB.
    assert len(protocol) < 8 * 1024 * 1024


# ── HEIC / HEIF ───────────────────────────────────────────────────────────────

def heic(size=(400, 300)):
    return photo(size, fmt="HEIF", quality=80)


def upload(filename, body):
    req = func.HttpRequest(
        "POST", "/api/upload_file",
        headers={"x-forwarded-user-email": EMAIL},
        params={"competition": "abc12345", "filename": filename}, body=body)
    return fa.upload_file._function.get_user_function()(req)


def test_heic_decodes_and_drops_its_metadata():
    raw = heic()
    assert b"SECRET-PHOTOGRAPHER" in raw
    jpeg, w, h = image_sanitize.clean_jpeg(raw)
    assert (w, h) == (400, 300)
    assert b"SECRET-PHOTOGRAPHER" not in jpeg
    assert not Image.open(io.BytesIO(jpeg)).getexif()


def test_a_heic_upload_is_stored_as_jpeg(storage):
    structure = storage.competition()
    resp = upload("IMG_0042.HEIC", heic())
    assert resp.status_code == 200
    (meta,) = structure["files"].values()
    assert meta["filename"] == "IMG_0042.jpg" and meta["kind"] == "image"
    assert meta["blob"].endswith("_IMG_0042.jpg")
    stored = storage.container.blobs[meta["blob"]]
    im = Image.open(io.BytesIO(stored))
    assert im.format == "JPEG" and im.size == (400, 300)
    assert b"SECRET-PHOTOGRAPHER" not in stored
    assert meta["size"] == len(stored)


def test_an_unreadable_heic_is_rejected(storage):
    structure = storage.competition()
    resp = upload("broken.heif", b"not a heic at all")
    assert resp.status_code == 400
    assert structure["files"] == {}
    assert storage.container.blobs == {}


def test_a_heic_in_the_fallback_zip_is_usable():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Juniorit/Blue-Herons_BHK.heic", heic())
    images, rejected = fallback_photos.parse_zip(buf.getvalue())
    assert rejected == [] and len(images) == 1
    assert Image.open(io.BytesIO(images[0]["jpeg"])).format == "JPEG"
    assert images[0]["team_norm"] == "blue herons"
