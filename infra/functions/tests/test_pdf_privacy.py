"""check_pdf_privacy — the published protocol holds nothing beyond its visible pages.

One protocol is assembled from made-up data (every name below is invented) that is
deliberately dirty in all the ways real inputs can be:

* photos straight off a "camera": EXIF with GPS position, body serial number,
  photographer and timestamps, plus XMP, IPTC (Photoshop APP13), an ICC profile
  and a JPEG comment — and a PNG carrying text chunks and EXIF;
* an uploaded results PDF whose producer left an author/hostname/path in the
  Document Info, XMP, a bookmark, a link and a comment annotation, a form field,
  JavaScript, a file attachment, page private data and a raw camera JPEG;
* a synchro team whose roster is switched off (it must not survive as hidden text).

The file is then inspected with pikepdf (qpdf) — deliberately not with pypdf, the
library that wrote it — and checked for metadata, image metadata, incremental
updates, orphaned objects, attachments, forms, JavaScript, annotations, outlines,
alt text and object names. Never use real skaters' or officials' data here.
"""
import base64
import io
import re

import pikepdf
import pytest
from PIL import ExifTags, Image
from PIL.PngImagePlugin import PngInfo
from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Text
from pypdf.generic import (ArrayObject, DecodedStreamObject, DictionaryObject, NameObject,
                           NumberObject, TextStringObject)
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

import assemble
import image_sanitize
import pdf_sanitize
import structure as st

COMPETITION = "Testikisat 2099"
ORGANIZER = "Kuvitteellinen Luisteluseura ry"

# Printed on the pages — allowed in page text, nowhere else.
SKATERS = ["Quorra VEXLUND", "Ossian KARVALA", "Senja PUROLA",
           "VEXLUND Ilma", "KARVALA Aino"]
OFFICIALS = ["Brannoc ZILVERTH"]
VISIBLE_TOKENS = ["Quorra", "VEXLUND", "Ossian", "KARVALA", "Senja", "PUROLA",
                  "Ilma", "Aino", "Brannoc", "ZILVERTH"]

# Never visible — must not appear anywhere in the file, in any form.
PHOTOGRAPHER = "Pellervo Fotografi"
NEVER_TOKENS = [
    "Pellervo", "Fotografi",            # EXIF Artist / IPTC by-line / PNG text
    "FAKECAM", "SN-0042",               # camera make / body serial number
    "IMG_secret_original",              # original filename in ImageDescription
    "XMP-SECRET", "COM-SECRET", "ICC-SECRET", "IPTC-SECRET",
    "bzilverth", "HOST-SECRET-PC", "C:\\Users",   # username / hostname / path
    "example.invalid",                  # link target
    "Ylvie", "HIDDENSURNAME",           # JS, attachment, form, roster switched off
    "creator@",                         # the competition's creator (metadata.json)
]


# ── dirty inputs ──────────────────────────────────────────────────────────────

def _iptc_app13(byline: bytes) -> bytes:
    """A Photoshop APP13 segment holding one IPTC 2:80 By-line record."""
    record = b"\x1c\x02\x50" + len(byline).to_bytes(2, "big") + byline
    resource = (b"8BIM\x04\x04\x00\x00" + len(record).to_bytes(4, "big") + record
                + (b"\x00" if len(record) % 2 else b""))
    payload = b"Photoshop 3.0\x00" + resource
    return b"\xff\xed" + (len(payload) + 2).to_bytes(2, "big") + payload


def _camera_exif(orientation: int = 1) -> Image.Exif:
    exif = Image.Exif()
    exif[ExifTags.Base.Artist] = PHOTOGRAPHER
    exif[ExifTags.Base.Make] = "FAKECAM"
    exif[ExifTags.Base.Model] = "FAKECAM X1"
    exif[ExifTags.Base.BodySerialNumber] = "FAKECAM-SN-0042"
    exif[ExifTags.Base.DateTime] = "2099:01:02 10:11:12"
    exif[ExifTags.Base.ImageDescription] = "IMG_secret_original.jpg"
    exif[ExifTags.Base.Orientation] = orientation
    gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
    gps[ExifTags.GPS.GPSLatitudeRef] = "N"
    gps[ExifTags.GPS.GPSLatitude] = (60.0, 10.0, 30.0)
    gps[ExifTags.GPS.GPSLongitudeRef] = "E"
    gps[ExifTags.GPS.GPSLongitude] = (24.0, 56.0, 15.0)
    return exif


XMP = (b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
       b'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
       b'<rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/">'
       b'<dc:creator>XMP-SECRET Pellervo Fotografi</dc:creator>'
       b'</rdf:Description></rdf:RDF></x:xmpmeta>')


def camera_jpeg(color=(180, 40, 60), size=(320, 240), orientation=1) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(
        buf, "JPEG", exif=_camera_exif(orientation), xmp=XMP,
        comment=b"COM-SECRET Pellervo", icc_profile=b"ICC-SECRET" * 8)
    data = buf.getvalue()
    return data[:2] + _iptc_app13(b"IPTC-SECRET Pellervo Fotografi") + data[2:]


def camera_png(color=(30, 90, 160), size=(300, 200)) -> bytes:
    info = PngInfo()
    info.add_text("Author", PHOTOGRAPHER)
    info.add_text("Comment", "COM-SECRET IMG_secret_original.png")
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG", pnginfo=info, exif=_camera_exif())
    return buf.getvalue()


def dirty_results_pdf() -> bytes:
    """A results sheet as some other tool might export it: visible names, plus
    everything that must not travel with them."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setAuthor("Brannoc Zilverth (bzilverth)")
    c.setTitle("C:\\Users\\bzilverth\\Desktop\\results.pdf")
    c.setCreator("FSM on HOST-SECRET-PC")
    c.setSubject("Ylvie HIDDENSURNAME")
    c.setKeywords("bzilverth, HOST-SECRET-PC")
    y = 780
    for line in ["RESULTS", "TESTISARJA FREE SKATING", "Pl. Name Club Points",
                 "1 Quorra VEXLUND TSK 101.10", "2 Ossian KARVALA MSK 99.90",
                 "3 Senja PUROLA LSK 88.80", "Referee Brannoc ZILVERTH"]:
        c.drawString(60, y, line)
        y -= 18
    # reportlab embeds a JPEG byte-for-byte, so this keeps its camera metadata.
    c.drawImage(ImageReader(io.BytesIO(camera_jpeg((10, 120, 10)))), 60, 400, 120, 90)
    c.bookmarkPage("quorra")
    c.addOutlineEntry("Quorra VEXLUND details Ylvie", "quorra", 0)
    c.linkURL("https://example.invalid/skater/quorra-vexlund?u=bzilverth",
              (60, 300, 260, 320))
    c.acroForm.textfield(name="skater_Ylvie", tooltip="Ylvie HIDDENSURNAME",
                         value="Ylvie", x=300, y=300, width=150, height=20)
    c.showPage()
    c.save()

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(buf.getvalue())))
    writer._root_object[NameObject("/OpenAction")] = DictionaryObject({
        NameObject("/S"): NameObject("/JavaScript"),
        NameObject("/JS"): TextStringObject("app.alert('Ylvie bzilverth');")})
    writer.add_attachment("roster_Ylvie.txt", b"HIDDENSURNAME Ylvie, born 2090")
    writer.add_annotation(0, Text(rect=(300, 500, 320, 520),
                                  text="note: Ylvie HIDDENSURNAME withdrew"))
    writer.xmp_metadata = XMP.replace(b"XMP-SECRET", b"XMP-SECRET bzilverth")
    page = writer.pages[0]
    page[NameObject("/PieceInfo")] = DictionaryObject({
        NameObject("/Illustrator"): DictionaryObject({
            NameObject("/Private"): TextStringObject("HOST-SECRET-PC bzilverth")})})
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def dirty_structure():
    s = st.new_structure("comp-privacy", COMPETITION, "01.02.2099",
                         "creator@example.invalid", "2099-01-01T00:00:00Z")
    s["event"].update(title=COMPETITION, organization=ORGANIZER,
                      authorization="Testiliitto", city="Mallila", rink="Testihalli")
    s["files"] = {
        "f-podium": {"filename": "IMG_secret_original.jpg", "kind": "image"},
        "f-team": {"filename": "IMG_secret_original.png", "kind": "image"},
        "f-header": {"filename": "header_Pellervo.jpg", "kind": "image"},
        "f-results": {"filename": "results_bzilverth.pdf", "kind": "pdf"},
    }
    s["header"] = {"mode": "custom", "fileId": "f-header"}

    single = st.new_category("Testisarja", "single", 0)
    single["podium"] = {"photo": "f-podium",
                        "names": ["TSK - Quorra VEXLUND", "MSK - Ossian KARVALA",
                                  "LSK - Senja PUROLA"]}
    single["totalResultsPdf"] = "f-results"
    single["segments"].append(st.new_segment("Free Skating", 0))
    s["categories"].append(single)

    synchro = st.new_category("Testi Muodostelma", "synchro", 1)
    shown = st.new_team("TSK", "Testi Tähdet")
    shown["members"] = ["VEXLUND Ilma", "KARVALA Aino"]
    shown["photo"] = "f-team"
    hidden = st.new_team("MSK", "Piilo Kiitäjät")
    hidden["members"] = ["HIDDENSURNAME Ylvie"]
    hidden["nameMode"] = "none"
    synchro["teams"] = [shown, hidden]
    s["categories"].append(synchro)
    return s


@pytest.fixture(scope="module")
def inputs():
    return {
        "f-podium": camera_jpeg(orientation=1),
        "f-team": camera_png(),
        "f-header": camera_jpeg((240, 240, 240), size=(1240, 120)),
        "f-results": dirty_results_pdf(),
    }


@pytest.fixture(scope="module")
def protocol(inputs) -> bytes:
    return assemble.assemble_protocol(dirty_structure(), inputs.get)


@pytest.fixture(scope="module")
def pdf(protocol):
    with pikepdf.open(io.BytesIO(protocol)) as doc:
        yield doc


# ── inspection helpers (pikepdf only) ─────────────────────────────────────────

def _walk(obj, seen=None):
    """Yield every object reachable from `obj` (indirect ones once)."""
    seen = set() if seen is None else seen
    stack = [obj]
    while stack:
        o = stack.pop()
        if not isinstance(o, pikepdf.Object):
            continue                    # pikepdf returns ints/bools/reals as Python
        if o.is_indirect:
            if o.objgen in seen:
                continue
            seen.add(o.objgen)
        yield o
        if isinstance(o, (pikepdf.Dictionary, pikepdf.Stream)):
            stack.extend(o[k] for k in o.keys())
        elif isinstance(o, pikepdf.Array):
            stack.extend(o)


def _all_objects(pdf):
    """Every object in the file — via the xref, not by reachability."""
    for o in pdf.objects:
        if o.objgen != (0, 0) and not isinstance(o, type(None)):
            yield o


def _dicts(pdf):
    for o in _all_objects(pdf):
        for sub in _walk(o):
            if isinstance(sub, (pikepdf.Dictionary, pikepdf.Stream)):
                yield sub


def _strings_and_names(pdf) -> list[str]:
    """Every string value, name value and dictionary key in the file."""
    found = []
    for o in _all_objects(pdf):
        for sub in _walk(o):
            if isinstance(sub, pikepdf.String):
                found.append(str(sub))
            elif isinstance(sub, pikepdf.Name):
                found.append(str(sub))
            if isinstance(sub, (pikepdf.Dictionary, pikepdf.Stream)):
                found.extend(str(k) for k in sub.keys())
    return found


def _stream_bytes(pdf) -> list[bytes]:
    out = []
    for o in _all_objects(pdf):
        if isinstance(o, pikepdf.Stream):
            try:
                out.append(o.read_bytes())
            except Exception:
                out.append(o.read_raw_bytes())
    return out


def _jpeg_markers(data: bytes) -> list[int]:
    markers, i = [], 2
    while i + 4 <= len(data) and data[i] == 0xFF:
        m = data[i + 1]
        if m == 0xFF:
            i += 1
            continue
        markers.append(m)
        if m == 0xDA:
            break
        if 0xD0 <= m <= 0xD7 or m == 0x01:
            i += 2
            continue
        i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    return markers


def _is_dct(img) -> bool:
    f = img.get("/Filter")
    last = f[-1] if isinstance(f, pikepdf.Array) and len(f) else f
    return last == "/DCTDecode"


def _jpeg_bytes(img, doc) -> bytes:
    """The bare JPEG of a DCT image. qpdf will not decode a chain it cannot finish,
    so the outer filters (A85, Flate) are undone on a copy that lists only them."""
    f = img.get("/Filter")
    outer = list(f)[:-1] if isinstance(f, pikepdf.Array) else []
    if not outer:
        return img.read_raw_bytes()
    copy = pikepdf.Stream(doc, img.read_raw_bytes())
    copy.Filter = pikepdf.Array(outer)
    return copy.read_bytes()


def _images(pdf):
    return [o for o in _all_objects(pdf)
            if isinstance(o, pikepdf.Stream) and o.get("/Subtype") == "/Image"]


def _page_text(protocol: bytes) -> str:
    reader = PdfReader(io.BytesIO(protocol))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


# ── the fixtures really are dirty (otherwise the checks prove nothing) ────────

def test_fixture_photos_carry_camera_metadata(inputs):
    im = Image.open(io.BytesIO(inputs["f-podium"]))
    exif = im.getexif()
    assert exif[ExifTags.Base.Artist] == PHOTOGRAPHER
    assert exif.get_ifd(ExifTags.IFD.GPSInfo)[ExifTags.GPS.GPSLatitudeRef] == "N"
    assert {"xmp", "comment", "icc_profile", "photoshop"} <= set(im.info)
    assert b"FAKECAM-SN-0042" in inputs["f-podium"]
    png = Image.open(io.BytesIO(inputs["f-team"]))
    assert png.info.get("Author") == PHOTOGRAPHER and png.getexif()


def test_fixture_results_pdf_carries_the_dirt(inputs):
    with pikepdf.open(io.BytesIO(inputs["f-results"])) as src:
        assert "bzilverth" in str(src.docinfo["/Author"])
        for key in ("/Outlines", "/AcroForm", "/Names", "/Metadata", "/OpenAction"):
            assert key in src.Root
        assert len(src.pages[0].Annots) >= 3            # link, widget, comment
        assert "/PieceInfo" in src.pages[0]
        jpeg = [o for o in _images(src) if _is_dct(o)]
        assert jpeg and 0xE1 in _jpeg_markers(_jpeg_bytes(jpeg[0], src))


# ── 1. Document Info and XMP ──────────────────────────────────────────────────

def test_document_info_is_competition_and_club_only(pdf):
    info = {str(k): str(v) for k, v in pdf.docinfo.items()}
    assert info == {
        "/Title": COMPETITION,
        "/Author": ORGANIZER,
        "/Creator": pdf_sanitize.CREATOR,
        "/Producer": pdf_sanitize.CREATOR,
    }


def test_no_xmp_anywhere(pdf):
    assert "/Metadata" not in pdf.Root
    for d in _dicts(pdf):
        assert "/Metadata" not in d
        assert d.get("/Type") != "/Metadata"


# ── 2. embedded images ────────────────────────────────────────────────────────

def test_every_photo_is_embedded(pdf):
    # podium, team photo, header band (x generated pages) and the results JPEG
    jpegs = [o for o in _images(pdf) if _is_dct(o)]
    assert len(jpegs) >= 4


def test_embedded_images_carry_no_exif_xmp_iptc(pdf):
    forbidden = set(range(0xE1, 0xEE)) | {0xEF, 0xFE}     # APP1–APP13, APP15, COM
    for img in _images(pdf):
        assert "/Metadata" not in img
        if not _is_dct(img):
            continue
        raw = _jpeg_bytes(img, pdf)
        assert not forbidden & set(_jpeg_markers(raw))
        im = Image.open(io.BytesIO(raw))
        assert not im.getexif()
        assert not {"exif", "xmp", "comment", "icc_profile", "photoshop"} & set(im.info)


# ── 3–5. structure: outlines, names, annotations, forms, JS, attachments ─────

def test_no_outlines_names_forms_actions_or_tags(pdf):
    for key in ("/Outlines", "/Names", "/Dests", "/AcroForm", "/OpenAction", "/AA",
                "/StructTreeRoot", "/MarkInfo", "/Collection", "/AF"):
        assert key not in pdf.Root, key


def test_no_annotations_on_any_page(pdf):
    for page in pdf.pages:
        assert "/Annots" not in page
        assert "/AA" not in page
        assert "/PieceInfo" not in page


def test_no_javascript_attachments_or_widgets(pdf):
    for d in _dicts(pdf):
        assert "/JS" not in d and "/JavaScript" not in d
        assert d.get("/S") not in ("/JavaScript", "/Launch", "/URI", "/SubmitForm")
        assert d.get("/Type") not in ("/EmbeddedFile", "/Filespec", "/Annot")
        assert d.get("/Subtype") not in ("/Widget", "/FileAttachment", "/Link", "/Text")
        assert "/EmbeddedFiles" not in d


def test_no_alt_text_beyond_visible_content(pdf):
    for d in _dicts(pdf):
        for key in ("/Alt", "/ActualText", "/E", "/TU", "/T", "/Contents"):
            if key in d:
                value = str(d[key])
                assert not any(t.lower() in value.lower()
                               for t in VISIBLE_TOKENS + NEVER_TOKENS), (key, value)


def test_no_hidden_text_render_mode(pdf):
    for page in pdf.pages:
        for operands, op in pikepdf.parse_content_stream(page):
            if str(op) == "Tr":
                assert int(operands[0]) != 3


# ── names: only in the visible text ───────────────────────────────────────────

def test_people_appear_only_as_visible_text(pdf, protocol):
    text = _page_text(protocol)
    for name in SKATERS + OFFICIALS:          # sanity: they *are* on the pages
        assert name in text, name
    metadata = " ".join(str(v) for v in pdf.docinfo.values()).lower()
    object_names = " ".join(_strings_and_names(pdf)).lower()
    # Collected first: a failing `not in` over a large haystack makes pytest
    # try to diff it, which takes minutes.
    leaks = [(where, token) for token in VISIBLE_TOKENS
             for where, hay in (("metadata", metadata), ("objects", object_names))
             if token.lower() in hay]
    assert leaks == []


def test_never_visible_data_is_nowhere_in_the_file(pdf, protocol):
    haystacks = [protocol, *_stream_bytes(pdf),
                 " ".join(_strings_and_names(pdf)).encode("utf-8"),
                 _page_text(protocol).encode("utf-8")]
    leaks = set()
    for token in NEVER_TOKENS:
        needles = {token.encode("utf-8"), token.encode("utf-16-be"),
                   token.lower().encode("utf-8"), token.upper().encode("utf-8")}
        if any(needle in hay for hay in haystacks for needle in needles):
            leaks.add(token)
    assert sorted(leaks) == []


# ── 6. one clean save ─────────────────────────────────────────────────────────

def test_written_in_one_non_incremental_save(protocol, pdf):
    assert protocol.count(b"startxref") == 1
    assert protocol.count(b"%%EOF") == 1
    assert "/Prev" not in pdf.trailer


def test_no_unreferenced_objects(pdf):
    reachable = {o.objgen for o in _walk(pdf.trailer) if o.is_indirect}
    in_file = {o.objgen for o in _all_objects(pdf)}
    assert in_file - reachable == set()


# ── unit level ────────────────────────────────────────────────────────────────

def test_clean_jpeg_keeps_orientation_and_drops_everything_else():
    jpeg, w, h = image_sanitize.clean_jpeg(camera_jpeg(size=(40, 20), orientation=6))
    assert (w, h) == (20, 40)                 # rotated into the pixels, tag gone
    im = Image.open(io.BytesIO(jpeg))
    assert im.size == (20, 40)
    assert not im.getexif()
    assert not {"exif", "xmp", "comment", "icc_profile", "photoshop"} & set(im.info)
    assert not re.search(rb"Pellervo|FAKECAM|SECRET", jpeg)


def test_strip_jpeg_metadata_is_lossless_and_keeps_jfif():
    raw = camera_jpeg()
    stripped = pdf_sanitize.strip_jpeg_metadata(raw)
    markers = _jpeg_markers(stripped)
    assert 0xE0 in markers and not {0xE1, 0xE2, 0xED, 0xFE} & set(markers)
    assert Image.open(io.BytesIO(stripped)).tobytes() == Image.open(io.BytesIO(raw)).tobytes()
    with pytest.raises(ValueError):
        pdf_sanitize.strip_jpeg_metadata(b"not a jpeg")


# ── sanitizer edge cases (one per way metadata could slip past it) ────────────

def _with_post_scan_comment(jpeg: bytes, text: bytes) -> bytes:
    """A COM segment between the last scan and EOI, where a naive stripper that
    stops at the first SOS never looks."""
    assert jpeg.endswith(b"\xff\xd9")
    com = b"\xff\xfe" + (len(text) + 2).to_bytes(2, "big") + text
    return jpeg[:-2] + com + b"\xff\xd9"


def _base_writer() -> PdfWriter:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.drawString(60, 780, "visible line")
    c.showPage()
    c.save()
    return PdfWriter(clone_from=PdfReader(io.BytesIO(buf.getvalue())))


def _image_xobject(writer, jpeg: bytes, filter_=None):
    img = DecodedStreamObject()
    img.set_data(jpeg)
    w, h = Image.open(io.BytesIO(jpeg)).size
    img.update({NameObject("/Type"): NameObject("/XObject"),
                NameObject("/Subtype"): NameObject("/Image"),
                NameObject("/Width"): NumberObject(w), NameObject("/Height"): NumberObject(h),
                NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
                NameObject("/BitsPerComponent"): NumberObject(8),
                NameObject("/Filter"): filter_ or NameObject("/DCTDecode")})
    return writer._add_object(img)


def _page_resources(writer) -> DictionaryObject:
    page = writer.pages[0]
    if "/Resources" not in page:
        page[NameObject("/Resources")] = DictionaryObject()
    return page["/Resources"]


def _add_xobject(writer, name, ref):
    res = _page_resources(writer)
    if "/XObject" not in res:
        res[NameObject("/XObject")] = DictionaryObject()
    res["/XObject"][NameObject(name)] = ref


def _sanitized(writer) -> bytes:
    pdf_sanitize.sanitize(writer, title=COMPETITION, author=ORGANIZER)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _leaks(data: bytes, token: bytes) -> bool:
    with pikepdf.open(io.BytesIO(data)) as doc:
        return any(token in hay for hay in [data, *_stream_bytes(doc)])


def test_metadata_after_the_scan_is_stripped():
    raw = _with_post_scan_comment(camera_jpeg(), b"POSTSCAN-SECRET")
    assert b"POSTSCAN-SECRET" in raw
    stripped = pdf_sanitize.strip_jpeg_metadata(raw)
    assert b"POSTSCAN-SECRET" not in stripped
    assert Image.open(io.BytesIO(stripped)).tobytes() == Image.open(io.BytesIO(raw)).tobytes()


def test_associated_files_on_pages_and_xobjects_are_dropped():
    writer = _base_writer()

    def filespec(payload: bytes):
        ef = DecodedStreamObject()
        ef.set_data(payload)
        ef[NameObject("/Type")] = NameObject("/EmbeddedFile")
        return writer._add_object(DictionaryObject({
            NameObject("/Type"): NameObject("/Filespec"),
            NameObject("/F"): TextStringObject("af.txt"),
            NameObject("/EF"): DictionaryObject({NameObject("/F"): writer._add_object(ef)})}))

    writer.pages[0][NameObject("/AF")] = ArrayObject([filespec(b"AF-PAGE-SECRET")])
    img = _image_xobject(writer, camera_jpeg())
    img.get_object()[NameObject("/AF")] = ArrayObject([filespec(b"AF-IMAGE-SECRET")])
    _add_xobject(writer, "/Im1", img)
    data = _sanitized(writer)
    assert not _leaks(data, b"AF-PAGE-SECRET")
    assert not _leaks(data, b"AF-IMAGE-SECRET")


def test_indirect_filter_entries_are_resolved():
    writer = _base_writer()
    name_ref = writer._add_object(NameObject("/DCTDecode"))
    _add_xobject(writer, "/Im1", _image_xobject(writer, camera_jpeg(), name_ref))
    _add_xobject(writer, "/Im2", _image_xobject(
        writer, camera_jpeg((1, 2, 3)), ArrayObject([writer._add_object(NameObject("/DCTDecode"))])))
    data = _sanitized(writer)
    assert not _leaks(data, b"Pellervo")
    assert not _leaks(data, b"FAKECAM-SN-0042")


@pytest.mark.parametrize("filter_, payload", [
    (NameObject("/DCTDecode"), b"not a jpeg at all"),
    (ArrayObject([NameObject("/FlateDecode"), NameObject("/DCTDecode")]), b"not flate data"),
])
def test_a_jpeg_that_cannot_be_cleaned_fails_generation(filter_, payload):
    writer = _base_writer()
    img = DecodedStreamObject()
    img.set_data(payload)
    img.update({NameObject("/Subtype"): NameObject("/Image"), NameObject("/Filter"): filter_})
    _add_xobject(writer, "/Im1", writer._add_object(img))
    with pytest.raises(pdf_sanitize.SanitizeError):
        pdf_sanitize.sanitize(writer, title=COMPETITION, author=ORGANIZER)


def test_soft_mask_groups_are_sanitized():
    writer = _base_writer()
    xmp = DecodedStreamObject()
    xmp.set_data(b"XMP-SMASK-SECRET")
    xmp[NameObject("/Type")] = NameObject("/Metadata")
    group = DecodedStreamObject()
    group.set_data(b"q /Im1 Do Q")
    group.update({
        NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Form"),
        NameObject("/BBox"): ArrayObject([NumberObject(0), NumberObject(0),
                                          NumberObject(10), NumberObject(10)]),
        NameObject("/Metadata"): writer._add_object(xmp),
        NameObject("/Resources"): DictionaryObject({NameObject("/XObject"): DictionaryObject({
            NameObject("/Im1"): _image_xobject(writer, camera_jpeg())})})})
    gs = DictionaryObject({NameObject("/SMask"): DictionaryObject({
        NameObject("/S"): NameObject("/Luminosity"),
        NameObject("/G"): writer._add_object(group)})})
    _page_resources(writer)[NameObject("/ExtGState")] = DictionaryObject(
        {NameObject("/GS1"): writer._add_object(gs)})
    data = _sanitized(writer)
    assert not _leaks(data, b"XMP-SMASK-SECRET")
    assert not _leaks(data, b"Pellervo")


def _add_inline_image(writer, settings: bytes, jpeg: bytes):
    stream = DecodedStreamObject()
    stream.set_data(b"q 20 0 0 20 100 100 cm\nBI " + settings + b" ID " + jpeg + b"\nEI\nQ\n")
    page = writer.pages[0]
    page[NameObject("/Contents")] = ArrayObject([page.raw_get("/Contents"),
                                                 writer._add_object(stream)])


def test_inline_jpegs_are_sanitized():
    writer = _base_writer()
    jpeg = camera_jpeg(size=(8, 8))
    _add_inline_image(writer, b"/W 8 /H 8 /CS /RGB /BPC 8 /F /DCT", jpeg)
    data = _sanitized(writer)
    assert not _leaks(data, b"Pellervo")
    assert not _leaks(data, b"FAKECAM-SN-0042")
    with pikepdf.open(io.BytesIO(data)) as doc:     # still a drawable inline image
        inline = [ops for ops, op in pikepdf.parse_content_stream(doc.pages[0])
                  if str(op) == "INLINE IMAGE"]
        assert len(inline) == 1


def test_an_inline_jpeg_behind_other_filters_fails_generation():
    writer = _base_writer()
    jpeg = camera_jpeg(size=(8, 8))
    a85 = base64.a85encode(jpeg, adobe=True)[2:]      # PDF ASCII85: no "<~"
    _add_inline_image(writer, b"/W 8 /H 8 /CS /RGB /BPC 8 /F [/A85 /DCT]", a85)
    with pytest.raises(pdf_sanitize.SanitizeError):
        pdf_sanitize.sanitize(writer, title=COMPETITION, author=ORGANIZER)


def test_cyclic_resources_do_not_recurse_forever():
    writer = _base_writer()
    resources = DictionaryObject()
    pattern = DecodedStreamObject()
    pattern.set_data(b"0 0 1 1 re f")
    pattern.update({NameObject("/PatternType"): NumberObject(1),
                    NameObject("/Resources"): writer._add_object(resources)})
    pattern_ref = writer._add_object(pattern)
    resources[NameObject("/Pattern")] = DictionaryObject({NameObject("/P1"): pattern_ref})
    writer.pages[0][NameObject("/Resources")] = pattern["/Resources"].indirect_reference
    _sanitized(writer)                          # must simply finish
