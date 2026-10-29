from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # optional
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, default))


def _load_proxies() -> list[str]:
    # STOCKWATCH_PROXIES="http://u:p@host:port,http://..."  (easiest on Railway: no file needed)
    out = [p.strip() for p in os.environ.get("STOCKWATCH_PROXIES", "").split(",") if p.strip()]
    path = os.environ.get("STOCKWATCH_PROXIES_FILE", "")
    if path and Path(path).exists():
        out += [ln.strip() for ln in Path(path).read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    return out


def _data_dir() -> str:
    return os.environ.get("STOCKWATCH_DATA_DIR", ".")


def _under_data(*parts: str) -> str:
    return str(Path(_data_dir(), *parts))


@dataclass
class Config:
    # ---- target site -------------------------------------------------------
    site_base: str = "https://www.myntra.com"
    api_base: str = "https://www.myntra.com"
    # JSON endpoint the PDP itself calls. If Myntra changes it, only this line changes.
    api_path: str = "/gateway/v2/product/{style_id}"

    # ---- fast (HTTP) engine -----------------------------------------------
    concurrency: int = field(default_factory=lambda: _env_int("STOCKWATCH_CONCURRENCY", 150))
    rps: float = field(default_factory=lambda: _env_float("STOCKWATCH_RPS", 60))
    min_rps: float = 2.0
    max_rps: float = field(default_factory=lambda: _env_float("STOCKWATCH_MAX_RPS", 400))
    timeout: float = 15.0
    retries: int = 3
    proxies: list[str] = field(default_factory=_load_proxies)
    warm_session: bool = True  # grab cookies from a real Chrome before going fast

    # ---- browser engine (search bar) --------------------------------------
    browser_tabs: int = field(default_factory=lambda: _env_int("STOCKWATCH_BROWSER_TABS", 4))
    headless: bool = field(default_factory=lambda: os.environ.get("STOCKWATCH_HEADLESS", "1") != "0")
    profile_dir: str = field(default_factory=lambda: _under_data(".chrome-profile"))
    search_selector: str = "input.desktop-searchBar, input[placeholder*='Search' i]"
    search_wait: float = 8.0  # seconds to wait for a search to land on the product page

    # ---- alerts ------------------------------------------------------------
    webhook_url: str = field(default_factory=lambda: os.environ.get("N8N_WEBHOOK_URL", ""))
    webhook_key: str = field(default_factory=lambda: os.environ.get("N8N_API_KEY", ""))
    chunk_size: int = 40          # styles per webhook call (= per WhatsApp message)
    max_alert_styles: int = 300   # cap per run so WhatsApp isn't flooded; rest is in the Excel report
    realert_hours: float = 24.0   # same OOS state is not re-sent before this

    always_notify: bool = field(default_factory=lambda: os.environ.get("STOCKWATCH_ALWAYS_NOTIFY", "0") == "1")

    # ---- storage (on Railway: mount a Volume and set STOCKWATCH_DATA_DIR=/data) ----
    data_dir: str = field(default_factory=_data_dir)
    out_dir: str = field(default_factory=lambda: _under_data("out"))
    db_path: str = field(default_factory=lambda: _under_data("out", "stockwatch.db"))

    # ---- web service -------------------------------------------------------
    api_key: str = field(default_factory=lambda: os.environ.get("STOCKWATCH_API_KEY", ""))
    default_mode: str = field(default_factory=lambda: os.environ.get("STOCKWATCH_MODE", "auto"))
    schedule_hours: float = field(default_factory=lambda: _env_float("STOCKWATCH_SCHEDULE_HOURS", 0))  # 0 = off
    max_upload_mb: int = 50

    def api_url(self, style_id: str) -> str:
        return self.api_base.rstrip("/") + self.api_path.format(style_id=style_id)
