# Changelog

All notable changes to this project are recorded here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) ·
Versioning: [SemVer](https://semver.org/).

## [Unreleased]

v0.1.0 is the first public release. It is not yet tagged; everything below
ships in that cut.

### Added

#### SERP engines

- **Bing** — HTML parser against the public results page. Default engine; works without a proxy for ~15–50 keywords/day.
- **Google** — Playwright + Camoufox browser with a minted-identity flow: one browser clears Google's JS challenge once, then replays queries via in-page `fetch()`. Requires a rotating residential or datacenter proxy.
- **SearXNG** — JSON API client against a self-hosted SearXNG instance (public instances rate-limit the JSON endpoint).
- **DataForSEO** — paid API, no proxy needed, parsed JSON results.
- **Serper** — paid API, cheaper alternative to DataForSEO at low volume.

#### Proxy support

- Rotating and sticky modes with provider-aware URL construction (Decodo-style sticky-port fragments supported).
- Proxies stored in SQLite via the `/proxies` dashboard; assigned per-keyword or inherited from the project default.
- Optional per-proxy IP blocklist to skip known-bad exits.
- Handshake test for new proxies before saving.

#### Dashboard

- Project + keyword CRUD with per-keyword engine picker.
- Concurrent run-all for every keyword in a project.
- History chart and block-state surface (`status` / `error` columns on `keyword_results`).
- Dark theme (Satoshi + JetBrains Mono).

#### Packaging and deploy

- MIT license.
- `pyproject.toml` (PEP 621), `pip install -e .` and `open-serp-checker` CLI entry point.
- Dockerfile (`python:3.12-slim` + Camoufox + Firefox dependencies + `gcc` / `libzstd-dev` for arm64 wheel compile).
- `docker-compose.yml` binding `127.0.0.1:8000` only.
- GitHub Actions: `ci.yml` (ruff `F`-only + pytest on push and PR), `docker.yml` (multi-arch `linux/amd64,linux/arm64` build to `ghcr.io` on `v*` tag).
- Issue templates (bug, feature, provider request) and a pull-request template.
- `docs/engines.md` and `docs/proxy-providers.md`.

#### Tests

- 45 pytest cases covering Bing, SearXNG, DataForSEO, Serper, and Google parsers against saved HTML / JSON fixtures. No network in tests.

### Internal

- Cross-provider minted-identity benchmark probe at `scripts/probes/minted_identity_cross_provider.py` — used to generate the pass-rate numbers that will populate the public proxy comparison.

[Unreleased]: https://github.com/munni0001/open-serp-checker/commits/main
