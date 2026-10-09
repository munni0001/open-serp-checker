# SERP engines

open-serp-checker ships with five pluggable engines. Pick per keyword via the `engine` field — the dashboard dropdown or the API.

## Comparison

| Engine | Free? | API key | Needs proxy? | Captcha risk | Best for |
|---|---|---|---|---|---|
| **bing** | yes | no | optional | low | Default. Bing is permissive on plain HTTP; 15-50 kw/day works without a proxy. |
| **google** | yes | no | **yes** | high | Real Google results. Requires a working rotating proxy — see `proxy-providers.md`. Uses the minted-identity engine (see sessions 3.7-3.9) for high pass rate on residential and datacenter pools. |
| **searxng** | yes¹ | no | optional | low | Google rankings without touching Google directly. SearXNG is a meta-search frontend; we hit its JSON API. ¹ Needs a self-hosted instance (`docker run -p 8080:8080 searxng/searxng`) — public instances universally rate-limit or anti-bot the JSON endpoint. |
| **dataforseo** | no | yes | no | none | Volume + accuracy. Pay per query (~$0.0006/SERP at 2026 pricing); DataForSEO runs the browser/proxy stack internally. |
| **serper** | no | yes | no | none | Cheaper DataForSEO alternative at low volume (~$0.0003/SERP). Same tradeoff. |

## Choosing

- **Just getting started?** Start with `bing`. It works out of the box.
- **Need Google ranks and willing to run infrastructure?** Use `google` with a rotating residential proxy. The cheapest working combination today is **Webshare datacenter** (see Session 3.9 results: 20/20 pass at ~$0 amortized cost per SERP).
- **Need Google ranks, allergic to proxies?** Self-host SearXNG and use `searxng`.
- **High volume (>1k SERPs/day) and want zero ops?** Pay for `dataforseo` or `serper`.

Direct Google scraping without a proxy is **not** a supported engine option — Google will captcha within ~50 requests. See `proxy-providers.md` for cost comparison.

## Configuration

Each engine except `bing` + `google` needs one or two env vars in `.env`:

```env
# SearXNG — self-host or private instance
SEARXNG_URL=http://localhost:8080

# DataForSEO (sign up at dataforseo.com)
DATAFORSEO_LOGIN=your@email.com
DATAFORSEO_PASSWORD=yourpassword

# Serper (sign up at serper.dev)
SERPER_API_KEY=your-key-here
```

Unconfigured engines fail with a clear error at run time; nothing crashes.

## Adding a new provider

Each engine is a single module under `src/core/engines/` exposing a `search(query, domain, geo, max_position, ...) -> dict` function (or `fetch` + `parse` for HTML-based engines). To add a new one:

1. Drop a new module alongside `bing.py` / `searxng.py`.
2. Add it to the `elif` chain in `src/core/scraper.py:SerpScraper.scrape_keyword`.
3. Add an `<option>` to the two engine dropdowns in `src/dashboard/templates/project.html`.
4. Add any config fields to `src/config.py:Settings`.
5. Document in this file.

That's it — no base class, no registry, no plugin system.
