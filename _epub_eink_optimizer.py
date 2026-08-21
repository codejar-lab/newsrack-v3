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

Unlike those projects (separate watcher/Docker services that post-process an
EPUB after the fact), this runs in-process as the last step of the existing
newsrack generation pipeline, so no extra service is needed.
"""
import logging
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Set

from PIL import Image, ImageOps

logger = logging.getLogger("newsrack.eink_optimizer")

_FONT_EXTS = {".ttf", ".otf", ".woff", ".woff2"}
_RASTER_EXTS = {".jpg", ".jpeg", ".png", ".gif"}


@dataclass
class EinkOptions:
    """Optimisation options tuned for the Xteink X3/X4/X4 Pro (800x480 panel)."""

    max_width: int = 800
    max_height: int = 480
    jpeg_quality: int = 75
    grayscale: bool = True
    posterize_bits: int = 4  # 2**4 = 16 grey levels, plenty for e-ink
    strip_fonts: bool = True


def _resize_dims(w: int, h: int, max_w: int, max_h: int):
    if w <= max_w and h <= max_h:
        return w, h
    ratio = min(max_w / w, max_h / h)
    return max(1, int(w * ratio)), max(1, int(h * ratio))


def _optimize_image(path: Path, opts: EinkOptions) -> None:
    try:
        with Image.open(path) as im:
            fmt = im.format
            if fmt not in ("JPEG", "MPO", "PNG", "GIF"):
                return

            has_alpha = im.mode in ("RGBA", "LA") or (
                im.mode == "P" and "transparency" in im.info
            )

            if opts.grayscale:
                im = im.convert("LA" if has_alpha else "L")

            new_size = _resize_dims(im.width, im.height, opts.max_width, opts.max_height)
            if new_size != (im.width, im.height):
                im = im.resize(new_size, Image.LANCZOS)

            if opts.grayscale and opts.posterize_bits < 8:
                if im.mode == "LA":
                    l_channel, a_channel = im.split()
                    l_channel = ImageOps.posterize(l_channel, opts.posterize_bits)
                    im = Image.merge("LA", (l_channel, a_channel))
                elif im.mode == "L":
                    im = ImageOps.posterize(im, opts.posterize_bits)

            if fmt in ("JPEG", "MPO"):
                if im.mode in ("LA", "RGBA"):
                    im = im.convert("L" if im.mode == "LA" else "RGB")
                im.save(path, format="JPEG", quality=opts.jpeg_quality, progressive=False)
            elif fmt == "PNG":
                im.save(path, format="PNG", optimize=True)
            elif fmt == "GIF":
                im.convert("L").save(path, format="GIF")
    except Exception:  # noqa, pylint: disable=broad-except
        logger.exception("Unable to optimise image %s for e-ink, leaving as-is", path)


def _strip_fonts(extract_dir: Path) -> None:
    removed_hrefs: Set[str] = set()
    for font_file in list(extract_dir.rglob("*")):
        if font_file.is_file() and font_file.suffix.lower() in _FONT_EXTS:
            removed_hrefs.add(font_file.name)
            font_file.unlink()

    if not removed_hrefs:
        return

    for opf_file in extract_dir.rglob("*.opf"):
        content = opf_file.read_text(encoding="utf-8")
        for href in removed_hrefs:
            content = re.sub(
                rf'<item\b[^>]*href="[^"]*{re.escape(href)}"[^>]*/?>\s*',
                "",
                content,
            )
        opf_file.write_text(content, encoding="utf-8")

    for css_file in extract_dir.rglob("*.css"):
        content = css_file.read_text(encoding="utf-8")
        for href in removed_hrefs:
            content = re.sub(
                rf"@font-face\s*\{{[^{{}}]*{re.escape(href)}[^{{}}]*\}}",
                "",
                content,
            )
        css_file.write_text(content, encoding="utf-8")


def _repackage_epub(extract_dir: Path, dest: Path) -> None:
    mimetype_file = extract_dir / "mimetype"
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        if mimetype_file.exists():
            zf.write(mimetype_file, "mimetype", compress_type=zipfile.ZIP_STORED)
        for file_path in sorted(extract_dir.rglob("*")):
            if not file_path.is_file() or file_path == mimetype_file:
                continue
            zf.write(file_path, file_path.relative_to(extract_dir))


def optimize_epub_for_eink(
    epub_path: Path, opts: EinkOptions = EinkOptions()
) -> bool:
    """
    Optimise an EPUB in-place for small e-ink readers: grayscale + resize +
    posterize images to the panel resolution, re-encode JPEGs as baseline,
    and strip embedded fonts (readers like the Xteink use their own).

    Returns True if the file was modified.
    """
    epub_path = Path(epub_path)
    if not epub_path.exists() or epub_path.suffix.lower() != ".epub":
        return False

    original_size = epub_path.stat().st_size
    with tempfile.TemporaryDirectory(prefix="eink_opt_") as tmp:
        extract_dir = Path(tmp)
        with zipfile.ZipFile(epub_path, "r") as zf:
            zf.extractall(extract_dir)

        image_files: List[Path] = [
            p
            for p in extract_dir.rglob("*")
            if p.is_file() and p.suffix.lower() in _RASTER_EXTS
        ]
        for image_file in image_files:
            _optimize_image(image_file, opts)

        if opts.strip_fonts:
            _strip_fonts(extract_dir)

        optimized_path = extract_dir.parent / f"{epub_path.stem}.optimized.epub"
        _repackage_epub(extract_dir, optimized_path)
        shutil.move(str(optimized_path), str(epub_path))

    new_size = epub_path.stat().st_size
    logger.info(
        "e-ink optimized %s: %d -> %d bytes (%.0f%% reduction)",
        epub_path.name,
        original_size,
        new_size,
        100 * (1 - new_size / original_size) if original_size else 0,
    )
    return True
