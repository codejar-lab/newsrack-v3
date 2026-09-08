#!/usr/bin/env python
# vim:fileencoding=utf-8
'''
Inshorts -- https://inshorts.com/en/read

Section-wise 60-word news summaries. Everything is pulled from Inshorts' own
data, so calibre never has to download an article page:

- "Top Stories" and "Trending" come from the public JSON feed
    GET /api/en/news?category=<cat>&max_limit=10&include_card_data=true
        [&news_offset=<min_news_id>]
  which is exactly what inshorts.com/en/read calls on scroll. It paginates:
  the response's ``data.min_news_id`` is fed back as ``news_offset`` for the
  next (older) page -- that is the site's "lazy loading". "Top Stories" is
  walked a few pages deep; "Trending" returns a big batch in one shot.

- Every other section (Business, Politics, World, ...) is a *tag* page. Its
  feed endpoint currently 500s for everyone (the site itself only shows the
  first slice), so those are read from the ``window.__STATE__`` blob embedded
  in the server-rendered https://inshorts.com/en/read/<slug> page -- ~10 fully
  formed cards each, no lazy loading available.

Each card already carries the whole short (``news_obj.content``), the headline,
image, source name/url and timestamp, so the article body is assembled here and
handed to calibre via the ``content`` key.
'''
import json
import re
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from xml.sax.saxutils import escape

import mechanize
from calibre.web.feeds.news import BasicNewsRecipe

_name = 'Inshorts'

API_URL = ('https://inshorts.com/api/en/news?category={cat}&max_limit=10'
           '&include_card_data=true')
READ_URL = 'https://inshorts.com/en/read/{slug}'
ARTICLE_URL = 'https://inshorts.com/en/news/{old_hash_id}'

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36')

# (label, category) sections served by the working JSON feed. `pages` is how
# many times to follow news_offset (10 cards/page).
API_SECTIONS = (
    ('Top Stories', 'top_stories', 3),
    ('Trending', 'trending', 1),
)

# (label, slug) sections read from the embedded __STATE__ of the tag page
# (their own feed endpoint is broken server-side -> ~10 cards, no pagination)
TAG_SECTIONS = (
    ('Business', 'business'),
    ('Politics', 'politics'),
    ('World', 'world'),
    ('India', 'national'),
    ('Startup', 'startup'),
    ('Technology', 'technology'),
    ('Sports', 'sports'),
    ('Science', 'science'),
    ('Entertainment', 'entertainment'),
    ('Automobile', 'automobile'),
    ('Education', 'education'),
    ('Environment', 'environment'),
    ('Hatke', 'hatke'),
    ('Miscellaneous', 'miscellaneous'),
)

_STATE_RE = re.compile(r'window\.__STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>', re.S)


class Inshorts(BasicNewsRecipe):
    title = _name + ' - ' + datetime.now().strftime('%d.%m.%y')
    __author__ = 'newsrack'
    description = (
        'Section-wise 60-word news summaries from Inshorts '
        '(https://inshorts.com/en/read).'
    )
    language = 'en_IN'
    encoding = 'utf-8'
    publication_type = 'newspaper'
    no_stylesheets = True
    remove_javascript = True
    remove_attributes = ['style', 'height', 'width']
    ignore_duplicate_articles = {'url'}
    remove_empty_feeds = True
    max_articles_per_feed = 20  # keep each section a lean scroll
    resolve_internal_links = False
    oldest_article = 1.5  # days -- drop anything staler than the last build
    timefmt = ''
    summary_length = 0  # section index = headline list only
    masthead_url = 'https://assets.inshorts.com/website_assets/images/logo_inshorts.png'

    extra_css = '''
        img {display:block; margin:0 auto;
             filter:grayscale(100%) !important;
             -webkit-filter:grayscale(100%) !important;}
        .byline {font-size:small; color:#202020; margin:0 0 .6em;}
        .src {font-size:small; color:#404040; margin-top:1em;}
    '''

    # ------------------------------------------------------------------ fetch
    def _get(self, url):
        br = self.clone_browser(self.browser)
        req = mechanize.Request(url, headers={
            'User-Agent': UA,
            'Accept-Language': 'en-IN,en;q=0.9',
        })
        return br.open_novisit(req, timeout=45).read().decode('utf-8', 'ignore')

    def _cutoff(self):
        return datetime.now(timezone.utc) - timedelta(days=self.oldest_article)

    # ------------------------------------------------------------------ cards
    @staticmethod
    def _card_list(payload):
        '''Normalise the two shapes: the JSON feed nests each card under
        ``news_obj``; __STATE__ list items do the same.'''
        data = payload.get('data', payload)
        return data.get('news_list', []) or data.get('list', []), \
            data.get('min_news_id')

    def _article(self, card):
        o = card.get('news_obj') or card
        if (o.get('news_type') or 'NEWS') != 'NEWS':
            return None
        title = (o.get('title') or '').strip()
        content = (o.get('content') or '').strip()
        if not title or not content:
            return None
        ts = o.get('created_at')
        dt = None
        if ts:
            dt = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc)
            if dt < self._cutoff():
                return None

        old_hash = o.get('old_hash_id') or o.get('hash_id') or ''
        source_name = o.get('source_name') or 'Inshorts'
        author = o.get('author_name') or ''
        image = o.get('image_url') or ''

        meta = source_name + (' · ' + author if author else '')
        if dt:
            meta += ' · ' + dt.astimezone(
                timezone(timedelta(hours=5, minutes=30))).strftime(
                    '%d %b %Y, %I:%M %p IST')

        # card only -- headline, byline, image, the 60-word short. No link to
        # (and no fetch of) the publisher's full article page.
        body = ['<h1>%s</h1>' % escape(title),
                '<p class="byline">%s</p>' % escape(meta)]
        if image:
            body.append('<img src="%s"/>' % escape(image, {'"': '&quot;'}))
        body.append('<p>%s</p>' % escape(content))

        art = {
            'title': title,
            # dedup key only -- never fetched, since 'content' is supplied
            'url': ARTICLE_URL.format(old_hash_id=old_hash) if old_hash
            else (o.get('source_url') or 'https://inshorts.com/'),
            'description': content,
            'content': '<html><body>' + ''.join(body) + '</body></html>',
        }
        if dt:
            art['date'] = dt.strftime('%a, %d %b %Y %H:%M:%S GMT')
        return art

    # --------------------------------------------------------------- sections
    def _api_section(self, label, cat, pages):
        seen, arts, offset = set(), [], None
        for _ in range(max(1, pages)):
            url = API_URL.format(cat=cat)
            if offset:
                url += '&news_offset=' + offset
            try:
                cards, offset = self._card_list(json.loads(self._get(url)))
            except Exception as e:
                self.log.warn('Inshorts %s: %s' % (label, e))
                break
            fresh = 0
            for c in cards:
                hid = (c.get('news_obj') or c).get('hash_id') or id(c)
                if hid in seen:
                    continue
                seen.add(hid)
                a = self._article(c)
                if a:
                    arts.append(a)
                    fresh += 1
            if not offset or not fresh:
                break
        return (label, arts) if arts else None

    def _tag_section(self, label, slug):
        try:
            html = self._get(READ_URL.format(slug=slug))
            m = _STATE_RE.search(html)
            if not m:
                self.log.warn('Inshorts %s: no __STATE__ on the page' % label)
                return None
            cards = json.loads(m.group(1)).get('news_list', {}).get('list', [])
        except Exception as e:
            self.log.warn('Inshorts %s: %s' % (label, e))
            return None
        arts = [a for a in (self._article(c) for c in cards) if a]
        return (label, arts) if arts else None

    def parse_index(self):
        jobs = [(self._api_section, s) for s in API_SECTIONS] + \
               [(self._tag_section, s) for s in TAG_SECTIONS]
        with ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(lambda j: j[0](*j[1]), jobs))

        feeds = [r for r in results if r and r[1]]
        if not feeds:
            raise ValueError('Inshorts: no articles could be fetched.')
        return feeds
