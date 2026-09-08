# AGENTS.md

Guidance for AI coding agents working in this repository.

## What this is

A fork of [`ping/newsrack`](https://github.com/ping/newsrack): a static site that
builds e-reader periodicals (EPUB) from [calibre](https://calibre-ebook.com/)
news recipes, publishes them with an OPDS catalogue, and deploys to GitHub Pages
via GitHub Actions.

**This fork's focus** is a single custom periodical, **"Daily Digest"** (recipe
slug `india-opinion`), tuned for the **Xteink X4 / X4 Pro** e-reader running the
**CrossInk / CrossPoint** firmware (ESP32, ~380 KB RAM, 4-level greyscale
SSD1677 panel, 480×800). `_recipes_custom.py` is deliberately pruned so the
build produces only this one book — see "Gotchas".

## Layout

| path | role |
|---|---|
| `_generate.py` | build orchestrator — reads the recipe registry, runs `ebook-convert`, builds the OPDS + HTML index. CLI args are all CI values; run it via `build.sh`, not directly. |
| `_recipes.py` | upstream recipe registry (`Recipe(...)` entries). **Don't edit** — fork changes go in `_recipes_custom.py`. |
| `_recipes_custom.py` | this fork's recipe registry. `_generate.py` uses `custom_recipes or default_recipes`, so a non-empty list here **replaces** `_recipes.py` entirely. |
| `_recipe_utils.py` | `Recipe` / `CoverOptions` dataclasses, `enable_on` schedule helpers, `xteink_conv_options`. |
| `_epub_eink_optimizer.py` | post-processes every generated EPUB for the e-ink target (see below). |
| `recipes/*.recipe.py` | upstream calibre `BasicNewsRecipe` subclasses. **Don't edit.** |
| `recipes_custom/*.recipe.py` | fork recipes. `india-opinion` and `daily-digest-live` are 3-line shims. |
| `recipes/includes/` | shared code imported by recipes via `os.environ['recipes_includes']`. **All Daily Digest logic lives in `recipes/includes/daily_digest.py`** (`class DailyDigestBase`). |
| `tests/` | `unittest` suite — only `tests_recipe_utils.py`. CI runs **no** test step. |
| `static/` | site assets (SCSS/JS) + bundled fonts (`OpenSans-*.ttf`, `NotoEmoji.ttf` used by the Daily Digest cover). |
| `.github/workflows/build.yml` → `build.sh` | the build. |

`*.recipe.py` files are calibre `*.recipe` scripts with a `.py` suffix for editor
support; `build.sh` / the test scripts copy them to `*.recipe` before use.

Files beginning `_` are framework/orchestration, not recipes.

## Setup & commands

Requires `ebook-convert` (calibre) on `PATH`. In this Codespace, calibre 9.14 is
at `/opt/calibre`, symlinked into `/usr/local/bin`. Python deps: `requirements.txt`
(the codespace already has them; calibre runs recipes with its **own** bundled
Python, not the system one).

```bash
# build one recipe to a plain EPUB (no e-ink optimizer)
./test_recipe.sh -r recipes_custom/india-opinion.recipe.py

# build one recipe exactly as CI does: Xteink convert profile + the e-ink
# optimizer. Output -> ./eink_test_output/<name>_<ts>.eink.epub
./test_eink.sh -r recipes_custom/india-opinion.recipe.py

# lint (config in .flake8 / tox.ini — E501/E203/E722/W503 ignored, black style)
flake8 recipes/includes/daily_digest.py _epub_eink_optimizer.py

# tests (see Gotchas — currently red for a pre-existing reason)
python -m unittest discover -s tests
```

Both test scripts `export recipes_includes="$(realpath recipes/includes/)"` —
if you invoke a recipe another way, set that env var yourself or the shim
`import daily_digest` fails.

A full Daily Digest build takes ~2–5 min (Indian Express is pulled through the
Wayback Machine, which is slow). Don't add speculative CI runs while iterating —
prefer `test_eink.sh` locally, then one push.

## The e-ink optimizer (`_epub_eink_optimizer.py`)

Runs on **every** recipe's EPUB in CI (`build.yml` sets `xteink_optimize: "true"`,
which overrides each recipe's own `optimize_for_eink` flag). `_generate.py`
swallows its exceptions — a crash there ships an *un-optimized* book silently,
so verify changes with `test_eink.sh` and check the log line
`e-ink optimized ...: N -> M bytes`.

What it does: fit images to 480×800, map to the 4 panel greys, **write content
images as 2-bit PNG** (rewriting the OPF manifest + `<img>`/`url()` refs),
strip embedded fonts / colour-shadow-animation CSS / scripts, remove calibre's
download footer + off-device links, rebuild calibre's Prev/Articles/Sections/Next
nav table as one small line, fold ligatures, drop OS artifacts, repackage with
`mimetype` first + images stored.

It is **idempotent**: a `/*eink-baseN*/` CSS marker and grey-palette/size
early-returns mean re-running is a no-op. Bump the marker (`eink-base4` →
`eink-base5`) when you change `_CSS_EINK_BASE` so already-processed books pick
it up.

**CrossInk CSS is limited** — its hand-written engine honours single-class and
tag selectors + `!important`, but **ignores descendant combinators**
(`.a .b {…}`). Fix device layout problems in the markup, not with clever CSS.

## Conventions

- Match the surrounding style; `flake8` clean for files you touch (line length
  is not enforced). `_recipes_custom.py` has ~30 pre-existing flake8 warnings —
  don't "fix" unrelated ones.
- Don't commit build output: `output/`, `eink_test_output/`, `public/`, `meta/`,
  `*.epub`, `*.whl` are gitignored.
- Default branch is `main`. Commit/push only when asked. End commit messages with
  `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`.
- The Daily Digest cover and section labels are user-tuned; check screenshots
  before changing spacing/sizing.

## Gotchas

- **Only `india-opinion` builds.** `_recipes_custom.py` ends with
  `recipes = [r for r in recipes if r.recipe == "india-opinion"]`, and that
  recipe is `enable_on=True`. Every other recipe (custom and upstream) is
  intentionally inactive. Don't "fix" this.
- **`tests_recipe_utils` fails** under Python 3.12 because `_recipe_utils.CoverOptions`
  is a mutable dataclass default (`cover_options: CoverOptions = CoverOptions()`).
  Pre-existing, unrelated to recipe work. CI runs on 3.10 where it's fine and
  runs no test step anyway.
- **`recipes/includes/daily_digest.py` is not importable standalone** — it does
  `from calibre.web.feeds.news import ...`. Test it through `ebook-convert` /
  `test_eink.sh`, or by exec'ing individual pure helpers.
- **Thread-safety in `daily_digest.py`**: `parse_index` fans the sources out
  across threads. `self.browser` (mechanize) is not thread-safe — worker fetches
  must use `self._open_bytes()` (clones the browser). Shared state
  (`_url_domain`, `_weekly_newsletters`, `_hindu_submap`, the Chromium worker)
  is guarded by `self._lock`. `preprocess_html` / `preprocess_raw_html` run on
  calibre's download threads.
- **Indian Express** edge-blocks datacentre IPs (CloudFront 403), so its content
  is fetched via Google News discovery + the Wayback Machine. **Substack** feeds
  (Public Policy, IWTK Quiz) 403 too and go through `api.rss2json.com` /
  `feed2json.org`. This is by design, not a bug to remove.
- `default_cover` / the optimizer render inside `ebook-convert`, where CWD may
  differ from the repo root — resolve bundled assets from `__file__`
  (`_STATIC_DIR` in `daily_digest.py`), not relative paths.
- The scheduled build is daily at `0 0 * * *` UTC = **05:30 IST**
  (`build.yml`). Pushes to any branch also trigger a build.
