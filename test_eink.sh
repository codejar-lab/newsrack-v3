#!/bin/bash
# Build a recipe straight to EPUB with the Xteink X3/X4/X4 Pro conversion
# profile, then run the in-pipeline e-ink optimizer (_epub_eink_optimizer.py)
# on it -- i.e. exactly what CI produces for a Recipe(optimize_for_eink=True,
# conv_options=xteink_conv_options). Use this to eyeball the small-device epub
# locally.
#
# Usage: ./test_eink.sh -r recipes_custom/india-opinion.recipe.py

set -euo pipefail

OUTPUT_DIR="./eink_test_output"
RECIPE_PATH=""

usage() {
    echo "Usage: $0 -r <recipe.recipe.py> [-o <output_dir>]"
}

while [[ $# -gt 0 ]]; do
    case $1 in
        -r|--recipe) RECIPE_PATH="$2"; shift 2 ;;
        -o|--output) OUTPUT_DIR="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1"; usage; exit 1 ;;
    esac
done

if [[ -z "$RECIPE_PATH" || ! -f "$RECIPE_PATH" ]]; then
    echo "Error: valid recipe file required (-r)"; usage; exit 1
fi

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
say() { echo -e "${1}${2}${NC}"; }

export recipes_includes="$(realpath recipes/includes/)"

# wipe the output dir first: the un-optimised intermediate is colour + full
# size and has bitten us repeatedly when a reader picks it up by mistake
rm -rf "$OUTPUT_DIR"
mkdir -p "$OUTPUT_DIR"
RECIPE_NAME="$(basename "$RECIPE_PATH" .recipe.py)"
TS="$(date +%Y%m%d_%H%M%S)"
TEMP_RECIPE="/tmp/${RECIPE_NAME}.recipe"
RAW_EPUB="$(mktemp /tmp/${RECIPE_NAME}_raw.XXXXXX.epub)"
EINK_EPUB="${OUTPUT_DIR}/${RECIPE_NAME}_${TS}.eink.epub"

cp "$RECIPE_PATH" "$TEMP_RECIPE"

# --- Xteink X3/X4/X4 Pro conversion profile (mirrors xteink_conv_options) ---
say "$GREEN" "Converting recipe -> EPUB (Xteink profile)..."
ebook-convert "$TEMP_RECIPE" "$RAW_EPUB" \
    --output-profile=tablet \
    --extra-css="img{height:auto !important;}" \
    --font-size-mapping="8,10,12,14,16,18,20,22" \
    --epub-max-image-size=800x800 \
    --no-svg-cover \
    --filter-css=color,background,background-color \
    -v
rm -f "$TEMP_RECIPE"

cp "$RAW_EPUB" "$EINK_EPUB"

say "$GREEN" "Running _epub_eink_optimizer.optimize_epub_for_eink ..."
python3 - "$EINK_EPUB" <<'PY'
import sys, logging
from pathlib import Path
logging.basicConfig(level=logging.INFO, format="%(message)s")
from _epub_eink_optimizer import EinkOptions, optimize_epub_for_eink
optimize_epub_for_eink(Path(sys.argv[1]), EinkOptions())
PY

# sanity: report any colour images left in the optimised epub
python3 - "$EINK_EPUB" <<'PY'
import sys, zipfile
from PIL import Image
import io
bad = []
with zipfile.ZipFile(sys.argv[1]) as z:
    for n in z.namelist():
        try:
            im = Image.open(io.BytesIO(z.read(n)))
        except Exception:
            continue
        pal = im.getpalette()
        grey_pal = not pal or all(pal[i] == pal[i+1] == pal[i+2] for i in range(0, len(pal), 3))
        if im.mode not in ("L", "LA", "1") and not (im.mode == "P" and grey_pal):
            bad.append(f"{n} [{im.mode}]")
print(f"colour images remaining: {len(bad)}")
for b in bad:
    print("  " + b)
PY

# the raw intermediate lives in /tmp and is deleted now -- $OUTPUT_DIR holds
# exactly one file, the optimised e-ink epub
rm -f "$RAW_EPUB"

eink_sz=$(du -h "$EINK_EPUB" | cut -f1)
say "$GREEN" "Done."
say "$YELLOW" "  eink EPUB : $EINK_EPUB ($eink_sz)"
say "$GREEN" "  ($OUTPUT_DIR contains only this file)"
