"""Engine dispatch for proxy_tests browser launches (session 4.10 Phase 2).

Two engines today:

- `chromium_stealth` — the historical default. Headless Chromium +
  `_STEALTH_ARGS` launch flags + `_STEALTH_INIT_JS` init script, UA
  overridden to `DEFAULT_USER_AGENT`. Unchanged behaviour from
  pre-4.10 runs.

- `camoufox` — Playwright's own Firefox API launched against the
  Camoufox patched binary via `camoufox.async_api.launch_options(...)`.
  No UA override, no stealth init script — Camoufox picks a coherent
  fingerprint bundle at the browser level. Viewport 1920x1080 matches
  the known-good recipe in `google-scrape/root-test.py`.

Callers pass `engine`; every launcher returns the same shape so
`pass_rate`/`warm`/`local_baseline` can stay engine-agnostic below the
launch line.

Route rules stay per-caller (they still call `context.route(...)` with
their own handler). Rationale: `pass_rate` and `warm` share a route
handler; `local_baseline` shares the same one; keeping route rules out
of here means this file doesn't need to know about them.
"""
from __future__ import annotations

from typing import Optional

from src.core.engines.google import (
    DEFAULT_USER_AGENT as _CHROMIUM_UA,
    _STEALTH_ARGS as _CHROMIUM_STEALTH_ARGS,
    _STEALTH_INIT_JS as _CHROMIUM_STEALTH_JS,
)

ENGINE_CHROMIUM_STEALTH = "chromium_stealth"
ENGINE_CAMOUFOX = "camoufox"
# Not launched by `launch_browser` — pass_rate handles it via the
# minted-identity machinery in src/core/engines/google_identity.py. Listed
# here so the engine axis (4.10) and dashboards see it as a valid engine.
ENGINE_GOOGLE_IDENTITY = "google_identity"
ENGINES = (ENGINE_CHROMIUM_STEALTH, ENGINE_CAMOUFOX, ENGINE_GOOGLE_IDENTITY)


async def launch_browser(pw, engine: str, pw_proxy: Optional[dict] = None):
    """Launch a browser for the given engine. Returns the browser handle.

    Proxy is applied at launch time for chromium (via `--proxy-server=` in
    launch args is *not* used here — we pass it via `context.proxy` for
    chromium so it stays per-context) and at launch time for camoufox
    (camoufox routes DNS through the proxy for geo-spoofing coherence, so
    proxy on launch is the recommended shape).

    Chromium note: the historical code passed proxy at the context level.
    We keep that — this function does NOT set proxy for chromium. Callers
    still pass `proxy=` in `new_context(...)`. Camoufox is different:
    proxy MUST be set at launch for the geoip/DNS coherence to work.
    """
    if engine == ENGINE_CHROMIUM_STEALTH:
        return await pw.chromium.launch(headless=True, args=_CHROMIUM_STEALTH_ARGS)

    if engine == ENGINE_CAMOUFOX:
        from camoufox.async_api import launch_options
        # geoip=True makes Camoufox align timezone/locale/screen with the
        # proxy's exit IP. Without it, a proxied browser leaks a mismatch
        # (US-shaped browser through a UK exit IP, etc.) which is itself a
        # fingerprint tell. Camoufox warns loudly when this is missing.
        # For no-proxy (local) it's a no-op — geoip lookup on the local IP.
        opts = launch_options(headless=True, proxy=pw_proxy, geoip=True)
        return await pw.firefox.launch(**opts)

    if engine == ENGINE_GOOGLE_IDENTITY:
        raise ValueError(
            f"engine {engine!r} is handled by pass_rate's identity axis "
            "(MintedIdentity), not launch_browser"
        )

    raise ValueError(f"unknown engine {engine!r}; expected one of {ENGINES}")


async def new_stealth_context(browser, engine: str, pw_proxy: Optional[dict] = None):
    """Open a new context with engine-appropriate stealth surface applied.

    Callers should still `context.route(...)` their own handler after this.

    Chromium context includes the stealth init JS and UA override.
    Camoufox context is deliberately minimal — the browser binary is
    already spoofing everything and adding a Chromium-shaped UA on top
    would break fingerprint coherence.
    """
    if engine == ENGINE_CHROMIUM_STEALTH:
        context = await browser.new_context(
            proxy=pw_proxy,
            locale="en-US",
            viewport={"width": 1280, "height": 800},
            user_agent=_CHROMIUM_UA,
        )
        await context.add_init_script(_CHROMIUM_STEALTH_JS)
        return context

    if engine == ENGINE_CAMOUFOX:
        # Proxy was applied at launch. No UA override, no init script.
        return await browser.new_context(
            locale="en-US",
            viewport={"width": 1920, "height": 1080},
        )

    raise ValueError(f"unknown engine {engine!r}; expected one of {ENGINES}")
