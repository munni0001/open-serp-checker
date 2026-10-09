# open-serp-checker

> Self-hosted, open-source SERP rank tracker. Bring your own proxy or API key.

Track your site's Google / Bing rankings for 10–50 keywords without paying $50/mo for a SaaS rank tracker. Runs locally on your laptop or a $5 VPS.

- **Five pluggable engines**: Bing (free), Google (via proxy), SearXNG (self-hosted), DataForSEO (paid API), Serper (paid API)
- **Provider-aware proxy support**: rotating / sticky modes, per-keyword or per-project assignment, IP blocklist to skip burned IPs
- **Minted-identity Google engine**: one Camoufox browser cleared Google's JS challenge once, replays queries via in-page `fetch()` — 20/20 pass rate on three different proxy providers in benchmark runs ([methodology](docs/engines.md))
- **FastAPI + SQLite**: zero external dependencies at runtime. No Postgres, no Redis
- **MIT licensed**

## Quickstart

### Docker (recommended)

```bash
docker run -p 127.0.0.1:8000:8000 -v $(pwd)/data:/data ghcr.io/munni0001/open-serp-checker:latest
```

Open <http://localhost:8000>. Done.

### Python (from source)

```bash
git clone https://github.com/munni0001/open-serp-checker
cd open-serp-checker
cp .env.example .env
pip install -e .
open-serp-checker          # or: uvicorn src.main:app --port 8000
```

Requires Python ≥ 3.11. For Google scraping, the Camoufox browser is fetched on first run (~500 MB).

## Engines

| Engine | Free? | Key needed | Needs proxy? | Captcha risk | Best for |
|---|---|---|---|---|---|
| **bing** | yes | no | optional | low | Default. 15–50 kw/day, no setup. |
| **google** | yes | no | **yes** | high | Real Google ranks via a rotating proxy. |
| **searxng** | yes | no | optional | low | Google results without touching Google. Self-host required. |
| **dataforseo** | no | yes | no | none | Volume + accuracy. Pay per query. |
| **serper** | no | yes | no | none | Cheaper DataForSEO alternative. |

Full comparison and setup: [docs/engines.md](docs/engines.md).

## Scope

- Personal / small-team rank tracking, ~10–50 keywords
- Runs on localhost or a private VPS
- Single-user — no auth, no multi-tenancy

For team / enterprise use with auth, see the roadmap in GitHub Discussions.

## Development

```bash
pip install -e .[dev]
ruff check src/ tests/
pytest -v
```

CI runs on every PR. See [CONTRIBUTING.md](CONTRIBUTING.md) for adding a new provider.

## License

MIT. See [LICENSE](LICENSE).
