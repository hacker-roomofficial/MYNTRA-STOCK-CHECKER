FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    STOCKWATCH_DATA_DIR=/data \
    STOCKWATCH_HEADLESS=1

WORKDIR /app

# 1) dependencies first (cached layer) + Chromium and its OS libraries
COPY requirements.txt .
RUN pip install -r requirements.txt \
 && playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*

# 2) application code
COPY main.py ./
COPY stockwatch ./stockwatch

# Fail the BUILD (not the running container) if the package did not get copied properly.
RUN python -c "import stockwatch.cli, stockwatch.server; print('import check OK')"

# Railway mounts the Volume at /data at runtime. Running as root avoids volume-permission errors.
RUN mkdir -p /data
EXPOSE 8080
CMD ["python", "main.py", "serve"]
