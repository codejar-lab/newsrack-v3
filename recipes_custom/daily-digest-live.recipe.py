#!/usr/bin/env python
# vim:fileencoding=utf-8
'''
Daily Digest (live) -- same content as `india-opinion`, but fetched through
calibre's Chromium/QtWebEngine backend (HTTP/2, real Chrome TLS fingerprint)
with low request pressure.

Rationale (from calibre source, commit 025d9cf, 2026-09-07):
  1. `browser_type = 'webengine'` swaps Mechanize for a Chromium network stack
     -- the first thing to try when a normal browser works but the recipe 403s.
  2. `simultaneous_downloads = 1` + `delay` -- for "succeeds at first, fails
     later" rate-limiting (Phoronix / Science / Economist do this).
  3. Chromium is also used for the individual troublesome fetches (Indian
     Express section feeds, Cloudflare-fronted Substack feeds) before falling
     back to the Google-News + Wayback / RSS-gateway workarounds.

WebEngine is not a Cloudflare/CAPTCHA solver and cannot change a blocked IP:
where the deploy IP is hard-blocked this behaves exactly like `india-opinion`.
Needs calibre >= 8; on older calibre `browser_type` is an inert attribute and
this simply runs the workarounds.
'''
import os
import sys
from datetime import datetime

sys.path.append(os.environ['recipes_includes'])
import daily_digest  # noqa: E402


class DailyDigestLive(daily_digest.DailyDigestBase):
    title = (daily_digest._name + ' (live) - '
             + datetime.now().strftime('%d.%m.%y'))

    browser_type = 'webengine'   # calibre >= 8: Chromium network stack, HTTP/2
    simultaneous_downloads = 1
    delay = 2                    # seconds between requests
    chromium_first = True        # try the site directly before the workarounds
