# Stock Intelligence dashboard (NOT the legacy API-only server)
FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    STOCKWATCH_DATA_DIR=/data \
    STOCKWATCH_HEADLESS=1 \
    STOCKWATCH_BROWSER_TABS=1 \
    STOCKWATCH_RPS=1 \
    STOCKWATCH_CONCURRENCY=1 \
    PLATFORM_SECURE_COOKIE=1
WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*
COPY main.py ./main.py
COPY stockwatch/ ./stockwatch/
RUN test -f stockwatch/ui/dashboard.html \
    && test -f stockwatch/ui/login.html \
    && test -f stockwatch/ui/app.js \
    && test -f stockwatch/ui/styles.css \
    && python -c "from stockwatch.platform import create_app; print('Dashboard files and imports OK')"
RUN mkdir -p /data/out
EXPOSE 8080
# The dashboard CLI reads the Railway PORT variable and binds 0.0.0.0.
CMD ["python", "main.py", "dashboard", "--host", "0.0.0.0"]
