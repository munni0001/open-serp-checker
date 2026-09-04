"""Parser tests for src/core/engines/google.py.

Fixtures live in tests/fixtures/ with the naming convention
`YYYY-MM-DD_<geo>_<slug>_<kind>.html` (see fixtures/README.md).
"""
from pathlib import Path

import pytest

from src.core.engines import google

FIXTURES = Path(__file__).parent / "fixtures"

MK_SERP = FIXTURES / "2026-09-04_mk_semrush-review_serp.html"
DE_MODAL = FIXTURES / "2026-09-04_de_semrush-review_serp-with-modal.html"
CONSENT = FIXTURES / "2026-09-04_synth_consent-interstitial.html"


# ---------- Block detection ----------

def test_consent_interstitial_is_blocked():
    html = CONSENT.read_text()
    r = google.parse(html, "example.com", max_position=100)
    assert r["status"] == "blocked"
    assert "consent" in r["error"].lower()
    assert r["position"] is None
    assert r["found"] is False


def test_sorry_page_final_url_is_blocked():
    html = "<html><body>please prove you're human</body></html>"
    r = google.parse(html, "example.com", max_position=100,
                     final_url="https://www.google.com/sorry/index?continue=...")
    assert r["status"] == "blocked"
    assert "captcha" in r["error"].lower() or "sorry" in r["error"].lower()


def test_http_429_is_blocked():
    r = google.parse("<html><body></body></html>", "example.com",
                     max_position=100, status_code=429)
    assert r["status"] == "blocked"


def test_empty_body_on_200_is_blocked():
    """Zero organics on a 200 OK is treated as a soft block (defensive)."""
    r = google.parse("<html><body></body></html>", "example.com", max_position=100)
    assert r["status"] == "blocked"


def test_de_modal_overlay_is_NOT_blocked():
    """Critical negative test: the JS modal-overlay consent variant serves
    a fully-populated SERP under the hood. Detector must not false-positive
    on the `Before you continue to Google` string appearing in embedded JS.
    """
    html = DE_MODAL.read_text()
    r = google.parse(html, "digitalnomadlifestyle.com", max_position=100)
    assert r["status"] == "ok", f"got blocked reason: {r['error']}"
    assert r["found"] is True
    assert r["position"] is not None and r["position"] >= 1


# ---------- Rank matching against the Macedonia SERP ----------

@pytest.mark.parametrize("domain,expected_pos", [
    ("digitalnomadlifestyle.com", 1),
    ("trustpilot.com", 2),
    ("docket.io", 3),
    ("behindrankings.com", 4),
    ("g2.com", 6),
    ("rankability.com", 7),
])
def test_mk_serp_rank_hits(domain, expected_pos):
    html = MK_SERP.read_text()
    r = google.parse(html, domain, max_position=100)
    assert r["status"] == "ok"
    assert r["found"] is True
    assert r["position"] == expected_pos


def test_mk_serp_not_found():
    html = MK_SERP.read_text()
    r = google.parse(html, "example.com", max_position=100)
    assert r["status"] == "ok"
    assert r["found"] is False
    assert r["position"] is None


def test_mk_serp_records_full_url_not_goto_wrapper():
    """When we hit, the recorded URL must be the plaintext external URL
    (from the JSON-in-HTML scan), NOT the opaque /goto?url=CAES... wrapper.
    """
    html = MK_SERP.read_text()
    r = google.parse(html, "digitalnomadlifestyle.com", max_position=100)
    assert r["url"].startswith("https://digitalnomadlifestyle.com")
    assert "/goto?" not in r["url"]
    assert "digitalnomadlifestyle.com/semrush-review-2" in r["url"]


def test_www_prefix_is_stripped_on_match():
    """Target `trustpilot.com` should match cite `www.trustpilot.com`."""
    html = MK_SERP.read_text()
    r = google.parse(html, "www.trustpilot.com", max_position=100)
    assert r["found"] is True
    assert r["position"] == 2


def test_max_position_caps_scan():
    html = MK_SERP.read_text()
    # digitalnomadlifestyle is at position 1, so max_position=0 should still
    # miss (the for-loop over enumerate is 1-indexed with idx > max_position).
    r = google.parse(html, "rankability.com", max_position=3)
    assert r["found"] is False


def test_empty_target_returns_ok_not_found():
    html = MK_SERP.read_text()
    r = google.parse(html, "", max_position=100)
    assert r["status"] == "ok"
    assert r["found"] is False
