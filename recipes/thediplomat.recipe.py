# Copyright (c) 2022 https://github.com/ping/
#
# This software is released under the GNU General Public License v3.0
# https://opensource.org/licenses/GPL-3.0

"""
thediplomat.com

Switched from the WordPress REST API (WordPressNewsrackRecipe) to plain RSS
feeds, matching the current kovidgoyal/calibre upstream recipe: thediplomat.com
now returns HTTP 401 Unauthorized for anonymous `?rest_route=/wp/v2/posts`
requests, so the REST-API-based scraper can no longer fetch any articles.
The RSS feeds below are still public.
"""
from datetime import datetime

from calibre.web.feeds.news import BasicNewsRecipe, classes

_name = "The Diplomat"


class TheDiplomat(BasicNewsRecipe):
    title = _name + " - " + datetime.now().strftime("%d.%m.%y")
    description = "The Diplomat is a current-affairs magazine for the Asia-Pacific, with news and analysis on politics, security, business, technology and life across the region. https://thediplomat.com/"
    language = "en"
    __author__ = "unkn0wn"
    publication_type = "magazine"

    oldest_article = 14  # days
    max_articles_per_feed = 100
    encoding = "utf-8"
    use_embedded_content = False
    no_stylesheets = True
    remove_attributes = ["style", "height", "width"]
    masthead_url = "https://thediplomat.com/wp-content/themes/td_theme_v3/assets/logo/diplomat_logo_black.svg"
    ignore_duplicate_articles = {"url"}
    extra_css = """
        #td-meta { font-size: x-small }
        #td-story-authors { font-size: small}
        #td-story-image{font-size: small; font-style: italic;}
        [id^="caption-attachment"] {font-size: small; font-style: italic;}
    """

    def get_cover_url(self):
        soup = self.index_to_soup("https://thediplomat.com")
        tag = soup.find(attrs={"class": "td-nav-mag"})
        if tag:
            url = tag.find("img")["src"].split("/")[-1]
            self.cover_url = "https://magazine.thediplomat.com/media/1080/" + url
        return super().get_cover_url()

    keep_only_tags = [
        dict(name="header", attrs={"id": "td-story-head"}),
        classes("td-media--story td-prose td-story-magazine__link td-authors"),
    ]

    remove_tags = [
        dict(name="aside", attrs={"id": "td-box-newsletter"}),
        classes("td-share td-ad td-ad-inline-txt"),
    ]

    feeds = [
        ("Features", "https://thediplomat.com/category/features/feed"),
        ("Interviews", "https://thediplomat.com/category/interviews/feed"),
        ("Magazine Preview", "https://thediplomat.com/category/magazine/feed"),
        # REGIONS
        ("Central Asia", "https://thediplomat.com/regions/central-asia/feed"),
        ("East Asia", "https://thediplomat.com/regions/east-asia/feed"),
        ("Oceania", "https://thediplomat.com/regions/oceania-region/feed"),
        ("South Asia", "https://thediplomat.com/regions/south-asia/feed"),
        ("South East Asia", "https://thediplomat.com/regions/southeast-asia/feed"),
        # TOPICS
        ("Diplomacy", "https://thediplomat.com/topics/diplomacy/feed"),
        ("Economy", "https://thediplomat.com/topics/economy/feed"),
        ("Environment", "https://thediplomat.com/topics/environment/feed"),
        ("Opinion", "https://thediplomat.com/topics/opinion/feed"),
        ("Politics", "https://thediplomat.com/topics/politics/feed"),
        ("Security", "https://thediplomat.com/topics/security/feed"),
        ("Society", "https://thediplomat.com/topics/society/feed"),
    ]

    def preprocess_html(self, soup):
        for img in soup.findAll("img", attrs={"src": True}):
            img["src"] = img["src"].replace("td-story-s-1", "td-story-s-2")
        return soup
