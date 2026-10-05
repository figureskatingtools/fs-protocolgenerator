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

Embedded JPEG streams are cleaned losslessly, by removing their APPn/COM marker
segments (EXIF, XMP, ICC, IPTC/Photoshop, comments) while keeping APP0 (JFIF) and
APP14 (Adobe — it tells the decoder how CMYK/YCC data is transformed, so dropping
it would change the colours). Re-encoding an inserted result sheet would cost
quality for no gain: the pixels themselves are what the page shows.
"""
from pypdf import PdfWriter
from pypdf.generic import (ArrayObject, DictionaryObject, IndirectObject, NameObject,
                           NullObject, StreamObject)

CREATOR = "figureskatingtools.com Protocol Generator"

# Page entries that carry no visible content: annotations (links, comments,
# widgets/form fields, file attachments), additional actions (JavaScript),
# per-page XMP, application private data, thumbnails, article beads and the
# tagging back-reference (the structure tree itself is never copied).
_PAGE_KEYS = ("/Annots", "/AA", "/Metadata", "/PieceInfo", "/Thumb", "/B",
              "/StructParents", "/ID", "/PZ", "/SeparationInfo")

# XObject entries that carry no visible content. /Name is the obsolete image
# name some producers fill with the source filename; /OPI points at the
# original high-resolution file.
_XOBJECT_KEYS = ("/Metadata", "/PieceInfo", "/LastModified", "/Name", "/OPI",
                 "/StructParent", "/StructParents")

# Document-catalog entries that are never part of a protocol's visible pages.
_ROOT_KEYS = ("/Outlines", "/Names", "/Dests", "/AcroForm", "/OpenAction", "/AA",
              "/Metadata", "/StructTreeRoot", "/MarkInfo", "/PieceInfo", "/SpiderInfo",
              "/Threads", "/Collection", "/AF", "/URI", "/Perms", "/Legal",
              "/Requirements", "/OCProperties")

# JPEG markers kept: APP0 (JFIF) and APP14 (Adobe colour transform).
_KEEP_APP = {0xE0, 0xEE}


def strip_jpeg_metadata(data: bytes) -> bytes:
    """Remove APP1–APP13, APP15 and COM segments from a baseline/progressive JPEG
    stream, leaving the entropy-coded image data untouched. Returns `data`
    unchanged when it is not a parseable JPEG."""
    if data[:2] != b"\xff\xd8":
        return data
    out = bytearray(b"\xff\xd8")
    i, n = 2, len(data)
    while i + 4 <= n:
        if data[i] != 0xFF:
            return data                     # not where a marker should be
        marker = data[i + 1]
        if marker == 0xFF:                  # fill byte
            i += 1
            continue
        if marker == 0xDA:                  # start of scan: copy the rest verbatim
            out += data[i:]
            return bytes(out)
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
            out += data[i:i + 2]            # stand-alone marker, no length
            i += 2
            continue
        seg_len = int.from_bytes(data[i + 2:i + 4], "big")
        end = i + 2 + seg_len
        if seg_len < 2 or end > n:
            return data
        is_app = 0xE0 <= marker <= 0xEF
        if not ((is_app and marker not in _KEEP_APP) or marker == 0xFE):
            out += data[i:end]
        i = end
    return data


def _filters(obj) -> list:
    f = obj.get("/Filter")
    if f is None:
        return []
    return [str(x) for x in f] if isinstance(f, list) else [str(f)]


def _clean_xobject(xobj, seen: set) -> None:
    if id(xobj) in seen:
        return
    seen.add(id(xobj))
    for key in _XOBJECT_KEYS:
        if key in xobj:
            del xobj[key]
    subtype = xobj.get("/Subtype")
    if subtype == "/Image":
        filters = _filters(xobj)
        if filters and filters[-1] in ("/DCTDecode", "/DCT") and isinstance(xobj, StreamObject):
            # reportlab writes JPEGs as [/ASCII85Decode /DCTDecode], other tools
            # may wrap them in Flate. pypdf's DCT "decode" is a pass-through, so
            # get_data() undoes the outer filters and yields the bare JPEG.
            try:
                jpeg = xobj._data if len(filters) == 1 else xobj.get_data()
            except Exception:
                jpeg = None             # outer filter pypdf can't undo: leave as is
            cleaned = strip_jpeg_metadata(jpeg) if jpeg is not None else None
            if cleaned is not None and (len(filters) > 1 or cleaned != jpeg):
                # pypdf's EncodedStreamObject.set_data only re-encodes Flate, so
                # store the bare (still DCT-encoded) bytes directly; /Length is
                # recomputed on write.
                xobj._data = cleaned
                if hasattr(xobj, "decoded_self"):
                    xobj.decoded_self = None
                xobj[NameObject("/Filter")] = NameObject("/DCTDecode")
                parms = xobj.get("/DecodeParms")
                if isinstance(parms, list):   # keep only the DCT filter's own
                    last = parms[-1] if len(parms) == len(filters) else None
                    if last is None or isinstance(last.get_object(), NullObject):
                        del xobj["/DecodeParms"]
                    else:
                        xobj[NameObject("/DecodeParms")] = last
        smask = xobj.get("/SMask")
        if smask is not None:
            _clean_xobject(smask.get_object(), seen)
    elif subtype == "/Form":
        _clean_resources(xobj.get("/Resources"), seen)


def _clean_resources(resources, seen: set) -> None:
    if resources is None:
        return
    resources = resources.get_object()
    xobjects = resources.get("/XObject")
    if xobjects is not None:
        for ref in xobjects.get_object().values():
            _clean_xobject(ref.get_object(), seen)
    # Tiling patterns carry their own resources (and so their own images).
    patterns = resources.get("/Pattern")
    if patterns is not None:
        for ref in patterns.get_object().values():
            pat = ref.get_object()
            if isinstance(pat, DictionaryObject) and "/Resources" in pat:
                _clean_resources(pat["/Resources"], seen)


def sanitize(writer: PdfWriter, *, title: str = "", author: str = "") -> None:
    """Strip everything but the visible page content from `writer`, in place, and
    set its Document Info to Title (competition) / Author (organising club)."""
    root = writer._root_object
    for key in _ROOT_KEYS:
        if key in root:
            del root[key]

    seen: set = set()
    for page in writer.pages:
        for key in _PAGE_KEYS:
            if key in page:
                del page[key]
        _clean_resources(page.get("/Resources"), seen)

    info = {"/Creator": CREATOR, "/Producer": CREATOR}
    if title:
        info["/Title"] = title
    if author:
        info["/Author"] = author
    writer.metadata = info      # replaces pypdf's default {/Producer: pypdf}

    # Drop whatever the stripping above left unreferenced (cloned annotations,
    # their appearance streams and actions, page XMP streams …).
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
