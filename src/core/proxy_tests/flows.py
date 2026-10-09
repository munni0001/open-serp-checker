"""Search-initiation flows (session 4.10 Phase 2c).

Ported from `google-scrape/root-test.py`. Two flows today:

- `direct` — historical: warm up google.com homepage, then
  `page.goto("/search?q=...")`. Cheap, but Google is far more
  suspicious of direct /search hits than homepage-originated ones.

- `home` — google-scrape's default: warm up google.com homepage,
  prime CONSENT cookie, click a consent modal if present, find the
  search input, `keyboard.type(kw, delay=10-30ms per char)`, press
  Enter, poll for results. Mimics a human sitting at a browser. This
  is what makes doconly viable (and probably matters for pass rate
  even at less aggressive tiers).

`initiate_search` returns after the search has been *submitted*.
Callers still handle wait-for-results / h3 counting / pass detection —
this keeps flow orthogonal to what "success" means in a given test.
"""
from __future__ import annotations

import random

FLOW_DIRECT = "direct"
FLOW_HOME = "home"
FLOWS = (FLOW_DIRECT, FLOW_HOME)

# Consent modal selectors — verbatim from google-scrape.
_CONSENT_SELECTORS = (
    "button:has-text('Accept all')",
    "button:has-text('I agree')",
    "button:has-text('Alle akzeptieren')",
    "button:has-text('Tout accepter')",
    "#L2AGLb",
    "button[aria-label*='Accept']",
)

# Search input selectors — Google rotates these; try in order.
_SEARCH_INPUT_SELECTORS = (
    "textarea[name='q']", "input[name='q']", "textarea[title='Search']",
    "input[title='Search']", "textarea[aria-label='Search']",
    "input[aria-label='Search']", "textarea",
)


async def _prime_consent(page, context) -> None:
    """Set the CONSENT cookie and click any visible consent modal.

    The cookie alone often suppresses the modal; the click is
    belt-and-suspenders for regions where Google shows it anyway.
    Failures here are non-fatal — a stray consent modal just means the
    caller sees the modal DOM instead of a SERP, which shows up as
    h3=0 and gets counted as a block.
    """
    try:
        await context.add_cookies([{
            "name": "CONSENT", "value": "YES+cb",
            "domain": ".google.com", "path": "/",
        }])
    except Exception:
        pass
    for sel in _CONSENT_SELECTORS:
        try:
            el = await page.query_selector(sel)
            if el:
                await el.click()
                await page.wait_for_timeout(1500)
                return
        except Exception:
            continue


async def _find_search_input(page):
    for sel in _SEARCH_INPUT_SELECTORS:
        try:
            el = await page.query_selector(sel)
            if el:
                return el
        except Exception:
            continue
    return None


async def initiate_search(
    page, context, keyword: str,
    flow: str = FLOW_DIRECT,
    base_url: str = "https://www.google.com",
    timeout_ms: int = 30_000,
) -> dict:
    """Warm the homepage, then submit the search per `flow`.

    Returns {"ok": bool, "http_status": int, "final_url": str,
             "error": Optional[str]}.

    On error the caller should treat as a block/timeout — a page is
    still open, but the search was never issued.
    """
    if flow not in FLOWS:
        raise ValueError(f"unknown flow {flow!r}; expected one of {FLOWS}")

    # Both flows warm the homepage first.
    try:
        r = await page.goto(
            base_url, wait_until="domcontentloaded", timeout=timeout_ms,
        )
        warmup_status = r.status if r else 0
    except Exception as e:
        return {"ok": False, "http_status": 0, "final_url": page.url or "",
                "error": f"warmup: {type(e).__name__}: {e}"[:200]}

    if flow == FLOW_DIRECT:
        search_url = f"{base_url}/search?q={keyword.replace(' ', '+')}"
        try:
            resp = await page.goto(
                search_url, wait_until="commit", timeout=timeout_ms,
            )
        except Exception as e:
            return {"ok": False, "http_status": 0, "final_url": page.url or "",
                    "error": f"search: {type(e).__name__}: {e}"[:200]}
        return {
            "ok": True,
            "http_status": resp.status if resp else warmup_status,
            "final_url": page.url or "",
            "error": None,
        }

    # FLOW_HOME
    await _prime_consent(page, context)
    inp = await _find_search_input(page)
    if not inp:
        return {"ok": False, "http_status": warmup_status,
                "final_url": page.url or "",
                "error": "no search input on homepage"}
    try:
        await inp.click()
        await page.keyboard.type(keyword, delay=random.randint(10, 30))
        await page.wait_for_timeout(random.randint(100, 300))
        await page.keyboard.press("Enter")
    except Exception as e:
        return {"ok": False, "http_status": warmup_status,
                "final_url": page.url or "",
                "error": f"type_enter: {type(e).__name__}: {e}"[:200]}

    # After Enter, wait for the SERP navigation to commit.
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    except Exception:
        pass

    return {
        "ok": True,
        # No status object from a keyboard-driven navigation; assume 200
        # if we got here. Real block detection happens in the caller via
        # url/h3 checks.
        "http_status": 200,
        "final_url": page.url or "",
        "error": None,
    }


async def wait_for_serp(page, timeout_ms: int = 15_000,
                        h3_bar: int = 5) -> int:
    """Poll for h3 count reaching h3_bar. Returns final h3 count.

    Longer than the historical 3s wait_for_selector("h3") — doconly
    strips the scripts that pre-render h3 tags, so the SERP takes
    longer to settle. 15s is a middle ground between the old 3s and
    google-scrape's 45s.
    """
    import asyncio
    deadline = asyncio.get_event_loop().time() + (timeout_ms / 1000)
    last = 0
    while asyncio.get_event_loop().time() < deadline:
        try:
            n = len(await page.query_selector_all("h3"))
            last = n
            if n >= h3_bar:
                return n
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return last
