# AGENTS.md

Guidance for AI coding agents working in this repository.

## What this is

A fork of [`ping/newsrack`](https://github.com/ping/newsrack): a static site that
builds e-reader periodicals (EPUB) from [calibre](https://calibre-ebook.com/)
news recipes, publishes them with an OPDS catalogue, and deploys to GitHub Pages
via GitHub Actions.

**This fork's focus** is a single custom periodical, **"Daily Digest"** (recipe
slug `india-opinion`), tuned for the **Xteink X4 Pro** e-reader (touch) running
**CrossInk 1.6.1** firmware (ESP32, ~380 KB RAM, 4-level greyscale
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
(X4 Pro, Sticky) — this is intentional here, do not "fix" it by converting
tables to lists.** CrossInk's touch-tap hit-testing
(`EpubReaderActivity::buildFootnoteTouchTargets`) only builds a tappable
hit-box for links in ordinary paragraph/line content; a link rendered inside
a `<table>` gets a permanently zero-sized touch target there — visually
present, silently untappable, and the tap falls through to an ordinary
page-turn.

A prior version of this optimizer (commit `841b3a0`, reverted by
`git revert` on 2026-09-13) rewrote both offending tables — the book-level
`class="toc"` index-of-feeds table (`_shrink_toc_table`) and each feed's
`class="article_summary"` article list (`_shrink_article_summary` +
`_wrap_einklist_runs`) — into bulleted `<ul><li>` lists specifically to make
their links tappable. **That fix caused a worse regression: sleep/wake
resume broke for any book navigated via those newly-tappable links.**
Confirmed both in the simulator and against two real user-downloaded EPUBs
(9 Sept build, pre-fix table markup, resumed correctly after sleep; 13 Sept
build, post-fix list markup, reset to the book index after every
sleep/wake). Five separate simulator tests (tap-then-sleep, tap with a
23s dwell before sleep, tap-then-pageturn-then-sleep, control page-turn-only
navigation on both old and new markup, and stripping `toc.ncx` navPoints
down to top-level-only) all isolated the same mechanism: a position reached
via `navigateToHref()` (which any tapped internal link goes through,
regardless of whether it came from a table or a list) does not get persisted
before a sleep/reboot the way a position reached via sequential
`nextPage()`/`prevPage()` does. That is a firmware-internal gap in
`EpubReaderActivity`'s resume-position persistence, not fixable from EPUB
content — and per this repo's explicit standing constraint, firmware is not
touched to fix it. Since the untappable `<table>` markup incidentally
prevents `navigateToHref` jumps into these pages in the first place (users
can only reach them by sequential page-turning, which always persists
correctly), reverting to plain tables was the working trade-off: correct
sleep/wake resume, at the cost of these two navigation aids not being
tap-driven.

**Update (2026-09-15): the book-level toc table is now dropped entirely
(`_TOC_TABLE_RE.sub("", new)`), not shown as a list.** A same-day
intermediate version rewrote it into a plain-text, non-link `<ul><li>`
list first — that was itself safe (no `<a href>` at all, so zero
possibility of a `navigateToHref()` call from that page, meaning the
resume-persistence gap above could never be triggered from it) — but the
list was still just a redundant first page (every other page already has
a "Sections" breadcrumb link back to it), so it's removed outright now
instead: the book's first page is just masthead + date, and the reader
reaches content by paging forward. If a book-level "Sections" *list* is
ever wanted again, the safe non-link `<ul><li>` version (see git history
around this date) is the pattern to reuse — do NOT add `href` back onto
it, or reintroduce `_shrink_article_summary`'s tappable list, unless the
firmware's `navigateToHref` resume-persistence gap is fixed first; that
specific combination (a link + a tap into it) is what reintroduces the
regression, not list markup by itself.

**Also as of 2026-09-15: a feed's own "article index" stub page inlines
that feed's headline list when its one linked article carries one.**
calibre auto-generates one such stub per feed —
`<div class="article_summary"><a class="summary_headline" href="...">
Title</a></div>`, one entry per calibre "article" in that feed. A recipe
that merges many cards into a single calibre article per feed (e.g.
`inshorts.recipe.py`'s one-category-= one-chapter chapters, see its
`_merge_section`) ends up with exactly *one* such entry, titled the same
as the chapter — a wasted extra page/tap telling the reader nothing new
before the real content, which already opens with its own plain-text
`<ul><li class="hl">` headline list. `_inline_single_article_summary`
(wired into `_clean_html_files` via a `_collect_headline_lists`
pre-scan of every HTML file for that `hl`-class list) detects when the
stub's one link points at a page carrying that list and splices the list
in verbatim (normalizing away calibre's own `class="calibreN"` stamps on
the `<ul>`/`<li>` it adds during conversion) in place of the link — same
non-link reasoning as the toc page above.

The list is *moved*, not copied: `_collect_consumed_headline_targets`
tracks which chapter files actually had their list inlined onto some
stub page, and `_clean_html_files` strips that same list (plus its
trailing divider `<hr/>`, if any) back out of the chapter file itself
right after — otherwise it rendered twice, once on the stub page and
again at the top of the chapter's own content, which is what "duplicate
list" feedback caught on the first version of this fix. A feed with
several genuinely distinct calibre articles (the normal case for other
recipes, e.g.
daily_digest's per-newsletter feeds) doesn't carry that `hl` list on its
target(s) and is left completely untouched.

The malformed-XHTML bug this fix also carried (`_ARTICLE_SUMMARY_RE`'s
non-greedy `(.*?)</div>` stopping at the first `</div>` — the nested
`summary_text` div's own close — instead of the true outer close) no longer
applies now that `_shrink_article_summary` itself is reverted; the general
lesson still stands for any future regex here: **verify a "capture until the
next `</tag>`" regex against real generator output with an XML parser (e.g.
`xml.dom.minidom.parse()`), not just one eyeballed sample, whenever the
source template might nest another same-named or same-shaped element
inside.**

**Update (2026-09-15): calibre's `summary_text` truncation artifact
("M…", "W…", ...) is now stripped from real (multi-article)
`article_summary` entries too.** `daily_digest`'s per-newsletter feeds
never went through `_shrink_article_summary` (reverted) or
`_inline_single_article_summary`'s single-link path — they carry several
genuinely distinct calibre articles, so both of those leave them alone —
which meant the broken one-character `summary_text` div (calibre's own
truncation of the article's opening text, always garbage, see the
"Calibre's per-feed article-index summary" note this repo used to carry)
was still visible on-device: a headline followed by a lone "M…" or
similar. `_SUMMARY_TEXT_RE` strips just that inner `<div
class="summary_text">...</div>` (a leaf element, so a non-greedy match to
its own next `</div>` is safe, unlike `article_summary`'s outer close
above) leaving the real `<a class="summary_headline">` link and its
surrounding `<div class="article_summary">` completely untouched — this
doesn't add, remove, or convert any link, so it carries none of the
navigateToHref/resume risk documented above.

**Update (2026-10-21): reading-experience pass.** The Prev/Articles/
Sections/Next bar is now *removed* (`_NAVBAR_RE.sub("")`, was
`_shrink_navbar`) — reading is sequential page turns and tapped links hit the
resume gap above. Each feed keeps one section page (`_simplify_feed_stub_pages`): it is what gives
the table of contents its section rows, because CrossInk names a spine file
after the FIRST toc entry pointing at it (`BookMetadataCache` spineToTocIndex),
so a section row needs a page of its own — repointing the section at its first
article (or dropping the stub) made that article show the section name, or no
section row at all. calibre's tappable headline list is rebuilt as the section
name plus a plain-text headline list (no links; stubs carrying an inlined `hl`
list, i.e. Inshorts, are left alone). `_ensure_leading_toc_entry` gives the
cover/masthead page a toc entry (else the footer says "Unnamed"). All article
images are kept; `daily_digest` only aborts photo-only items under
`MIN_ARTICLE_WORDS`. CSS: `text-align:left` is in the base rules but CrossInk's
reader setting "Paragraph alignment" (default Justified) overrides book CSS
unless set to "Book's Style"; `_clamp_large_sizes` caps font-size at 1.25em and
em bottom margins at 0.6em in place ; `<pre>`/`<code>`
become plain paragraphs. IE article headers carry `x-title` (1.45em) / `x-summary` (0.65em: font sizes
land on whole points, 0.85em/0.75em of 14pt look like body text; CrossInk
ignores italic, margin-left and text-align on it), and photo captions are
rebuilt as `p.x-cap` (a bare caption `<span>` renders LARGER than body text)
(calibre's generated copies of those rules are removed; marker `eink-base9`). Titles are
`<Name> - DD-MM-YYYY`; CrossInk saves a catalogue download as `<title>.epub`
when the OPDS entry has no `<author>` and its file browser sorts names
ascending/number-aware, so OPDS entry titles lead with the
`_countdown` number and carry no author. In `daily_digest`: newsletters come
first (then the newspaper pages), with short names (no `NL:`); newsletter issues over
`SPLIT_MIN_WORDS` split into one chapter per story (`_split_stories`); a heading repeating the title is dropped. Published
books are named `<366-day-of-year>-<date>-<slug>.epub` (`_generate.book_stem`)
so the newest sorts first in a per-year folder.


**Update (2026-10-22): content-cleaning pass from a per-feed review.** Gotcha
first: **never give an element a class in the recipe that has no CSS rule at
calibre time** — calibre's CSS flattening reuses an original class name for
every identically-styled element, so `class="x-summary"` landed on nearly every
paragraph in the book (and its 0.65em rule would have shrunk all text). Header
elements are marked by stable `id` (`translation-title-*`, `translation-excerpt-*`,
`xcap-N`) and `_mark_header_classes` adds `x-title`/`x-summary`/`x-cap` in the
optimizer, after calibre. Cleaning added to `daily_digest.py`: `_scrub_text`
(zero-width chars incl. in `alt`, `(NYT $)` tags, `(Chart 1)`, footnote `*`,
literal backslash-n, `word.Next` joins, WOTD `//` and backslash pronunciation,
"views are personal"; external links are unwrapped first because
`(<a>Reuters</a> $)` splits the text), `_strip_cta_blocks` (sponsor block ending
in a lone "Get the Report"), `_cut_tails` (legal/SEBI footers, "Related stories",
sign-offs end the article), `_drop_echoes` (standfirst repeated in body/caption),
`_strip_bylines_and_bios`, `_unwrap_tweets`, `_strip_edition_intro` (Daily Brief
"In today's edition..."), Devanagari-only paragraphs dropped, Hindu "fifty years
ago" archive wire items aborted, brand suffix stripped from newsletter titles,
and the Daily Brief's "Beyond Today's Brief" promo chapter dropped (heading text
carries `&#8217;`, so unescape before matching). Images are NOT dropped (explicit
requirement). Known gaps: a mid-article teaser+image that is not repeated in a
Related block; quiz answers that sit in their own tags; Letters subject lines
are unstyled.

**Also as of 2026-09-15: `daily_digest`'s newsletter articles no longer
repeat across consecutive daily builds.** `NEWSLETTER_MAX_AGE_DAYS`
(1.15 days) is wider than the ~1 day between builds, so an entry posted
late in one day's window could still look "fresh" the next day too — the
same newsletter entry (same url) would show up in two consecutive
epubs. `parse_newsletters` now persists every included article's url
(with the date it was included) to `meta/daily_digest_seen_urls__
<RecipeClassName>.json` and filters against it on every run, dropping
anything already published in a past build. `meta/` is the CI job's own
cross-run artifact folder — already downloaded at the start of every
GitHub Actions run and re-uploaded at the end (see
`.github/workflows/build.yml`'s "meta-artifacts" steps and
`_generate.py`'s `meta_folder`, previously used there only for job-log
state) — so this needed no new CI wiring, secrets, or permissions.
Records are pruned after `_SEEN_URLS_RETENTION_DAYS` (14, comfortably
longer than the freshness window) so the file never grows unbounded.
Scoped per recipe class name (`self.__class__.__name__`), not global, so
`DailyDigestLive` and `IndiaOpinionDigest` — both `DailyDigestBase`
subclasses pulling the same newsletters — can't suppress each other's
articles. This only touches `parse_newsletters`; the four main
opinion-page sources (Indian Express/Hindu/Livemint/Business Standard)
have their own separate fetch paths and are not affected.

**Gotcha found while testing this locally:** rapid back-to-back
`test_eink.sh` runs against `IndiaOpinionDigest` can make the *other*
four sources (via `_ie_via_wayback`'s Google News + Wayback Machine
scraping, and presumably similar scraping for Hindu/Livemint/Business
Standard) transiently return nothing — confirmed by temporarily removing
the seen-urls file and re-running, which brought newsletters back and
the same build then succeeded. Likely self-inflicted rate-limiting from
testing too fast locally, not a real production risk on a normal
once-daily CI cadence, and not something this newsletter-dedup change
caused — but it does mean a newsletter-empty day is now more common than
before (previously newsletters almost always contributed *something*),
so a day where newsletters have genuinely nothing new **and** the other
four sources have a real (not self-inflicted) transient failure would
now hit `parse_index`'s `raise ValueError('No articles could be fetched
from any source.')` where it might not have before. Not fixed here since
it wasn't asked for; if it ever fires in a real CI run, that's the
mechanism to know about.

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

- **Only `india-opinion` and `inshorts` build.** `_recipes_custom.py` ends with
  `_active = {"india-opinion", "inshorts"}`. Every other recipe (custom and
  upstream) is intentionally inactive. Don't "fix" this.
- **Production builds run in the local container** (`local/`, supercronic at
  06:00 IST), not on GitHub Actions — `build.yml` is manual-only. See
  `local/README.md`. Rebuild the image to pick up code changes.
- **Newsletter seen-URLs are staged.** `daily_digest._save_seen_urls` writes
  `meta/daily_digest_seen_urls__*.json.pending`; `local/run-and-publish.sh`
  promotes it only after a successful publish (CI sets `DIGEST_COMMIT_SEEN=1`
  to write directly). Before this, test/DRY_RUN builds marked the next
  morning's newsletters as already published.
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
