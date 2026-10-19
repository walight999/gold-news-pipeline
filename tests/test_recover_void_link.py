"""2026-10-19 13:15 UTC: after #190 shipped the viewer unwrap, the same actor
returned a SECOND shape — the feed re-serialized by an HTML parser: <link/>
closed as a void element with the URL left as the next text node, and camelCase
tags lowercased. Production logged '30 entries but none has a URL'. The
fixture below is copied from that live response's structure."""
from __future__ import annotations

from src.apify_source import unwrap_xml_viewer
from src.normalizer import normalize
from src.parser import parse_feed

SRC = {"id": "fxstreet_apify", "tier": 2, "role": "gold_bias",
       "source_class": "wire", "organization": "fxstreet"}

HTML_SERIALIZED = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
 <channel>
  <title>
   FXStreet Forex &amp; Commodities News
  </title>
  <link/>
  https://www.fxstreet.com/news/feed
  <item>
   <title>
    <![CDATA[We are all watching bond yields as a precursor for the currency market]]>
   </title>
   <link/>
   https://www.fxstreet.com/analysis/we-are-all-watching-bond-yields-202610191244
   <guid ispermalink="false">
    71f385dc-33ee-4642-a285-820839f85a1e
   </guid>
   <pubdate>
    Mon, 19 Oct 2026 12:44:23 GMT
   </pubdate>
   <description>
    <![CDATA[Bond yields and currencies decouple over short periods.]]>
   </description>
  </item>
  <item>
   <title>
    <![CDATA[British Pound: BoE tightening seen over-priced - BBH]]>
   </title>
   <link />
   https://www.fxstreet.com/news/british-pound-boe-tightening-202610191230
   <pubdate>
    Mon, 19 Oct 2026 12:30:00 GMT
   </pubdate>
  </item>
 </channel>
</rss>
"""


def test_void_link_shape_loses_urls_without_repair():
    raw = parse_feed(HTML_SERIALIZED.encode(), SRC)
    assert len(raw) == 2 and not any(e.get("url", "").startswith("https://") for e in raw)


def test_void_link_repair_restores_urls_and_dates():
    es = parse_feed(unwrap_xml_viewer(HTML_SERIALIZED.encode()), SRC)
    assert [e["url"] for e in es] == [
        "https://www.fxstreet.com/analysis/we-are-all-watching-bond-yields-202610191244",
        "https://www.fxstreet.com/news/british-pound-boe-tightening-202610191230"]
    assert all(e.get("published_ts") is not None for e in es)      # pubdate → pubDate
    assert es[0]["title"].startswith("We are all watching bond yields")
    assert len(normalize(es)) == 2


def test_proper_xml_with_link_elements_untouched():
    good = (b'<rss version="2.0"><channel><item><title>T</title>'
            b'<link>https://x.example/a</link></item></channel></rss>')
    assert unwrap_xml_viewer(good) is good
