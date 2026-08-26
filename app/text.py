"""
Cleaning catalogue prose.

McGill's course descriptions are scraped from a rendered page, and 704 of the
9,933 carry the page's markup with them -- 1,504 anchor tags, each one a link
back to another course in McGill's own search UI:

    covers the first level (<a data-action="result-detail"
    data-group="code:GERM 202D1" href="/search/?p=GERM%20202D1">GERM 202D1</a>)

The frontend escapes everything it renders, which is right -- descriptions are
third-party text and rendering them as HTML would be an injection hole. But the
consequence is that those 704 courses displayed their markup as visible text.

So the markup is removed rather than trusted. The anchor's TEXT is kept, since
that text is the course code a reader wants: "covers the first level (GERM
202D1/GERM 202D2) in one term" is exactly the sentence McGill meant to write.

Done at serialization rather than in the database, so it holds no matter what a
future catalogue load puts there, and needs no migration to start working.
"""

import html
import re

_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"[ \t ]+")


def clean(text):
    """Catalogue prose with its markup removed and its entities decoded."""
    if not text:
        return text
    out = _TAG.sub("", text)
    # After the tags: "&amp;" is common, and a handful carry &nbsp; and numeric
    # entities. Unescaping second means an entity that encoded a bracket cannot
    # reintroduce a tag.
    out = html.unescape(out)
    out = _SPACE.sub(" ", out)
    return out.strip()
