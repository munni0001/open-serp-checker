# Security policy

## Reporting a vulnerability

**Please do not file a public GitHub issue for security problems.**

Report via GitHub's private vulnerability reporting:

1. Open the [Security tab](https://github.com/munni0001/open-serp-checker/security).
2. Click **Report a vulnerability**.
3. Fill out the form — reproduction steps, affected version, impact.

I'll acknowledge within 7 days and share a fix timeline within 14 days.
This is a solo-maintainer project; please be patient.

## Supported versions

| Version | Supported |
|---|---|
| 0.x | Yes — latest minor only. Patches land on `main`; upgrade by re-pulling the Docker image or `git pull`. |

Pre-1.0: breaking changes are possible across minor versions. Pin a specific
tag in production (e.g. `ghcr.io/munni0001/open-serp-checker:v0.1.0`).

## Scope

**In scope:**

- Code in `src/` (the app itself).
- Published Docker images on GHCR.
- GitHub Actions workflows that run on this repository.

**Out of scope:**

- Vulnerabilities in upstream dependencies — report to the upstream project. If the pinned version here is exploitable in a way that uniquely affects this project's usage, do flag it.
- Issues that require physical access to a machine already running the app.
- Rate-limiting or CAPTCHA responses from third-party SERP providers (Google, Bing, SearXNG, DataForSEO, Serper). Those are provider enforcement, not vulnerabilities.
- Plaintext storage of proxy credentials in the local SQLite DB: this is intentional. This project is a single-user local tool. Do not expose port 8000 publicly, and do not share your `.db` file.

## Disclosure

I'll credit you in the GitHub Security Advisory and the release notes unless
you'd prefer to remain anonymous.
