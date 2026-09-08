#!/usr/bin/env python
# vim:fileencoding=utf-8
'''
Daily Digest -- Opinion / Op-Ed / Editorial pages of Business Standard, The
Hindu, Live Mint and The Indian Express, the Science page of The Hindu, plus a
set of newsletters.

All the logic lives in recipes/includes/daily_digest.py so the plain and the
`browser_type='webengine'` variant (daily-digest-live) stay in sync. The base
is referenced via the module (not imported by name) so calibre's recipe
detection picks the class defined *here*.
'''
import os
import sys

sys.path.append(os.environ['recipes_includes'])
import daily_digest  # noqa: E402


class IndiaOpinionDigest(daily_digest.DailyDigestBase):
    pass
