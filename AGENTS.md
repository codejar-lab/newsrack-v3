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
nav table as one small line, **rewrite the book-level index-of-feeds
`<table class="toc">` and each feed's article list as a bulleted `<ul><li>`**
(`_shrink_toc_table` / `_shrink_article_summary`, see below; the latter also
drops calibre's own broken one-character article "summaries"), collapse runs
of 2+ `<br>` down to one, **normalize every CSS rule's vertical margin so
`margin-top` is always `0` and `margin-bottom` carries the full value**
(`_normalize_margins`, see below), fold ligatures, drop OS artifacts,
repackage with `mimetype` first + images stored.

It is **idempotent**: a `/*eink-baseN*/` CSS marker and grey-palette/size
early-returns mean re-running is a no-op. Bump the marker (`eink-base4` →
`eink-base5`) when you change `_CSS_EINK_BASE` so already-processed books pick
it up.

**CrossInk CSS is limited** — its hand-written engine honours single-class and
tag selectors + `!important`, but **ignores descendant combinators**
(`.a .b {…}`). Fix device layout problems in the markup, not with clever CSS.

**A run of 2+ `<br>` renders as a full extra blank line, not a small gap.**
CrossInk's layout engine (`ChapterHtmlSlimParser::startNewTextBlock`, the
`fromBrElement` / "empty `<br>` block" case) treats a second consecutive
`<br>` landing on an already-empty `<br>`-created block as a deliberate
scene/section break, and injects a full line height of blank space on top of
the next block's own margin. That's correct for a real "\* \* \*" break, but
newsletters (Zerodha's Daily Brief, Finshots, ...) routinely use `"<br><br>"`
as a plain inline paragraph separator — on-device that reads as a huge,
unintended gap between paragraphs. `_clean_html_files` collapses any run of
2+ `<br>` to a single one (`_MULTI_BR_RE`) so it renders as an ordinary line
break instead. A single `<br>` is unaffected (it only starts a new, normal
block — the oversized-gap path needs a *second* `<br>` landing on that
still-empty block). Don't try to "fix" this with margin/line-height CSS on
the surrounding tags — see the descendant-selector limitation above; fix the
`<br>` run itself, in the markup.

**CrossInk does not collapse adjacent vertical margins.** A browser merges
touching `margin-bottom` + `margin-top` into the larger of the two;
`ChapterHtmlSlimParser::makePages` (and the equivalent code for `<hr>`) just
adds both, in full, every time. An ordinary `p{margin:1em 0}` (top *and*
bottom both 1em, completely unremarkable Calibre output) renders as a 2em gap
between two paragraphs, and a paragraph → `<hr>` → heading transition (each
with its own top+bottom margin) stacks into 3+ em of blank space — the
"huge gap between paragraphs" bug. `_strip_css` → `_normalize_margins` fixes
this at the CSS level: every rule's `margin`/`margin-top`/`margin-bottom` is
rewritten so `margin-top` is always `0` and `margin-bottom` carries the
larger/only original value (falling back to the old top value when no
bottom was ever set) — the standard "spacing lives only in margin-bottom"
convention used by any non-collapsing renderer (this is also why the emails
you get from every SaaS product use it). `<hr>` isn't left bare: CrossInk's
own `emitHorizontalRule` substitutes a sensible default (half a line height)
whenever an hr's `margin-top` resolves to `0`, so zeroing it doesn't remove
its gap, it just stops that gap from *adding* to the next element's own
margin. This can't be done as a handful of added override rules in
`_CSS_EINK_BASE` — Calibre's own per-element classes (`.calibre9`,
`.calibre12`, ...) always win over a same-property bare-tag rule
(`resolveStyle()` applies tag → class → tag.class in that fixed priority
order, regardless of source order or `!important`), and those class names
are arbitrary/regenerated on every build. The existing declared values have
to be rewritten in place instead.

**Links inside an HTML `<table>` are untappable on a CrossInk touch device
(X4 Pro, Sticky) — don't emit one for anything meant to be tapped.**
CrossInk's touch-tap hit-testing (`EpubReaderActivity::buildFootnoteTouchTargets`)
only builds a tappable hit-box for links in ordinary paragraph/line content;
a link rendered inside a `<table>` renders through a different page-layout
path and gets a permanently zero-sized touch target there — visually
present, silently untappable, and the tap falls through to an ordinary
page-turn (looks like it "did nothing" or "reloaded the page"). This is
fixable in firmware, but per explicit instruction that fix does not live in
the CrossInk repo (see its own `AGENTS.md` > "Touch Input Gotchas") — instead
every `<table>` that calibre generates with real navigation links in it gets
rewritten here, in `_clean_html_files`, into a bulleted `<ul><li>` list: the
per-article Prev/Articles/Sections/Next navbar table (`_shrink_navbar`,
matches `class="touchscreen_navbar"` / `"calibre_navbar"`, still a single
compact line, not a bulleted list — it's a breadcrumb, not a list of things)
and the book-level index-of-feeds table (`_shrink_toc_table`, matches
`class="toc"` → one `<li>` per section, its article count folded into the
link itself as `Label [N]` so the whole thing — bullet, label, count — reads
and taps as one unit; square brackets, not round, so it never gets confused
for the "(some text)" that occasionally appears in a section's own label).
CrossInk draws a real "•" bullet for every `<li>` regardless of CSS
(`ChapterHtmlSlimParser`'s block-tag handling for `"li"`; `CssDisplay` has no
`list-item` value, so this isn't CSS-driven, it's hardcoded per-`<li>`
markup). If calibre/a future recipe ever adds another table with real links
in it, it needs the same treatment here — don't assume a new table is
automatically fine just because the known ones are handled.

**Calibre's per-feed article-index "summary" is always garbage — drop it,
don't try to fix it.** Each feed's own index page lists its articles as
`<div class="article_summary"><a class="summary_headline" href="...">Title</a>
<div class="summary_text">...</div></div>`; the `summary_text` is calibre's
own truncation of the article's opening text down to a single character plus
an ellipsis ("W…", "A…", "P…", ...) — a calibre/feed-parsing artifact in the
upstream conversion, not something reachable from this repo's recipe code or
fixable with CSS. `_shrink_article_summary` drops it entirely and rewrites
each entry as a bare `<li><a href="...">Title</a></li>` (see above), grouped
into one `<ul>` per feed by `_wrap_einklist_runs` (using a throwaway
`class="einklist"` marker so the wrap only ever touches list items *this*
function just created, never a real list an article's own body might
contain).

**Found and fixed: tapping an article's title sometimes landed on the book's
main index (or a neighbouring feed) instead of opening that article.** Not a
firmware bug and not a bad `href` (both were checked and ruled out first).
The actual cause: `.eink-nav`'s `font-size:60%` never really shrinks anything
on-device (dead CSS — see the CrossInk CSS-limits note above), so this
"compact" breadcrumb renders at full body text size and can wrap across 2-3
lines for a long feed name (e.g. "NL: The Daily Brief (Zerodha)"). Combined
with CrossInk's fixed 48px minimum touch-target size
(`TOUCH_FOOTNOTE_TARGET_SIZE`, which pads every link's tap zone ~24px on
every side regardless of the link's own rendered height), the nav's own
padded tap zone reached down far enough to overlap the very next link on the
page (the first article title, or the heading right above it) — so a tap
aimed at the article could land on "Sections" (or the neighbouring-feed
link) in the nav instead. Confirmed live with scripted taps in the
simulator, isolating clean single-tap repro cases (see git history for the
`CROSSPOINT_SIM_INPUT_SCRIPT` sequences used) that showed the exact boundary
moving as the fix below was applied — this is what "confirm it live" (the
previous version of this note) actually turned up.

Fixed with two changes to `_shrink_navbar`, neither touching firmware:
1. **Truncate every nav label** to `_NAV_LABEL_MAX_LEN` (16) characters with
   a trailing `…`, so the breadcrumb stays short regardless of the
   underlying title length and is far less likely to wrap.
2. **Frame the nav with a bare `<hr/>` on each side.** An `<hr>` has no
   `href` (no touch target of its own to interfere with anything), but
   CrossInk's `emitHorizontalRule` gives it a real default margin (half a
   line height, each side) when none is set in CSS — genuine physical
   separation from whatever precedes/follows, independent of the
   truncation. This is what actually closed the gap: truncation alone
   (shorter text, same margins) was not enough on its own to stop the
   overlap, since the 48px minimum pad can still exceed a single short
   line's own margin.

If a similar "wrong destination" symptom shows up elsewhere (a different
list, a different breadcrumb), suspect this same mechanism first — an
undersized margin next to *any* link, not a href/navigation bug — before
re-litigating firmware link resolution.

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
