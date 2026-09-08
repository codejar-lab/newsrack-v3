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
import time
from collections import defaultdict
from datetime import date, datetime, timezone
from urllib.parse import quote

import mechanize
from html5_parser import parse

from calibre.web.feeds.news import BasicNewsRecipe, classes

_name = 'Daily Digest'

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
NEWSLETTER_MAX_AGE_DAYS = 1.25    # same discovery window as everything else
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
    oldest_article = 1.25  # days (Live Mint RSS + Indian Express discovery)
    recursions = 0
    timeout = 45  # bound each fetch; archived Indian Express images can be slow
    masthead_url = 'https://www.thehindu.com/theme/images/th-online/thehindu-logo.svg'

    # When True, blocked feeds/sections are first tried through calibre's
    # Chromium transport (see chromium_get) before the Google-News + Wayback /
    # RSS-gateway workarounds. Subclass DailyDigestLive turns this on together
    # with browser_type='webengine' and low concurrency.
    chromium_first = False

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

    def _chromium_get(self, url, timeout=45):
        '''Chromium fetch, or None if it is blocked / errors / isn't wanted.'''
        if not self.chromium_first:
            return None
        try:
            html = chromium_get(self._scraper_storage, url, timeout)
        except Exception as e:
            self.log.warn('  Chromium fetch failed for %s: %s' % (url, e))
            return None
        if looks_blocked(html):
            self.log('  Chromium fetch blocked (challenge/403): %s' % url)
            return None
        return html

    # ------------------------------------------------------------------ cover
    def default_cover(self, cover_file):
        '''Title + a big date. Any weekly / occasional newsletter that made it
        into this edition is named underneath; the daily sources are not.'''
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            return False

        W, H = 1400, 1900
        bg, fg, accent = '#f7f5f0', '#1a1a1a', '#8a1c1c'
        img = Image.new('RGB', (W, H), bg)
        d = ImageDraw.Draw(img)

        font_dirs = (
            'static', 'recipes/static',
            '/usr/share/fonts/truetype/dejavu',
            '/usr/share/fonts/truetype/liberation',
            '/usr/share/fonts/truetype/liberation2',
            '/usr/share/fonts/truetype/noto',
            '/usr/share/fonts/opentype/noto',
            '/usr/share/fonts/TTF', '/Library/Fonts',
        )
        font_files = {
            True: ('OpenSans-Bold.ttf', 'DejaVuSerif-Bold.ttf',
                   'DejaVuSans-Bold.ttf', 'LiberationSerif-Bold.ttf',
                   'LiberationSans-Bold.ttf', 'NotoSerif-Bold.ttf'),
            False: ('OpenSans-Regular.ttf', 'DejaVuSerif.ttf', 'DejaVuSans.ttf',
                    'LiberationSerif-Regular.ttf', 'NotoSerif-Regular.ttf'),
        }

        def font(size, bold=True):
            key = (bold, size)
            if key in self._font_cache:
                return self._font_cache[key]
            f = None
            for fdir in font_dirs:
                for name in font_files[bold]:
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

        def fit(text, size, bold=True, max_w=W - 240):
            while size > 12:
                f = font(size, bold)
                if d.textlength(text, font=f) <= max_w:
                    return f
                size -= 4
            return font(size, bold)

        def centered(y, text, f, fill=fg):
            w = d.textlength(text, font=f)
            d.text(((W - w) / 2, y), text, font=f, fill=fill)

        d.rectangle([70, 70, W - 70, H - 70], outline=accent, width=6)

        centered(300, 'DAILY', fit('DAILY', 180))
        centered(520, 'DIGEST', fit('DIGEST', 180))
        d.line([170, 800, W - 170, 800], fill=accent, width=5)

        now = datetime.now()
        centered(920, now.strftime('%A'), font(72, bold=False))
        date_str = now.strftime('%d %B %Y')
        centered(1010, date_str, fit(date_str, 130))

        weeklies = list(dict.fromkeys(self._weekly_newsletters))[:5]
        if weeklies:
            d.line([320, 1290, W - 320, 1290], fill=accent, width=3)
            centered(1340, 'This edition also includes', font(46, bold=False))
            y = 1430
            for nm in weeklies:
                centered(y, nm, font(60))
                y += 100

        img.save(cover_file, 'JPEG', quality=90)
        cover_file.flush()
        return True

    # ------------------------------------------------------------------ index
    def parse_index(self):
        feeds = []
        for label, fn in (
            ('%s', self.parse_newsletters),
            ('Indian Express: %s', self.parse_indian_express),
            ('The Hindu: %s', self.parse_hindu),
            ('Live Mint: %s', self.parse_livemint),
            ('Business Standard: %s', self.parse_business_standard),
        ):
            try:
                got = fn()
            except Exception as e:
                self.log.warn('Failed to fetch %s: %s' % (label % 'source', e))
                got = []
            for section, articles in got:
                if articles:
                    feeds.append((label % section, articles))

        if not feeds:
            raise ValueError('No articles could be fetched from any source.')
        return feeds

    # ----------------------------------------------------------- newsletters
    def parse_newsletters(self):
        out = []
        for name, url, weekly in NEWSLETTER_FEEDS:
            try:
                entries = self._fetch_feed(name, url)
            except Exception as e:
                self.log.warn('Newsletter %s: %s' % (name, e))
                continue
            if not entries:
                continue
            fresh = _within_window(entries, NEWSLETTER_MAX_AGE_DAYS)
            if not fresh:
                self.log('Newsletter %s: nothing in the last %s days'
                         % (name, NEWSLETTER_MAX_AGE_DAYS))
                continue
            if weekly or _cadence_is_weekly(entries):
                self._weekly_newsletters.append(name)

            arts = []
            for e in fresh:
                self._url_domain[e['url']] = 'newsletter'
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
            out.append(('Newsletter: ' + name, arts))
        return out

    def _fetch_feed(self, name, url):
        '''RSS feed body -> list of `_feed_entries` dicts. Tries: the direct
        fetch, then (if enabled) calibre's Chromium transport, then a public
        RSS-to-JSON gateway.'''
        try:
            raw = self.index_to_soup(url, raw=True)
            if isinstance(raw, bytes):
                raw = raw.decode('utf-8', 'ignore')
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
        raw = self.index_to_soup(url, raw=True)
        data = json.loads(raw)['data']
        out = []
        for section in data:
            if section == 'EpaperImage' or not self._wanted(section):
                continue
            articles = []
            for article in data[section]:
                a_url = 'https://www.business-standard.com' + article['article_url']
                self._url_domain[a_url] = 'bs'
                articles.append({
                    'title': article['heading1'],
                    'description': article.get('sub_heading') or '',
                    'url': a_url,
                })
            if articles:
                out.append((section, articles))
        return out

    def parse_hindu(self):
        base = 'https://www.thehindu.com'
        edition = 'th_delhi'
        today = date.today().strftime('%Y-%m-%d')
        url = base + '/todays-paper/' + today + '/' + edition + '/'
        soup = self.index_to_soup(url)
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
                for item in data[sec]:
                    a_url = absurl(item['href'], base)
                    self._url_domain[a_url] = 'hindu'
                    desc = 'Page no.' + item.get('pageno', '') + ' | ' + (
                        item.get('teaser_text') or '')
                    feeds_dict[section].append({
                        'title': item['articleheadline'],
                        'url': a_url,
                        'description': desc,
                    })
            break
        return list(feeds_dict.items())

    def parse_livemint(self):
        raw = self.index_to_soup('https://www.livemint.com/rss/opinion', raw=True)
        articles = rss_articles(raw, self.oldest_article, self._url_domain, 'mint')
        return [('Opinion', articles)]

    # ---- Indian Express via Google News discovery + Wayback Machine fetch ----
    def _http_get(self, url, data=None):
        req = mechanize.Request(url, data=data, headers={
            'User-Agent': GNEWS_UA,
            'Accept-Language': 'en-IN,en;q=0.9',
        })
        return self.browser.open_novisit(req, timeout=60).read().decode(
            'utf-8', 'ignore')

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
                self._url_domain[e['url']] = 'ie'
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
            self._url_domain[wb] = 'ie'
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
    def _clean_newsletter(self, soup):
        for x in soup.findAll(attrs={'class': NL_JUNK}):
            x.extract()
        for a in soup.findAll('a'):
            if self.tag_to_string(a).strip().lower() in (
                    'subscribe', 'subscribe now', 'share', 'read online',
                    'view in browser', 'unsubscribe', 'upgrade to paid'):
                a.extract()
        for img in soup.findAll('img', attrs={'width': '1'}):
            img.extract()
        for tag in soup.findAll(['form', 'button', 'iframe', 'script', 'style']):
            tag.extract()

    def _clean_hindu(self, soup):
        self._trim_to(soup, soup.find(attrs={'class': 'article-section'}))
        for x in soup.findAll(attrs={'class': HINDU_JUNK}):
            x.extract()
        for cap in soup.findAll('p', attrs={'class': 'caption'}):
            cap.name = 'figcaption'
        for img in soup.findAll('img', attrs={'data-original': True}):
            img['src'] = img['data-original']
        for h3 in soup.findAll(**classes('sub-title')):
            h3.name = 'p'

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
