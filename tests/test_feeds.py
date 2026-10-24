"""``hookspool.feeds`` (ARCHITECTURE 14.4): Atom and RSS entries as dicts, and a parser that refuses what a feed
never needs (a DOCTYPE, entities, external references) and bounds the rest."""
from __future__ import annotations

import pytest

from hookspool import feeds

ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:x="urn:example:ext">
  <id>urn:feed:1</id><title> Example   Feed </title><subtitle>About</subtitle><updated>2026-01-06T10:00:00Z</updated>
  <link rel="self" href="https://example.com/feed.atom"/><link rel="alternate" href="https://example.com/"/>
  <entry>
    <id>urn:entry:2</id><title>Second
      entry</title><updated>2026-01-06T10:00:00-05:00</updated><published>2026-01-05T10:00:00Z</published>
    <link rel="enclosure" href="https://example.com/file.bin"/><link href="https://example.com/2"/>
    <summary type="html">&lt;b&gt;hi&lt;/b&gt;</summary>
    <content type="text/xml"><number>7</number><name> Example </name></content>
    <author><name>An Author</name></author>
    <category term="news" label="News" scheme="https://example.com/scheme"/><category label="only label"/><category/>
    <x:rank>3</x:rank>
  </entry>
  <entry><id>urn:entry:1</id><title>First</title><content type="text">plain body</content></entry>
</feed>"""

RSS = """<?xml version="1.0"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>Channel</title><link>https://example.com/</link><description>About it</description>
  <lastBuildDate>Tue, 06 Jan 2026 10:00:00 GMT</lastBuildDate>
  <item><guid>g-1</guid><title>Item one</title><link>https://example.com/1</link><pubDate>Tue, 06 Jan 2026 09:00:00 GMT</pubDate>
    <description>Short</description><content:encoded>Long body</content:encoded><dc:creator>Someone</dc:creator>
    <category domain="https://example.com/c">tag</category></item>
  <item><title>Item two</title><link>https://example.com/2</link></item>
</channel></rss>"""

RDF = """<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/"
  xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel><title>Old style</title><link>https://example.com/</link></channel>
<item><title>Only item</title><link>https://example.com/o</link><dc:date>2026-01-01T00:00:00Z</dc:date></item>
</rdf:RDF>"""


# ---- what comes out ---------------------------------------------------------------------------------------
def test_an_atom_feed_becomes_dicts():
    f = feeds.parse_feed(ATOM)
    assert (f["format"], f["id"], f["title"], f["subtitle"], f["updated"]) == (
        "atom", "urn:feed:1", "Example Feed", "About", "2026-01-06T10:00:00Z")
    assert f["link"] == "https://example.com/"                       # rel=alternate, not rel=self
    second, first = f["entries"]
    assert second["id"] == "urn:entry:2" and second["title"] == "Second entry"
    assert second["link"] == "https://example.com/2"                 # a rel-less link is the alternate; not the enclosure
    assert (second["updated"], second["published"]) == ("2026-01-06T10:00:00-05:00", "2026-01-05T10:00:00Z")
    assert second["summary"] == "<b>hi</b>" and second["author"] == "An Author"
    assert second["categories"] == [{"term": "news", "label": "News", "scheme": "https://example.com/scheme"},
                                    {"term": "", "label": "only label", "scheme": ""}]
    assert second["fields"] == {"number": "7", "name": "Example", "rank": "3"}
    assert first["content"] == "plain body" and first["fields"] == {} and first["link"] == ""


def test_an_rss_feed_becomes_dicts():
    f = feeds.parse_feed(RSS)
    assert (f["format"], f["title"], f["subtitle"], f["link"]) == ("rss", "Channel", "About it", "https://example.com/")
    assert f["updated"] == "Tue, 06 Jan 2026 10:00:00 GMT"
    one, two = f["entries"]
    assert (one["id"], one["title"], one["link"], one["summary"], one["content"], one["author"]) == (
        "g-1", "Item one", "https://example.com/1", "Short", "Long body", "Someone")
    assert one["published"] == "Tue, 06 Jan 2026 09:00:00 GMT"
    assert one["categories"] == [{"term": "tag", "label": "", "scheme": "https://example.com/c"}]
    assert two["id"] == "" and two["link"] == "https://example.com/2"


def test_an_rss_1_0_feed_becomes_dicts():
    f = feeds.parse_feed(RDF)
    assert f["format"] == "rss" and f["title"] == "Old style" and len(f["entries"]) == 1
    assert f["entries"][0]["title"] == "Only item" and f["entries"][0]["updated"] == "2026-01-01T00:00:00Z"


def test_bytes_and_an_encoding_declaration_work():
    latin = '<?xml version="1.0" encoding="ISO-8859-1"?><feed xmlns="http://www.w3.org/2005/Atom"><title>Caf\xe9</title></feed>'
    assert feeds.parse_feed(latin.encode("latin-1"))["title"] == "Caf\xe9"
    assert feeds.parse_feed(latin)["title"] == "Caf\xe9"                    # a str with a declaration is fine too
    assert feeds.parse_feed(b"\xef\xbb\xbf" + ATOM.encode())["id"] == "urn:feed:1"
    assert feeds.parse_feed(bytearray(ATOM.encode()))["id"] == "urn:feed:1"


def test_a_feed_with_no_entries_is_valid():
    assert feeds.parse_feed('<feed xmlns="http://www.w3.org/2005/Atom"><title>t</title></feed>')["entries"] == []


def test_text_is_capped():
    big = "x" * 100_000
    f = feeds.parse_feed(f'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>{big}</title>'
                         f'<summary>{big}</summary></entry></feed>')
    e = f["entries"][0]
    assert len(e["title"]) == feeds.MAX_TITLE_CHARS and len(e["summary"]) == feeds.MAX_TEXT_CHARS


@pytest.mark.parametrize("value,want", [
    ("2026-01-02T10:00:00-05:00", 1767366000.0), ("2026-01-02T15:00:00Z", 1767366000.0),
    ("2026-01-02T15:00:00", 1767366000.0), ("Fri, 02 Jan 2026 15:00:00 GMT", 1767366000.0),
    ("", None), ("yesterday", None), (None, None), (5, None)])
def test_parse_time(value, want):
    assert feeds.parse_time(value) == want


# ---- what is refused --------------------------------------------------------------------------------------
LAUGHS = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
          '<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">]>'
          '<feed xmlns="http://www.w3.org/2005/Atom"><title>&lol3;</title></feed>')
FILE = ('<?xml version="1.0"?><!DOCTYPE f [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        '<feed xmlns="http://www.w3.org/2005/Atom"><title>&x;</title></feed>')
URL = ('<?xml version="1.0"?><!DOCTYPE f [<!ENTITY x SYSTEM "http://127.0.0.1:1/x">]>'
       '<feed xmlns="http://www.w3.org/2005/Atom"><title>&x;</title></feed>')
PARAM = ('<?xml version="1.0"?><!DOCTYPE f [<!ENTITY % p SYSTEM "http://127.0.0.1:1/x.dtd"> %p;]>'
         '<feed xmlns="http://www.w3.org/2005/Atom"/>')
EXT_DTD = '<?xml version="1.0"?><!DOCTYPE feed SYSTEM "http://127.0.0.1:1/feed.dtd"><feed xmlns="http://www.w3.org/2005/Atom"/>'
EMPTY_DTD = '<!DOCTYPE feed><feed xmlns="http://www.w3.org/2005/Atom"/>'
NOTATION = '<!DOCTYPE feed [<!NOTATION n SYSTEM "x">]><feed xmlns="http://www.w3.org/2005/Atom"/>'
ATTLIST = '<!DOCTYPE feed [<!ATTLIST feed a CDATA "b">]><feed xmlns="http://www.w3.org/2005/Atom"/>'
QUADRATIC = ('<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "' + "a" * 5000 + '">]><feed xmlns="http://www.w3.org/2005/Atom"><title>'
             + "&a;" * 5000 + "</title></feed>")


@pytest.mark.parametrize("doc", [LAUGHS, FILE, URL, PARAM, EXT_DTD, EMPTY_DTD, NOTATION, ATTLIST, QUADRATIC],
                         ids=["billion-laughs", "file-entity", "url-entity", "parameter-entity", "external-dtd",
                              "empty-doctype", "notation", "attlist", "quadratic-blowup"])
def test_a_doctype_or_entity_declaration_is_refused_before_anything_expands(doc):
    for form in (doc, doc.encode()):
        with pytest.raises(feeds.FeedError, match="refused"):
            feeds.parse_feed(form)


def test_the_doctype_is_refused_in_other_encodings_too():
    doc = '<?xml version="1.0" encoding="UTF-16"?>' + LAUGHS.split("?>", 1)[1]
    with pytest.raises(feeds.FeedError):
        feeds.parse_feed(doc.encode("utf-16"))


def test_an_undefined_entity_is_an_error_not_an_expansion():
    with pytest.raises(feeds.FeedError, match="not well-formed"):
        feeds.parse_feed('<feed xmlns="http://www.w3.org/2005/Atom"><title>&nope;</title></feed>')


def test_the_standard_character_references_still_work():
    f = feeds.parse_feed('<feed xmlns="http://www.w3.org/2005/Atom"><title>a &amp; b &#65; &lt;</title></feed>')
    assert f["title"] == "a & b A <"


@pytest.mark.parametrize("doc", ["", "   \n", "not xml at all", "<feed", "<html><body>x</body></html>",
                                 '<feed xmlns="urn:other"/>', "<rss version='2.0'/>", "<a><b></a>", "\x00\x01"])
def test_what_is_not_a_feed_is_refused(doc):
    with pytest.raises(feeds.FeedError):
        feeds.parse_feed(doc)


@pytest.mark.parametrize("value", [None, 5, ["<feed/>"], {"a": 1}])
def test_only_text_or_bytes_are_accepted(value):
    with pytest.raises(feeds.FeedError):
        feeds.parse_feed(value)


def test_a_document_over_the_size_cap_is_refused():
    doc = '<feed xmlns="http://www.w3.org/2005/Atom"><title>' + "x" * 200 + "</title></feed>"
    with pytest.raises(feeds.FeedError, match="over 100 bytes"):
        feeds.parse_feed(doc, max_bytes=100)
    with pytest.raises(feeds.FeedError, match="over 100 bytes"):
        feeds.parse_feed(doc.encode(), max_bytes=100)
    assert feeds.parse_feed(doc)["title"].startswith("xxx")


def test_the_size_cap_counts_encoded_bytes_of_a_str():
    doc = '<feed xmlns="http://www.w3.org/2005/Atom"><title>' + "é" * 60 + "</title></feed>"
    assert len(doc) < 150 < len(doc.encode())
    with pytest.raises(feeds.FeedError, match="over 150 bytes"):
        feeds.parse_feed(doc, max_bytes=150)


def test_too_many_entries_are_refused():
    doc = '<feed xmlns="http://www.w3.org/2005/Atom">' + "<entry><id>x</id></entry>" * 11 + "</feed>"
    with pytest.raises(feeds.FeedError, match="more than 10 entries"):
        feeds.parse_feed(doc, max_entries=10)
    assert len(feeds.parse_feed(doc)["entries"]) == 11


def test_deep_nesting_is_refused():
    doc = '<feed xmlns="http://www.w3.org/2005/Atom">' + "<a>" * 5000 + "</a>" * 5000 + "</feed>"
    with pytest.raises(feeds.FeedError, match="nested"):
        feeds.parse_feed(doc)


def test_an_error_names_the_rule_and_never_the_content():
    secret = "SENTINEL-do-not-echo"
    for doc in (f'<!DOCTYPE {secret} [<!ENTITY {secret} "v">]><feed/>', f"<feed>{secret}", f"<{secret}/>"):
        with pytest.raises(feeds.FeedError) as ei:
            feeds.parse_feed(doc)
        assert secret not in str(ei.value) and ei.value.__cause__ is None
