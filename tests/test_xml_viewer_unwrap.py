"""2026-10-19: the first live FXStreet recovery parsed 30 entries but produced 0
events. scrapeunblocker returns Chrome's pretty-print XML viewer; <link> is an
HTML void element there, so every URL was lost and normalize() dropped them all
silently. unwrap_xml_viewer rebuilds the raw XML."""
from __future__ import annotations

import html

from src.apify_source import unwrap_xml_viewer
from src.normalizer import normalize
from src.parser import parse_feed

SRC = {"id": "fxstreet_apify", "tier": 2, "role": "gold_bias",
       "source_class": "wire", "organization": "fxstreet"}

RSS = ('<?xml version="1.0" encoding="utf-8"?>'
       '<rss xmlns:atom="http://www.w3.org/2005/Atom" version="2.0"><channel>'
       '<title>FXStreet Forex &amp; Commodities News</title>'
       '<link>https://www.fxstreet.com/news/feed</link>'
       '<item><title>Gold climbs as US yields slip</title>'
       '<link>https://www.fxstreet.com/news/gold-climbs-202610191200</link>'
       '<description>XAU/USD rises &amp; holds above $4,150.</description>'
       '<pubDate>Mon, 19 Oct 2026 12:00:00 Z</pubDate></item>'
       '<item><title>Fed&#39;s Waller speaks</title>'
       '<link>https://www.fxstreet.com/news/waller-202610191210</link>'
       '<pubDate>Mon, 19 Oct 2026 12:10:00 Z</pubDate></item>'
       '</channel></rss>')


def _as_chrome_viewer(xml: str) -> str:
    """Mimic Chrome's viewer: each tag escaped inside spans, tokens split over
    lines, attributes in their own spans (shape copied from a real response)."""
    import re
    body = xml.split("?>", 1)[1]
    out = []
    for tok in re.findall(r"<[^>]+>|[^<]+", body):
        if tok.startswith("<"):
            m = re.match(r"<(/?)(\S+?)((?:\s+[^>]*?)?)(/?)>", tok)
            close, name, attrs, selfc = m.groups()
            parts = [f'<span class="html-tag">\n      &lt;{close}{name}\n']
            for an, av in re.findall(r'(\S+?)="([^"]*)"', attrs or ""):
                parts.append('<span class="html-attribute">\n <span class="html-attribute-name">\n'
                             f'        {an}\n       </span>\n       ="\n'
                             '<span class="html-attribute-value">\n'
                             f'        {html.escape(av)}\n       </span>\n       "\n      </span>\n')
            parts.append(f"      {selfc}&gt;\n     </span>\n")
            out.append("".join(parts))
        else:
            out.append(f'<span class="text">\n {html.escape(html.unescape(tok))}\n</span>\n')
    return ('<html><head><style>div.header{}</style></head><body>'
            '<div class="header"><span>This XML file does not appear to have any style '
            'information</span></div><div class="pretty-print"><div class="folder">'
            + "".join(f'<div class="line">{x}</div>' for x in out) + "</div></div></body></html>")


def test_viewer_page_loses_urls_without_unwrap():
    # the failure we saw live: titles parse, URLs don't, normalize drops all
    viewer = _as_chrome_viewer(RSS)
    raw = parse_feed(viewer.encode(), SRC)
    assert not any(e.get("url") for e in raw)
    assert normalize(raw) == []


def test_unwrap_restores_links_and_text():
    viewer = _as_chrome_viewer(RSS)
    es = parse_feed(unwrap_xml_viewer(viewer.encode()), SRC)
    assert [e["url"] for e in es] == [
        "https://www.fxstreet.com/news/gold-climbs-202610191200",
        "https://www.fxstreet.com/news/waller-202610191210"]
    assert es[0]["title"] == "Gold climbs as US yields slip"
    assert "&" in es[0]["summary"] and "$4,150" in es[0]["summary"]
    assert es[1]["title"] == "Fed's Waller speaks"
    assert len(normalize(es)) == 2


def test_unwrap_accepts_str_and_passes_plain_xml_through():
    viewer = _as_chrome_viewer(RSS)
    assert unwrap_xml_viewer(viewer).startswith(b"<rss")
    plain = RSS.encode()
    assert unwrap_xml_viewer(plain) is plain
    assert unwrap_xml_viewer(None) is None and unwrap_xml_viewer(b"") == b""
    # a viewer page with no rss/feed root is returned unchanged
    junk = '<div class="pretty-print">&lt;html&gt;</div>'
    assert unwrap_xml_viewer(junk) == junk
