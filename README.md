# Stock Intelligence — Railway Dashboard

**Railway ka default app ab login + dashboard hai; legacy API-only server nahi.**

Start command: `python main.py dashboard --host 0.0.0.0` (uses Railway `$PORT`).

Full deployment instructions: [RAILWAY_SETUP_HINDI.md](RAILWAY_SETUP_HINDI.md).

For local testing:
```
python -m pip install -r requirements.txt
python -m playwright install chromium
# Set PLATFORM_ADMIN_USER, PLATFORM_ADMIN_PASSWORD (12+ chars) in environment
python main.py dashboard --host 127.0.0.1 --port 8080
```

The `stockwatch/` directory is an **internal Python package** containing the Myntra stock checking engine, browser automation, Google Sheet sync and the UI source. It does not represent a separate user-facing app. Do not delete it, because `main.py` imports it.
