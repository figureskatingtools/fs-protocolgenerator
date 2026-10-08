"""
Last pass over the merged protocol before it is written: the published file must
hold nothing beyond what its pages show.

The generated pages are clean by construction (reportlab, photos re-encoded by
image_sanitize), but the inserted PDFs — FSM/ISU result exports and any custom
cover or last page — arrive from elsewhere and may carry page-level XMP, editor
private data, link/widget/comment annotations, page actions (JavaScript), or
JPEG images still holding their camera EXIF. `sanitize(writer, title, author)`
strips all of that from every page and every image/form XObject reachable from
it, drops any document-level outline, name tree, form, action, XMP or structure
tree, replaces the Document Info with exactly Title/Author/Creator/Producer, and
finally drops the objects the stripping orphaned so the single `writer.write`
contains only what is referenced.

Embedded JPEGs — image XObjects (reached through /XObject, soft masks, tiling
patterns and Type 3 glyphs) and inline BI/ID/EI images — are cleaned losslessly, by removing their APPn/COM marker
segments (EXIF, XMP, ICC, IPTC/Photoshop, comments) while keeping APP0 (JFIF) and
APP14 (Adobe — it tells the decoder how CMYK/YCC data is transformed, so dropping
it would change the colours). Re-encoding an inserted result sheet would cost
quality for no gain: the pixels themselves are what the page shows. A JPEG that
cannot be decoded or parsed fails generation (SanitizeError) instead of being
published as it came.
"""
import re

from pypdf import PdfWriter
from pypdf.filters import FlateDecode
from pypdf.generic import (ArrayObject, ContentStream, DictionaryObject, IndirectObject,
                           NameObject, NullObject, StreamObject)

CREATOR = "figureskatingtools.com Protocol Generator"


class SanitizeError(Exception):
    """Content that cannot be proven clean (an undecodable or malformed JPEG, an
    inline JPEG behind other filters). Generation fails closed rather than
    publish it; the message never quotes document data."""


# Page entries that carry no visible content: annotations (links, comments,
# widgets/form fields, file attachments), additional actions (JavaScript),
# associated files, per-page XMP, application private data, thumbnails, article
# beads and the tagging back-reference (the structure tree itself is never copied).
_PAGE_KEYS = ("/Annots", "/AA", "/AF", "/Metadata", "/PieceInfo", "/Thumb", "/B",
              "/StructParents", "/ID", "/PZ", "/SeparationInfo")

# XObject entries that carry no visible content. /Name is the obsolete image
# name some producers fill with the source filename; /OPI points at the
# original high-resolution file; /AF attaches associated files.
_XOBJECT_KEYS = ("/Metadata", "/PieceInfo", "/LastModified", "/Name", "/OPI", "/AF",
                 "/StructParent", "/StructParents")

# Document-catalog entries that are never part of a protocol's visible pages.
_ROOT_KEYS = ("/Outlines", "/Names", "/Dests", "/AcroForm", "/OpenAction", "/AA",
              "/Metadata", "/StructTreeRoot", "/MarkInfo", "/PieceInfo", "/SpiderInfo",
              "/Threads", "/Collection", "/AF", "/URI", "/Perms", "/Legal",
              "/Requirements", "/OCProperties")

# JPEG markers kept: APP0 (JFIF) and APP14 (Adobe colour transform).
_KEEP_APP = {0xE0, 0xEE}

_DCT = ("/DCTDecode", "/DCT")


def strip_jpeg_metadata(data: bytes) -> bytes:
    """Remove every APP1–APP13, APP15 and COM segment from a JPEG — before the
    first scan, between progressive scans and after the last one — and anything
    after EOI, leaving the entropy-coded image data untouched. Raises ValueError
    on data that does not parse as a JPEG, so callers can fail closed."""
    if data[:2] != b"\xff\xd8":
        raise ValueError("not a JPEG")
    out = bytearray(b"\xff\xd8")
    i, n = 2, len(data)
    while i < n:
        if data[i] != 0xFF:
            raise ValueError("expected a marker")
        while i + 1 < n and data[i + 1] == 0xFF:    # fill bytes
            i += 1
        if i + 1 >= n:
            raise ValueError("truncated marker")
        marker = data[i + 1]
        if marker == 0xD9:                          # EOI: drop any trailing bytes
            out += b"\xff\xd9"
            return bytes(out)
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
            out += data[i:i + 2]                    # stand-alone marker, no length
            i += 2
            continue
        if i + 4 > n:
            raise ValueError("truncated segment")
        seg_len = int.from_bytes(data[i + 2:i + 4], "big")
        end = i + 2 + seg_len
        if seg_len < 2 or end > n:
            raise ValueError("bad segment length")
        is_app = 0xE0 <= marker <= 0xEF
        if not ((is_app and marker not in _KEEP_APP) or marker == 0xFE):
            out += data[i:end]
        i = end
        if marker == 0xDA:
            # Entropy-coded data runs to the next real marker: 0xFF is followed
            # by 0x00 (byte stuffing) or RSTn inside it, by anything else at a marker.
            j = i
            while True:
                j = data.find(b"\xff", j)
                if j == -1 or j + 1 >= n:           # no EOI: the rest is scan data
                    out += data[i:]
                    return bytes(out)
                nxt = data[j + 1]
                if nxt == 0x00 or 0xD0 <= nxt <= 0xD7:
                    j += 2
                    continue
                break
            out += data[i:j]
            i = j
    return bytes(out)


def _get(d, key):
    """`d[key]` with any indirect reference resolved (pypdf's .get() does not)."""
    v = d.get(key)
    return None if v is None else v.get_object()


def _filters(obj, key="/Filter") -> list:
    f = _get(obj, key)
    if f is None:
        return []
    items = f if isinstance(f, list) else [f]
    return [str(x.get_object()) for x in items]


def _set_decoded(stream, data: bytes) -> None:
    """Replace a stream's content with `data`, Flate-compressed."""
    stream._data = FlateDecode.encode(data)
    if hasattr(stream, "decoded_self"):
        stream.decoded_self = None
    stream[NameObject("/Filter")] = NameObject("/FlateDecode")
    if "/DecodeParms" in stream:
        del stream["/DecodeParms"]


_HAS_INLINE_IMAGE = re.compile(rb"(?:^|[\s\]>)])BI[\s/]")


def _clean_inline_images(stream, writer) -> None:
    """Strip metadata from inline (BI … ID … EI) JPEGs in one content stream.
    The JPEG bytes are swapped in place in the decoded stream, so everything
    around them — including the whitespace before EI — stays byte-identical."""
    if not isinstance(stream, StreamObject):
        return
    try:
        data = stream.get_data()
    except Exception:
        raise SanitizeError("content stream could not be decoded") from None
    if b"ID" not in data or not _HAS_INLINE_IMAGE.search(data):
        return
    try:
        operations = ContentStream(stream, writer).operations
    except Exception:
        raise SanitizeError("content stream with an inline image could not be parsed") from None
    new = data
    for operands, operator in operations:
        if operator != b"INLINE IMAGE":
            continue
        settings = operands["settings"]
        filters = _filters(settings, "/F") or _filters(settings, "/Filter")
        if not filters or filters[-1] not in _DCT:
            continue
        if len(filters) > 1:
            raise SanitizeError("inline JPEG behind other filters")
        old = operands["data"]
        k = old.rfind(b"\xff\xd9")
        tail = old[k + 2:] if k >= 0 and not old[k + 2:].strip() else b""
        try:
            cleaned = strip_jpeg_metadata(old) + tail
        except ValueError:
            raise SanitizeError("inline JPEG could not be parsed") from None
        if cleaned != old:
            new = new.replace(old, cleaned, 1)
    if new != data:
        _set_decoded(stream, new)


def _clean_jpeg_xobject(xobj) -> None:
    filters = _filters(xobj)
    # reportlab writes JPEGs as [/ASCII85Decode /DCTDecode], other tools may wrap
    # them in Flate. pypdf's DCT "decode" is a pass-through, so get_data() undoes
    # the outer filters and yields the bare JPEG.
    try:
        jpeg = xobj._data if len(filters) == 1 else xobj.get_data()
        cleaned = strip_jpeg_metadata(jpeg)
    except Exception:
        raise SanitizeError("embedded JPEG could not be decoded") from None
    if len(filters) == 1 and cleaned == jpeg:
        return
    # pypdf's EncodedStreamObject.set_data only re-encodes Flate, so store the
    # bare (still DCT-encoded) bytes directly; /Length is recomputed on write.
    parms = _get(xobj, "/DecodeParms")
    xobj._data = cleaned
    if hasattr(xobj, "decoded_self"):
        xobj.decoded_self = None
    xobj[NameObject("/Filter")] = NameObject("/DCTDecode")
    if isinstance(parms, list):        # keep only the DCT filter's own parameters
        last = parms[-1].get_object() if len(parms) == len(filters) else None
        if last is None or isinstance(last, NullObject):
            del xobj["/DecodeParms"]
        else:
            xobj[NameObject("/DecodeParms")] = last


def _clean_xobject(xobj, writer, seen: set) -> None:
    if not isinstance(xobj, DictionaryObject) or id(xobj) in seen:
        return
    seen.add(id(xobj))
    for key in _XOBJECT_KEYS:
        if key in xobj:
            del xobj[key]
    subtype = _get(xobj, "/Subtype")
    if subtype == "/Image":
        if isinstance(xobj, StreamObject) and (_filters(xobj) or [None])[-1] in _DCT:
            _clean_jpeg_xobject(xobj)
        for key in ("/SMask", "/Mask"):
            mask = _get(xobj, key)
            if isinstance(mask, StreamObject):
                _clean_xobject(mask, writer, seen)
    elif subtype == "/Form":
        _clean_content(xobj, writer, seen)


def _clean_content(holder, writer, seen: set) -> None:
    """A content-bearing stream (form, tiling pattern, Type 3 glyph): its inline
    images and its resources."""
    _clean_inline_images(holder, writer)
    _clean_resources(_get(holder, "/Resources"), writer, seen)


def _clean_resources(resources, writer, seen: set) -> None:
    if not isinstance(resources, DictionaryObject) or id(resources) in seen:
        return
    seen.add(id(resources))                # resource dicts are shared and can cycle
    xobjects = _get(resources, "/XObject")
    if isinstance(xobjects, DictionaryObject):
        for ref in xobjects.values():
            _clean_xobject(ref.get_object(), writer, seen)
    # Tiling patterns carry their own content and resources.
    patterns = _get(resources, "/Pattern")
    if isinstance(patterns, DictionaryObject):
        for ref in patterns.values():
            pat = ref.get_object()
            if isinstance(pat, DictionaryObject) and id(pat) not in seen:
                seen.add(id(pat))
                _clean_content(pat, writer, seen)
    # Soft masks: /ExtGState → /SMask → /G is a transparency-group form.
    states = _get(resources, "/ExtGState")
    if isinstance(states, DictionaryObject):
        for ref in states.values():
            gs = ref.get_object()
            smask = _get(gs, "/SMask") if isinstance(gs, DictionaryObject) else None
            if isinstance(smask, DictionaryObject):
                _clean_xobject(_get(smask, "/G"), writer, seen)
    # Type 3 fonts draw glyphs with their own content streams and resources.
    fonts = _get(resources, "/Font")
    if isinstance(fonts, DictionaryObject):
        for ref in fonts.values():
            font = ref.get_object()
            if (isinstance(font, DictionaryObject) and id(font) not in seen
                    and _get(font, "/Subtype") == "/Type3"):
                seen.add(id(font))
                procs = _get(font, "/CharProcs")
                if isinstance(procs, DictionaryObject):
                    for proc in procs.values():
                        _clean_inline_images(proc.get_object(), writer)
                _clean_resources(_get(font, "/Resources"), writer, seen)


def sanitize(writer: PdfWriter, *, title: str = "", author: str = "") -> None:
    """Strip everything but the visible page content from `writer`, in place, and
    set its Document Info to Title (competition) / Author (organising club).
    Raises SanitizeError for content that cannot be proven clean."""
    root = writer._root_object
    for key in _ROOT_KEYS:
        if key in root:
            del root[key]

    seen: set = set()
    for page in writer.pages:
        for key in _PAGE_KEYS:
            if key in page:
                del page[key]
        contents = _get(page, "/Contents")
        for stream in (contents if isinstance(contents, ArrayObject) else [contents]):
            if stream is not None:
                _clean_inline_images(stream.get_object(), writer)
        _clean_resources(_get(page, "/Resources"), writer, seen)

    info = {"/Creator": CREATOR, "/Producer": CREATOR}
    if title:
        info["/Title"] = title
    if author:
        info["/Author"] = author
    writer.metadata = info      # replaces pypdf's default {/Producer: pypdf}

    # Drop whatever the stripping above left unreferenced (cloned annotations,
    # their appearance streams and actions, page XMP streams …).
    collect_garbage(writer)


def dedupe(writer: PdfWriter, max_passes: int = 8) -> None:
    """Merge byte-identical objects (the header/footer band every generated page
    brings along) into one. pypdf's `compress_identical_objects` makes a single
    pass, so an image whose /SMask copies only became one object in that pass
    still differs from its twins by reference; repeat until nothing merges."""
    def live():
        return sum(1 for o in writer._objects if o is not None)
    before = live()
    for _ in range(max_passes):
        writer.compress_identical_objects(remove_duplicates=True, remove_unreferenced=True)
        after = live()
        if after == before:
            break
        before = after
    collect_garbage(writer)


def collect_garbage(writer: PdfWriter) -> int:
    """Free every object not reachable from the trailer (/Root, /Info), so the
    written file holds only what the document references. pypdf's own
    `remove_unreferenced` only drops objects nothing points at, which leaves a
    detached cluster that points at itself — a widget annotation and its
    appearance stream, say — in the file. Returns the number of objects freed."""
    reachable: set[int] = set()
    stack = [writer._root_object]
    if writer._info is not None:
        stack.append(writer._info)
    while stack:
        obj = stack.pop()
        if isinstance(obj, IndirectObject):
            if obj.pdf is not writer or obj.idnum in reachable:
                continue
            reachable.add(obj.idnum)
            obj = obj.get_object()
        if isinstance(obj, DictionaryObject):      # streams included
            stack.extend(obj.values())
        elif isinstance(obj, ArrayObject):
            stack.extend(obj)
    for ref in (writer._root_object.indirect_reference,
                getattr(writer._info, "indirect_reference", None)):
        if ref is not None:
            reachable.add(ref.idnum)

    freed = 0
    for i, obj in enumerate(writer._objects):
        if obj is not None and (i + 1) not in reachable:
            writer._objects[i] = None
            freed += 1
    return freed
