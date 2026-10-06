"""Pure-logic tests for src/core/engines/google_identity.py.

These cover the block-classification logic only — the browser-dependent
mint/serp paths are exercised live via scripts/probes and the pass_rate
battery (they need a proxy + Camoufox).
"""
from src.core.engines import google_identity as gi


def test_classify_counts_h3():
    c = gi.classify("<html><h3>one</h3><h3>two</h3></html>")
    assert c["h3"] == 2
    assert c["sorry"] is False
    assert c["enablejs"] is False


def test_classify_detects_sorry_in_url():
    c = gi.classify("<html><body>x</body></html>",
                    final_url="https://www.google.com/sorry/index?continue=...")
    assert c["sorry"] is True


def test_classify_detects_sorry_in_body():
    c = gi.classify(
        "<html><body>Our systems have detected unusual traffic "
        "from your computer network (unusual-traffic example)</body></html>"
    )
    assert c["sorry"] is True


def test_classify_detects_enablejs():
    c = gi.classify("<html><body>/httpservice/retry/enablejs</body></html>")
    assert c["enablejs"] is True


def test_blocked_reason_ok_on_serp():
    body = "<html><body>" + "".join(f"<h3>r{i}</h3>" for i in range(10)) + "</body></html>"
    assert gi.blocked_reason(200, body, 10) is None


def test_blocked_reason_429():
    assert gi.blocked_reason(429, "<html></html>", 0) == "http 429 rate-limited"


def test_blocked_reason_sorry():
    body = ("<html><body>Our systems have detected unusual traffic "
            "from your computer network (unusual-traffic example)</body></html>")
    assert gi.blocked_reason(200, body, 0) == "sorry"


def test_blocked_reason_enablejs_when_no_h3():
    body = "<html><body>/httpservice/retry/enablejs</body></html>"
    assert gi.blocked_reason(200, body, 0) == "enablejs"


def test_blocked_reason_enablejs_does_not_mask_real_serp():
    """h3 presence wins over an enablejs script marker (probe semantics)."""
    body = "<html><body>/httpservice/retry/enablejs<h3>real result</h3></body></html>"
    assert gi.blocked_reason(200, body, 1) is None


def test_blocked_reason_soft_block_empty():
    assert gi.blocked_reason(200, "<html><body>empty</body></html>", 0) == (
        "empty SERP (likely soft block)"
    )


def test_blocked_reason_no_body_no_block():
    """An empty body (no <body> tag) is a fetch failure, not a soft block."""
    assert gi.blocked_reason(200, "", 0) is None


def test_default_query_limit_matches_documented_window():
    assert gi.DEFAULT_QUERY_LIMIT == 25
    assert gi.MAX_MINTS == 3