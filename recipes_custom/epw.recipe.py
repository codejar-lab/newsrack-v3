#!/usr/bin/env python
# vim:fileencoding=utf-8
# License: GPLv3 Copyright: 2015, Kovid Goyal <kovid at kovidgoyal.net>

import re
from collections import OrderedDict

from calibre.web.feeds.news import BasicNewsRecipe, classes

# matches /journal/<year>/<issue> links regardless of year, so this recipe
# doesn't need to be updated every January (see git history for the bug this
# hardcoded-year check used to cause)
JOURNAL_ISSUE_RE = re.compile(r'/journal/(\d{4})/(\d+)')


def absurl(x):
    if x.startswith('/'):
        x = 'https://www.epw.in' + x
        return x


class EconomicAndPoliticalWeekly(BasicNewsRecipe):
    title = 'Economic and Political Weekly'
    __author__ = 'Kovid Goyal'
    description = 'Economic and Political news from India'
    publisher = 'epw.in'
    category = 'news, finances, politics, India'
    oldest_article = 7
    max_articles_per_feed = 100
    no_stylesheets = True
    use_embedded_content = False
    simultaneous_downloads = 9
    encoding = 'utf-8'
    language = 'en_IN'
    publication_type = 'newspaper'
    masthead_url = 'https://www.epw.in/themes/contrib/epw/images/epw_masthead-new.png'

    keep_only_tags = [
        dict(id='main-content'),
        dict(name='article', attrs={'class': lambda x: x and 'node--type-epw-article' in x}),
    ]
    remove_tags = [
            dict(name=['meta', 'link', 'svg', 'button', 'iframe']),
            classes('premium-message listen-to-this citations mob_only'),
            dict(attrs={'id':['block-textresizemanualcode', 'block-ysreadermodeblock', 'block-supportepwwidget']})
        ]

    def get_cover_url(self):
        # Get the homepage to find the latest issue URL
        soup = self.index_to_soup('https://www.epw.in/')
        
        # Find the latest issue link with multiple strategies
        latest_issue_url = None
        
        # Strategy 1: Look in the latest issue block
        latest_issue_section = soup.find('div', class_='hm-section latest_issue_blk')
        if latest_issue_section:
            self.log('Found latest_issue_blk section')
            # Look for any link with journal pattern
            links = latest_issue_section.find_all('a', href=True)
            for link in links:
                href = link['href']
                self.log('Checking link in latest_issue_blk:', href)
                if JOURNAL_ISSUE_RE.search(href):
                    latest_issue_url = 'https://www.epw.in' + href if href.startswith('/') else href
                    self.log('Found latest issue URL for cover (Strategy 1):', latest_issue_url)
                    break

        # Strategy 2: Look for any journal link on the homepage
        if not latest_issue_url:
            self.log('Strategy 1 failed, trying Strategy 2')
            all_links = soup.find_all('a', href=True)
            journal_links = []
            for link in all_links:
                href = link['href']
                match = JOURNAL_ISSUE_RE.search(href)
                if match:
                    year, issue_num = int(match.group(1)), int(match.group(2))
                    journal_links.append(((year, issue_num), href))

            if journal_links:
                # Sort by (year, issue number) and get the highest (latest)
                journal_links.sort(reverse=True)
                latest_href = journal_links[0][1]
                latest_issue_url = 'https://www.epw.in' + latest_href if latest_href.startswith('/') else latest_href
                self.log('Found latest issue URL for cover (Strategy 2):', latest_issue_url, 'Issue:', journal_links[0][0])

        # Strategy 3: Look for the current year pattern more broadly
        if not latest_issue_url:
            self.log('Strategy 2 failed, trying Strategy 3')
            # Look for any div or section that might contain current issue
            current_issue_elements = soup.find_all(['div', 'section'], class_=lambda x: x and ('current' in x.lower() or 'latest' in x.lower() or 'issue' in x.lower()))
            for element in current_issue_elements:
                self.log('Checking element with class:', element.get('class'))
                links = element.find_all('a', href=True)
                for link in links:
                    href = link['href']
                    if JOURNAL_ISSUE_RE.search(href):
                        latest_issue_url = 'https://www.epw.in' + href if href.startswith('/') else href
                        self.log('Found latest issue URL for cover (Strategy 3):', latest_issue_url)
                        break
                if latest_issue_url:
                    break
        
        # If we still haven't found it, fail without fallback
        if not latest_issue_url:
            self.log('ERROR: Could not find latest issue URL on homepage')
            return None
        
        # Parse the issue page to find the cover image
        try:
            issue_soup = self.index_to_soup(latest_issue_url)
            
            # Look for the cover image with id="journal-cover-img"
            cover_img = issue_soup.find('img', id='journal-cover-img')
            if cover_img and cover_img.get('src'):
                cover_url = cover_img['src']
                # Make sure it's an absolute URL
                if cover_url.startswith('/'):
                    cover_url = 'https://www.epw.in' + cover_url
                
                self.log('Found cover image:', cover_url)
                return cover_url
            else:
                self.log('Could not find cover image with id="journal-cover-img"')
                
        except Exception as e:
            self.log('Error getting cover image:', str(e))
            
        return None

    def parse_index(self):
        # Get the homepage to find the latest issue URL with improved detection
        soup = self.index_to_soup('https://www.epw.in/')
        
        # Find the latest issue link with multiple strategies
        latest_issue_url = None
        
        # Strategy 1: Look in the latest issue block
        latest_issue_section = soup.find('div', class_='hm-section latest_issue_blk')
        if latest_issue_section:
            self.log('Found latest_issue_blk section')
            # Look for any link with journal pattern
            links = latest_issue_section.find_all('a', href=True)
            for link in links:
                href = link['href']
                self.log('Checking link in latest_issue_blk:', href)
                if JOURNAL_ISSUE_RE.search(href):
                    latest_issue_url = 'https://www.epw.in' + href if href.startswith('/') else href
                    self.log('Found latest issue URL (Strategy 1):', latest_issue_url)
                    break

        # Strategy 2: Look for any journal link on the homepage and find the highest issue number
        if not latest_issue_url:
            self.log('Strategy 1 failed, trying Strategy 2')
            all_links = soup.find_all('a', href=True)
            journal_links = []
            for link in all_links:
                href = link['href']
                match = JOURNAL_ISSUE_RE.search(href)
                if match:
                    year, issue_num = int(match.group(1)), int(match.group(2))
                    journal_links.append(((year, issue_num), href))

            if journal_links:
                # Sort by (year, issue number) and get the highest (latest)
                journal_links.sort(reverse=True)
                latest_href = journal_links[0][1]
                latest_issue_url = 'https://www.epw.in' + latest_href if latest_href.startswith('/') else latest_href
                self.log('Found latest issue URL (Strategy 2):', latest_issue_url, 'Issue:', journal_links[0][0])
        
        # If we still haven't found it, error out
        if not latest_issue_url:
            self.log('ERROR: Could not find latest issue URL on homepage')
            return []
        
        # Now parse the latest issue page
        issue_soup = self.index_to_soup(latest_issue_url)
        # Try to extract the issue name/title from the issue page
        issue_name = None
        # Common patterns: h1, div.issue-title, etc.
        h1 = issue_soup.find('h1')
        if h1 and h1.get_text(strip=True):
            issue_name = h1.get_text(strip=True)
        else:
            # Try div with class containing 'issue' or 'title'
            div_title = issue_soup.find('div', class_=lambda x: x and ('issue' in x.lower() or 'title' in x.lower()))
            if div_title and div_title.get_text(strip=True):
                issue_name = div_title.get_text(strip=True)
        if issue_name:
            self.title = issue_name
            self.log('Set recipe title to issue name:', issue_name)
        self.title = 'EPW: ' + issue_name # type: ignore
        sections = OrderedDict()
        # Find the main content area that contains all the sections
        content_area = issue_soup.find('div', class_='view-content')
        if not content_area:
            self.log('Could not find view-content area')
            return []
        # Parse the content by looking for h3 section headers followed by article rows
        current_section = None
        for element in content_area.find_all(['h3', 'div']):
            if element.name == 'h3':
                # This is a section header
                section_link = element.find('a')
                if section_link:
                    current_section = self.tag_to_string(section_link).strip()
                    if current_section not in sections:
                        sections[current_section] = []
                        self.log('\n\n' + current_section)
            elif element.name == 'div' and 'views-row' in element.get('class', []):
                # This is an article row
                if current_section:
                    title_div = element.find('div', class_='views-field-title')
                    if title_div:
                        # Look for the link inside the title div - it might be in an h4 or directly in the div
                        title_link = title_div.find('a', href=True)
                        if title_link:
                            title = self.tag_to_string(title_link).strip()
                            url = absurl(title_link['href'])
                            # Get authors if available
                            desc = ''
                            authors_div = element.find('div', class_='views-field-field-authors')
                            if authors_div:
                                authors = self.tag_to_string(authors_div).strip()
                                if authors:
                                    desc = authors
                            self.log('\t', title, url)
                            if desc:
                                self.log('\t\t', desc)
                            sections[current_section].append({
                                'title': title,
                                'url': url,
                                'description': desc
                            })
        return [(t, articles) for t, articles in sections.items() if articles]