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
- https://github.com/b1rdmania/epubkit  (the 20-step web optimiser)
- https://github.com/uxjulia/CrossInk    (Xteink X3/X4 CrossPoint firmware fork)
- common e-ink / Kindle / Kobo EPUB slimming guides (grayscale + downscale +
  posterize images, drop embedded fonts, strip colour/shadow CSS, recompress
  the container with max deflate)

CrossInk / CrossPoint compatibility (its EPUB parser is expat + a hand-written
XHTML/CSS renderer on an ESP32 with ~380 KB usable RAM and a 4-level SSD1677
panel), applied here:
- every image fit to the 480x800 panel, mapped to the 4 panel greys
  (0/85/170/255), auto-contrast + a mild boost, alpha flattened onto white
- content images written as 2-bit (4-colour) PNG -- ~3x smaller than the
  equivalent 4-tone JPEG, which only adds DCT noise around the 4 levels;
  the OPF manifest and every <img>/<link>/url() ref are rewritten to match
- any JPEG that is kept stays baseline (never progressive)
- <picture><source> collapsed to the single <img>
- image entries STORED (not deflated) in the zip so the firmware skips an
  inflate pass; mimetype still first and stored
- interaction-only attributes (data-*, aria-*, role, tabindex, itemprop, ...)
  stripped from the markup; Latin ligatures folded to ASCII
- OS artifacts (.DS_Store, __MACOSX, ._*, Thumbs.db) dropped
- embedded fonts removed (the device uses its own EpdFont / SD-card fonts)

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

from PIL import Image, ImageEnhance, ImageFile, ImageOps

# a partially-downloaded image must still open so we can greyscale it
ImageFile.LOAD_TRUNCATED_IMAGES = True

logger = logging.getLogger("newsrack.eink_optimizer")

_FONT_EXTS = {".ttf", ".otf", ".woff", ".woff2", ".eot"}
_RASTER_EXTS = {
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".apng", ".gif",
    ".bmp", ".webp", ".tif", ".tiff", ".avif",
}
_HTML_EXTS = {".xhtml", ".html", ".htm"}

# OS/reader cruft that must never end up inside the repackaged container
# (epubkit / CrossInk "clean OS artifacts" step)
_OS_ARTIFACTS = {
    ".ds_store", "thumbs.db", "desktop.ini", ".spotlight-v100", ".trashes",
}
_OS_ARTIFACT_DIRS = {"__macosx"}


def _is_os_artifact(p: Path) -> bool:
    if p.name.lower() in _OS_ARTIFACTS or p.name.startswith("._"):
        return True
    return any(part.lower() in _OS_ARTIFACT_DIRS for part in p.parts)


# The SSD1677 e-ink controller on the Xteink X3/X4 drives a 4-level greyscale
# panel: black, dark grey, light grey, white. Mapping every image onto exactly
# those tones (with Floyd-Steinberg dithering) is what CrossInk / CrossPoint's
# own optimiser and epubkit do -- it matches what the hardware can actually
# show, keeps files tiny, and avoids the muddy mid-greys a 16-level posterise
# leaves that the panel then has to round anyway.
_EINK_TONES = (0, 85, 170, 255)

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
_CSS_BASE_MARKER = "/*eink-base3*/"
_CSS_EINK_BASE = (
    _CSS_BASE_MARKER
    + "html,body{background:#fff !important;color:#000 !important;}"
    + "img{max-width:100% !important;height:auto !important;"
    # belt-and-braces: even if a reader swaps in its own copy of an image, or
    # this epub was built without the image pass (local test_recipe.sh), the
    # renderer still shows it monochrome.
    + "filter:grayscale(100%) !important;"
    + "-webkit-filter:grayscale(100%) !important;}"
    + "*{text-shadow:none !important;box-shadow:none !important;"
    + "background-image:none !important;}"
    # calibre's inter-article nav bar (Prev/Articles/Sections/Next) renders
    # huge on a 480px panel -- shrink it to a thin strip
    + ".touchscreen_navbar,.touchscreen_navbar td,.touchscreen_navbar a,"
    + ".calibre_navbar,.calibre_navbar a{font-size:55% !important;"
    + "padding:0 2px !important;line-height:1.15 !important;border:0 !important;}"
    + ".touchscreen_navbar,.calibre_navbar{margin:0 0 3px !important;"
    + "border:0 !important;}"
    + ".touchscreen_navbar hr,.calibre_navbar hr{display:none !important;}"
)


@dataclass
class EinkOptions:
    """Optimisation options tuned for the Xteink X3/X4/X4 Pro (800x480 panel)."""

    # the X4 panel is 480x800 portrait -- epubkit's target too
    max_width: int = 480
    max_height: int = 800
    # a cover is the one image allowed to keep portrait proportions
    cover_max_width: int = 480
    cover_max_height: int = 800
    jpeg_quality: int = 75
    grayscale: bool = True
    # CrossInk/epubkit-style tone mapping: map onto the 4 panel greys
    # (0/85/170/255). Dithering looks better on the panel but adds
    # high-frequency noise that balloons JPEG; keep it off for the JPEG path
    # (flat 4-level regions compress *smaller* than the original) and let the
    # SSD1677 controller do its own dither on display.
    eink_quantize: bool = True
    dither: bool = False
    autocontrast: bool = True
    # mild boost so scans / news photos stay legible after 4-level quantise
    # (epubkit uses 1.5; a touch gentler here to protect midtone detail)
    contrast_boost: float = 1.2
    posterize_bits: int = 4  # fallback if eink_quantize is off (16 grey levels)
    png_colors: int = 16  # palette size for PNGs kept as PNG
    # a 4-tone image is ~3x smaller as a 2-bit PNG than as JPEG (JPEG only adds
    # DCT noise around the 4 levels). Convert content JPEGs to PNG and rewrite
    # the manifest / <img> refs. CrossInk has a native PngToBmpConverter.
    jpeg_to_png: bool = True
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
        if _is_os_artifact(p):
            try:
                p.unlink()
            except OSError:
                pass
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


_eink_palette_cache: List[Image.Image] = []


def _eink_palette_image() -> Image.Image:
    """A 4-entry greyscale palette image for `Image.quantize`. The source must
    be RGB for `quantize(palette=...)` to match correctly (an L source silently
    mismaps), so callers convert first."""
    if not _eink_palette_cache:
        pal = Image.new("P", (1, 1))
        pal.putpalette([c for tone in _EINK_TONES for c in (tone, tone, tone)])
        _eink_palette_cache.append(pal)
    return _eink_palette_cache[0]


# nearest-tone lookup table, for the non-dithered path
_EINK_LUT = bytes(
    min(_EINK_TONES, key=lambda t: abs(t - i)) for i in range(256)
)


def _flatten_alpha(im: Image.Image) -> Image.Image:
    """Composite any transparency onto white (JPEG has no alpha, and a black
    fill is what you get otherwise)."""
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(bg, rgba).convert("RGB")
    return im


def _eink_tone(l_img: Image.Image, opts: EinkOptions) -> Image.Image:
    """Autocontrast + gentle contrast boost + quantise an L image to the 4
    panel greys (optionally Floyd-Steinberg dithered). Returns an L image."""
    out = l_img
    if opts.autocontrast:
        try:
            out = ImageOps.autocontrast(out, cutoff=1)
        except OSError:
            pass
    if opts.contrast_boost and abs(opts.contrast_boost - 1.0) > 1e-3:
        out = ImageEnhance.Contrast(out).enhance(opts.contrast_boost)
    if opts.eink_quantize:
        if opts.dither:
            out = out.convert("RGB").quantize(
                palette=_eink_palette_image(), dither=Image.FLOYDSTEINBERG
            ).convert("L")
        else:
            out = out.point(_EINK_LUT)
    elif opts.posterize_bits < 8:
        out = ImageOps.posterize(out, opts.posterize_bits)
    return out


def _grey_palette(im: Image.Image) -> bool:
    pal = im.getpalette() if im.mode == "P" else None
    return not pal or all(
        pal[i] == pal[i + 1] == pal[i + 2]
        for i in range(0, len(pal) - 2, 3)
    )


def _optimize_image(path: Path, opts: EinkOptions):
    """Greyscale + downscale + tone-map `path`. Content JPEGs are rewritten as
    2-bit PNG (see EinkOptions.jpeg_to_png). Returns ``(is_non_colour,
    new_path_or_None)`` -- new_path is set only when the file was renamed."""
    try:
        with Image.open(path) as src:
            src.load()
            src_format = src.format
            im = src.copy()
    except Exception:  # not a decodable image, leave it alone
        return False, None

    fmt = _WRITABLE_FORMATS.get(
        (src_format or path.suffix.lstrip(".").upper()).replace("JPG", "JPEG"),
        None,
    )
    if fmt is None:
        # a format PIL read but cannot write (e.g. AVIF w/o plugin) -- don't
        # risk corrupting the manifest, just try to at least greyscale it
        _grayscale_file(path)
        return False, None

    max_w = opts.cover_max_width if _is_cover(path) else opts.max_width
    max_h = opts.cover_max_height if _is_cover(path) else opts.max_height
    # already-optimised: greyscale (L/LA/1, or a grey-palette PNG we made last
    # run) and already within the panel size -> leave it, keeping the pass
    # idempotent and avoiding a re-encode generation
    if (
        opts.grayscale
        and (im.mode in ("L", "LA", "1")
             or (im.mode == "P" and _grey_palette(im)))
        and im.width <= max_w and im.height <= max_h
    ):
        return True, None

    try:
        has_alpha = im.mode in ("RGBA", "LA", "PA") or (
            im.mode == "P" and "transparency" in im.info
        )
        keep_alpha = has_alpha and fmt in ("PNG", "WEBP")

        new_size = _resize_dims(im.width, im.height, max_w, max_h)
        if new_size != (im.width, im.height):
            im = im.resize(new_size, Image.LANCZOS)

        if not opts.grayscale:
            if im.mode not in ("L", "LA", "RGB", "RGBA"):
                im = im.convert("RGBA" if has_alpha else "RGB")
        elif keep_alpha:
            rgba = im.convert("RGBA")
            alpha = rgba.getchannel("A")
            im = Image.merge("LA", (_eink_tone(rgba.convert("L"), opts), alpha))
        else:
            im = _eink_tone(_flatten_alpha(im).convert("L"), opts)

        to_png = (
            fmt == "JPEG" and opts.grayscale and opts.eink_quantize
            and opts.jpeg_to_png and im.mode in ("L", "1")
        )
        if to_png:
            # 4-tone L image (from _eink_tone) -> 2-bit palette PNG
            out = im.convert("RGB").quantize(
                palette=_eink_palette_image(), dither=Image.NONE)
            target = path.with_suffix(".png")
            out.save(target, format="PNG", optimize=True)
            if target != path:
                try:
                    path.unlink()
                except OSError:
                    pass
                return opts.grayscale, target
            return opts.grayscale, None
        if fmt == "JPEG":
            im.convert("L" if im.mode in ("L", "LA", "1") else "RGB").save(
                path, format="JPEG", quality=opts.jpeg_quality,
                progressive=False, optimize=True,
                subsampling=(0 if im.mode == "RGB" else -1),
            )
        elif fmt == "PNG":
            out = im
            if opts.grayscale and out.mode == "L" and opts.eink_quantize:
                # a 4-tone L image -> a 4-colour palette PNG (tiny). _eink_tone
                # already mapped the pixels onto the 4 tones; this just packs
                # them into a 2-bit palette.
                out = out.convert("RGB").quantize(
                    palette=_eink_palette_image(), dither=Image.NONE)
            elif opts.grayscale and out.mode == "L" and 0 < opts.png_colors < 256:
                out = out.quantize(colors=opts.png_colors,
                                   method=Image.MEDIANCUT)
            out.save(path, format="PNG", optimize=True)
        elif fmt == "GIF":
            (im if im.mode == "L" else im.convert("L")).save(
                path, format="GIF", optimize=True)
        elif fmt == "WEBP":
            (im if im.mode in ("L", "LA") else im.convert("L")).save(
                path, format="WEBP", quality=opts.jpeg_quality)
        else:  # BMP / TIFF -- keep the container, just tone it
            (im if im.mode in ("L", "LA") else im.convert("L")).save(
                path, format=fmt
            )
        return opts.grayscale, None
    except Exception:  # noqa, pylint: disable=broad-except
        logger.exception("Unable to optimise image %s for e-ink", path)
        if opts.grayscale:
            _grayscale_file(path)
        return False, None


def _process_images(images: List[Path], other: List[Path],
                    opts: EinkOptions) -> Dict[str, str]:
    """Optimise every image; then a cheap belt-and-braces sweep that greyscales
    anything colour that slipped through (odd extension, format PIL couldn't
    round-trip, ...). Returns ``{old filename: new filename}`` for the JPEGs
    rewritten as PNG, so the manifest / markup can be updated."""
    handled: Dict[Path, bool] = {}
    renames: Dict[str, str] = {}
    live: List[Path] = []
    if images:
        workers = min(opts.image_workers, len(images))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(lambda p: _optimize_image(p, opts), images))
        for path, (ok, new_path) in zip(images, results):
            if new_path is not None and new_path != path:
                renames[path.name] = new_path.name
                handled[new_path] = ok
                live.append(new_path)
            else:
                handled[path] = ok
                live.append(path)

    if not opts.grayscale:
        return renames

    for p in live + [q for q in other if _sniff_image(q)]:
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
    return renames


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
# interaction-only attributes that just cost the 380 KB-RAM firmware parse time
# (epubkit / CrossInk strip data-*, aria-*, role, tabindex, ...). Applied only
# inside opening tags (see _strip_junk_attrs) and only in their `name="value"`
# form, so prose like "the role of" is never touched.
_OPEN_TAG_RE = re.compile(r"<[a-zA-Z][^>]*>")
_JUNK_ATTR_RE = re.compile(
    r"""\s(?:data-[\w:.-]+|aria-[\w-]+|role|tabindex|contenteditable|draggable|"""
    r"""spellcheck|autocapitalize|itemprop|itemscope|itemtype|longdesc)"""
    r"""\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'>]+)""",
    re.IGNORECASE,
)


def _strip_junk_attrs(html: str) -> str:
    return _OPEN_TAG_RE.sub(lambda m: _JUNK_ATTR_RE.sub("", m.group(0)), html)
# Latin ligature codepoints -> ASCII, so books render on fonts (EpdFont / SD
# fonts) that have no ligature glyphs
_LIGATURES = {
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi",
    "ﬄ": "ffl", "ﬅ": "ft", "ﬆ": "st",
}
_LIGATURE_RE = re.compile("[" + "".join(_LIGATURES) + "]")
# <picture><source ...> — the firmware only wants a single <img>; a leftover
# <source> with a remote/responsive srcset can shadow it and render nothing
_SOURCE_RE = re.compile(r"<source\b[^>]*/?>", re.IGNORECASE)


def _rewrite_image_refs(text_files: List[Path], renames: Dict[str, str]) -> None:
    """After JPEGs were rewritten as PNG, fix every reference to them: manifest
    hrefs, <img src>/srcset, CSS url(), NCX. Also flips the OPF media-type of
    the renamed items to image/png."""
    if not renames:
        return
    # calibre gives every image in a book a unique basename, so matching refs
    # by filename (not full path) is safe and covers href/src/srcset/url() alike
    subs = [
        (re.compile(r"(?<![\w.\-])" + re.escape(old) + r"(?![\w])"), new)
        for old, new in renames.items()
    ]
    for path in text_files:
        content = _read_text(path)
        if content is None:
            continue
        new = content
        for rx, repl in subs:
            new = rx.sub(repl, new)
        if path.suffix.lower() == ".opf":
            new = re.sub(
                r'(<item\b[^>]*\bhref="[^"]*\.png"[^>]*\bmedia-type=")'
                r'image/jpe?g(")', r"\1image/png\2", new, flags=re.IGNORECASE)
            new = re.sub(
                r'(<item\b[^>]*\bmedia-type=")image/jpe?g("[^>]*\bhref="'
                r'[^"]*\.png")', r"\1image/png\2", new, flags=re.IGNORECASE)
        if new != content:
            path.write_text(new, encoding="utf-8")


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
        new = _SOURCE_RE.sub("", new)
        new = _strip_junk_attrs(new)
        if _LIGATURE_RE.search(new):
            new = _LIGATURE_RE.sub(lambda m: _LIGATURES[m.group(0)], new)
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
            if _is_os_artifact(file_path):
                continue
            arc = file_path.relative_to(extract_dir)
            # already-compressed payloads: store, so the RAM-constrained
            # firmware doesn't spend an inflate pass on them
            ct = (
                zipfile.ZIP_STORED
                if file_path.suffix.lower() in _RASTER_EXTS
                else zipfile.ZIP_DEFLATED
            )
            zf.write(file_path, arc, compress_type=ct)


# --------------------------------------------------------------------------- #
#  entry point                                                                 #
# --------------------------------------------------------------------------- #
def optimize_epub_for_eink(
    epub_path: Path, opts: Optional[EinkOptions] = None
) -> bool:
    """
    Optimise an EPUB in-place for small e-ink readers (Xteink X3/X4 running the
    CrossPoint / CrossInk firmware): grayscale + resize to the 480x800 panel +
    map onto the 4 SSD1677 greys, content images rewritten as 2-bit PNG (with
    the manifest / markup refs updated), alpha flattened onto white; strip
    embedded fonts, drop colour/shadow/animation CSS and any scripts, remove
    calibre's download-source footer and off-device links, strip
    interaction-only markup attributes, fold Latin ligatures, drop OS
    artifacts, then repackage with mimetype first + image entries stored so the
    RAM-constrained firmware skips an inflate pass.

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
        renames = _process_images(b["images"], b["other"], opts)
        if opts.strip_fonts:
            _strip_fonts(b["fonts"], b["opf"], b["css"])
        if opts.strip_css:
            _strip_css(b["css"])
        _clean_html_files(b["html"], opts)
        if renames:
            ncx = [p for p in b["other"] if p.suffix.lower() == ".ncx"]
            _rewrite_image_refs(
                b["opf"] + b["html"] + b["css"] + ncx, renames)

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
