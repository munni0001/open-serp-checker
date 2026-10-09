"""Bing parser test — hits `parse()` on a hand-built mini-HTML, no network.

The Bing engine's full fetch+parse is unit-covered elsewhere by live-collected
fixtures; this file covers the hot-path invariants that would catch a regression
during refactoring (e.g. a selector change breaking all callers).
"""
from src.core.engines import bing


# Minimal Bing SERP shape — li.b_algo > h2 > a[href]. The /ck/a wrapper is
# Bing's redirect form; `_resolve_redirect` must unwrap it.
BING_HTML = """
<html><body>
  <ol id="b_results">
    <li class="b_algo"><h2><a href="https://wikipedia.org/wiki/Python">Wikipedia</a></h2></li>
    <li class="b_algo"><h2><a href="https://realpython.com/">Real Python</a></h2></li>
    <li class="b_algo"><h2><a href="https://www.example.com/py">Example</a></h2></li>
  </ol>
</body></html>
"""


def test_bing_finds_match_at_rank_3():
    r = bing.parse(BING_HTML, "example.com", max_position=10)
    assert r["found"] is True
    assert r["position"] == 3


def test_bing_strips_www_prefix_on_match():
    r = bing.parse(BING_HTML, "www.example.com", max_position=10)
    assert r["position"] == 3


def test_bing_not_found_returns_falsy():
    r = bing.parse(BING_HTML, "missing.test", max_position=10)
    assert r == {"position": None, "url": "", "found": False}


def test_bing_empty_html_returns_not_found():
    r = bing.parse("", "example.com", max_position=10)
    assert r["found"] is False


def test_bing_max_position_caps_scan():
    r = bing.parse(BING_HTML, "example.com", max_position=2)
    # example.com is at rank 3 — top-2 scan must miss it.
    assert r["found"] is False
