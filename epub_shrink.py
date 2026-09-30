#!/usr/bin/env python3
"""epub-shrink - reduce EPUB file size by recompressing its embedded images.

Most oversized EPUBs are not big because of their text but because of their
artwork. 24-bit PNGs of photographs or painted illustrations are commonly
several megabytes each, and PNG is a poor container for continuous-tone art.
Some publishers also ship every image as RGBA even when nothing is
transparent, which roughly doubles the file size for no visible benefit.

This tool walks an EPUB, converts oversized opaque PNG artwork to JPEG,
re-encodes JPEGs that are larger than they need to be, and repacks the archive
in a spec-compliant way (the `mimetype` entry must come first and be stored
uncompressed). Text, metadata, styles and reading order are left untouched.

Requires Pillow:  python3 -m pip install --user Pillow

Examples
--------
    epub_shrink.py book.epub                    # writes book_compressed.epub
    epub_shrink.py book.epub -o small.epub --quality 65
    epub_shrink.py *.epub --dry-run             # report, write nothing
    epub_shrink.py ~/Books --in-place           # whole library, originals to Backups/
    epub-shrink book.epub                       # after ./install.sh
"""

from __future__ import annotations

import argparse
import copy
import io
import math
import os
import posixpath
import re
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ElementTree
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

try:
    from PIL import Image, ImageChops
except ImportError:  # pragma: no cover - environment guard
    sys.exit("epub-shrink requires Pillow. Install it with:\n"
             "    python3 -m pip install --user Pillow")

# A file declared as one media-type but stored as another is a hard EPUBCheck
# error, so media-type must be corrected whenever an image is converted.
MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}
IMAGE_EXTS = frozenset(MEDIA_TYPES)
XML_DOCS = (".xhtml", ".html", ".htm", ".xml", ".ncx", ".opf", ".css", ".smil")
MARKUP_DOCS = (".xhtml", ".html", ".htm", ".opf", ".ncx")

CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"

# Reader presets. Each is a complete, self-consistent set of knobs so a user
# picks a device rather than reasoning about four separate numbers.
#
#   quality        JPEG quality for PNG artwork being converted (1-95)
#   jpeg_quality   quality for existing JPEGs being re-encoded
#   min_dim        skip artwork whose smallest side is under this
#   max_dim        downscale anything larger than this on its longest side
#   min_png_bytes  leave PNGs smaller than this alone
#   min_jpeg_bytes leave JPEGs smaller than this alone
#   cover_quality  separate, higher quality for the cover so it stays sharp,
#                  because a cover is the one image a reader always sees large
#
# max_dim matters for e-ink panels: no Kindle screen has more than roughly
# 1600x1200 usable pixels, so a 3000px illustration is stored at three times
# the resolution any device can show.
PRESETS: dict[str, dict[str, int]] = {
    "kindle-basic": {
        "quality": 72, "jpeg_quality": 70, "min_dim": 300, "max_dim": 1200,
        "min_png_bytes": 24 * 1024, "min_jpeg_bytes": 12 * 1024,
        "cover_quality": 82,
    },
    "kindle-paperwhite": {
        "quality": 80, "jpeg_quality": 78, "min_dim": 400, "max_dim": 1600,
        "min_png_bytes": 40 * 1024, "min_jpeg_bytes": 16 * 1024,
        "cover_quality": 88,
    },
    "kindle-oasis": {
        "quality": 85, "jpeg_quality": 82, "min_dim": 500, "max_dim": 2100,
        "min_png_bytes": 60 * 1024, "min_jpeg_bytes": 20 * 1024,
        "cover_quality": 92,
    },
    "tablet": {
        "quality": 88, "jpeg_quality": 86, "min_dim": 600, "max_dim": 2400,
        "min_png_bytes": 80 * 1024, "min_jpeg_bytes": 24 * 1024,
        "cover_quality": 94,
    },
    "smallest": {
        "quality": 55, "jpeg_quality": 52, "min_dim": 200, "max_dim": 1000,
        "min_png_bytes": 12 * 1024, "min_jpeg_bytes": 8 * 1024,
        "cover_quality": 70,
    },
    "custom": {},
}
DEFAULT_PRESET = "kindle-paperwhite"
# The ratio between the two quality settings is preserved when a size target
# searches, so "quality 60" always means the same trade-off as in the slider.
_JPEG_RATIO = 0.96


def apply_preset(opts: argparse.Namespace, name: str) -> None:
    """Copy a preset's numbers onto opts. 'custom' leaves opts untouched."""
    preset = PRESETS.get(name)
    if not preset:
        return
    for key, value in preset.items():
        setattr(opts, key, value)
    opts.preset = name


def parse_size(text: str) -> int:
    """Parse '5MB', '700k', '1.5 GB' or a plain byte count into bytes."""
    m = re.fullmatch(r"\s*([\d.]+)\s*([kmgt]?i?b?)\s*", text, re.IGNORECASE)
    if not m:
        raise argparse.ArgumentTypeError(f"not a size: {text!r}")
    value = float(m.group(1))
    suffix = m.group(2).lower().rstrip("b").rstrip("i")
    scale = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3,
             "t": 1024 ** 4}.get(suffix)
    if scale is None:
        raise argparse.ArgumentTypeError(f"unknown size unit in {text!r}")
    return int(value * scale)


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def human(n: float) -> str:
    v = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(v) < 1024 or unit == "GB":
            return f"{v:.1f} {unit}"
        v /= 1024
    return f"{v:.1f} GB"


def pct_saved(new: float, old: float) -> str:
    if not old:
        return "unchanged"
    return f"{(1 - new / old) * 100:+.0f}%"


# --------------------------------------------------------------------------
# result types
# --------------------------------------------------------------------------

@dataclass
class Item:
    """One image and what the tool decided to do with it."""
    arcname: str                 # path inside the epub
    name: str                    # original basename
    width: int
    height: int
    orig_bytes: int
    new_bytes: int
    action: str                  # "png->jpeg", "reencoded" or "kept"
    new_name: str | None = None  # basename written to the output
    psnr: float | None = None    # fidelity of the re-encode, in dB
    is_cover: bool = False       # the book's cover, kept at higher quality
    scaled: bool = False         # was downscaled to fit the target screen
    quality_used: int | None = None  # quality actually applied
    note: str = ""               # anything worth telling the user about it

    @property
    def changed(self) -> bool:
        return self.action in ("png->jpeg", "reencoded")


@dataclass
class Report:
    src_path: str = ""
    out_path: str = ""
    src_bytes: int = 0
    out_bytes: int = 0
    items: list[Item] = field(default_factory=list)
    renames: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    stripped: list[str] = field(default_factory=list)
    quality_used: int | None = None  # set when a size target moved the quality

    @property
    def image_before(self) -> int:
        return sum(i.orig_bytes for i in self.items)

    @property
    def image_after(self) -> int:
        return sum(i.new_bytes for i in self.items)

    @property
    def saved_bytes(self) -> int:
        return self.src_bytes - self.out_bytes


# --------------------------------------------------------------------------
# image handling
# --------------------------------------------------------------------------

def alpha_is_opaque(im: Image.Image) -> bool:
    """True when the image carries no meaningful transparency.

    Such images can become JPEG without discarding an alpha channel. Some
    Kindle files include an all-opaque alpha plane purely out of habit.
    """
    if im.mode in ("RGBA", "LA", "PA"):
        return im.convert("RGBA").getchannel("A").getextrema()[0] >= 255
    return im.mode != "P" or not im.info.get("transparency")


def unique_name(stem: str, ext: str, taken: set[str]) -> str:
    """Pick a filename that is not already in use.

    The current name is released first, which is what lets a like-for-like
    JPEG re-encode keep its filename and leave every reference untouched.
    """
    taken.discard(f"{stem}{ext}".lower())
    candidate, n = f"{stem}{ext}", 1
    while candidate.lower() in taken:
        candidate, n = f"{stem}_{n}{ext}", n + 1
    taken.add(candidate.lower())
    return candidate


def encode_jpeg(im: Image.Image, quality: int) -> bytes:
    """Encode to progressive JPEG bytes, flattening any alpha onto white."""
    if im.mode in ("RGBA", "LA", "PA", "P"):
        rgba = im.convert("RGBA")
        flat = Image.new("RGB", rgba.size, (255, 255, 255))
        flat.paste(rgba, mask=rgba.getchannel("A"))
        im = flat
    else:
        im = im.convert("RGB")
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality, optimize=True, progressive=True,
            subsampling=1)
    return buf.getvalue()


def encode_under(im: Image.Image, max_bytes: int, start_quality: int,
                 min_quality: int = 20) -> tuple[bytes, int]:
    """Encode at the highest quality whose output fits under `max_bytes`.

    Used when downscaling an already heavily compressed image: quality must
    come down far enough that the smaller, corrected-size version still costs
    fewer bytes than the original, so the tool never grows a book. If even the
    floor quality cannot fit, that lowest result is returned and the caller
    decides to keep the original.
    """
    data = encode_jpeg(im, start_quality)
    if len(data) < max_bytes:
        return data, start_quality
    lo, hi, best_q, best_data = min_quality, start_quality - 1, start_quality, data
    while lo <= hi:
        mid = (lo + hi) // 2
        trial = encode_jpeg(im, mid)
        if len(trial) < max_bytes:
            best_q, best_data = mid, trial
            lo = mid + 1
        else:
            hi = mid - 1
    return best_data, best_q


def measure_psnr(a: Image.Image, b: Image.Image) -> float | None:
    """PSNR in dB between two same-size images (99.0 when identical).

    Lets a caller confirm a given quality setting is not visibly harming the
    artwork: above roughly 35 dB the difference is not perceptible.
    """
    if a.size != b.size:
        return None
    diff = ImageChops.difference(a.convert("RGB"), b.convert("RGB"))
    sq = sum(i * i * c for i, c in enumerate(diff.convert("L").histogram()))
    mse = sq / float(a.size[0] * a.size[1])
    return 99.0 if mse == 0 else 10.0 * math.log10(255.0 * 255.0 / mse)


# --------------------------------------------------------------------------
# epub structure
# --------------------------------------------------------------------------

def find_opf(zf: zipfile.ZipFile) -> str:
    """Resolve the package document path via META-INF/container.xml."""
    try:
        container = zf.read("META-INF/container.xml")
    except KeyError:
        raise SystemExit("Not an EPUB: META-INF/container.xml is missing.")
    for rootfile in ET.fromstring(container).iter(f"{{{CONTAINER_NS}}}rootfile"):
        path = rootfile.get("full-path")
        if path:
            return path
    raise SystemExit("Not an EPUB: container.xml declares no rootfile.")


def iter_image_entries(zf: zipfile.ZipFile) -> list[str]:
    return [n for n in zf.namelist()
            if os.path.splitext(n)[1].lower() in IMAGE_EXTS]


def rewrite_manifest(opf: str, renames: dict[str, str]) -> str:
    """Update href, id and media-type for every renamed image.

    Only the attribute values that must change are touched, so the rest of the
    package document survives byte for byte.
    """
    item_re = re.compile(r"<item\b[^>]*/>")
    attr_re = re.compile(r'([\w:-]+)="([^"]*)"')

    def fix(match: re.Match) -> str:
        tag = match.group(0)
        attrs = dict(attr_re.findall(tag))
        href = attrs.get("href")
        if not href:
            return tag
        old = os.path.basename(href)
        new = renames.get(old)
        if not new:
            return tag

        out = tag.replace(f'href="{href}"', f'href="{href[:-len(old)]}{new}"')
        # Some producers use the filename as the id; keep it in sync so spine
        # idrefs and guide references continue to resolve.
        if attrs.get("id") == old:
            out = re.sub(rf'(?<=id="){re.escape(old)}(?=")', new, out)
        want = MEDIA_TYPES.get(os.path.splitext(new)[1].lower())
        if want and "media-type" in attrs:
            out = re.sub(r'(?<=media-type=")[^"]*(?=")', want, out)
        return out

    return item_re.sub(fix, opf)


def collect_unused(zf: zipfile.ZipFile, opf: str, opf_name: str) -> list[str]:
    """Return archive entries that nothing in the book refers to.

    Deliberately conservative, because removing something a reader still wants
    is far worse than leaving a few kilobytes on disk. A file is only a
    candidate when it is absent from the manifest *and* no document points at
    it. Structural files, every document, the navigation document, the cover and
    anything under META-INF are always kept.
    """
    all_names = zf.namelist()
    opf_dir = os.path.dirname(opf_name)

    def to_arc(base_dir: str, href: str) -> str:
        href = href.split("#", 1)[0].strip()
        if not href or href.startswith(("http://", "https://", "data:", "mailto:")):
            return ""
        if href.startswith("/"):
            return posixpath.normpath(href.lstrip("/"))
        return posixpath.normpath(posixpath.join(base_dir, href))

    protected: set[str] = {opf_name, "mimetype", "META-INF/container.xml"}
    declared: set[str] = set()
    for tag in re.findall(r"<item\b[^>]*/>", opf):
        attrs = dict(re.findall(r'([\w:-]+)="([^"]*)"', tag))
        href = attrs.get("href")
        if not href:
            continue
        arc = to_arc(opf_dir, href)
        if not arc:
            continue
        declared.add(arc)
        props = (attrs.get("properties") or "").split()
        mtype = attrs.get("media-type") or ""
        if "cover-image" in props or mtype.endswith("application/x-dtbncx+xml"):
            protected.add(arc)

    # A resource is live if a document points at it. Stylesheets pull in fonts
    # and images through url(...) rather than src/href, so both are matched.
    referenced: set[str] = set()
    attr_re = re.compile(r'(?:src|href)="([^"]+)"')
    url_re = re.compile(r"url\(\s*['\"]?([^'\")]+)")
    for name in all_names:
        if not name.lower().endswith(XML_DOCS):
            continue
        protected.add(name)  # a document is only removed via its manifest entry
        try:
            text = zf.read(name).decode("utf-8")
        except (KeyError, UnicodeDecodeError):
            continue
        base = os.path.dirname(name)
        for match in attr_re.finditer(text):
            arc = to_arc(base, match.group(1))
            if arc:
                referenced.add(arc)
        for match in url_re.finditer(text):
            arc = to_arc(base, match.group(1))
            if arc:
                referenced.add(arc)

    return sorted(
        n for n in all_names
        if n not in protected and n not in declared and n not in referenced
        and not n.startswith("META-INF/") and not n.endswith("/")
    )


def drop_manifest_items(opf: str, removed: list[str], opf_name: str) -> str:
    """Delete manifest entries whose files are no longer in the archive."""
    opf_dir = os.path.dirname(opf_name)
    targets = {posixpath.normpath(posixpath.join(opf_dir, r)) for r in removed}

    def fix(match: re.Match) -> str:
        tag = match.group(0)
        attrs = dict(re.findall(r'([\w:-]+)="([^"]*)"', tag))
        href = attrs.get("href")
        if not href:
            return tag
        arc = posixpath.normpath(posixpath.join(opf_dir, href.split("#", 1)[0]))
        return "" if arc in targets else tag

    return re.sub(r"<item\b[^>]*/>\s*", fix, opf)


def find_cover(zf: zipfile.ZipFile, opf_name: str) -> str | None:
    """Return the archive path of the cover image, or None.

    Two conventions are in use: EPUB 2 points at an id with
    `<meta name="cover" content="..."/>`, while EPUB 3 marks the item itself
    with `properties="cover-image"`. Books in the wild use either, sometimes
    both, so both are checked.
    """
    try:
        opf = zf.read(opf_name).decode("utf-8", "replace")
    except KeyError:
        return None
    opf_dir = os.path.dirname(opf_name)

    def to_arc(href: str) -> str:
        return posixpath.normpath(posixpath.join(opf_dir, href.split("#", 1)[0]))

    items: dict[str, str] = {}
    for tag in re.findall(r"<item\b[^>]*/>", opf):
        attrs = dict(re.findall(r'([\w:-]+)="([^"]*)"', tag))
        if attrs.get("id") and attrs.get("href"):
            items[attrs["id"]] = to_arc(attrs["href"])

    # EPUB 3 explicit marker wins.
    for tag in re.findall(r"<item\b[^>]*/>", opf):
        attrs = dict(re.findall(r'([\w:-]+)="([^"]*)"', tag))
        if "cover-image" in (attrs.get("properties") or "").split():
            return items.get(attrs.get("id", ""))

    # EPUB 2 meta indirection.
    m = re.search(r'<meta[^>]*\bname="cover"[^>]*\bcontent="([^"]+)"', opf)
    if m and m.group(1) in items:
        return items[m.group(1)]
    # Some producers write the attributes the other way round.
    m = re.search(r'<meta[^>]*\bcontent="([^"]+)"[^>]*\bname="cover"', opf)
    if m and m.group(1) in items:
        return items[m.group(1)]

    # Last resort: a file literally named like a cover.
    for n in zf.namelist():
        if re.search(r"cover", os.path.basename(n), re.IGNORECASE) \
                and os.path.splitext(n)[1].lower() in IMAGE_EXTS:
            return n
    return None


def fit_within(im: Image.Image, max_dim: int) -> Image.Image:
    """Downscale so the longest side is at most `max_dim`, never upscale."""
    longest = max(im.size)
    if not max_dim or longest <= max_dim:
        return im
    scale = max_dim / float(longest)
    size = (max(1, round(im.size[0] * scale)), max(1, round(im.size[1] * scale)))
    return im.resize(size, Image.LANCZOS)


def repack(src_dir: str, out_path: str) -> None:
    """Zip a directory into a spec-compliant EPUB.

    `mimetype` is written first and stored uncompressed so a reader can detect
    the format from the opening bytes of the file.
    """
    entries: list[tuple[str, str]] = []
    for root, _dirs, names in os.walk(src_dir):
        for n in names:
            full = os.path.join(root, n)
            rel = os.path.relpath(full, src_dir).replace(os.sep, "/")
            entries.append((full, rel))
    entries.sort(key=lambda e: (e[1] != "mimetype", e[1]))

    tmp_out = f"{out_path}.partial"
    # ZIP_DEFLATED must be set on the archive: ZipFile defaults to ZIP_STORED,
    # which silently ignores the per-entry compresslevel and leaves every
    # markup file uncompressed (worth hundreds of KB on a normal book).
    with zipfile.ZipFile(tmp_out, "w", compression=zipfile.ZIP_DEFLATED,
                         allowZip64=True) as zf:
        for full, rel in entries:
            if rel == "mimetype":
                with open(full, "rb") as fh:
                    zf.writestr(zipfile.ZipInfo(rel), fh.read(), zipfile.ZIP_STORED)
            else:
                zf.write(full, rel, compresslevel=9)
    os.replace(tmp_out, out_path)


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def validate(path: str, renames: dict[str, str]) -> list[str]:
    """Return structural problems found in the output; empty means it is sound."""
    problems: list[str] = []
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        present = set(names)

        if zf.testzip() is not None:
            problems.append("zip archive is corrupt")
        if not names or names[0] != "mimetype":
            problems.append("mimetype is not the first archive entry")
        elif zf.getinfo("mimetype").compress_type != zipfile.ZIP_STORED:
            problems.append("mimetype entry is compressed (must be stored)")
        if "mimetype" in present:
            if zf.read("mimetype").decode("ascii", "replace").strip() != "application/epub+zip":
                problems.append("mimetype has unexpected contents")

        opf_name = find_opf(zf)
        opf = zf.read(opf_name).decode("utf-8", "replace")
        # Manifest hrefs are relative to the directory holding the package
        # document, not to the root of the archive. Books produced by common
        # retail pipelines keep the OPF in OEBPS/ and list hrefs like
        # "images/cover.jpg", so they must be resolved against opf_dir.
        opf_dir = os.path.dirname(opf_name)

        def resolve(href: str) -> str:
            """Turn a manifest href into an archive path, or '' if remote."""
            if href.startswith(("http://", "https://", "mailto:", "data:")):
                return ""
            if href.startswith("/"):
                return href.lstrip("/")
            return posixpath.normpath(posixpath.join(opf_dir, href))

        # Manifest entries must exist and declare the type they actually are.
        seen: set[str] = set()
        ids: set[str] = set()
        for tag in re.findall(r"<item\b[^>]*/>", opf):
            attrs = dict(re.findall(r'([\w:-]+)="([^"]*)"', tag))
            href, mtype, iid = attrs.get("href"), attrs.get("media-type"), attrs.get("id")
            if not href:
                continue
            if href in seen:
                problems.append(f"duplicate manifest href: {href}")
            seen.add(href)
            if iid:
                ids.add(iid)
            target = resolve(href)
            if not target:
                continue  # remote resource
            if target not in present:
                problems.append(
                    f"manifest points at a missing file: {href} (expected at {target})")
                continue
            want = MEDIA_TYPES.get(os.path.splitext(target)[1].lower())
            if want and mtype and mtype != want:
                problems.append(f"media-type mismatch for {href}: {mtype} != {want}")

        for idref in re.findall(r'idref="([^"]+)"', opf):
            if ids and idref not in ids:
                problems.append(f"spine references undeclared id: {idref}")

        # No document may still point at a filename that was renamed away.
        for n in names:
            if not n.lower().endswith(XML_DOCS):
                continue
            try:
                text = zf.read(n).decode("utf-8")
            except UnicodeDecodeError:
                continue
            for old in renames:
                if re.search(rf'(?:src|href)="[^"]*{re.escape(old)}"', text):
                    problems.append(f"{n} still references renamed image {old}")

        # Every local resource a document points at must exist in the archive.
        # References are relative to the document's own directory.
        ref_re = re.compile(r'(?:src|href)="([^"]+)"')
        checked_refs = 0
        for n in names:
            if not n.lower().endswith(MARKUP_DOCS):
                continue
            try:
                text = zf.read(n).decode("utf-8")
            except UnicodeDecodeError:
                continue
            doc_dir = os.path.dirname(n)
            for ref in ref_re.findall(text):
                if not ref or ref.startswith(("http://", "https://", "mailto:", "data:", "#")):
                    continue
                target = ref.split("#", 1)[0]
                if not target:
                    continue
                checked_refs += 1
                if target.startswith("/"):
                    resolved = target.lstrip("/")
                else:
                    resolved = posixpath.normpath(posixpath.join(doc_dir, target))
                if resolved not in present:
                    problems.append(
                        f"{n} references a missing resource: {ref} (expected {resolved})")

        # Documents and images must actually parse.
        for n in names:
            if n.lower().endswith(MARKUP_DOCS):
                try:
                    ET.fromstring(zf.read(n))
                except ET.ParseError as exc:
                    problems.append(f"{n} is not well-formed XML: {exc}")
        for n in names:
            if os.path.splitext(n)[1].lower() in IMAGE_EXTS:
                try:
                    Image.open(io.BytesIO(zf.read(n))).load()
                except Exception as exc:  # noqa: BLE001 - surface any decode failure
                    problems.append(f"{n} failed to decode: {exc}")

    return problems


# --------------------------------------------------------------------------
# the actual work
# --------------------------------------------------------------------------

def plan_image(name: str, raw: bytes, taken: set[str],
               opts: argparse.Namespace,
               is_cover: bool = False) -> tuple[Item, bytes | None, str | None]:
    """Decide what to do with one image.

    Returns the item, the replacement bytes (None to keep the original) and a
    note explaining anything unexpected.
    """
    stem = os.path.splitext(name)[0]
    ext = os.path.splitext(name)[1].lower()
    size = len(raw)

    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
    except Exception as exc:  # noqa: BLE001
        return (Item(name, name, 0, 0, size, size, "kept"), None,
                f"{name}: cannot decode ({exc})")

    item = Item(name, name, im.size[0], im.size[1], size, size, "kept")
    item.is_cover = is_cover

    if ext == ".svg":
        # Vector art: already minimal, and re-encoding would be lossy.
        return item, None, None

    max_dim = getattr(opts, "max_dim", 0) or 0
    # A cover is the one image always shown edge to edge, so it gets its own
    # higher quality and is worth re-encoding even when it is already small.
    quality = getattr(opts, "cover_quality", opts.quality) if is_cover else opts.quality

    scaled = fit_within(im, max_dim) if max_dim else im
    item.scaled = scaled.size != im.size
    if item.scaled:
        item.width, item.height = scaled.size
    # PSNR is measured against the image at the size actually written, so a
    # downscaled result is judged on encoding loss rather than on the resize.
    reference = scaled

    if ext == ".png" and alpha_is_opaque(scaled) \
            and (size >= opts.min_png_bytes or item.scaled) \
            and min(scaled.size) >= opts.min_dim:
        new = unique_name(stem, ".jpg", taken)
        item.quality_used = quality
        data = encode_jpeg(scaled, quality)
        if len(data) >= size:
            if not item.scaled:
                # Already tighter than the chosen quality permits; keep it.
                return item, None, None
            # A resized image must still cost less than the source, so quality
            # comes down until the smaller, corrected-size image actually fits.
            data, item.quality_used = encode_under(scaled, size, quality)
            if len(data) >= size:
                return item, None, None
        item.action = "png->jpeg"
        item.new_name = new
        item.new_bytes = len(data)
        if opts.psnr:
            item.psnr = measure_psnr(reference, Image.open(io.BytesIO(data)))
        return item, data, None

    if ext in (".jpg", ".jpeg") and (size >= opts.min_jpeg_bytes or item.scaled):
        new = unique_name(stem, ".jpg", taken)
        use_q = getattr(opts, "jpeg_quality", quality) if not is_cover else quality
        item.quality_used = use_q
        data = encode_jpeg(scaled, use_q)
        if len(data) >= size:
            if not item.scaled:
                # Already well optimised; keep the original bytes rather than
                # spending quality on a file that gains nothing.
                return item, None, None
            data, item.quality_used = encode_under(scaled, size, use_q)
            if len(data) >= size:
                return item, None, None
        item.action = "reencoded"
        item.new_name = new
        item.new_bytes = len(data)
        if opts.psnr:
            item.psnr = measure_psnr(reference, Image.open(io.BytesIO(data)))
        return item, data, None

    return item, None, None


def plan_all_images(images: list[str], raw_images: dict[str, bytes],
                    opts: argparse.Namespace,
                    cover: str | None) -> tuple[list[Item], dict[str, bytes], dict[str, str]]:
    """Plan every image once. Returns items, replacements and the rename map."""
    taken = {os.path.basename(n).lower() for n in images}
    items: list[Item] = []
    replacements: dict[str, bytes] = {}
    renames: dict[str, str] = {}
    for arcname in images:
        name = os.path.basename(arcname)
        item, new_bytes, note = plan_image(name, raw_images[arcname], taken, opts,
                                           is_cover=(arcname == cover))
        item.arcname = arcname
        items.append(item)
        if note:
            item.note = note
        if new_bytes is not None and item.new_name:
            replacements[arcname] = new_bytes
            if item.new_name != name:
                renames[name] = item.new_name
    return items, replacements, renames


def scaled_opts(opts: argparse.Namespace, quality: int) -> argparse.Namespace:
    """A copy of opts with both quality settings moved to a given level.

    The ratio between the two is preserved so that one number still describes a
    single trade-off, which is what makes a size target predictable.
    """
    trial = copy.copy(opts)
    trial.quality = quality
    ratio = (opts.jpeg_quality / opts.quality) if opts.quality else _JPEG_RATIO
    trial.jpeg_quality = max(1, min(95, round(quality * ratio)))
    trial.cover_quality = max(1, min(95, round(quality * ratio / _JPEG_RATIO)))
    return trial


def search_size_target(images: list[str], raw_images: dict[str, bytes],
                       fixed_bytes: int, opts: argparse.Namespace,
                       target: int, cover: str | None) -> int:
    """Find the highest quality whose estimated output still fits `target`.

    Only the images are re-encoded during the search, never the archive, so the
    whole hunt costs a few seconds of JPEG work and exactly one final repack.
    JPEG size falls monotonically as quality drops, which is what makes a binary
    search valid here.
    """
    lo, hi, best = 1, 95, 1
    while lo <= hi:
        mid = (lo + hi) // 2
        items, replacements, _renames = plan_all_images(
            images, raw_images, scaled_opts(opts, mid), cover)
        # Kept images carry their original size in new_bytes, so summing every
        # item covers both passthrough artwork and re-encoded artwork.
        total = fixed_bytes + sum(i.new_bytes for i in items)
        if total <= target:
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    return best


def compress_epub(src: str, out: str, opts: argparse.Namespace,
                  report: Report | None = None) -> Report:
    """Rewrite `src` into `out` with recompressed artwork. Returns a Report."""
    rep = report or Report()
    rep.src_path, rep.out_path = src, out
    rep.src_bytes = os.path.getsize(src)

    with zipfile.ZipFile(src) as zf:
        opf_name = find_opf(zf)
        images = iter_image_entries(zf)
        raw_images = {n: zf.read(n) for n in images}
        cover = find_cover(zf, opf_name) if getattr(opts, "cover_quality", 0) else None
        # Everything that is not artwork passes through untouched, so its
        # compressed size is a fixed floor when hunting for a size target.
        image_set = set(images)
        fixed_bytes = sum(i.compress_size for i in zf.infolist()
                          if i.filename not in image_set)
        manifest = zf.read(opf_name).decode("utf-8", "replace")

    target = getattr(opts, "target_bytes", 0)
    if target:
        chosen = search_size_target(images, raw_images, fixed_bytes, opts,
                                    target, cover)
        if chosen < opts.quality:
            rep.notes.append(
                f"size target {human(target)}: quality lowered from "
                f"{opts.quality} to {chosen}")
        if chosen <= 1:
            # The search bottoms out at quality 1 rather than destroying the
            # artwork further, so the target can legitimately be unreachable.
            # Say so instead of letting the user wonder why the book is bigger.
            trial, _replacements, _renames = plan_all_images(
                images, raw_images, scaled_opts(opts, 1), cover)
            floor_bytes = fixed_bytes + sum(i.new_bytes for i in trial)
            if floor_bytes > target:
                rep.notes.append(
                    f"size target {human(target)} not reachable: even at the "
                    f"lowest quality the book is about {human(floor_bytes)}. "
                    f"The artwork is already as compressed as it can go "
                    f"without visible damage; use --max-dim to shrink the "
                    f"images themselves, or --strip-unused to drop unused files.")
        opts = scaled_opts(opts, chosen)
        rep.quality_used = chosen

    rep.items, replacements, rep.renames = plan_all_images(
        images, raw_images, opts, cover)
    for item in rep.items:
        if item.note:
            rep.notes.append(item.note)
    if cover:
        rep.notes.append(f"cover: {os.path.basename(cover)} kept at quality "
                         f"{getattr(opts, 'cover_quality', opts.quality)}")

    unused: list[str] = []
    if getattr(opts, "strip_unused", False):
        with zipfile.ZipFile(src) as zf:
            unused = collect_unused(zf, manifest, opf_name)
    if unused:
        rep.notes.append(f"stripped {len(unused)} unused file(s)")
        rep.stripped = unused

    with tempfile.TemporaryDirectory(prefix="epub-shrink-") as tmp:
        stage = os.path.join(tmp, "book")
        with zipfile.ZipFile(src) as zf:
            zf.extractall(stage)

        # Drop files nothing points at, and take them out of the manifest.
        for arcname in unused:
            path = os.path.join(stage, arcname)
            if os.path.isfile(path):
                os.remove(path)
        if unused:
            opf_path = os.path.join(stage, opf_name)
            with open(opf_path, encoding="utf-8") as fh:
                opf = fh.read()
            with open(opf_path, "w", encoding="utf-8") as fh:
                fh.write(drop_manifest_items(opf, unused, opf_name))

        # Swap in the recompressed artwork, renaming where the type changed.
        for arcname, data in replacements.items():
            path = os.path.join(stage, arcname)
            item = next(i for i in rep.items if i.arcname == arcname)
            if os.path.exists(path):
                os.remove(path)
            with open(os.path.join(os.path.dirname(path), item.new_name), "wb") as fh:
                fh.write(data)

        # Update the package document and every reference to a renamed file.
        if rep.renames:
            opf_path = os.path.join(stage, opf_name)
            with open(opf_path, encoding="utf-8") as fh:
                opf = fh.read()
            with open(opf_path, "w", encoding="utf-8") as fh:
                fh.write(rewrite_manifest(opf, rep.renames))

            for root, _dirs, names in os.walk(stage):
                for n in names:
                    if not n.lower().endswith(XML_DOCS):
                        continue
                    path = os.path.join(root, n)
                    try:
                        with open(path, encoding="utf-8") as fh:
                            doc = fh.read()
                    except UnicodeDecodeError:
                        continue
                    # Literal replacement: filenames are not regex patterns.
                    for old, new in rep.renames.items():
                        doc = doc.replace(old, new)
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.write(doc)

        repack(stage, out)

    rep.out_bytes = os.path.getsize(out)

    # Only report problems this run introduced. Plenty of real books ship with
    # broken references already, and blaming the tool for damage it did not do
    # would train you to ignore the output.
    before = set(validate(src, {}))
    after = validate(out, rep.renames)
    rep.problems = [p for p in after if p not in before]
    for problem in before & set(after):
        rep.notes.append(f"pre-existing in the source, left alone: {problem}")
    return rep


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------

def default_output(src: str, suffix: str) -> str:
    base = os.path.splitext(src)[0]
    candidate, n = f"{base}{suffix}.epub", 1
    while os.path.exists(candidate):
        candidate = f"{base}{suffix}-{n}.epub"
        n += 1
    return candidate


def find_backup_dir(src: str, override: str | None) -> str:
    """Choose where the untouched original of `src` is archived.

    Preference order: an explicit path, then a "Backups" folder beside the
    book, then one at the top of the directory tree. The last option matters
    for library apps such as Audiobookshelf, which track a book by its path:
    keeping the original in a Backups folder means the compressed file can take
    the original's exact name and location, so the library entry keeps working
    while the pristine copy sits safely out of the way.
    """
    if override:
        return os.path.abspath(os.path.expanduser(override))
    book_dir = os.path.dirname(os.path.abspath(src))
    candidate = os.path.join(book_dir, "Backups")
    if os.path.isdir(candidate):
        return candidate
    return candidate


def backup_path(src: str, backup_dir: str) -> str:
    """Return an unused path inside `backup_dir` for the original of `src`."""
    name = os.path.basename(src)
    candidate = os.path.join(backup_dir, name)
    n = 1
    while os.path.exists(candidate):
        stem, ext = os.path.splitext(name)
        candidate = os.path.join(backup_dir, f"{stem}-{n}{ext}")
        n += 1
    return candidate


def archive_original(src: str, backup_dir: str | None = None) -> str:
    """Move the original of `src` into a Backups folder, return where it went.

    The original is moved rather than copied, so replacing it in place costs no
    extra disk space and there is exactly one copy of the untouched book.
    """
    target_dir = find_backup_dir(src, backup_dir)
    os.makedirs(target_dir, exist_ok=True)
    dest = backup_path(src, target_dir)
    shutil.move(os.path.abspath(src), dest)
    return dest


def find_epubs(root: str, recursive: bool = True) -> list[str]:
    """Return every EPUB under `root`, searching subdirectories by default."""
    found: list[str] = []
    if os.path.isfile(root):
        return [root] if root.lower().endswith(".epub") else []
    if recursive:
        for dirpath, dirnames, filenames in os.walk(root):
            # A previous run's Backups folder is archive, not input.
            dirnames[:] = sorted(d for d in dirnames if d != "Backups")
            for name in sorted(filenames):
                if name.lower().endswith(".epub"):
                    found.append(os.path.join(dirpath, name))
    else:
        for name in sorted(os.listdir(root)):
            full = os.path.join(root, name)
            if os.path.isfile(full) and name.lower().endswith(".epub"):
                found.append(full)
    return found


def _local(tag: str) -> str:
    """The tag or attribute name without its XML namespace."""
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _attr(el, name: str) -> str | None:
    """An attribute value looked up by local name, ignoring the namespace.

    Publishers are wildly inconsistent here: the same role can be written as
    `opf:role`, `ns0:role` or bare `role`, and all three are valid.
    """
    for key, value in el.attrib.items():
        if _local(key) == name.lower() and value:
            return value
    return None


def _text(el) -> str:
    """All text inside an element, including from mixed content, tidied."""
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


EMPTY_META: dict = {
    "title": None, "subtitle": None,
    "author": None, "authors": [], "narrators": [],
    "series": None, "series_index": None,
    "publisher": None, "published_year": None, "published_date": None,
    "language": None, "isbn": None, "asin": None,
    "description": None, "genres": [], "tags": [],
}


def read_book_meta(path: str) -> dict:
    """Read a book's own OPF metadata.

    This is an independent implementation on top of `xml.etree.ElementTree`.
    It deliberately covers the same fields, and reads them in the same
    precedence order, that Audiobookshelf's own OPF parser does — so whatever
    the server would have worked out for itself from the file is exactly what we
    send it, and an upload never arrives with less detail than leaving the file
    on disk would have given. That was a behavioural decision, not a copy: no
    Audiobookshelf code is used, imported or translated here, and the OPF
    specification is the authority for what each element means.
    Credit and reference: https://github.com/advplyr/audiobookshelf, whose
    `server/utils/parsers/parseOpfMetadata.js` is the behavioural reference.

    Returns a dict shaped like EMPTY_META. Never raises for a broken or
    unreadable archive — absent fields simply come back empty, so the caller
    can fall back to the filename and the user's own defaults.
    """
    meta: dict = {k: (list(v) if isinstance(v, list) else v)
                  for k, v in EMPTY_META.items()}
    try:
        with zipfile.ZipFile(path) as zf:
            opf = zf.read(find_opf(zf)).decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError, OSError, ValueError):
        return meta
    try:
        root = ElementTree.fromstring(opf)
    except ElementTree.ParseError:
        # Fall back to a loose scan: a malformed OPF should still yield a
        # title rather than nothing at all.
        for field in ("title", "creator"):
            found = re.search(rf"<dc:{field}[^>]*>(.*?)</dc:{field}>",
                              opf, re.IGNORECASE | re.DOTALL)
            if found:
                meta[field if field == "title" else "author"] = \
                    re.sub(r"\s+", " ", found.group(1)).strip()
        if meta["author"]:
            meta["authors"] = [meta["author"]]
        return meta

    # The <metadata> block is what carries the descriptive fields; some books
    # put a stray element outside it, so fall back to scanning the whole root.
    package_meta = next((el for el in root.iter() if _local(el.tag) == "metadata"),
                        root)

    creators: list[tuple[str, str | None, str | None]] = []  # value, role, id
    identifiers: list[tuple[str, str | None]] = []  # value, scheme
    meta_tags: list = []
    for el in package_meta:
        name = _local(el.tag)
        if name == "meta":
            meta_tags.append(el)
        elif name == "title" and not meta["title"]:
            meta["title"] = _text(el) or None
        elif name == "subtitle" and not meta["subtitle"]:
            meta["subtitle"] = _text(el) or None
        elif name == "creator":
            creators.append((_text(el), _attr(el, "role"), _attr(el, "id")))
        elif name == "contributor":
            creators.append((_text(el), _attr(el, "role") or "nrt",
                             _attr(el, "id")))
        elif name == "publisher" and not meta["publisher"]:
            meta["publisher"] = _text(el) or None
        elif name == "language" and not meta["language"]:
            meta["language"] = _text(el) or None
        elif name == "date" and not meta["published_date"]:
            date = _text(el)
            meta["published_date"] = date or None
            # ABS stores the year on its own, taken from the front of the date.
            if date[:4].isdigit():
                meta["published_year"] = date[:4]
        elif name == "description" and not meta["description"]:
            meta["description"] = _strip_tags(_text(el)) or None
        elif name == "subject":
            subject = _text(el)
            if subject and subject not in meta["genres"]:
                meta["genres"].append(subject)
        elif name == "tag":
            tag = _text(el)
            if tag and tag not in meta["tags"]:
                meta["tags"].append(tag)
        elif name == "identifier":
            identifiers.append((_text(el), _attr(el, "scheme")))

    # EPUB 3 lets role/file-as be refined onto a creator by id, which is how
    # Calibre and most modern tools actually write it.
    roles: dict[str, str] = {}
    for el in meta_tags:
        refines = _attr(el, "refines") or ""
        prop = _attr(el, "property") or ""
        if refines.startswith("#") and prop.lower() == "role":
            value = _attr(el, "content") or _text(el)
            if value:
                roles[refines[1:]] = value.strip()

    authors: list[str] = []
    narrators: list[str] = []
    for value, role, el_id in creators:
        if not value:
            continue
        role = (roles.get(el_id or "") or role or "").lower()
        if role in ("aut", "author") and value not in authors:
            authors.append(value)
        elif role in ("nrt", "narrator") and value not in narrators:
            narrators.append(value)

    if not authors:
        # No usable role information: fall back to the first creator, which is
        # the author far more often than not.
        for value, _role, _id in creators:
            if value:
                authors = [value]
                break
    meta["authors"], meta["narrators"] = authors, narrators
    meta["author"] = authors[0] if authors else None

    for value, scheme in identifiers:
        target = {"isbn": "isbn", "asin": "asin"}.get((scheme or "").lower())
        if target and value and not meta[target]:
            meta[target] = value

    # Series is Calibre's convention in practice; ABS reads it the same way.
    index: str | None = None
    for el in meta_tags:
        name = (_attr(el, "name") or "").lower()
        content = (_attr(el, "content") or "").strip()
        if not content:
            continue
        if name == "calibre:series" and not meta["series"]:
            meta["series"] = content
        elif name == "calibre:series_index" and index is None:
            index = content
    meta["series_index"] = index
    return meta


def derive_upload_name(meta: dict, fallback: str) -> str:
    """The filename to upload a book under, built from its own metadata.

    "Author - Title.epub", with "(Series #2)" appended when the book declares a
    series. Falls back to the on-disk stem when the metadata is too thin to
    build a sensible name, and strips anything a filesystem would object to.
    """
    title = str(meta.get("title") or "").strip()
    author = str(meta.get("author") or "").strip()
    series = str(meta.get("series") or "").strip()
    # Coerced because a series index is as likely to arrive as an int as a
    # string once it has been through an editable table cell.
    index = str(meta.get("series_index") or "").strip()
    stem = fallback or "book"
    if title:
        stem = " - ".join(p for p in (author, title) if p)
        if series:
            stem += f" ({series} #{index})" if index else f" ({series})"
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", stem)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return f"{cleaned or 'book'}.epub"


def backup_for(src: str, backup_dir: str | None = None) -> str | None:
    """The archived original of `src`, or None if there is no backup.

    Matches by filename inside the backup location, which is the same rule the
    archive step uses, so a backup made by an earlier in-place run is found.
    """
    target_dir = find_backup_dir(src, backup_dir)
    if not os.path.isdir(target_dir):
        return None
    name = os.path.basename(src)
    direct = os.path.join(target_dir, name)
    if os.path.isfile(direct):
        return direct
    # The archive step avoids name clashes with a -1/-2 suffix.
    stem, ext = os.path.splitext(name)
    matches = sorted(f for f in os.listdir(target_dir)
                     if f.startswith(stem) and f.endswith(ext))
    if not matches:
        return None
    return os.path.join(target_dir, matches[-1])


def restore_original(src: str, backup_dir: str | None = None) -> str:
    """Swap `src` back for its archived original, returning the restored path.

    Raises FileNotFoundError when there is no backup to restore from.
    """
    backup = backup_for(src, backup_dir)
    if not backup:
        raise FileNotFoundError(
            f"no archived original found for {src} in "
            f"{find_backup_dir(src, backup_dir)}")
    # Move the compressed book aside, put the original back, then discard the
    # compressed copy so a crash in between never loses either file.
    out = f"{src}.compressed"
    os.replace(src, out)
    try:
        os.replace(backup, src)
    except OSError:
        shutil.move(backup, src)
    os.remove(out)
    return src


def format_report(rep: Report, opts: argparse.Namespace,
                  verbose: bool = False) -> str:
    """Render a run's outcome as readable text (CLI, file export, GUI)."""
    lines: list[str] = []
    if rep.src_path and rep.src_path != rep.out_path:
        lines.append(f"\n{rep.src_path} -> {rep.out_path}")
    elif rep.src_path:
        lines.append(f"\n{rep.src_path}")
    lines.append(f"  images  {human(rep.image_before)} -> {human(rep.image_after)}  "
                 f"({pct_saved(rep.image_after, rep.image_before)})")
    if rep.stripped:
        shown = ", ".join(os.path.basename(s) for s in rep.stripped[:3])
        more = f" and {len(rep.stripped) - 3} more" if len(rep.stripped) > 3 else ""
        lines.append(f"  unused  {len(rep.stripped)} file(s) removed ({shown}{more})")
    if rep.src_bytes and rep.out_bytes:
        lines.append(
            f"  file    {human(rep.src_bytes)} -> {human(rep.out_bytes)}  "
            f"({pct_saved(rep.out_bytes, rep.src_bytes)}, "
            f"{human(abs(rep.saved_bytes))} {'saved' if rep.saved_bytes >= 0 else 'larger'})")
    lines.append("  checks  "
                 + ("passed (mimetype, manifest, media-types, references, decoding)"
                    if not rep.problems
                    else f"{len(rep.problems)} problem(s): "
                         + "; ".join(rep.problems[:3])))
    for note in rep.notes:
        lines.append(f"    note: {note}")
    if verbose:
        lines.append("  images in detail:")
        for item in rep.items:
            detail = f"    {item.arcname}: {item.width}x{item.height} " \
                     f"{human(item.orig_bytes)}"
            if item.changed:
                detail += f" -> {human(item.new_bytes)} ({item.action}"
                if item.scaled:
                    detail += ", downscaled"
                if item.is_cover:
                    detail += ", cover"
                if item.psnr is not None:
                    detail += f", PSNR {item.psnr:.1f} dB"
                detail += ")"
                if item.new_name and item.new_name != os.path.basename(item.arcname):
                    detail += f" as {item.new_name}"
            elif item.is_cover:
                detail += " (cover, kept)"
            lines.append(detail)
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="epub-shrink",
        description="Reduce EPUB size by recompressing embedded images.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog="The input is never modified unless --in-place is given.",
    )
    p.add_argument("epub", nargs="+",
                   help="EPUB file(s) or folder(s) to compress")
    p.add_argument("-o", "--output", help="output path (single input only)")
    p.add_argument("--in-place", action="store_true",
                   help="replace the input in place; the original is moved to "
                        "a Backups folder, so the filename and library entry "
                        "are preserved")
    p.add_argument("--backup-dir", metavar="DIR",
                   help="where originals are archived (default: a Backups "
                        "folder beside each book)")
    p.add_argument("--no-backup", action="store_true",
                   help="with --in-place, discard the original instead of "
                        "archiving it")
    p.add_argument("--restore", action="store_true",
                   help="swap each input back for its archived original "
                        "(undoes a previous --in-place)")
    p.add_argument("--suffix", default="_compressed",
                   help="suffix for the generated output filename")
    p.add_argument("--no-recursive", action="store_true",
                   help="do not search subdirectories when given a folder")
    p.add_argument("--preset", choices=sorted(PRESETS),
                   default=DEFAULT_PRESET,
                   help="reader preset; its numbers are applied over whatever "
                        "--quality/--min-dim/... are given")
    p.add_argument("--target", dest="target_bytes", metavar="SIZE",
                   type=parse_size,
                   help="aim for an output under this size, e.g. 5MB or 700k; "
                        "quality is lowered as far as needed and the chosen "
                        "value is reported")
    p.add_argument("--cover-quality", type=int,
                   help="quality for the cover image (default: preset value)")
    p.add_argument("--max-dim", type=int,
                   help="downscale artwork whose longest side exceeds this "
                        "(default: from the preset)")
    p.add_argument("--strip-unused", action="store_true",
                   help="remove archive entries that no document and no "
                        "manifest entry refer to")
    p.add_argument("--jobs", type=int, default=1, metavar="N",
                   help="compress up to N books in parallel")
    p.add_argument("-q", "--quality", type=int, default=None,
                   help="JPEG quality (1-95) for converted PNG artwork; "
                        "overrides the preset")
    p.add_argument("--jpeg-quality", type=int, default=None,
                   help="JPEG quality for re-encoding existing JPEGs")
    p.add_argument("--min-dim", type=int, default=None,
                   help="smallest side in px before a PNG is converted")
    p.add_argument("--min-png-bytes", type=int, default=None,
                   help="ignore PNGs smaller than this")
    p.add_argument("--min-jpeg-bytes", type=int, default=None,
                   help="ignore JPEGs smaller than this")
    p.add_argument("--psnr", action="store_true",
                   help="report per-image PSNR to judge quality loss")
    p.add_argument("--dry-run", action="store_true",
                   help="report what would change without writing output")
    p.add_argument("--report-file", metavar="PATH",
                   help="also write the full report to this file")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="list every image decision")
    return p


def print_report(rep: Report, opts: argparse.Namespace) -> None:
    text = format_report(rep, opts, verbose=opts.verbose
                         or (opts.dry_run and any(i.changed for i in rep.items)))
    if text:
        print(text)
    for problem in rep.problems:
        print(f"    PROBLEM: {problem}", file=sys.stderr)
    if opts.report_file and rep.out_path != "(dry run, nothing written)":
        with open(opts.report_file, "a" if getattr(opts, "_report_append", False) else "w",
                  encoding="utf-8") as fh:
            fh.write(text + "\n")


def apply_preset_defaults(opts: argparse.Namespace) -> None:
    """Fill every option the user left unset from the selected preset.

    Explicit values always win; a preset only supplies the numbers the caller
    did not state. 'custom' keeps what was set and falls back to the default
    preset for the rest. max_dim is 0 (no downscaling) when nothing supplies it.
    """
    preset = PRESETS.get(getattr(opts, "preset", ""), {}) or PRESETS[DEFAULT_PRESET]
    if getattr(opts, "preset", "") in ("", "custom"):
        preset = {**PRESETS[DEFAULT_PRESET], **preset}
    for key in ("quality", "jpeg_quality", "min_dim", "min_png_bytes",
                "min_jpeg_bytes", "cover_quality", "max_dim"):
        if getattr(opts, key, None) is None and key in preset:
            setattr(opts, key, preset[key])


def main(argv: list[str] | None = None) -> int:
    opts = build_parser().parse_args(argv)
    apply_preset_defaults(opts)

    exit_code = 0

    # Expand any folder arguments into the EPUBs they contain.
    targets: list[str] = []
    for entry in opts.epub:
        if os.path.isdir(entry):
            found = find_epubs(entry, recursive=not opts.no_recursive)
            if not found:
                print(f"{entry}: no EPUB files found", file=sys.stderr)
                exit_code = 1
            targets.extend(found)
        else:
            targets.append(entry)

    if opts.output and len(targets) > 1:
        print("--output cannot be used with multiple inputs", file=sys.stderr)
        return 2

    if opts.restore:
        for src in targets:
            try:
                restore_original(src, opts.backup_dir)
                print(f"restored {src} from backup")
            except FileNotFoundError as exc:
                print(str(exc), file=sys.stderr)
                exit_code = 1
        return exit_code

    def process_one(src: str) -> int:
        if not os.path.isfile(src):
            print(f"{src}: no such file", file=sys.stderr)
            return 1

        if opts.in_place:
            # Build beside the original under a temporary name. Writing straight
            # to src would overwrite the original before it could be archived,
            # which loses the very copy the backup exists to preserve.
            out = f"{src}.shrinking"
        elif opts.output:
            out = opts.output
        else:
            out = default_output(src, opts.suffix)

        try:
            rep = compress_epub(src, out, opts)
        except zipfile.BadZipFile as exc:
            print(f"{src}: not a valid zip archive ({exc})", file=sys.stderr)
            return 1
        except SystemExit as exc:
            print(f"{src}: {exc}", file=sys.stderr)
            return 1

        if opts.dry_run:
            rep.out_path = "(dry run, nothing written)"
            if os.path.exists(out):
                os.remove(out)
        elif opts.in_place:
            # The compressed book sits in a temporary file, so the original is
            # still on disk. Only now, after validation passed, is it archived
            # and then replaced.
            if rep.problems:
                print(f"{src}: not replaced, output failed validation "
                      f"({len(rep.problems)} problems)", file=sys.stderr)
                os.remove(out)
                return 1
            try:
                if opts.no_backup:
                    os.remove(src)
                    backup = "(original discarded)"
                else:
                    backup = archive_original(src, opts.backup_dir)
            except OSError as exc:
                print(f"{src}: could not archive the original ({exc}); "
                      f"replacement aborted", file=sys.stderr)
                os.remove(out)
                return 1
            os.replace(out, src)
            rep.out_path = src
            rep.notes.append(f"original archived to {backup}")
        else:
            if rep.problems:
                print(f"{out}: written but failed validation; not trustworthy",
                      file=sys.stderr)

        print_report(rep, opts)
        return 1 if rep.problems else 0

    if opts.jobs > 1 and len(targets) > 1:
        opts._report_append = True
        with ThreadPoolExecutor(max_workers=opts.jobs) as pool:
            futures = [pool.submit(process_one, src) for src in targets]
            for fut in as_completed(futures):
                exit_code |= fut.result()
    else:
        opts._report_append = bool(opts.report_file) and len(targets) > 1
        for src in targets:
            exit_code |= process_one(src)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
