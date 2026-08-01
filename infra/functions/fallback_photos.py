"""
Fallback team pictures imported from one optional ZIP of accreditation photos.

Teams often have no competition (kiss'n'cry) photo, but the organizer's
accreditation system does hold a picture of every team. Those come as one ZIP
whose convention is:

    SM-seniorit/Helsinki-Finettes_HTK.jpeg     <- folder ≈ category, file = team_club
    Juniorit/Team-Unique_TUK.jpg
    Some-Team_Club.jpeg                        <- ZIP-root files are fine too

So: the **folder** name loosely matches a category name (only a tie-break hint —
organizers name folders freely), the **file stem** is `Team-Name_Club-Name`, with
`-`/`_` standing in for spaces. Matching is therefore done on the *team name*
(required) with the club and the folder acting as bonuses; anything that stays
ambiguous is reported back and lands in the Uploads tray for manual placement.

This module is pure (no Azure dependencies): `parse_zip` turns ZIP bytes into
re-encoded JPEGs, `match_team` maps one image onto a structure's team. The
caller (`function_app.upload_fallback_photos`) does the blob/registry work.
"""
import io
import logging
import unicodedata
import zipfile
from pathlib import PurePosixPath

try:
    from PIL import Image, ImageOps, UnidentifiedImageError
except Exception:  # pragma: no cover
    Image = None
    ImageOps = None
    UnidentifiedImageError = Exception

# Guards: a ZIP is operator-supplied, so cap the work it can ask for. The
# per-entry limit is checked against the *claimed* uncompressed size, before any
# decompression happens (zip bomb).
MAX_ENTRIES = 300
MAX_ENTRY_BYTES = 30 * 1024 * 1024
# Re-encode every picture down to a print-sufficient size (the protocol places
# them at ~180mm wide at most) so the blob container doesn't fill with 8MP JPEGs.
TARGET_LONG_EDGE = 2000
JPEG_QUALITY = 88

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")


# ── name normalisation ────────────────────────────────────────────────────────

def normalize(s: str) -> str:
    """Fold a team/club/category name to a comparable form: case-insensitive,
    `-`/`_` treated as spaces, punctuation dropped, whitespace collapsed, and
    diacritic-insensitive.

    Comparisons must be diacritic-insensitive because the two sides disagree:
    accreditation ZIP filenames are ASCII-folded ("Creme-de-Ments",
    "Helsinki-JaaLeidit") while the XML team names carry the diacritics ("Crème de
    Ments", "Helsinki JääLeidit"). NFKD splits each accented letter into a base
    letter plus a combining mark, and the letter/number filter below then drops the
    marks along with the punctuation and symbols."""
    out = []
    for ch in unicodedata.normalize("NFKD", s or ""):
        if ch in "-_":
            out.append(" ")
        elif ch.isspace():
            out.append(" ")
        elif unicodedata.category(ch)[0] in ("L", "N"):
            out.append(ch)
        # everything else (punctuation, symbols, marks) is dropped
    return " ".join("".join(out).split()).casefold()


def _despaced(s: str) -> str:
    return s.replace(" ", "")


def split_stem(stem: str):
    """`"Helsinki-Finettes_HTK"` -> `("Helsinki-Finettes", "HTK")`. A stem without
    an underscore is all team name."""
    if "_" in stem:
        team_part, club_part = stem.split("_", 1)
        return team_part, club_part
    return stem, ""


# ── ZIP parsing ───────────────────────────────────────────────────────────────

def _is_junk(name: str, filename: str) -> bool:
    """macOS/Windows archive noise that must never surface as a rejected file."""
    if "__MACOSX" in name.upper():
        return True
    return filename.startswith("._") or filename.startswith(".")


def parse_zip(zip_bytes):
    """Read an accreditation-photo ZIP into `(images, rejected)`.

    Each image is `{folder, filename, team_norm, club_norm, jpeg}` — `folder` is
    the first path component (None for ZIP-root files) and `jpeg` the re-encoded
    bytes. `rejected` collects the names of entries that looked like pictures but
    could not be used (unsupported extension, oversized, corrupt); archive junk
    (directories, `__MACOSX`, dotfiles) is skipped silently. A bad ZIP raises
    `zipfile.BadZipFile` for the caller to turn into a 400."""
    images, rejected = [], []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for info in zf.infolist():
            if len(images) >= MAX_ENTRIES:
                break
            name = info.filename.replace("\\", "/")
            if info.is_dir() or name.endswith("/"):
                continue
            path = PurePosixPath(name)
            filename = path.name
            if not filename or _is_junk(name, filename):
                continue
            if not filename.lower().endswith(IMAGE_EXTS):
                rejected.append(filename)
                continue
            if info.file_size > MAX_ENTRY_BYTES:
                rejected.append(filename)
                continue

            # Only the basename and the first folder component are ever used —
            # nothing is written to disk, so a zip-slip path is harmless.
            parts = path.parts
            folder = parts[0] if len(parts) > 1 else None

            try:
                jpeg = _reencode(zf.read(info))
            except Exception as e:   # UnidentifiedImage / DecompressionBomb / OSError
                logging.warning(f"Fallback photo {filename} rejected: {e}")
                rejected.append(filename)
                continue

            team_part, club_part = split_stem(path.stem)
            images.append({
                "folder": folder,
                "filename": filename,
                "team_norm": normalize(team_part),
                "club_norm": normalize(club_part),
                "jpeg": jpeg,
            })
    return images, rejected


def _reencode(raw: bytes) -> bytes:
    """EXIF-rotate, downscale to TARGET_LONG_EDGE and re-encode as JPEG (mirrors
    branding._load_reader). PIL's decompression-bomb cap stays on deliberately:
    an oversized picture is rejected rather than decoded."""
    if Image is None:
        raise RuntimeError("PIL is not available")
    im = Image.open(io.BytesIO(raw))
    try:
        im = ImageOps.exif_transpose(im)
    except Exception:
        pass
    if im.mode != "RGB":
        im = im.convert("RGB")
    im.thumbnail((TARGET_LONG_EDGE, TARGET_LONG_EDGE))
    out = io.BytesIO()
    im.save(out, format="JPEG", quality=JPEG_QUALITY)
    return out.getvalue()


# ── matching an image onto a team ─────────────────────────────────────────────

def _same_name(a: str, b: str) -> bool:
    """Equal normalised names, also tolerating a differing word split
    ("Team Unique" vs "TeamUnique")."""
    return bool(a) and (a == b or _despaced(a) == _despaced(b))


def match_team(structure: dict, team_norm: str, club_norm: str, folder_norm: str):
    """Find the (category, team) an image belongs to, or `(None, None)`.

    The **team name must match** (required gate). The club and the folder-vs-
    category-name hint only *rank* the survivors: a club is often an abbreviation
    that the structure spells out (or vice versa), so it can never be required.
    A tie between two distinct teams is left unresolved on purpose — the caller
    reports the file and leaves it in the tray."""
    scored = []
    for cat in structure.get("categories", []):
        cat_norm = normalize(cat.get("name", ""))
        for team in cat.get("teams", []):
            name_norm = normalize(team.get("name", ""))
            if not name_norm or not _same_name(name_norm, team_norm):
                continue
            score = 0
            if club_norm and _same_name(club_norm, normalize(team.get("org", ""))):
                score += 2
            if folder_norm and cat_norm and (folder_norm in cat_norm or cat_norm in folder_norm):
                score += 1
            scored.append((score, cat, team))

    if not scored:
        return None, None
    best = max(s for s, _, _ in scored)
    top = [(cat, team) for s, cat, team in scored if s == best]
    if len(top) > 1 and any(t is not top[0][1] for _, t in top):
        return None, None          # ambiguous → tray + reported
    return top[0]
