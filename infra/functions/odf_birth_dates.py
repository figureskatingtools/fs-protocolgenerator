"""
Dates of birth out of FS Manager participant files, before this app stores one.

KEEP IN SYNC: the two sections below ("birth dates out of participant files" and
"participant files uploaded by users", up to the "this app's entry points"
heading) are a byte-identical copy of the canonical code in the
figureskatingtools-site repo, `infra/functions/function_app.py` (the first
section is also in its `infra/hovtp/function_app.py`, pinned there by
`infra/hovtp/tests/test_parity.py`). The platform strips the files FS Manager
pushes and users upload to the shared competition files; this copy strips what
this app stores itself. Change the site first and copy the sections over
verbatim — `tests/test_birth_dates.py` pins them by SHA-256, so an edit made
only here fails the suite (the test says how to recompute the hash).

Nothing in the Protocol Generator uses a skater's date of birth, and the site's
privacy notice says none is stored. Every participant file this app keeps goes
through here first: a direct upload or a pool import (`_register_upload`, by
content, like the platform's uploads) and the roster XML `import_rosters`
archives under `rosters/` (every one of those, whatever it declares). Only the
birth-date attributes and elements go; every other byte stays as it arrived.
"""
import io
import re
import xml.etree.ElementTree as ET


# ── birth dates out of participant files ──────────────────────────────────────
# KEEP IN SYNC: everything from here to the end of the section is byte-identical
# in infra/functions/function_app.py and infra/hovtp/function_app.py (pinned by
# infra/hovtp/tests/test_parity.py). FS Manager's participant files carry each
# skater's date of birth (`BirthDate`); nothing on the site uses it, so it is cut
# out before a file is stored. Only the birth-date attributes and elements go:
# every other byte stays as it arrived, because the tools parse these files.

PARTICIPANT_DOCUMENT_TYPES = frozenset({
    "DT_PARTIC",
    "DT_PARTIC_TEAMS",
    "DT_PARTIC_UPDATE",
    "DT_PARTIC_TEAMS_UPDATE",
})

# One token of well-formed XML markup. Text between tokens is copied untouched.
# Attribute values may hold '>' but never '<', which is what keeps this exact.
_XML_MARKUP_RE = re.compile(
    rb"<!--.*?-->"
    rb"|<!\[CDATA\[.*?\]\]>"
    rb"|<\?.*?\?>"
    rb"|<!DOCTYPE(?:[^\[>]|\[.*?\])*>"
    rb"|</(?P<end>[^\s>]+)\s*>"
    rb"|<(?P<start>[^\s/>!?]+)"
    rb"(?P<attrs>(?:\s+[^\s=/>]+\s*=\s*(?:\"[^\"]*\"|'[^']*'))*)\s*(?P<empty>/?)>",
    re.S,
)
_XML_ATTRIBUTE_RE = re.compile(rb"\s+(?P<name>[^\s=/>]+)\s*=\s*(?:\"[^\"]*\"|'[^']*')")


class BirthDateError(ValueError):
    """A participant file whose birth dates could not be removed safely."""


def _is_birth_date_name(name) -> bool:
    """`BirthDate` in any case, with or without a namespace (`{uri}` or `prefix:`)."""
    if isinstance(name, bytes):
        name = name.decode("latin-1")
    if not isinstance(name, str) or name == "xmlns" or name.startswith("xmlns:"):
        return False
    return name.rsplit("}", 1)[-1].rsplit(":", 1)[-1].lower() == "birthdate"


def _has_birth_date(root) -> bool:
    return any(
        _is_birth_date_name(element.tag) or any(_is_birth_date_name(k) for k in element.attrib)
        for element in root.iter()
    )


def _birth_date_free_shape(element):
    """The parsed tree as nested lists, minus every birth date: what must survive."""
    text = element.text or ""
    children = []
    for child in element:
        if _is_birth_date_name(child.tag):
            # A removed element's tail joins the text before it.
            if children:
                children[-1][1] += child.tail or ""
            else:
                text += child.tail or ""
            continue
        children.append([_birth_date_free_shape(child), child.tail or ""])
    attrs = sorted((k, v) for k, v in element.attrib.items() if not _is_birth_date_name(k))
    return [element.tag, attrs, text, children]


def _remove_birth_date_markup(data: bytes) -> bytes:
    """Drop `BirthDate` attributes and elements from the raw bytes, nothing else."""
    out = []
    pos = 0
    skip_depth = 0      # > 0 while inside a removed <BirthDate> element
    for match in _XML_MARKUP_RE.finditer(data):
        start = match.group("start")
        if skip_depth:
            if start is not None and not match.group("empty"):
                skip_depth += 1
            elif match.group("end") is not None:
                skip_depth -= 1
            pos = match.end()
            continue

        out.append(data[pos:match.start()])
        pos = match.end()
        if start is None:
            out.append(match.group(0))
        elif _is_birth_date_name(start):
            skip_depth = 0 if match.group("empty") else 1
        else:
            attrs = match.group("attrs")
            kept = _XML_ATTRIBUTE_RE.sub(
                lambda m: b"" if _is_birth_date_name(m.group("name")) else m.group(0), attrs)
            out.append(data[match.start():match.start("attrs")] + kept
                       + data[match.end("attrs"):match.end()])
    out.append(data[pos:])
    return b"".join(out)


def strip_birth_dates(data: bytes) -> bytes:
    """
    `data` without its birth dates. A file without any comes back as the very
    same bytes. Raises BirthDateError — and the caller must then store nothing —
    when `data` is not XML, or when the result would not parse, would still hold
    a birth date, or would differ from the original in anything else.
    """
    try:
        original = ET.fromstring(data)
    except ET.ParseError:
        raise BirthDateError("not valid XML")
    if not _has_birth_date(original):
        return data

    stripped = _remove_birth_date_markup(data)
    try:
        result = ET.fromstring(stripped)
    except ET.ParseError:
        raise BirthDateError("the stripped file is not valid XML")
    if _has_birth_date(result):
        raise BirthDateError("a birth date could not be removed")
    if _birth_date_free_shape(result) != _birth_date_free_shape(original):
        raise BirthDateError("removing the birth dates changed something else")
    return stripped


# ── participant files uploaded by users ───────────────────────────────────────

def _odf_document_type(data: bytes):
    """An ODF file's declared `DocumentType` (upper-cased, "" if it declares
    none), or None when `data` does not open with an `OdfBody` root. Reads only
    as far as the root's start tag, so a PDF or an image fails on its first byte.
    """
    try:
        for _event, element in ET.iterparse(io.BytesIO(data), events=("start",)):
            if not isinstance(element.tag, str) or element.tag.rsplit("}", 1)[-1] != "OdfBody":
                return None
            for key, value in element.attrib.items():
                if key.rsplit("}", 1)[-1] == "DocumentType":
                    return (value or "").strip().upper()
            return ""
    except (ET.ParseError, ValueError):
        return None
    return None


def _strip_upload_birth_dates(body: bytes) -> bytes:
    """
    A user upload gets the same treatment as an FSM push: a participant file
    loses its birth dates before it is stored. Recognised by its content, not
    its name — an ODF file declaring a participant DocumentType, or declaring
    none at all (the tools recognise a roster by its shape then). Anything else
    is returned untouched. Raises BirthDateError (store nothing) when a
    participant file cannot be cleaned.
    """
    document_type = _odf_document_type(body)
    if document_type is None:
        return body
    if document_type and document_type not in PARTICIPANT_DOCUMENT_TYPES:
        return body
    return strip_birth_dates(body)


# ── this app's entry points ───────────────────────────────────────────────────

def strip_upload(body: bytes) -> bytes:
    """An uploaded file as the platform would store it: a participant file
    (recognised by content) without its birth dates, anything else untouched.
    Raises BirthDateError when a participant file cannot be cleaned."""
    return _strip_upload_birth_dates(body)
