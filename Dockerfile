# open-serp-checker — self-hosted SERP rank tracker
#
# Image includes Camoufox (fetched at pip install) + its runtime deps so Google
# scraping works out of the box. Image is ~1.2 GB — the browser is non-optional
# for the Google engine. If you only use Bing / SearXNG / DataForSEO / Serper,
# a slimmer image is possible by skipping the camoufox-fetch step and the
# Firefox system packages — PRs welcome.

FROM python:3.12-slim

# System packages Firefox-under-Playwright needs: fonts, GTK, X libs, DBus.
# Order matters only for the apt cache; the layer size is the same.
RUN apt-get update && apt-get install -y --no-install-recommends \
      libgtk-3-0 libdbus-glib-1-2 libx11-xcb1 libxcomposite1 libxdamage1 \
      libxrandr2 libxkbcommon0 libasound2 libatk1.0-0 libatk-bridge2.0-0 \
      libcups2 libdrm2 libxshmfence1 libnss3 libnspr4 libpango-1.0-0 \
      libpangocairo-1.0-0 libgbm1 libxss1 fonts-liberation ca-certificates \
      curl \
      gcc build-essential libzstd-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first for layer caching — code changes don't invalidate.
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/
RUN pip install --no-cache-dir -e .

# Fetch the Camoufox browser bundle (geoip + Firefox binary).
RUN python -m camoufox fetch

# SQLite DB lives in /data so it survives container restarts via volume mount.
ENV DATABASE_PATH=/data/open_serp_checker.db
RUN mkdir -p /data

EXPOSE 8000
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
