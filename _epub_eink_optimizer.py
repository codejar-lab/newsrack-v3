# Copyright (c) 2022 https://github.com/ping/
#
# This software is released under the GNU General Public License v3.0
# https://opensource.org/licenses/GPL-3.0
"""
Post-processes a generated EPUB so it is lighter and faster to render on
small e-ink readers (Xteink X3/X4/X4 Pro and similar 800x480-class screens).

Ideas adapted from:
- https://github.com/uxjulia/auto-epub-optimizer
- https://github.com/uxjulia/inky-self-hosted
- common e-ink / Kindle / Kobo EPUB slimming guides (grayscale + downscale +
  posterize images, drop embedded fonts, strip colour/shadow CSS, recompress
  the container with max deflate)

Unlike those projects (separate watcher/Docker services that post-process an
EPUB after the fact), this runs in-process as the last step of the existing
newsrack generation pipeline, so no extra service is needed.

Implementation notes:
- The EPUB tree is walked *once* into typed buckets; every pass takes a file
  list, never a fresh ``rglob``.
- Each HTML file is read and written *once*: all the markup regexes run in a
  single pass (``_clean_html_files``).
- Images are processed in a thread pool (PIL releases the GIL around
  decode/encode) and are re-saved in their original container format, so the
  OPF ``media-type`` stays valid.
"""
import logging
import os
import re
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from PIL import Image, ImageFile, ImageOps

# a partially-downloaded image must still open so we can greyscale it
ImageFile.LOAD_TRUNCATED_IMAGES = True

logger = logging.getLogger("newsrack.eink_optimizer")

_FONT_EXTS = {".ttf", ".otf", ".woff", ".woff2", ".eot"}
_RASTER_EXTS = {
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".apng", ".gif",
    ".bmp", ".webp", ".tif", ".tiff", ".avif",
}
_HTML_EXTS = {".xhtml", ".html", ".htm"}

# image container magic bytes, for the belt-and-braces sweep over oddly named
# / extensionless files
_IMAGE_MAGIC = (
    b"\xff\xd8\xff",          # JPEG
    b"\x89PNG\r\n\x1a\n",     # PNG
    b"GIF87a", b"GIF89a",     # GIF
    b"BM",                    # BMP
    b"II*\x00", b"MM\x00*",   # TIFF
)

# Pillow format -> a format string it can also write back
_WRITABLE_FORMATS = {
    "JPEG": "JPEG", "MPO": "JPEG", "JFIF": "JPEG",
    "PNG": "PNG", "APNG": "PNG",
    "GIF": "GIF", "BMP": "BMP", "WEBP": "WEBP",
    "TIFF": "TIFF",
}

# CSS declarations that mean nothing on a 1-bit/greyscale panel and only add
# weight (and sometimes render artefacts). Stripped from every stylesheet.
_CSS_DEAD_PROPS = (
    "color",
    "background",
    "background-color",
    "background-image",
    "background-repeat",
    "background-position",
    "background-size",
    "box-shadow",
    "-webkit-box-shadow",
    "text-shadow",
    "filter",
    "-webkit-filter",
    "opacity",
    "transition",
    "-webkit-transition",
    "animation",
    "-webkit-animation",
    "transform",
    "-webkit-transform",
)

# marker so the CSS pass is idempotent across re-runs / multiple stylesheets
_CSS_BASE_MARKER = "/*eink-base*/"
_CSS_EINK_BASE = (
    _CSS_BASE_MARKER
    + "html,body{background:#fff !important;color:#000 !important;}"
    + "img{max-width:100% !important;height:auto !important;}"
    + "*{text-shadow:none !important;box-shadow:none !important;"
    + "background-image:none !important;}"
)


@dataclass
class EinkOptions:
    """Optimisation options tuned for the Xteink X3/X4/X4 Pro (800x480 panel)."""

    max_width: int = 800
    max_height: int = 480
    # a cover is the one image allowed to keep portrait proportions
    cover_max_width: int = 600
    cover_max_height: int = 800
    jpeg_quality: int = 75
    grayscale: bool = True
    posterize_bits: int = 4  # 2**4 = 16 grey levels, plenty for e-ink
    png_colors: int = 16  # palette size for PNGs kept as PNG
    strip_fonts: bool = True
    strip_css: bool = True
    strip_scripts: bool = True
    zip_compresslevel: int = 9
    image_workers: int = 8


# --------------------------------------------------------------------------- #
#  tree walk                                                                   #
# --------------------------------------------------------------------------- #
def _walk(extract_dir: Path) -> Dict[str, List[Path]]:
    """One pass over the extracted EPUB, bucketed by role."""
    buckets: Dict[str, List[Path]] = {
        "images": [], "html": [], "css": [], "opf": [], "fonts": [], "other": [],
    }
    for p in extract_dir.rglob("*"):
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        if ext in _RASTER_EXTS:
            buckets["images"].append(p)
        elif ext in _HTML_EXTS:
            buckets["html"].append(p)
        elif ext == ".css":
            buckets["css"].append(p)
        elif ext == ".opf":
            buckets["opf"].append(p)
        elif ext in _FONT_EXTS:
            buckets["fonts"].append(p)
        else:
            buckets["other"].append(p)
    return buckets


def _read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        try:
            return path.read_text(encoding="latin-1")
        except OSError:
            return None


# --------------------------------------------------------------------------- #
#  images                                                                      #
# --------------------------------------------------------------------------- #
def _resize_dims(w: int, h: int, max_w: int, max_h: int):
    if w <= max_w and h <= max_h:
        return w, h
    ratio = min(max_w / w, max_h / h)
    return max(1, int(w * ratio)), max(1, int(h * ratio))


def _is_cover(path: Path) -> bool:
    stem = path.stem.lower()
    return stem == "cover" or stem.startswith(("cover-", "cover_", "cover."))


def _sniff_image(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            head = fh.read(16)
    except OSError:
        return False
    if head[8:12] == b"WEBP":
        return True
    return any(head.startswith(m) for m in _IMAGE_MAGIC)


def _grayscale_file(path: Path) -> None:
    """Last-resort: whatever else failed, make sure the file is not colour,
    keeping its container format."""
    try:
        with Image.open(path) as im:
            if im.mode in ("L", "LA", "1"):
                return
            fmt = _WRITABLE_FORMATS.get(im.format or "", "PNG")
            has_alpha = "A" in im.mode or (
                im.mode == "P" and "transparency" in im.info
            )
            g = im.convert("LA" if has_alpha else "L")
            if fmt == "JPEG" and g.mode == "LA":
                g = g.convert("L")
            g.save(path, format=fmt, optimize=(fmt in ("PNG", "GIF")))
    except Exception:  # noqa, pylint: disable=broad-except
        logger.exception("Could not grayscale %s", path)


def _optimize_image(path: Path, opts: EinkOptions) -> bool:
    """Greyscale + downscale + posterize `path`, re-saving in its original
    container format. Returns True if the file is now guaranteed non-colour."""
    try:
        with Image.open(path) as src:
            src.load()
            src_format = src.format
            im = src.copy()
    except Exception:  # not a decodable image, leave it alone
        return False

    fmt = _WRITABLE_FORMATS.get(
        (src_format or path.suffix.lstrip(".").upper()).replace("JPG", "JPEG"),
        None,
    )
    if fmt is None:
        # a format PIL read but cannot write (e.g. AVIF w/o plugin) -- don't
        # risk corrupting the manifest, just try to at least greyscale it
        _grayscale_file(path)
        return False

    # already-optimised (or already-small greyscale) image: re-encoding a
    # posterized JPEG just adds a generation of ringing and grows the file, so
    # leave it untouched -- this also makes the whole pass idempotent
    if (
        opts.grayscale
        and im.mode in ("L", "LA", "1")
        and im.width <= (opts.cover_max_width if _is_cover(path) else opts.max_width)
        and im.height <= (opts.cover_max_height if _is_cover(path) else opts.max_height)
    ):
        return True

    try:
        has_alpha = im.mode in ("RGBA", "LA", "PA") or (
            im.mode == "P" and "transparency" in im.info
        )

        if opts.grayscale:
            im = im.convert("LA" if has_alpha else "L")
        elif im.mode not in ("L", "LA", "RGB", "RGBA"):
            im = im.convert("RGBA" if has_alpha else "RGB")

        if _is_cover(path):
            max_w, max_h = opts.cover_max_width, opts.cover_max_height
        else:
            max_w, max_h = opts.max_width, opts.max_height
        new_size = _resize_dims(im.width, im.height, max_w, max_h)
        if new_size != (im.width, im.height):
            im = im.resize(new_size, Image.LANCZOS)

        if opts.grayscale and opts.posterize_bits < 8:
            if im.mode == "LA":
                l_channel, a_channel = im.split()
                l_channel = ImageOps.posterize(l_channel, opts.posterize_bits)
                im = Image.merge("LA", (l_channel, a_channel))
            elif im.mode == "L":
                im = ImageOps.posterize(im, opts.posterize_bits)

        if fmt == "JPEG":
            im.convert("L" if im.mode in ("L", "LA", "1") else "RGB").save(
                path, format="JPEG", quality=opts.jpeg_quality,
                progressive=False, optimize=True,
            )
        elif fmt == "PNG":
            if im.mode in ("LA", "RGBA"):
                im.save(path, format="PNG", optimize=True)
            else:
                out = im.convert("L") if im.mode != "L" else im
                if opts.grayscale and 0 < opts.png_colors < 256:
                    out = out.quantize(colors=opts.png_colors,
                                       method=Image.MEDIANCUT)
                out.save(path, format="PNG", optimize=True)
        elif fmt == "GIF":
            im.convert("L").save(path, format="GIF", optimize=True)
        elif fmt == "WEBP":
            save_im = im if im.mode in ("L", "LA") else im.convert("L")
            save_im.save(path, format="WEBP", quality=opts.jpeg_quality)
        else:  # BMP / TIFF -- keep the container, just greyscale it
            (im if im.mode in ("L", "LA") else im.convert("L")).save(
                path, format=fmt
            )
        return opts.grayscale
    except Exception:  # noqa, pylint: disable=broad-except
        logger.exception("Unable to optimise image %s for e-ink", path)
        if opts.grayscale:
            _grayscale_file(path)
        return False


def _process_images(images: List[Path], other: List[Path], opts: EinkOptions):
    """Optimise every image; then a cheap belt-and-braces sweep that greyscales
    anything colour that slipped through (odd extension, format PIL couldn't
    round-trip, ...)."""
    handled: Dict[Path, bool] = {}
    if images:
        workers = min(opts.image_workers, len(images))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = ex.map(lambda p: _optimize_image(p, opts), images)
            for path, ok in zip(images, results):
                handled[path] = ok

    if not opts.grayscale:
        return

    for p in images + [q for q in other if _sniff_image(q)]:
        if handled.get(p):
            continue
        try:
            with Image.open(p) as probe:
                mode = probe.mode
                pal = probe.getpalette()
            grey_palette = not pal or all(
                pal[i] == pal[i + 1] == pal[i + 2]
                for i in range(0, len(pal) - 2, 3)
            )
        except Exception:
            continue
        if mode in ("L", "LA", "1") or (mode == "P" and grey_palette):
            continue
        _grayscale_file(p)


# --------------------------------------------------------------------------- #
#  fonts                                                                       #
# --------------------------------------------------------------------------- #
def _strip_fonts(fonts: List[Path], opfs: List[Path], csss: List[Path]) -> None:
    removed = set()
    for f in fonts:
        removed.add(f.name)
        try:
            f.unlink()
        except OSError:
            pass
    if not removed:
        return

    for opf in opfs:
        content = _read_text(opf)
        if content is None:
            continue
        new = content
        for href in removed:
            new = re.sub(
                rf'<item\b[^>]*href="[^"]*{re.escape(href)}"[^>]*/?>\s*', "", new
            )
        if new != content:
            opf.write_text(new, encoding="utf-8")

    for css in csss:
        content = _read_text(css)
        if content is None:
            continue
        new = content
        for href in removed:
            new = re.sub(
                rf"@font-face\s*\{{[^{{}}]*{re.escape(href)}[^{{}}]*\}}", "", new
            )
        if new != content:
            css.write_text(new, encoding="utf-8")


# --------------------------------------------------------------------------- #
#  CSS                                                                         #
# --------------------------------------------------------------------------- #
_PROP_ALT = "|".join(re.escape(p) for p in _CSS_DEAD_PROPS)
# value pattern allows balanced (...) so `url(data:...;base64,...)` isn't cut
# at the semicolon inside the data URI
_DEAD_DECL_RE = re.compile(
    rf"(?<![-\w])(?:{_PROP_ALT})\s*:(?:[^;}}(]|\([^)]*\))*;?", re.IGNORECASE
)
_FONT_FAMILY_RE = re.compile(
    r"font-family\s*:(?:[^;}(]|\([^)]*\))*;?", re.IGNORECASE
)
_FONT_FACE_RE = re.compile(r"@font-face\s*\{[^{}]*\}", re.IGNORECASE)
_CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def _minify_css(css: str) -> str:
    css = _CSS_COMMENT_RE.sub("", css)
    css = re.sub(r"\s+", " ", css)
    css = re.sub(r"\s*([{}:;,])\s*", r"\1", css)
    css = re.sub(r";}", "}", css)
    return css.strip()


def _strip_css(csss: List[Path]) -> None:
    for css in csss:
        content = _read_text(css)
        if content is None or _CSS_BASE_MARKER in content:
            continue
        content = _FONT_FACE_RE.sub("", content)
        content = _DEAD_DECL_RE.sub("", content)
        content = _FONT_FAMILY_RE.sub("", content)
        content = _minify_css(content) + "\n" + _CSS_EINK_BASE
        css.write_text(content, encoding="utf-8")


# --------------------------------------------------------------------------- #
#  HTML  (one read/write per file, all regexes in one pass)                    #
# --------------------------------------------------------------------------- #
_SCRIPT_RE = re.compile(r"<script\b.*?</script>", re.IGNORECASE | re.DOTALL)
_HTML_COMMENT_RE = re.compile(r"<!--(?!\[if).*?-->", re.DOTALL)
# the <hr> calibre puts immediately before the download-source footer
_FOOTER_HR_RE = re.compile(
    r"<hr\b[^>]*>\s*(?=<p\b[^>]*>(?:(?!</p>).)*?calibre-downloaded-from)",
    re.IGNORECASE | re.DOTALL,
)
# the "This article was downloaded by calibre from <url>" paragraph itself
_FOOTER_P_RE = re.compile(
    r"<p\b[^>]*>(?:(?!</p>).)*?calibre-downloaded-from(?:(?!</p>).)*?</p>",
    re.IGNORECASE | re.DOTALL,
)
# <a> pointing off-device: absolute http(s), protocol-relative, mailto, tel.
# Internal epub navigation (relative paths, #anchors) is left clickable.
_EXTERNAL_LINK_RE = re.compile(
    r"<a\b[^>]*\bhref\s*=\s*([\"'])\s*(?:(?:https?:)?//|mailto:|tel:)[^\"']*\1[^>]*>"
    r"(.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
# <img> whose src never got localised -> broken placeholder in an offline book
_REMOTE_IMG_RE = re.compile(
    r"<img\b[^>]*\bsrc\s*=\s*([\"'])\s*(?:https?:)?//[^\"']*\1[^>]*>", re.IGNORECASE
)


def _clean_html_files(htmls: List[Path], opts: EinkOptions) -> None:
    for path in htmls:
        content = _read_text(path)
        if content is None:
            continue
        new = content
        if opts.strip_scripts:
            new = _SCRIPT_RE.sub("", new)
        new = _HTML_COMMENT_RE.sub("", new)
        new = _FOOTER_HR_RE.sub("", new)
        new = _FOOTER_P_RE.sub("", new)
        new = _EXTERNAL_LINK_RE.sub(r"\2", new)
        new = _REMOTE_IMG_RE.sub("", new)
        if new != content:
            path.write_text(new, encoding="utf-8")


# --------------------------------------------------------------------------- #
#  repackage                                                                   #
# --------------------------------------------------------------------------- #
def _repackage_epub(extract_dir: Path, dest: Path, compresslevel: int = 9) -> None:
    mimetype_file = extract_dir / "mimetype"
    with zipfile.ZipFile(
        dest, "w", zipfile.ZIP_DEFLATED, compresslevel=compresslevel
    ) as zf:
        if mimetype_file.exists():
            zf.write(mimetype_file, "mimetype", compress_type=zipfile.ZIP_STORED)
        for file_path in sorted(extract_dir.rglob("*")):
            if not file_path.is_file() or file_path == mimetype_file:
                continue
            zf.write(file_path, file_path.relative_to(extract_dir))


# --------------------------------------------------------------------------- #
#  entry point                                                                 #
# --------------------------------------------------------------------------- #
def optimize_epub_for_eink(
    epub_path: Path, opts: Optional[EinkOptions] = None
) -> bool:
    """
    Optimise an EPUB in-place for small e-ink readers: grayscale + resize +
    posterize images to the panel resolution (keeping each image's container
    format), strip embedded fonts, drop colour/shadow/animation CSS and any
    scripts, remove calibre's download-source footer and off-device links,
    then recompress the container with maximum deflate.

    Returns True if the file was modified.
    """
    opts = opts or EinkOptions()
    epub_path = Path(epub_path)
    if not epub_path.exists() or epub_path.suffix.lower() != ".epub":
        return False

    original_size = epub_path.stat().st_size
    with tempfile.TemporaryDirectory(prefix="eink_opt_") as tmp:
        extract_dir = Path(tmp)
        with zipfile.ZipFile(epub_path, "r") as zf:
            zf.extractall(extract_dir)

        b = _walk(extract_dir)
        _process_images(b["images"], b["other"], opts)
        if opts.strip_fonts:
            _strip_fonts(b["fonts"], b["opf"], b["css"])
        if opts.strip_css:
            _strip_css(b["css"])
        _clean_html_files(b["html"], opts)

        # atomic in-place replace, on the same filesystem as the epub
        try:
            orig_mode = epub_path.stat().st_mode & 0o777
        except OSError:
            orig_mode = 0o644
        fd, tmp_out = tempfile.mkstemp(
            dir=str(epub_path.parent), prefix=".eink_", suffix=".epub"
        )
        os.close(fd)
        try:
            _repackage_epub(extract_dir, Path(tmp_out), opts.zip_compresslevel)
            os.chmod(tmp_out, orig_mode)
            os.replace(tmp_out, epub_path)
        except Exception:
            try:
                os.unlink(tmp_out)
            except OSError:
                pass
            raise

    new_size = epub_path.stat().st_size
    logger.info(
        "e-ink optimized %s: %d -> %d bytes (%.0f%% reduction)",
        epub_path.name,
        original_size,
        new_size,
        100 * (1 - new_size / original_size) if original_size else 0,
    )
    return True
