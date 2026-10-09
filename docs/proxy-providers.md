# Proxy providers

open-serp-checker works direct (no proxy) for Bing. For Google, you need a working rotating residential or datacenter proxy. This doc compares the proxy vendors we've tested.

> **Affiliate disclosure.** Links on this page are affiliate links. Using them helps fund the project at no cost to you. Every provider works identically without going through our link — pick what fits your budget.

## Short answer

If you just need something that works:

- **High volume, cost-sensitive**: [Webshare](https://www.webshare.io/?ref=open-serp-checker) datacenter. ~$3/mo flat, ~$0 amortized per SERP. Tested 20/20 pass on Google in Session 3.9.
- **Residential IPs for geo-sensitive queries**: [DataImpulse](https://dataimpulse.com/?ref=open-serp-checker) or [Byteful](https://byteful.com/?ref=open-serp-checker). ~$0.15-0.45 per 1k SERPs. Both 20/20 in Session 3.9.
- **Premium residential**: [Oxylabs](https://oxylabs.io/?ref=open-serp-checker) or [Bright Data](https://brightdata.com/?ref=open-serp-checker). Pricier, best pool quality.

## Full comparison

| Provider | Type | Pricing (2026) | $/1k Google SERPs¹ | Tested 20/20? | Notes |
|---|---|---|---|---|---|
| [Webshare](https://www.webshare.io/?ref=open-serp-checker) | datacenter | ~$3/mo flat (unlimited BW) | ~$0 | ✅ | Best price/performance for high-volume. Datacenter IPs may be flagged at >1k queries/hr/IP. |
| [Byteful](https://byteful.com/?ref=open-serp-checker) | residential | ~$3/GB | ~$0.45 | ✅ | First-mint burn rate higher than peers — needs the 3-attempt retry budget. |
| [DataImpulse](https://dataimpulse.com/?ref=open-serp-checker) | residential | ~$1-2/GB | ~$0.15-0.30 | ✅ | Fastest latency of the residential options tested. |
| [Decodo](https://decodo.com/?ref=open-serp-checker) | residential | ~$5/GB | ~$0.70-1.50 | ✅ (3.7) | Historical baseline. Session 3.9 run deferred — credentials needed refresh. |
| [Oxylabs](https://oxylabs.io/?ref=open-serp-checker) | residential | ~$8-15/GB | ~$1.20-2.25 | — | Premium pool quality; Session 3.9 run deferred. |
| [Bright Data](https://brightdata.com/?ref=open-serp-checker) | residential | ~$8.50/GB | ~$1.30 | — | Largest residential pool. Not tested in-repo; vendor is well-documented elsewhere. |
| [Shifter](https://shifter.io/?ref=open-serp-checker) | residential | ~$30+/mo per port | depends on volume | — | Port-based model. Overpays for low-volume rank tracking. |

¹ Approximate per-SERP cost using the Session 3.9 measurement of ~130-150 KB wire bytes per Google SERP in minted-identity mode. Varies with provider's billing coefficient (DataImpulse applies 2x for state/city filters, for example).

## How to pick

1. **Volume question.** Under 500 SERPs/day → any option works, pick on price. Over 2k/day → datacenter (Webshare) unless you need residential geo accuracy.
2. **Geo-targeting question.** Need results from a specific state/city? Residential with sub-country targeting (Oxylabs, Byteful, DataImpulse all offer this). Datacenter geos are coarser.
3. **Ops question.** Don't want to think about proxies at all? Skip this entire page and use `dataforseo` or `serper` as the SERP engine instead — they bundle proxies into their API price.

## Setup

Once you've signed up, grab the rotating-mode credentials from the provider dashboard. In the open-serp-checker dashboard:

1. Go to **Proxies** (top-right nav).
2. Click **Add proxy**.
3. Fill in: name, provider (dropdown), host, port, username, password, mode = `rotating`.
4. Hit **Test** to verify auth before saving.

Set the proxy as a project default (project settings) or per-keyword (keyword edit).

## Rotating vs sticky

open-serp-checker's Google engine assumes **rotating mode** for high pass rate. The minted-identity model (Session 3.7-3.9) depends on the exit IP changing per new connection but holding for the lifetime of a parked browser connection — rotating pools cooperate; sticky pools re-use burned IPs. If your proxy row's `mode` is `sticky`, Google falls back to the plain-navigation path with lower pass rate.

Set `mode='rotating'` unless you have a specific reason not to.

## Credential hygiene

Credentials go in the dashboard's Proxies form and are stored in SQLite (plaintext — open-serp-checker is a single-user local tool, not a SaaS). Don't commit `open_serp_checker.db`, don't expose port 8000 publicly, and rotate proxy credentials if you suspect leakage. `.env` and `*.db` are gitignored by default.
