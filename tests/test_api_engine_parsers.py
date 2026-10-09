"""Parser tests for the JSON-based engines (SearXNG, DataForSEO, Serper).

No network: the `search()` function in each engine is a thin HTTP wrapper
over the `parse(data, domain, max_position)` pure function. These tests hit
`parse()` directly with hand-crafted response shapes.
"""
from src.core.engines import dataforseo, searxng, serper


# ---- SearXNG ----

SEARXNG_PAYLOAD = {
    "results": [
        {"url": "https://wikipedia.org/wiki/Python", "title": "Wikipedia"},
        {"url": "https://realpython.com/", "title": "Real Python"},
        {"url": "https://www.example.com/py", "title": "Example"},
    ],
}


def test_searxng_finds_match_at_rank_3():
    r = searxng.parse(SEARXNG_PAYLOAD, "example.com", max_position=10)
    assert r["found"] is True
    assert r["position"] == 3
    assert r["url"] == "https://www.example.com/py"


def test_searxng_strips_www_prefix_on_match():
    r = searxng.parse(SEARXNG_PAYLOAD, "www.example.com", max_position=10)
    assert r["position"] == 3


def test_searxng_not_found_returns_falsy():
    r = searxng.parse(SEARXNG_PAYLOAD, "missing.test", max_position=10)
    assert r == {"position": None, "url": "", "found": False}


def test_searxng_empty_target_returns_not_found():
    r = searxng.parse(SEARXNG_PAYLOAD, "", max_position=10)
    assert r["found"] is False


def test_searxng_max_position_caps_scan():
    r = searxng.parse(SEARXNG_PAYLOAD, "example.com", max_position=2)
    # example.com is at rank 3; scanning only top 2 should miss it.
    assert r["found"] is False


# ---- DataForSEO ----

DATAFORSEO_PAYLOAD = {
    "tasks": [{
        "result": [{
            "items": [
                {"type": "organic", "rank_absolute": 1, "url": "https://wikipedia.org/wiki/Python"},
                # ads/paa items sprinkled in — must be skipped by the type filter.
                {"type": "paid", "rank_absolute": 2, "url": "https://ad.sponsored.test/"},
                {"type": "organic", "rank_absolute": 3, "url": "https://realpython.com/"},
                {"type": "organic", "rank_absolute": 5, "url": "https://www.example.com/py"},
            ],
        }],
    }],
}


def test_dataforseo_uses_rank_absolute_not_list_index():
    r = dataforseo.parse(DATAFORSEO_PAYLOAD, "example.com", max_position=10)
    # example.com has rank_absolute=5 — must return 5, not 3 (its position in the organic-only list).
    assert r["position"] == 5


def test_dataforseo_skips_non_organic_items():
    r = dataforseo.parse(DATAFORSEO_PAYLOAD, "ad.sponsored.test", max_position=10)
    # Paid result must NOT be matched as an organic position.
    assert r["found"] is False


def test_dataforseo_empty_tasks_returns_not_found():
    r = dataforseo.parse({"tasks": []}, "example.com", max_position=10)
    assert r == {"position": None, "url": "", "found": False}


def test_dataforseo_missing_result_returns_not_found():
    r = dataforseo.parse({"tasks": [{}]}, "example.com", max_position=10)
    assert r["found"] is False


# ---- Serper ----

SERPER_PAYLOAD = {
    "organic": [
        {"position": 1, "link": "https://wikipedia.org/wiki/Python", "title": "Wikipedia"},
        {"position": 2, "link": "https://realpython.com/"},
        {"position": 3, "link": "https://www.example.com/py"},
    ],
}


def test_serper_finds_match_at_position_3():
    r = serper.parse(SERPER_PAYLOAD, "example.com", max_position=10)
    assert r["position"] == 3
    assert r["url"] == "https://www.example.com/py"


def test_serper_handles_missing_organic_array():
    r = serper.parse({}, "example.com", max_position=10)
    assert r == {"position": None, "url": "", "found": False}


def test_serper_skips_items_missing_link_or_position():
    data = {"organic": [{"position": 1}, {"link": "https://foo.test/"}]}
    r = serper.parse(data, "foo.test", max_position=10)
    # Second item has no 'position' field — must be skipped, not matched at 0 or None.
    assert r["found"] is False
