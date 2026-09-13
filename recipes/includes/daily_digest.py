'''
Daily Digest -- shared base

The Opinion / Op-Ed / Editorial pages of four Indian dailies - Business
Standard, The Hindu, Live Mint and The Indian Express - the Science page of
The Hindu, plus a set of newsletters.
'''
import email.utils
import json
import os
import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from urllib.parse import quote

import mechanize
from html5_parser import parse

from calibre.web.feeds.news import BasicNewsRecipe, classes

_name = 'Daily Digest'

_HERE = os.path.dirname(os.path.abspath(__file__))
# repo static/ dir (recipes/includes/ -> ../../static) -- holds the bundled
# OpenSans faces and the monochrome Noto Emoji font used on the cover
_STATIC_DIR = os.path.normpath(os.path.join(_HERE, '..', '..', 'static'))

GNEWS_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
)

# --- Indian Express is edge-blocked (CloudFront 403) for many data-centre IPs,
# so its own RSS feeds are unreachable from CI. Work around it with a two-hop
# route that never touches indianexpress.com directly:
#   1. DISCOVER fresh article URLs via Google News RSS (site: + when: query),
#      decoding Google's opaque /rss/articles/<id> links to the real URL.
#   2. FETCH each article through the Wayback Machine (archive.org's crawler is
#      not blocked), triggering an on-demand capture for anything not archived.
WB_SNAPSHOT_MAX_AGE_DAYS = 2       # accept an existing capture this fresh
WB_SAVE_BUDGET_SECONDS = 240      # stop asking Save Page Now after this long
GNEWS_ITEMS_PER_SECTION = 8       # decode at most this many candidates/section
GNEWS_KEEP_PER_SECTION = 3        # ... and keep at most this many

# calibre only treats a feed as "embedded" when its content averages more than
# this many chars/article (calibre.web.feeds.Feed.has_embedded_content). Short
# posts (Word of the Day) would otherwise be re-fetched from a blocked URL, so
# they are padded with an HTML comment past the threshold; the e-ink optimizer
# strips the comment back out of the shipped book.
CALIBRE_EMBEDDED_MIN_CHARS = 2000

# section-name keywords we keep. OPINION_KEYS is matched against Business
# Standard *and* Hindu section names, so it must not contain fragments like
# 'edit' that substring-match unrelated names ("Credit ...").
OPINION_KEYS = (
    'opinion', 'editorial', 'op-ed', 'oped', 'comment',
    'analysis', 'columns', 'voices',
)
HINDU_EXTRA_KEYS = ('science', 'edit')  # The Hindu's section is literally 'Edit'

# extra RSS/Atom newsletters bundled into the digest: (name, url, weekly?).
# `weekly=True` means an occasional/weekly cadence -- named on the cover when
# it makes the edition. It is also inferred from post spacing as a fallback.
NEWSLETTER_FEEDS = (
    ('Finshots', 'https://finshots.in/rss/', False),
    ('The Daily Brief (Zerodha)', 'https://thedailybrief.zerodha.com/feed', False),
    ('Masala Chai', 'https://rss.beehiiv.com/feeds/Jk0t0xwJeq.xml', False),
    ('The Core', 'https://rss.beehiiv.com/feeds/4BOnz8D132.xml', False),
    ('Word of the Day', 'https://www.merriam-webster.com/wotd/feed/rss2', False),
    ('The Download (MIT Tech Review)',
     'https://www.technologyreview.com/topic/download-newsletter/feed/', False),
    ('Public Policy', 'https://publicpolicy.substack.com/feed', True),
    ('Last Week in AI', 'https://lastweekin.ai/feed', True),
    ('India Wants to Know Quiz', 'https://iwtkquiz.substack.com/feed', True),
)
NEWSLETTER_MAX_AGE_DAYS = 1.15   # same discovery window as everything else
WEEKLY_GAP_DAYS = 4              # avg days between posts to count as "weekly"

# public RSS-to-JSON gateways, tried in order when a feed 403s us directly
# (Substack sits behind Cloudflare). Both fetch server-side and were verified
# working against the blocked feeds.
FEED_GATEWAYS = (
    'https://api.rss2json.com/v1/api.json?rss_url=',
    'https://feed2json.org/convert?url=',
)


def _class_matcher(tokens, prefixes=()):
    '''Return a predicate over a class attribute (str or list of str): True if
    any whole class token equals one of `tokens`, starts with one of
    `prefixes`, or -- for CSS-module names like ``Foo_bar__hash`` -- has a
    `Foo_bar` part that equals or is prefixed by a token.'''
    tokens = tuple(tokens)
    prefixes = tuple(prefixes)

    def match(c):
        if not c:
            return False
        for cls in (c.split() if isinstance(c, str) else c):
            if prefixes and cls.startswith(prefixes):
                return True
            base = cls.split('__', 1)[0]  # drop CSS-module hash suffix
            for tok in tokens:
                if cls == tok or base == tok or base.startswith(tok + '_'):
                    return True
        return False

    return match


# --- per-source junk sets, built once at import ---------------------------- #
NL_JUNK = _class_matcher(
    ('subscribe', 'subscription', 'share', 'social', 'cta', 'footer',
     'email-footer', 'promo', 'sponsor', 'advertisement', 'poll',
     'recommendation', 'recommendations', 'paywall', 'unsubscribe', 'referral',
     'button-wrapper'),
    prefixes=('subscription-widget', 'subscribe-widget', 'poll-embed',
              'social-share'),
)
HINDU_JUNK = _class_matcher(
    ('hide-mobile', 'comments-shares', 'share-page', 'editiondetails'),
)
NL_JUNK_EXTRA = _class_matcher(
    ('ad', 'ads', 'advert', 'ad-wrapper', 'ad-container', 'sponsored',
     'sponsorship', 'partner-message', 'beehiiv-ad', 'native-ad',
     'recommendations-widget', 'subscribe-cta', 'footer-cta'),
    prefixes=('ad-', 'ads-', 'advert', 'sponsor'),
)

# A block whose (short) text starts with one of these is a sponsor slot / house
# ad / boilerplate, not article content. Matched only against block elements
# and only when the block's text is short, so a real paragraph that happens to
# mention a sponsor is never dropped.
_NL_AD_MARKERS = (
    'message from our sponsor', 'a message from our sponsor', 'together with',
    'presented by', 'sponsored by', 'brought to you by', 'in partnership with',
    'advertisement', 'a word from our sponsor', 'our sponsor', 'partner message',
    'from our partners', 'sponsored content',
)
# Phrases that identify house-keeping / promo paragraphs in newsletters.
_NL_PROMO_RES = tuple(re.compile(p, re.IGNORECASE) for p in (
    r'add .{0,40} as a preferred source',
    r'\bhit subscribe\b',
    r'\bsubscribe\b.{0,30}\bif you haven',
    r'we strip stories off the jargon',
    r'just one mail every morning',
    r"if you'?re already a subscriber",
    r"if you'?re someone who loves to keep tabs",
    r'was this (?:email|newsletter) forwarded to you',
    r'forward(?:ed)? this (?:email|newsletter|to a friend)',
    r'share this with your friends',
    r'\bjoin us on whatsapp\b',
    r'upgrade to paid', r'become a paid subscriber',
    r'click here to see what all of the hype is about',
    r'learn how to apply now',
    r'thank you for reading\.? do share',
    r'how did you like (?:today|this)',
    r'rate (?:today|this) (?:edition|newsletter)',
))
# Section headings after which everything (to the next <hr>/heading) is a footer.
_NL_FOOTER_HEADINGS = (
    'the team', 'written by', 'about the author', 'about us', 'credits',
    'share the love', 'refer a friend', 'referral', 'feedback',
)
MINT_JUNK = _class_matcher(
    ('giftArticle', 'pTopic', 'alsoRead', 'manualbacklink', 'autobacklink',
     'autobacklink-topic', 'topicsTag', 'psTopicsHeading', 'psTopLogo',
     'psTopLogoItem', 'premiumImgIcon', 'double_gift_box', 'premiumSlider',
     'moreAbout', 'milestone', 'benefitText', 'checkCibilBtn', 'Joinus',
     'moreStory', 'sepStory', 'disclamerText', 'disclaimerText', 'bs_logo',
     'ecologoStory', 'author-widget', 'similarStoriesClass', 'moreFromSecClass',
     'linkStories', 'sidebarAdv', 'taboolaHeight', 'gadgetSlider', 'ninSec',
     'socialHolder', 'openinApp2', 'mobAppDownload', 'trendingSimilarHeight',
     'moreNews', 'lastAdSlot'),
    prefixes=('storyPage_alsoRead__', 'storyPage_bcrumb__',
              'storyPage_premiumSlider__', 'storyPage_firstPublishDate__'),
)
IE_KEEP = _class_matcher(
    ('heading-part', 'full-details', 'top-opinion', 'article-main-head',
     'top-description', 'top-image-part', 'story_details'),
)
IE_JUNK = _class_matcher(
    ('share-social', 'appstext', 'ie-int-campign-ad', 'ie-breadcrumb',
     'custom_read_button', 'unitimg', 'copyright', 'storytags',
     'pdsc-related-modify', 'news-guard', 'premium-story', 'append_social_share',
     'digital-subscriber-only', 'h-text-widget', 'ie-premium', 'ie-first-publish',
     'adboxtop', 'adsizes', 'immigrationimg', 'next-story-wrap', 'ie-ie-share',
     'next-story-box', 'brand-logo', 'quote_section', 'ie-customshare',
     'osv-ad-class', 'custom-share', 'o-story-paper-quite', 'ie-network-commenting',
     'audio-player-tts-sec', 'o-story-list', 'subscriber_hide', 'author-social',
     'author-follow', 'author-img', 'author-block', 'premium_widget_below_article',
     'most-read-container', 'desktop-full-ad', 'iers_mr_widget',
     'ie-newsletter-widget', 'related-widget', 'related-widget-full',
     'ev-widget-story', 'editor-date-logo', 'ie-tags', 'more-from'),
)
_IE_LAZY_ATTRS = ('data-src', 'data-lazy-src', 'data-original', 'data-srcset',
                  'data-lazy-srcset')


def _parse_feed_date(s):
    '''Parse an RSS/Atom date string into a timezone-aware datetime (UTC), or
    None. `email.utils.parsedate` alone silently drops the offset, which on a
    ~1-day window is a multi-hour error for non-UTC feeds.'''
    if not s:
        return None
    s = s.strip().replace(' Sept ', ' Sep ')
    try:
        dt = email.utils.parsedate_to_datetime(s)
        if dt is not None:
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        pass
    for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%SZ'):
        try:
            return datetime.strptime(s[:19], fmt[:19]).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _rss_cd(tag, chunk):
    m = re.search(
        r'<%s(?:\s[^>]*)?>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</%s>' % (tag, tag),
        chunk, re.S,
    )
    return m.group(1).strip() if m else ''


def _feed_entries(raw):
    '''Parse an RSS/Atom feed body into normalised entry dicts. No filtering.
    Each dict: title, url, description (text), content (html or None),
    date (aware datetime or None), date_str.'''
    if isinstance(raw, bytes):
        raw = raw.decode('utf-8', 'ignore')
    blocks = re.findall(r'<item>(.*?)</item>', raw, re.S) \
        or re.findall(r'<entry[\s>](.*?)</entry>', raw, re.S)
    out = []
    for chunk in blocks:
        url = (_rss_cd('link', chunk) or _rss_cd('guid', chunk)
               or _rss_cd('id', chunk))
        if not url.startswith('http'):
            m = re.search(r'<link[^>]*\bhref="([^"]+)"', chunk)
            url = m.group(1) if m else url
        title = _rss_cd('title', chunk)
        if not url or not title:
            continue
        desc_html = _rss_cd('description', chunk) or _rss_cd('summary', chunk)
        body = _rss_cd('content:encoded', chunk) or _rss_cd('content', chunk)
        if not body and '<' in desc_html and len(desc_html) > 200:
            body = desc_html  # feeds whose <description> is the whole article
        pd = (_rss_cd('pubDate', chunk) or _rss_cd('published', chunk)
              or _rss_cd('updated', chunk) or _rss_cd('dc:date', chunk))
        out.append({
            'title': title,
            'url': url,
            'description': re.sub(r'<[^>]+>', '', desc_html).strip(),
            'content': body or None,
            'date': _parse_feed_date(pd),
            'date_str': pd,
        })
    return out


def _within_window(entries, max_age_days):
    '''Entries no older than max_age_days. An entry with no parseable date
    survives only when the feed has *no* dated entries at all -- otherwise one
    malformed pubDate would drag in a whole archive.'''
    now = datetime.now(timezone.utc)
    any_dated = any(e['date'] for e in entries)
    cutoff = max_age_days * 86400
    out = []
    for e in entries:
        if e['date'] is None:
            if not any_dated:
                out.append(e)
        elif (now - e['date']).total_seconds() <= cutoff:
            out.append(e)
    return out


def _cadence_is_weekly(entries):
    '''True if the feed's recent posts average >= WEEKLY_GAP_DAYS apart.
    Needs >= 2 dated posts -- otherwise there is no evidence, so False.'''
    dates = sorted((e['date'] for e in entries if e['date']), reverse=True)[:5]
    if len(dates) < 2:
        return False
    span = (dates[0] - dates[-1]).total_seconds() / 86400
    return span / (len(dates) - 1) >= WEEKLY_GAP_DAYS


def rss_articles(raw, max_age_days, url_sink=None, domain=None, want_content=False):
    '''Feed body -> calibre article dicts, filtered to max_age_days.'''
    out = []
    for e in _within_window(_feed_entries(raw), max_age_days):
        if url_sink is not None and domain:
            url_sink[e['url']] = domain
        art = {'title': e['title'], 'url': e['url'],
               'description': e['description']}
        if e['date_str']:
            art['date'] = e['date_str']
        if want_content and e['content'] and len(e['content']) > 40:
            art['content'] = e['content']
        out.append(art)
    return out


# Live Mint prefixes its RSS titles with a desk tag ("Mint Quick Edit | ...")
# or the columnist's name ("Ajit Ranade: ..."). The desk tag is a real
# sub-section (used below); the rest is noise in front of the headline.
_MINT_DESK_RE = re.compile(
    r'^\s*(?:mint\s+)?(quick edit|primer|explainer|snapview|long story|'
    r'straight talk|top of mind|mark to market|for the record)\s*[|:–-]\s*',
    re.IGNORECASE,
)
# leading "First Last: " / "First M. Last: " columnist byline
_MINT_BYLINE_RE = re.compile(
    r'^\s*((?:[A-Z][A-Za-z.’\'-]+\s+){1,3}[A-Z][A-Za-z.’\'-]+)\s*:\s+'
    r'(?=[A-Z0-9“])'
)


def _mint_desk(title):
    '''(sub-section label, cleaned title). Sub-section is None for a plain
    opinion piece.'''
    m = _MINT_DESK_RE.match(title or '')
    desk = None
    if m:
        desk = m.group(1).title()
        if desk.lower() == 'quick edit':
            desk = 'Quick Edit'
        title = title[m.end():]
    title = _MINT_BYLINE_RE.sub('', title, count=1)
    return desk, title.strip(' -–|').strip()


def absurl(url, base):
    if url.startswith('/'):
        url = base + url
    return url


# --- Chromium (QtWebEngine) transport -------------------------------------- #
# calibre >= 8 can route recipe fetches through a real Chromium network stack
# (HTTP/2, genuine Chrome TLS fingerprint) via `browser_type = 'webengine'`,
# and `calibre.scraper.simple.read_url` exposes the same transport for one-off
# fetches. This gets past header/fingerprint 403s -- it does NOT solve a
# Cloudflare JS challenge or a hard datacentre-IP block, so every use is
# guarded with `looks_blocked()` and falls back to the existing workarounds.
_BLOCK_MARKERS = (
    'just a moment', 'cf-browser-verification', 'cf-challenge', 'cf_chl_opt',
    'attention required', 'access denied', 'request could not be satisfied',
    'error 1020', 'ray id',
)


def looks_blocked(html):
    if not html:
        return True
    h = html[:5000].lower()
    if any(m in h for m in _BLOCK_MARKERS):
        return True
    if len(html) < 1500 and ('403' in h or 'forbidden' in h):
        return True
    return False


def chromium_get(storage, url, timeout=45):
    '''Fetch `url` through calibre's Chromium/QtWebEngine scraper. `storage`
    is a caller-owned list used to cache the worker across calls.'''
    from calibre.scraper.simple import read_url
    return read_url(storage, url, timeout)  # positional: sig varies by version


class DailyDigestBase(BasicNewsRecipe):
    title = _name + ' - ' + datetime.now().strftime('%d.%m.%y')
    # calibre appends this to the title on conversion; empty => the shipped book
    # is just "Daily Digest - dd.mm.yy" with no trailing " [Weekday, dd Mon YYYY]"
    timefmt = ''
    __author__ = 'newsrack'
    description = (
        'Opinion, Op-Ed and Editorial pages of Business Standard, The Hindu, '
        'Live Mint and The Indian Express, the Science page of The Hindu, plus '
        'a set of newsletters.'
    )
    language = 'en_IN'
    encoding = 'utf-8'
    no_stylesheets = True
    remove_javascript = True
    remove_attributes = ['style', 'height', 'width']
    simultaneous_downloads = 9
    ignore_duplicate_articles = {'title', 'url'}
    remove_empty_feeds = True
    resolve_internal_links = True
    # section index lists article titles only -- no summary / first-paragraph blurb
    summary_length = 0
    oldest_article = 1.15  # days (Live Mint RSS + Indian Express discovery)
    recursions = 0
    timeout = 45  # bound each fetch; archived Indian Express images can be slow
    masthead_url = 'https://www.thehindu.com/theme/images/th-online/thehindu-logo.svg'

    # When True, blocked feeds/sections are first tried through calibre's
    # Chromium transport (see chromium_get) before the Google-News + Wayback /
    # RSS-gateway workarounds. Subclass DailyDigestLive turns this on together
    # with browser_type='webengine' and low concurrency.
    chromium_first = False

    # --- per-source on/off switches. Flip to False to drop a whole source from
    # the digest (its section(s) simply don't appear). Live Mint and Business
    # Standard are off for now.
    fetch_newsletters = True
    fetch_indian_express = True
    fetch_hindu = True
    fetch_livemint = False
    fetch_business_standard = False

    # individual newsletter feeds to skip, by name (see NEWSLETTER_FEEDS)
    DISABLED_NEWSLETTERS = set()

    extra_css = '''
        img {display:block; margin:0 auto;
             filter:grayscale(100%) !important;
             -webkit-filter:grayscale(100%) !important;}
        .caption, .cap, #img-cap {font-size:small; text-align:center;}
        .author, .dateLine, .auth, .cat, .articleInfo {font-size:small; color:#202020;}
        .subhead, .subhead_lead, .bold {font-weight:bold;}
        .italic, .sub-title, .sub, .summary {font-style:italic; color:#202020;}
        em, blockquote {color:#202020;}
    '''

    # parse_index fans the five sources out across threads (unless the Chromium
    # transport is in play -- that path wants low request pressure and its
    # scraper worker is single-use). Shared mutable state (_url_domain,
    # _weekly_newsletters, the Chromium worker) is guarded by _lock; every
    # network fetch in a worker uses its own cloned browser.
    parallel_sources = True

    def __init__(self, *args, **kwargs):
        BasicNewsRecipe.__init__(self, *args, **kwargs)
        self._url_domain = {}
        ak, sk = os.environ.get('IA_ACCESS_KEY'), os.environ.get('IA_SECRET_KEY')
        self._ia_auth = (ak, sk) if ak and sk else None
        self._wb_save_fails = 0
        self._wb_save_dead = False
        self._wb_t0 = time.monotonic()
        self._weekly_newsletters = []  # filled by parse_index, shown on cover
        self._font_cache = {}
        self._scraper_storage = []  # calibre.scraper.simple worker cache
        self._lock = threading.Lock()

    # ------------------------------------------------------- fetch helpers
    def _set_domain(self, url, dom):
        with self._lock:
            self._url_domain[url] = dom

    def _open_bytes(self, url, data=None, timeout=60):
        '''Fetch `url` on a *cloned* browser -- safe to call from several
        parse_index workers at once (self.browser is not thread-safe).'''
        br = self.clone_browser(self.browser)
        req = mechanize.Request(url, data=data, headers={
            'User-Agent': GNEWS_UA,
            'Accept-Language': 'en-IN,en;q=0.9',
        })
        return br.open_novisit(req, timeout=timeout).read()

    def _chromium_get(self, url, timeout=45):
        '''Chromium fetch, or None if it is blocked / errors / isn't wanted.'''
        if not self.chromium_first:
            return None
        try:
            with self._lock:  # the scraper worker is not concurrency-safe
                html = chromium_get(self._scraper_storage, url, timeout)
        except Exception as e:
            self.log.warn('  Chromium fetch failed for %s: %s' % (url, e))
            return None
        if looks_blocked(html):
            self.log('  Chromium fetch blocked (challenge/403): %s' % url)
            return None
        return html

    # ------------------------------------------------------------------ cover
    # canvas is the standard ebook-cover 1:1.6 (like Amazon/KDP etc.), so the
    # reader's library view shows no letterbox band. The e-ink optimizer
    # downsizes it to fit the Xteink X4 480x800 panel. Rendered large for clean
    # anti-aliasing.
    _COVER_W, _COVER_H = 1200, 1920

    _COVER_FONT_DIRS = (
        _STATIC_DIR, 'static', 'recipes/static',
        '/usr/share/fonts/truetype/dejavu',
        '/usr/share/fonts/truetype/liberation',
        '/usr/share/fonts/truetype/liberation2',
        '/usr/share/fonts/truetype/noto',
        '/usr/share/fonts/opentype/noto',
        '/usr/share/fonts/TTF', '/Library/Fonts',
    )
    _COVER_FONT_FILES = {
        ('serif', True): ('LiberationSerif-Bold.ttf', 'NotoSerif-Bold.ttf',
                          'DejaVuSerif-Bold.ttf', 'OpenSans-Bold.ttf'),
        ('serif', False): ('LiberationSerif-Regular.ttf', 'NotoSerif-Regular.ttf',
                           'DejaVuSerif.ttf', 'OpenSans-Regular.ttf'),
        ('sans', True): ('OpenSans-Bold.ttf', 'DejaVuSans-Bold.ttf',
                         'LiberationSans-Bold.ttf', 'NotoSans-Bold.ttf'),
        ('sans', False): ('OpenSans-Regular.ttf', 'DejaVuSans.ttf',
                          'LiberationSans-Regular.ttf', 'NotoSans-Regular.ttf'),
    }

    def _cover_font(self, size, bold=True, serif=False):
        key = ('serif' if serif else 'sans', bold, size)
        if key in self._font_cache:
            return self._font_cache[key]
        from PIL import ImageFont
        f = None
        for fdir in self._COVER_FONT_DIRS:
            for name in self._COVER_FONT_FILES[key[:2]]:
                try:
                    f = ImageFont.truetype(os.path.join(fdir, name), size)
                    break
                except OSError:
                    continue
            if f:
                break
        if f is None:
            f = ImageFont.load_default()
        self._font_cache[key] = f
        return f

    def _cover_motif(self, img, M):
        '''Paint the newspaper glyph in the lower-left. Positioned from the
        glyph's real bbox so it sits fully inside the frame with clear space
        below -- nothing touches the bottom edge. Falls back to a grey disc
        if the emoji font is unavailable.'''
        from PIL import Image, ImageDraw, ImageFont
        W, H = img.size
        size = 300
        bottom_gap = 150          # clear space between glyph and bottom border
        left_pad = M + 28
        d = ImageDraw.Draw(img)

        f = None
        for fdir in self._COVER_FONT_DIRS:
            try:
                f = ImageFont.truetype(os.path.join(fdir, 'NotoEmoji.ttf'), size)
                break
            except OSError:
                continue
        if f is None:
            y0 = H - M - bottom_gap - size
            d.ellipse([left_pad, y0, left_pad + size, y0 + size],
                      fill=(150, 150, 150))
            return

        glyph = '\U0001F4F0'
        try:
            bx0, by0, bx1, by1 = f.getbbox(glyph)
        except Exception:
            bx0, by0, bx1, by1 = 0, 0, size, size
        # place so the glyph's visible box bottom-left is at the target point
        draw_x = left_pad - bx0
        draw_y = (H - M - bottom_gap - by1)
        d.text((draw_x, draw_y), glyph, font=f, fill=(55, 55, 55))

    def default_cover(self, cover_file):
        '''A spare black-on-off-white cover sized for the Xteink X4 panel:
        "DAILY DIGEST" over the day and date, any weekly newsletter in this
        edition listed below, a soft grey motif bottom-left and a small
        IDEAS / PEOPLE / PROGRESS tag bottom-right.'''
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            return False

        W, H = self._COVER_W, self._COVER_H
        bg, ink, faint = '#f4f3ef', '#111111', '#3a3a3a'
        img = Image.new('RGB', (W, H), bg)
        d = ImageDraw.Draw(img)
        M = 90                       # inner border margin
        X = 150                      # left text column

        def fit(text, size, bold=True, serif=False, max_w=W - X - 155):
            while size > 14:
                f = self._cover_font(size, bold, serif)
                if d.textlength(text, font=f) <= max_w:
                    return f
                size -= 6
            return self._cover_font(size, bold, serif)

        def left(y, text, f, fill=ink):
            d.text((X, y), text, font=f, fill=fill)

        # a newspaper icon (monochrome Noto Emoji, U+1F4F0) as the lower-left
        # motif, clipped inside the border frame
        self._cover_motif(img, M)
        d = ImageDraw.Draw(img)

        d.rectangle([M, M, W - M, H - M], outline=ink, width=4)

        left(190, 'DAILY', fit('DAILY', 230, serif=True))
        left(440, 'DIGEST', fit('DIGEST', 230, serif=True))
        d.line([X, 770, X + 190, 770], fill=ink, width=7)

        now = datetime.now()
        # big date with the weekday under it (short month name so the larger
        # font size still fits the column width). `left(y, ...)` draws with y
        # as the font's ascender line, not the glyph's own visible top/bottom
        # -- at this date font size that gap is tens of pixels, so a fixed
        # pixel offset between the two lines looks cramped or overlapping
        # depending on font metrics. Measure each line's real rendered bbox
        # with textbbox() and place the next line a fixed *visual* gap below
        # it instead of guessing a raw y delta.
        date_str = now.strftime('%d %b %Y')
        date_y = 860
        date_font = fit(date_str, 150, bold=True)
        left(date_y, date_str, date_font)
        date_bottom = d.textbbox((X, date_y), date_str, font=date_font)[3]

        wk_str = now.strftime('%A').upper()
        wk_font = self._cover_font(58, bold=True)
        wk_gap = 34
        # this font/size's own ascender-to-glyph-top offset, so the glyph's
        # visible top (not the ascender line) lands `wk_gap` below date_bottom
        wk_top_offset = d.textbbox((X, 0), wk_str, font=wk_font)[1]
        wk_y = date_bottom + wk_gap - wk_top_offset
        left(wk_y, wk_str, wk_font, fill=faint)
        wk_bottom = d.textbbox((X, wk_y), wk_str, font=wk_font)[3]

        weeklies = list(dict.fromkeys(self._weekly_newsletters))[:5]
        y = wk_bottom + 100
        for nm in weeklies:
            left(y, nm, fit(nm, 60, bold=False))
            y += 92

        tag_f = self._cover_font(44, bold=False)
        ty = H - M - 150 - 3 * 78
        for word in ('IDEAS', 'PEOPLE', 'PROGRESS'):
            s = ' '.join(word)
            w = d.textlength(s, font=tag_f)
            d.text((W - M - 60 - w, ty), s, font=tag_f, fill=faint)
            ty += 78

        img.save(cover_file, 'JPEG', quality=92)
        cover_file.flush()
        return True

    # ------------------------------------------------------------------ index
    SOURCES = (
        ('%s', 'parse_newsletters', 'fetch_newsletters'),
        ('Indian Express: %s', 'parse_indian_express', 'fetch_indian_express'),
        ('The Hindu: %s', 'parse_hindu', 'fetch_hindu'),
        ('Live Mint: %s', 'parse_livemint', 'fetch_livemint'),
        ('Business Standard: %s', 'parse_business_standard',
         'fetch_business_standard'),
    )

    def _run_source(self, name):
        try:
            return getattr(self, name)() or []
        except Exception as e:
            self.log.warn('Failed to fetch %s: %s' % (name, e))
            return []

    def parse_index(self):
        sources = [s for s in self.SOURCES if getattr(self, s[2], True)]
        if not sources:
            raise ValueError('Every source is disabled (fetch_* flags).')
        if self.parallel_sources and not self.chromium_first:
            with ThreadPoolExecutor(max_workers=len(sources)) as ex:
                results = list(ex.map(
                    lambda s: self._run_source(s[1]), sources))
        else:
            results = [self._run_source(s[1]) for s in sources]

        feeds = []
        for (label, _, _), got in zip(sources, results):
            for section, articles in got:
                if articles:
                    feeds.append((label % section, articles))

        if not feeds:
            raise ValueError('No articles could be fetched from any source.')
        return feeds

    # ----------------------------------------------------------- newsletters
    def parse_newsletters(self):
        nl_feeds = [t for t in NEWSLETTER_FEEDS
                    if t[0] not in self.DISABLED_NEWSLETTERS]
        if not nl_feeds:
            return []
        with ThreadPoolExecutor(max_workers=min(8, len(nl_feeds))) as ex:
            fetched = list(ex.map(
                lambda t: (t, self._fetch_feed_safe(t[0], t[1])),
                nl_feeds))

        out = []
        for (name, url, weekly), entries in fetched:
            if not entries:
                continue
            fresh = _within_window(entries, NEWSLETTER_MAX_AGE_DAYS)
            if not fresh:
                self.log('Newsletter %s: nothing in the last %s days'
                         % (name, NEWSLETTER_MAX_AGE_DAYS))
                continue
            if weekly or _cadence_is_weekly(entries):
                with self._lock:
                    self._weekly_newsletters.append(name)

            arts = []
            for e in fresh:
                self._set_domain(e['url'], 'newsletter')
                art = {'title': e['title'], 'url': e['url'],
                       'description': e['description']}
                if e['date_str']:
                    art['date'] = e['date_str']
                body = e['content']
                if body and len(body) > 40:
                    c = '<div class="x-newsletter">' + body + '</div>'
                    pad = CALIBRE_EMBEDDED_MIN_CHARS + 400 - len(c)
                    if pad > 0:
                        c += '<!--' + ' ' * pad + '-->'
                    art['content'] = c
                arts.append(art)
            out.append(('NL: ' + name, arts))
        return out

    def _fetch_feed_safe(self, name, url):
        try:
            return self._fetch_feed(name, url)
        except Exception as e:
            self.log.warn('Newsletter %s: %s' % (name, e))
            return []

    def _fetch_feed(self, name, url):
        '''RSS feed body -> list of `_feed_entries` dicts. Tries: the direct
        fetch, then (if enabled) calibre's Chromium transport, then a public
        RSS-to-JSON gateway.'''
        try:
            raw = self._open_bytes(url).decode('utf-8', 'ignore')
            if '<item' in raw or '<entry' in raw:
                return _feed_entries(raw)
            self.log.warn('Newsletter %s: direct feed was not RSS' % name)
        except Exception as e:
            self.log.warn('Newsletter %s: direct feed failed (%s)' % (name, e))

        html = self._chromium_get(url)
        if html and ('<item' in html or '<entry' in html):
            self.log('Newsletter %s: fetched via Chromium' % name)
            return _feed_entries(html)

        for gw in FEED_GATEWAYS:
            host = gw.split('/')[2]
            try:
                entries = self._feed_via_gateway(gw, url)
            except Exception as e:
                self.log.warn('Newsletter %s: %s failed (%s)' % (name, host, e))
                continue
            if entries:
                self.log('Newsletter %s: fetched via %s' % (name, host))
                return entries
        return []

    def _feed_via_gateway(self, gateway, url):
        data = json.loads(self._http_get(gateway + quote(url, safe='')))
        items = data.get('items') or []
        out = []
        for it in items:
            title = it.get('title', '')
            link = it.get('link') or it.get('url') or it.get('id') or ''
            if not title or not link:
                continue
            pd = (it.get('pubDate') or it.get('date_published')
                  or it.get('date_modified') or '')
            body = it.get('content') or it.get('content_html') or ''
            desc_html = (it.get('description') or it.get('summary')
                         or it.get('content_text') or '')
            if not body and '<' in desc_html and len(desc_html) > 200:
                body = desc_html
            out.append({
                'title': title, 'url': link,
                'description': re.sub(r'<[^>]+>', '', desc_html).strip(),
                'content': body or None,
                'date': _parse_feed_date(pd), 'date_str': pd,
            })
        return out

    # ------------------------------------------------------------- Hindu / BS
    def _wanted(self, section, extra=()):
        s = section.lower()
        return any(k in s for k in OPINION_KEYS + tuple(extra))

    def parse_business_standard(self):
        today = datetime.today().strftime('%d-%m-%Y')
        url = 'https://apibs.business-standard.com/category/today-paper?sortBy=' + today
        data = json.loads(self._open_bytes(url))['data']
        out = []
        for section in data:
            if section == 'EpaperImage' or not self._wanted(section):
                continue
            articles = []
            for article in data[section]:
                a_url = 'https://www.business-standard.com' + article['article_url']
                self._set_domain(a_url, 'bs')
                articles.append({
                    'title': article['heading1'],
                    'description': article.get('sub_heading') or '',
                    'url': a_url,
                })
            if articles:
                out.append((section, articles))
        return out

    # The Hindu print edition's opinion page ("TH_Edit") is one flat bucket of
    # ~15 items -- editorials, lead, op-ed, letters, the daily quiz. There is no
    # sub-section field on the todays-paper JSON or the article pages, so the
    # sub-section is recovered by matching each headline against The Hindu's own
    # per-section RSS feeds (real web addresses), with a couple of exact-title
    # rules for the pieces those feeds don't carry.
    HINDU_OPINION_FEEDS = (
        ('Editorials', 'https://www.thehindu.com/opinion/editorial/feeder/default.rss'),
        ('Lead', 'https://www.thehindu.com/opinion/lead/feeder/default.rss'),
        ('Op-Ed', 'https://www.thehindu.com/opinion/op-ed/feeder/default.rss'),
        ('Columns', 'https://www.thehindu.com/opinion/columns/feeder/default.rss'),
        ('Interview', 'https://www.thehindu.com/opinion/interview/feeder/default.rss'),
    )

    @staticmethod
    def _hkey(title):
        return tuple(re.sub(r'[^a-z0-9 ]', ' ', (title or '').lower()).split())

    def _hindu_subsections(self):
        with self._lock:
            if getattr(self, '_hindu_submap', None) is not None:
                return self._hindu_submap

        def one(item):
            label, feurl = item
            try:
                return label, _feed_entries(self._open_bytes(feurl))
            except Exception as e:
                self.log.warn('The Hindu %s feed: %s' % (label, e))
                return label, []

        with ThreadPoolExecutor(
                max_workers=len(self.HINDU_OPINION_FEEDS)) as ex:
            fetched = list(ex.map(one, self.HINDU_OPINION_FEEDS))

        submap = {}
        for label, entries in fetched:
            for e in entries:
                k = self._hkey(e['title'])
                # skip non-English (the lead feed carries Hindi items)
                if len(k) >= 2 and re.search('[a-z]', e['title']):
                    submap.setdefault(k[:6], label)
        with self._lock:
            self._hindu_submap = submap
        return submap

    def _hindu_label(self, headline):
        k = self._hkey(headline)
        if k[:3] == ('letters', 'to', 'the') or k[:2] == ('letters', 'to'):
            return 'Letters'
        if k[:3] == ('the', 'daily', 'quiz') or k[:3] == ('know', 'your', 'english'):
            return 'Quiz & Language'
        for key, label in self._hindu_subsections().items():
            if key[:len(k)] == k or k[:len(key)] == key:
                return label
        return 'Opinion'

    HINDU_SECTION_ORDER = ('Editorials', 'Lead', 'Op-Ed', 'Columns', 'Interview',
                           'Opinion', 'Letters', 'Quiz & Language', 'Science')

    def parse_hindu(self):
        base = 'https://www.thehindu.com'
        edition = 'th_delhi'
        today = date.today().strftime('%Y-%m-%d')
        url = base + '/todays-paper/' + today + '/' + edition + '/'
        soup = self.index_to_soup(
            self._open_bytes(url).decode('utf-8', 'ignore'))
        feeds_dict = defaultdict(list)
        for script in soup.findAll('script'):
            txt = self.tag_to_string(script)
            if 'grouped_articles = {"' not in txt:
                continue
            art = re.search(r'grouped_articles = ({\".*)', txt)
            data = json.JSONDecoder().raw_decode(art.group(1))[0]
            for sec in data:
                section = sec.replace('TH_', '').replace('_', ' ').strip()
                if not self._wanted(section, HINDU_EXTRA_KEYS):
                    continue
                is_science = 'science' in section.lower()
                for item in data[sec]:
                    a_url = absurl(item['href'], base)
                    self._set_domain(a_url, 'hindu')
                    desc = 'Page no.' + item.get('pageno', '') + ' | ' + (
                        item.get('teaser_text') or '')
                    label = ('Science' if is_science
                             else self._hindu_label(item['articleheadline']))
                    feeds_dict[label].append({
                        'title': item['articleheadline'],
                        'url': a_url,
                        'description': desc,
                    })
            break
        return [(k, feeds_dict[k]) for k in self.HINDU_SECTION_ORDER
                if feeds_dict.get(k)] + \
               [(k, v) for k, v in feeds_dict.items()
                if k not in self.HINDU_SECTION_ORDER]

    def parse_livemint(self):
        raw = self._open_bytes('https://www.livemint.com/rss/opinion')
        articles = rss_articles(raw, self.oldest_article)
        for art in articles:
            self._set_domain(art['url'], 'mint')
        # Mint's opinion feed has no sub-section field and rewrites every URL to
        # /opinion/online-views/, so the only taxonomy it exposes is the desk
        # tag in the headline. Split "Quick Edit" out; tidy every title.
        buckets = defaultdict(list)
        for art in articles:
            desk, clean = _mint_desk(art['title'])
            art['title'] = clean or art['title']
            buckets[desk or 'Opinion'].append(art)
        order = ['Opinion', 'Quick Edit', 'Primer', 'Explainer']
        return [(k, buckets[k]) for k in order if buckets.get(k)] + \
               [(k, v) for k, v in buckets.items() if k not in order]

    # ---- Indian Express via Google News discovery + Wayback Machine fetch ----
    def _http_get(self, url, data=None):
        return self._open_bytes(url, data=data).decode('utf-8', 'ignore')

    def _gnews_decode(self, gid):
        '''Resolve a Google-News /rss/articles/<gid> id to the publisher url.'''
        html = self._http_get('https://news.google.com/rss/articles/' + gid)
        sig = re.search(r'data-n-a-sg="([^"]+)"', html)
        ts = re.search(r'data-n-a-ts="([^"]+)"', html)
        if not (sig and ts):
            return None
        inner = json.dumps([
            'garturlreq',
            [['X', 'X', ['X', 'X'], None, None, 1, 1, 'US:en', None, 1,
              None, None, None, None, None, 0, 1],
             'en-US', 'US', 1, [2, 4, 8], 1, 1, None, 0, 0, None, 0],
            gid, int(ts.group(1)), sig.group(1),
        ])
        f_req = json.dumps([[['Fbv4je', inner, None, '1']]])
        body = self._http_get(
            'https://news.google.com/_/DotsSplashUi/data/batchexecute',
            data=b'f.req=' + quote(f_req).encode(),
        )
        m = re.search(r'\[\\"garturlres\\",\\"(https?:[^\\"]+)', body)
        if not m:
            return None
        url = m.group(1).replace('\\/', '/')
        return re.sub(r'\\u([0-9a-fA-F]{4})',
                      lambda x: chr(int(x.group(1), 16)), url)

    @staticmethod
    def _wb_snap(ts, url):
        return 'https://web.archive.org/web/%sid_/%s' % (ts, url)

    def _wb_lookup(self, url):
        '''Timestamp of the closest existing Wayback capture, or None.'''
        try:
            av = json.loads(self._http_get(
                'https://archive.org/wayback/available?url=' + quote(url, safe='')
            ))
            return av.get('archived_snapshots', {}).get('closest', {}).get('timestamp')
        except Exception:
            return None

    def _wb_save(self, url):
        '''Capture url via Save Page Now, returning the new timestamp or None.
        archive.org S3 keys (env IA_ACCESS_KEY / IA_SECRET_KEY) lift the
        anonymous rate limit substantially.'''
        hdrs = {'User-Agent': GNEWS_UA}
        if self._ia_auth:
            hdrs['Authorization'] = 'LOW %s:%s' % self._ia_auth
        attempts = 3 if self._ia_auth else 1
        for attempt in range(attempts):
            try:
                br = self.clone_browser(self.browser)
                resp = br.open_novisit(
                    mechanize.Request('https://web.archive.org/save/' + url,
                                      headers=hdrs), timeout=45)
                m = re.search(r'/web/(\d{14})/', resp.geturl())
                if m:
                    return m.group(1)
                body = resp.read(60000).decode('utf-8', 'ignore')
                m = re.search(r'web/(\d{14})/https?://', body)
                return m.group(1) if m else None
            except Exception as e:
                if getattr(e, 'code', None) == 429 and attempt < attempts - 1:
                    time.sleep(20)
                    continue
                self.log.warn('  Wayback capture failed for %s: %s' % (url, e))
                return None
        return None

    def _wb_resolve(self, url):
        '''A raw Wayback snapshot url for `url` (capturing on demand), or None.'''
        ts = self._wb_lookup(url)
        if ts:
            age = (datetime.now() - datetime.strptime(ts[:8], '%Y%m%d')).days
            if age <= WB_SNAPSHOT_MAX_AGE_DAYS + 3:
                return self._wb_snap(ts, url)
        # circuit breaker: stop hammering Save Page Now once it refuses, or
        # once we've spent too long on captures this run
        if self._wb_save_dead or (
                time.monotonic() - self._wb_t0) > WB_SAVE_BUDGET_SECONDS:
            return self._wb_snap(ts, url) if ts else None
        new_ts = self._wb_save(url)
        if new_ts:
            self._wb_save_fails = 0
            time.sleep(3)
            return self._wb_snap(new_ts, url)
        self._wb_save_fails += 1
        if self._wb_save_fails >= 2:
            self._wb_save_dead = True
            self.log.warn('  Save Page Now unavailable this run; using existing '
                          'captures only for remaining Indian Express articles')
        return self._wb_snap(ts, url) if ts else None

    IE_SECTIONS = (
        ('Editorials', 'opinion/editorials'),
        ('Columns', 'opinion/columns'),
        ('Explained', 'explained'),
    )

    def parse_indian_express(self):
        if self.chromium_first:
            direct = self._ie_direct_feeds()
            if direct:
                self.log('Indian Express: %d section(s) fetched directly'
                         % len(direct))
                return direct
            self.log('Indian Express: direct feeds blocked; '
                     'using Google News + Wayback')
        return self._ie_via_wayback()

    def _ie_direct_feeds(self):
        '''Indian Express section RSS + article HTML fetched straight from the
        site through the Chromium transport. Works only where the deploy IP is
        not CloudFront-blocked; returns [] otherwise so the caller falls back
        to the Google-News + Wayback route. Article HTML is embedded so the
        result does not depend on calibre's (Mechanize) downloader, which the
        site also blocks.'''
        now = datetime.now(timezone.utc)
        cutoff = self.oldest_article * 86400
        out = []
        for section, path in self.IE_SECTIONS:
            html = self._chromium_get(
                'https://indianexpress.com/section/%s/feed/' % path)
            if not html or ('<item' not in html and '<entry' not in html):
                continue
            arts = []
            for e in _feed_entries(html):
                if e['date'] and (now - e['date']).total_seconds() > cutoff:
                    continue
                page = self._chromium_get(e['url'], timeout=40)
                if not page:
                    continue
                self._set_domain(e['url'], 'ie')
                art = {'title': e['title'], 'url': e['url'],
                       'description': e['description'],
                       'content': '<div class="x-ie-direct">' + page + '</div>'}
                if e['date_str']:
                    art['date'] = e['date_str']
                arts.append(art)
                if len(arts) >= GNEWS_KEEP_PER_SECTION:
                    break
            if arts:
                out.append((section, arts))
        return out

    def _ie_via_wayback(self):
        sections = self.IE_SECTIONS
        self._wb_save_fails = 0
        self._wb_save_dead = False
        self._wb_t0 = time.monotonic()
        now = datetime.now(timezone.utc)
        cutoff = self.oldest_article * 86400
        seen = set()

        # phase 1: discover fresh IE article urls via Google News. Search a
        # slightly wider window (Google's `when:` is day-granular) then filter
        # each item precisely by its <pubDate>.
        cand = []  # (section, title, real_url)
        for section, path in sections:
            q = 'site:indianexpress.com/article/{}/ when:2d'.format(path)
            gn = ('https://news.google.com/rss/search?q=' + quote(q)
                  + '&hl=en-IN&gl=IN&ceid=IN:en')
            try:
                feed = self._http_get(gn)
            except Exception as e:
                self.log.warn('Indian Express (%s) discovery failed: %s'
                              % (section, e))
                continue
            picked = examined = 0
            for chunk in re.findall(r'<item>(.*?)</item>', feed, re.S):
                if picked >= GNEWS_KEEP_PER_SECTION \
                        or examined >= GNEWS_ITEMS_PER_SECTION:
                    break
                examined += 1
                link = _rss_cd('link', chunk)
                title = _rss_cd('title', chunk)
                gid_m = re.search(r'/rss/articles/([^/?<]+)', link)
                if not (gid_m and title):
                    continue
                dt = _parse_feed_date(_rss_cd('pubDate', chunk))
                if dt and (now - dt).total_seconds() > cutoff:
                    continue
                try:
                    real = self._gnews_decode(gid_m.group(1))
                except Exception:
                    real = None
                if not real or 'indianexpress.com/article/' not in real:
                    continue
                real = real.split('?')[0].replace('/lite/', '/')
                if real in seen:
                    continue
                seen.add(real)
                picked += 1
                title = re.sub(r'\s*[-|]\s*The Indian Express\s*$', '', title).strip()
                cand.append((section, title, real))

        if not cand:
            return []

        # phase 2: resolve each url to a Wayback snapshot, serially (SPN2 rate
        # limits anonymous callers hard); stale captures are accepted as a
        # fallback so a slow archive never sinks the whole section
        self.log('Resolving %d Indian Express article(s) via the Wayback '
                 'Machine%s...' % (
                     len(cand), ' (authenticated)' if self._ia_auth else ''))
        feeds_dict = defaultdict(list)
        for section, title, real in cand:
            wb = self._wb_resolve(real)
            if not wb:
                self.log.warn('  skipped (no archive): %s' % real)
                continue
            self._set_domain(wb, 'ie')
            feeds_dict[section].append(
                {'title': title, 'url': wb, 'description': ''})
        return list(feeds_dict.items())

    # ------------------------------------------------------------- extraction
    def _domain(self, url):
        if url in self._url_domain:
            return self._url_domain[url]
        if 'business-standard.com' in url:
            return 'bs'
        if 'thehindu.com' in url:
            return 'hindu'
        if 'livemint.com' in url:
            return 'mint'
        if 'indianexpress.com' in url:
            return 'ie'
        return ''

    def preprocess_raw_html(self, raw, url):
        dom = self._domain(url)
        if dom == 'bs':
            raw = self._bs_raw(raw, url)
        elif dom == 'mint':
            raw = re.sub(
                r'(<p>\s*)(<[^(\/|a|i|b|em|strong)])', r'\g<2>', re.sub(
                    r'(<p>\s*&nbsp;\s*<\/p>)|(<p>\s*<\/p>)|(<p\s*\S+>&nbsp;\s*<\/p>)',
                    '', raw))
        if dom in ('bs', 'hindu', 'mint', 'ie'):
            # a marker link identifies the source to preprocess_html (which
            # cannot use instance attrs -- calibre downloads on many threads)
            m = re.search(r'/web/(\d{14})', url or '')
            ts = m.group(1) if (dom == 'ie' and m) else ''
            tag = ('<link rel="x-india-opinion" data-src="%s" data-wbts="%s"/>'
                   % (dom, ts))
            new = re.sub(r'(<head\b[^>]*>)', r'\1' + tag, raw, count=1,
                         flags=re.IGNORECASE)
            return new if new != raw else tag + raw
        return raw

    def _bs_raw(self, raw, url):
        root = parse(raw)
        m = root.xpath('//script[@id="__NEXT_DATA__"]')
        data = json.loads(m[0].text)
        img_url = data['props']['pageProps']['articleSchema'].get('articleImageUrl')
        data = data['props']['pageProps']['data']
        title = '<h1>' + data['pageTitle'] + '</h1>'
        cat = subhead = auth = lede = caption = ''
        dac = data.get('defaultArticleCat') or {}
        if dac.get('h1_tag'):
            cat = '<div class="cat">' + dac['h1_tag'] + '</div>'
        if data.get('metaDescription'):
            subhead = '<p class="sub">' + data['metaDescription'] + '</p>'
        try:
            pdate = datetime.fromtimestamp(int(data['publishDate'])).strftime(
                '%b %d, %Y | %I:%M %p')
        except Exception:
            pdate = ''
        authors = []
        for aut in data.get('articleMappedMultipleAuthors', {}) or {}:
            authors.append(data['articleMappedMultipleAuthors'][str(aut)])
        auth = ('<div><p class="auth">' + ', '.join(authors) + ' | '
                + data.get('placeName', '') + ' | ' + pdate + '</p></div>')
        fi = data.get('featuredImageObj') or {}
        if fi.get('url') or img_url:
            lede = '<p class="cap"><img src="{}">'.format(img_url or fi.get('url'))
            if fi.get('alt_text'):
                caption = '<span>' + fi['alt_text'] + '</span></p>'
        body = data.get('htmlContent', '')
        return ('<html><head></head><body>' + cat + title + subhead + auth
                + lede + caption + '<div><br>' + body + '</div></body></html>')

    def preprocess_html(self, soup):
        marker = soup.find('link', attrs={'rel': 'x-india-opinion'})
        if marker is not None:
            dom = marker.get('data-src', '')
            wb_ts = marker.get('data-wbts', '') or ''
            marker.extract()
        elif soup.find(attrs={'class': 'x-newsletter'}):
            dom, wb_ts = 'newsletter', ''
        elif soup.find(attrs={'class': 'x-ie-direct'}):
            dom, wb_ts = 'ie', ''  # embedded IE article from _ie_direct_feeds
        else:
            dom, wb_ts = '', ''

        if dom == 'newsletter':
            self._clean_newsletter(soup)
        elif dom == 'hindu':
            self._clean_hindu(soup)
        elif dom == 'mint':
            self._clean_mint(soup)
        elif dom == 'ie':
            self._clean_ie(soup, wb_ts)
        # 'bs' is already clean from _bs_raw

        for attr in self.remove_attributes:
            for x in soup.findAll(attrs={attr: True}):
                del x[attr]
        return soup

    # --- per-source cleaners --------------------------------------------- #
    @staticmethod
    def _norm_txt(s):
        return re.sub(r'\s+', ' ', (s or '')).strip()

    def _clean_newsletter(self, soup):
        for x in soup.findAll(attrs={'class': NL_JUNK}):
            x.extract()
        for x in soup.findAll(attrs={'class': NL_JUNK_EXTRA}):
            x.extract()
        for tag in soup.findAll(['form', 'button', 'iframe', 'script', 'style',
                                 'source']):
            tag.extract()
        for img in soup.findAll('img', attrs={'width': '1'}):
            img.extract()
        for a in soup.findAll('a'):
            if self.tag_to_string(a).strip().lower() in (
                    'subscribe', 'subscribe now', 'share', 'read online',
                    'view in browser', 'unsubscribe', 'upgrade to paid'):
                a.extract()

        # obvious ad-network / house-ad images
        for img in soup.findAll('img'):
            src = (img.get('src') or '').lower()
            if any(k in src for k in (
                    'nl_banner', 'nl-banner', '/ad/', '/ads/', 'adcreative',
                    'ad_banner', '/sponsor', 'doubleclick', 'preferred-source',
                    'preferredsource')):
                (img.find_parent(['figure', 'p']) or img).extract()

        self._strip_ad_blocks(soup)

        # collapse a wrapper left holding nothing but whitespace
        for d in soup.findAll(['div', 'section']):
            if not self._norm_txt(self.tag_to_string(d)) and not d.find('img'):
                d.extract()

    def _strip_ad_blocks(self, soup):
        '''Drop sponsor slots, subscribe boilerplate and the trailing footer
        from an embedded newsletter, without touching article prose. Only short
        block elements are considered, and a sponsor/footer heading removes
        just its own run (up to the next <hr> or heading).'''
        block_tags = ('p', 'div', 'section', 'table', 'li', 'aside', 'figure',
                      'blockquote')
        heading_tags = ('h1', 'h2', 'h3', 'h4', 'h5', 'h6')

        # 1. heading markers -> remove the heading and its following run
        for el in list(soup.findAll(heading_tags + ('p', 'strong', 'a'))):
            if el.parent is None:
                continue
            t = self._norm_txt(self.tag_to_string(el)).lower().strip(' :·—-')
            if not t or len(t) > 60:
                continue
            is_ad = any(t == m or t.startswith(m) for m in _NL_AD_MARKERS)
            is_foot = t in _NL_FOOTER_HEADINGS
            if not (is_ad or is_foot):
                continue
            anchor = el if el.name in heading_tags else (el.parent or el)
            for sib in list(anchor.find_next_siblings()):
                nm = getattr(sib, 'name', None)
                if nm == 'hr' or (is_ad and nm in heading_tags):
                    if nm == 'hr':
                        sib.extract()
                    break
                sib.extract()
            anchor.extract()

        # 2. short promo / CTA paragraphs anywhere in the body
        for el in list(soup.findAll(block_tags)):
            if el.parent is None or el.find(block_tags):
                continue  # only leaf blocks
            txt = self._norm_txt(self.tag_to_string(el))
            if not txt or len(txt) > 320:
                continue
            if any(r.search(txt) for r in _NL_PROMO_RES):
                el.extract()

    _STOCK_CREDIT = (r'wikimedia\s*commons|wikipedia|creative\s*commons|'
                     r'public\s*domain|pixabay|unsplash|pexels|flickr|istock|'
                     r'getty\s*images|shutterstock|freepik')
    # caption/alt that is *only* a stock credit
    _CREDIT_ONLY_RE = re.compile(
        r'^(?:photo|image|pic|illustration)?\s*[:\-]?\s*(?:%s)[\w .]*\.?$'
        % _STOCK_CREDIT, re.IGNORECASE)
    # a stock credit tacked on the end of a longer alt/caption
    _CREDIT_TAIL_RE = re.compile(r'(?:%s)[\w ]*\s*$' % _STOCK_CREDIT, re.IGNORECASE)
    # free-media credits => the image is decorative filler (quiz / explainer
    # clip-art), safe to drop. Wire-service credits (Getty/AP/PTI/Reuters) are
    # left alone -- those sit on real news photos.
    _FILLER_CREDIT_RE = re.compile(
        r'(?:wikimedia\s*commons|wikipedia|creative\s*commons|public\s*domain)'
        r'[\w ]*\s*$', re.IGNORECASE)
    # slideshow position counter used as alt text ("1 of 2")
    _SLIDE_ALT_RE = re.compile(r'^\s*\d+\s+of\s+\d+\s*$', re.IGNORECASE)
    _HINDU_BODY_SELECTORS = (
        {'class': 'article-section'},
        {'itemprop': 'articleBody'},
        {'class': lambda c: c and 'articlebodycontent' in c},
        {'id': lambda i: i and i.startswith('content-body-')},
    )

    def _clean_hindu(self, soup):
        body = None
        for sel in self._HINDU_BODY_SELECTORS:
            body = soup.find(attrs=sel)
            if body is not None:
                break
        self._trim_to(soup, body)
        for x in soup.findAll(attrs={'class': HINDU_JUNK}):
            x.extract()
        for tag in soup.findAll(['source', 'button', 'svg']):
            tag.extract()
        # ad slots + the todays-paper photo-gallery / slideshow wrapper
        for x in soup.findAll(attrs={'id': lambda i: i and i.startswith((
                'photo-gallery', 'Desktop_', 'Mweb_', 'MWeb_', 'div-gpt',
                'arthardpv', 'artproduct'))}):
            x.extract()
        # slideshow-counter / bare-credit captions (div.caption or p.caption)
        for cap in soup.findAll(attrs={'class': 'caption'}):
            t = self._norm_txt(self.tag_to_string(cap))
            if (not t or self._SLIDE_ALT_RE.match(t)
                    or self._CREDIT_ONLY_RE.match(t)
                    or self._FILLER_CREDIT_RE.search(t)):
                cap.extract()
        for cap in soup.findAll('p', attrs={'class': 'caption'}):
            cap.name = 'figcaption'
        for img in soup.findAll('img', attrs={'data-original': True}):
            img['src'] = img['data-original']
        for h3 in soup.findAll(**classes('sub-title')):
            h3.name = 'p'
        # drop decorative / credit-only / slideshow images
        for fig in soup.findAll('figure'):
            cap = fig.find(['figcaption', 'figCaption'])
            if cap is not None and self._CREDIT_ONLY_RE.match(
                    self._norm_txt(self.tag_to_string(cap))):
                fig.extract()
        for img in soup.findAll('img'):
            alt = self._norm_txt(img.get('alt', ''))
            if (self._CREDIT_ONLY_RE.match(alt) or self._SLIDE_ALT_RE.match(alt)
                    or self._FILLER_CREDIT_RE.search(alt)):
                (img.find_parent('figure') or img).extract()
                continue
            if alt:  # trim a trailing credit off an otherwise useful alt
                img['alt'] = self._CREDIT_TAIL_RE.sub('', alt).strip(' .—-')

    def _clean_mint(self, soup):
        art = soup.find('article', attrs={
            'id': lambda x: x and x.startswith(('article_', 'box_'))})
        if art is None:
            art = soup.find(attrs={
                'class': lambda x: x and x.startswith('storyPage_storyBox__')})
        self._trim_to(soup, art)

        for tag in soup.findAll(['meta', 'link', 'svg', 'button', 'iframe',
                                 'style', 'noscript']):
            tag.extract()
        for x in soup.findAll(attrs={'id': lambda i: i and i in (
                'gift-an-article', 'faqSection', 'seoText', 'ellipsisId',
                'webPageWrapId', 'premium', 'hidden-article-id-0')}):
            x.extract()
        for x in soup.findAll(attrs={'class': MINT_JUNK}):
            x.extract()
        for tag in soup.findAll(['p', 'h2', 'h3', 'h4', 'strong', 'div']):
            if self.tag_to_string(tag).strip().startswith(
                    ('Also Read', 'Also read', 'ALSO READ')):
                tag.extract()
        # "About the Author" + the byline blurb that follows it
        for el in list(soup.findAll(['h2', 'h3', 'h4', 'strong', 'p', 'div'])):
            if el.parent is None:
                continue
            t = self._norm_txt(self.tag_to_string(el)).lower().strip(' :')
            if t in ('about the author', 'about the authors'):
                for sib in list(el.find_next_siblings()):
                    if getattr(sib, 'name', None) in (
                            'h1', 'h2', 'h3', 'h4', 'hr'):
                        break
                    sib.extract()
                el.extract()
        for h2 in soup.findAll('h2'):
            h2.name = 'h4'
        for img in soup.findAll('img', attrs={'data-src': True}):
            img['src'] = img['data-src']

    def _clean_ie(self, soup, wb_ts):
        og = soup.find('meta', attrs={'property': 'og:image'})
        lead_img = og['content'] if (og and og.get('content')) else ''

        nodes = soup.findAll(attrs={'class': IE_KEEP})
        self._trim_to_many(soup, nodes)

        for tag in soup.findAll(['meta', 'link', 'svg', 'button', 'iframe',
                                 'style', 'noscript', 'form']):
            tag.extract()
        for x in soup.findAll('div', attrs={'id': 'ie_story_comments'}):
            x.extract()
        for x in soup.findAll(attrs={'class': IE_JUNK}):
            x.extract()
        for a in soup.findAll('a', attrs={'href': lambda h: h and (
                h.endswith('/?utm_source=newbanner')
                or 'utm_source=newsletter' in h)}):
            a.extract()

        kept_imgs = self._ie_images(soup, wb_ts)
        if not kept_imgs and lead_img:
            s = lead_img
            if s.startswith('//'):
                s = 'https:' + s
            if wb_ts and s.startswith(('http://', 'https://')) \
                    and 'web.archive.org' not in s:
                s = 'https://web.archive.org/web/%sim_/%s' % (wb_ts, s)
            fig = soup.new_tag('p')
            fig['class'] = 'cap'
            im = soup.new_tag('img')
            im['src'] = s
            fig.append(im)
            body = soup.find('body') or soup
            h1 = body.find(['h1', 'h2'])
            (h1.insert_after(fig) if h1 is not None else body.insert(0, fig))
        for h in soup.findAll(('h2', 'h3')):
            h.name = 'h4'

    @staticmethod
    def _ie_images(soup, wb_ts):
        '''Keep Indian Express article images but serve each from the same
        Wayback capture as the page (images.indianexpress.com is edge-blocked,
        so a direct fetch just hangs). Returns the number of images kept.'''
        for src in soup.findAll('source'):  # leave <picture> wrapping its <img>
            src.extract()
        kept = 0
        for img in soup.findAll('img'):
            s = img.get('src', '')
            for a in _IE_LAZY_ATTRS:
                if not s or s.startswith('data:'):
                    v = img.get(a, '')
                    if v:
                        s = v.split()[0].split(',')[0].strip()
            alt = img.get('alt', '')
            for a in list(img.attrs):
                if a not in ('src', 'alt'):
                    del img[a]
            low = s.lower()
            if (not s or s.startswith('data:')
                    or '/wp-content/themes/' in low or low.endswith('.svg')
                    or s.endswith('-button-300-ie.jpeg')
                    or any(k in low for k in ('subscri', 'promo', '/adsystem/',
                                              'logo'))):
                img.extract()
                continue
            if s.startswith('//'):
                s = 'https:' + s
            if s.startswith('/web/'):
                s = 'https://web.archive.org' + s
            if wb_ts and s.startswith(('http://', 'https://')) \
                    and 'web.archive.org' not in s:
                s = 'https://web.archive.org/web/%sim_/%s' % (wb_ts, s)
            img['src'] = s
            if alt:
                img['alt'] = alt
            kept += 1
        for fig in soup.findAll('figure'):
            if not fig.find('img'):
                fig.extract()
        return kept

    @staticmethod
    def _trim_to(soup, node):
        '''keep only `node` inside body'''
        if node is None:
            return
        body = soup.find('body') or soup
        for child in list(body.contents):
            child.extract()
        body.append(node)

    @staticmethod
    def _trim_to_many(soup, nodes):
        '''keep only top-level `nodes` (drop any nested in another) inside body'''
        nodes = [n for n in nodes if n is not None]

        def _nested(n):
            return any(p is o for o in nodes for p in n.parents)

        top = [n for n in nodes if not _nested(n)]
        if not top:
            return
        body = soup.find('body') or soup
        for child in list(body.contents):
            child.extract()
        for n in top:
            body.append(n)
