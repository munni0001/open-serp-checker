# Contributing

Thanks for your interest in open-serp-checker. This is a small single-maintainer project — issues and PRs are welcome, but please read this first so we don't both waste time.

## Dev setup

```bash
git clone https://github.com/munni0001/open-serp-checker
cd open-serp-checker
python -m venv venv && source venv/bin/activate
pip install -e .[dev]
cp .env.example .env

# Run tests
pytest -v

# Lint + format
ruff check src/ tests/
ruff format src/ tests/
```

Requires Python ≥ 3.11.

## Coding style

- `ruff` for linting + formatting. Config in `pyproject.toml`. CI blocks on both.
- No classes when a function works. No abstract base classes until there are 3 concrete callers.
- Functions over methods, modules over packages. The engine layer uses one module per engine with top-level `fetch()` / `parse()` or `search()` functions — follow that pattern for new providers.
- `httpx` for HTTP, not `requests`. Async where it costs nothing; sync where it keeps the code honest.
- Comments: only where the "why" is non-obvious. The code should name the "what."

## Adding a new SERP engine

1. Drop a module at `src/core/engines/<name>.py`. Export either:
   - `fetch(query, ...) -> str` + `parse(html, domain, max_position) -> dict` for HTML-based engines
   - `search(query, domain, ...) -> dict` + a pure `parse(data, domain, max_position) -> dict` for API-based engines
2. The returned dict shape: `{"position": int | None, "url": str, "found": bool}`. `SerpScraper` adds `status` / `error`.
3. Add an `elif` branch in `src/core/scraper.py:SerpScraper.scrape_keyword`.
4. Add an `<option>` to **both** engine `<select>`s in `src/dashboard/templates/project.html` (add form + inline edit).
5. If the engine needs config (API key, URL), add fields to `src/config.py:Settings` and document in `.env.example`.
6. Add a parser test under `tests/test_<name>_parser.py` — the pure parse function is the testable surface. No network in tests.
7. Add the engine to the comparison table in `docs/engines.md`.

Keep each engine module under ~100 lines. If you need more, the complexity probably belongs elsewhere.

## Adding a new proxy provider

The proxy layer handles any HTTP CONNECT-tunneled proxy with username:password auth. For provider-specific URL shapes (e.g. Decodo's sticky-port `sessionduration` fragment), extend `build_proxy_url` in `src/core/scraper.py`.

For the cross-provider benchmark probe, add the provider to `PROVIDER_CONFIGS` in `scripts/probes/minted_identity_cross_provider.py` with the known-working rotating-endpoint shape.

## PR expectations

- One logical change per PR. If you fix a bug AND add a feature, that's two PRs.
- Describe *why*, not *what* — the diff shows what.
- Tests pass locally before pushing. CI will run them again.
- No `# TODO`, no commented-out code, no "WIP" commits on `main`.
- Match existing style even if you'd write it differently.

## Reporting bugs

Use the GitHub Issues bug template. Include:

- What you tried to do
- What happened (full error, not a paraphrase)
- Your Python version, OS, and whether you're running Docker or source
- The engine and proxy config (redact credentials)

## Not accepting

- CAPTCHA solvers / payment integrations for them. Scope creep.
- Headful-browser-only features. Must work in Docker.
- Multi-user / auth. See the roadmap — this is a planned v0.2+ feature; PRs welcome **after** the design is agreed on in Discussions.
- Changes that require Postgres / Redis / Celery / any external daemon. SQLite + stdlib is the whole product.
