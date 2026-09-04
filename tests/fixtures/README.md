# Google SERP fixtures

Test inputs for `src/core/engines/google.py`. Every fixture is a real (or synthetic) HTTP response body, saved so parser behavior is reproducible without hitting Google.

## Naming convention

`YYYY-MM-DD_<geo>_<slug>_<kind>.html`

- **date** — capture date, ISO. Chronological sort surfaces evolution at a glance.
- **geo** — ISO 3166 alpha-2 lowercase (`mk`, `de`, `us`, `uk`, ...) or `synth` for hand-written fixtures.
- **slug** — the query, kebab-cased (`semrush-review`, `best-ai-companion`).
- **kind** — `serp` (normal), `serp-with-modal` (SERP + JS consent modal overlay), `consent-interstitial`, `captcha`, `sorry`, `ads-heavy`, `paa-heavy`, `forum-block`, etc. Grow as we hit new features.

## When Google changes something

**Do NOT overwrite an existing fixture.** Add a new one with today's date and update the row in the table below. Old fixtures stay so we can bisect what changed and when.

## Fixtures

| File | Date | Geo | Query | Kind | Notes |
|---|---|---|---|---|---|
| `2026-09-04_mk_semrush-review_serp.html` | 2026-09-04 | MK | `semrush review` | serp | 7 organics, no consent, no ads. Discussion block at position 5 (`myfirstwebsite.com` + Reddit sub-links). Uses `/goto?url=CAES...` opaque wrappers on all hrefs; `<cite>` still holds the real domain. |
| `2026-09-04_de_semrush-review_serp-with-modal.html` | 2026-09-04 | DE | `semrush review` | serp-with-modal | Modal consent overlay rendered client-side; underlying SERP is fully populated (7 organics, 19 external URLs). **Critical negative test:** parser must NOT flag as blocked despite `"Before you continue to Google"` appearing in embedded JS. |
| `2026-09-04_synth_consent-interstitial.html` | 2026-09-04 | synth | `test` | consent-interstitial | Hand-written minimal interstitial: title = `Before you continue to Google`, form action = `consent.google.com/save`. Proves block detector fires on the true full-page interstitial. Upgrade to a real capture the first time we hit one in production. |

## Adding a new fixture

1. Save the HTML with the naming convention above.
2. Add a row to the table with 1-line notes on what's notable about it (new SERP feature, new class name, new redirect pattern, etc.).
3. Add an expected-outcome entry to `tests/test_google_parser.py`'s parametrize table so the test asserts against it.
