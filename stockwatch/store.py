"""SQLite store: resume support + alert de-duplication.

Why SQLite: a 5-lakh-style run must survive a crash/Ctrl-C and continue exactly where it
stopped (--resume), and the same out-of-stock state must not WhatsApp you again and again.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Iterable, Iterator

from .models import ALERT_STATUSES, Item, SizeInfo, Status, StyleResult

_SCHEMA = """
CREATE TABLE IF NOT EXISTS results(
    run_id TEXT NOT NULL, style_id TEXT NOT NULL, status TEXT NOT NULL,
    brand TEXT, name TEXT, oos_sizes TEXT, in_sizes TEXT, error TEXT, checked_at REAL,
    PRIMARY KEY(run_id, style_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS alert_state(
    style_id TEXT PRIMARY KEY, signature TEXT NOT NULL, alerted_at REAL NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS jobs(
    run_id TEXT PRIMARY KEY, input_path TEXT NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL,
    created_at REAL NOT NULL, finished_at REAL, summary TEXT, error TEXT
) WITHOUT ROWID;
"""


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(_SCHEMA)

    # ---- results ---------------------------------------------------------
    def save_many(self, run_id: str, results: Iterable[StyleResult]) -> None:
        rows = [
            (run_id, r.style_id, r.status.value, r.brand, r.name,
             ",".join(r.oos_sizes), ",".join(r.in_sizes), r.error, r.checked_at)
            for r in results
        ]
        if rows:
            with self.db:
                self.db.executemany("INSERT OR REPLACE INTO results VALUES (?,?,?,?,?,?,?,?,?)", rows)

    def done_ids(self, run_id: str) -> set[str]:
        """Finished = anything except ERROR (errors get retried on resume)."""
        cur = self.db.execute("SELECT style_id FROM results WHERE run_id=? AND status!='ERROR'", (run_id,))
        return {r[0] for r in cur}

    def error_ids(self, run_id: str) -> set[str]:
        cur = self.db.execute("SELECT style_id FROM results WHERE run_id=? AND status='ERROR'", (run_id,))
        return {r[0] for r in cur}

    def counts(self, run_id: str) -> dict[str, int]:
        cur = self.db.execute("SELECT status, COUNT(*) FROM results WHERE run_id=? GROUP BY status", (run_id,))
        return dict(cur.fetchall())

    def iter_results(self, run_id: str) -> Iterator[dict]:
        cur = self.db.execute(
            "SELECT style_id,status,brand,name,oos_sizes,in_sizes,error,checked_at "
            "FROM results WHERE run_id=? ORDER BY status, style_id", (run_id,))
        for sid, st, brand, name, oos, ins, err, ts in cur:
            yield {
                "style_id": sid, "status": st, "brand": brand or "", "name": name or "",
                "oos_sizes": oos or "", "in_sizes": ins or "", "error": err or "",
                "checked_at_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts or 0)),
            }

    # ---- alert de-dup ----------------------------------------------------
    def pending_alerts(self, run_id: str, realert_hours: float) -> list[dict]:
        """OOS styles whose state is new/changed (or last alerted > realert_hours ago)."""
        state = {r[0]: (r[1], r[2]) for r in self.db.execute("SELECT style_id, signature, alerted_at FROM alert_state")}
        horizon = time.time() - realert_hours * 3600
        marks = ",".join("?" * len(ALERT_STATUSES))
        cur = self.db.execute(
            f"SELECT style_id,status,brand,name,oos_sizes FROM results WHERE run_id=? AND status IN ({marks})",
            (run_id, *[s.value for s in ALERT_STATUSES]))
        out = []
        for sid, st, brand, name, oos in cur:
            sig = f"{st}|{','.join(sorted((oos or '').split(',')))}"
            prev = state.get(sid)
            if prev is None or prev[0] != sig or prev[1] < horizon:
                out.append({"style_id": sid, "status": st, "brand": brand or "", "name": name or "",
                            "oos_sizes": [s for s in (oos or "").split(",") if s],
                            "url": f"https://www.myntra.com/{sid}", "_sig": sig})
        out.sort(key=lambda a: (a["status"] != "ALL_OOS", a["style_id"]))  # fully-OOS first
        return out

    def mark_alerted(self, alerts: Iterable[dict]) -> None:
        now = time.time()
        rows = [(a["style_id"], a["_sig"], now) for a in alerts]
        if rows:
            with self.db:
                self.db.executemany("INSERT OR REPLACE INTO alert_state VALUES (?,?,?)", rows)

    def clear_recovered(self, run_id: str) -> int:
        """Styles that are OK again forget their alert state, so a future OOS alerts immediately."""
        with self.db:
            cur = self.db.execute(
                "DELETE FROM alert_state WHERE style_id IN "
                "(SELECT style_id FROM results WHERE run_id=? AND status='OK')", (run_id,))
        return cur.rowcount

    # ---- jobs (web service) ----------------------------------------------
    def job_create(self, run_id: str, input_path: str, mode: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO jobs(run_id,input_path,mode,status,created_at) VALUES (?,?,?,'queued',?)",
                (run_id, input_path, mode, time.time()))

    def jobs_unfinished(self) -> list[dict]:
        """queued or running = was interrupted (deploy/crash) -> re-enqueue on startup."""
        cur = self.db.execute(
            f"SELECT {self._JOB_COLS} FROM jobs WHERE status IN ('queued','running') ORDER BY created_at")
        return [self._job_row(r) for r in cur]

    def job_resume(self, run_id: str) -> None:
        with self.db:
            self.db.execute("UPDATE jobs SET status='running', error=NULL WHERE run_id=?", (run_id,))

    def job_finish(self, run_id: str, status: str, summary: dict | None = None, error: str = "") -> None:
        import json

        with self.db:
            self.db.execute(
                "UPDATE jobs SET status=?, finished_at=?, summary=?, error=? WHERE run_id=?",
                (status, time.time(), json.dumps(summary) if summary else None, error or None, run_id))

    def _job_row(self, r) -> dict:
        import json

        return {"run_id": r[0], "input_path": r[1], "mode": r[2], "status": r[3], "created_at": r[4],
                "finished_at": r[5], "summary": json.loads(r[6]) if r[6] else None, "error": r[7]}

    _JOB_COLS = "run_id,input_path,mode,status,created_at,finished_at,summary,error"

    def job_get(self, run_id: str) -> dict | None:
        r = self.db.execute(f"SELECT {self._JOB_COLS} FROM jobs WHERE run_id=?", (run_id,)).fetchone()
        return self._job_row(r) if r else None

    def jobs_recent(self, n: int = 20) -> list[dict]:
        cur = self.db.execute(f"SELECT {self._JOB_COLS} FROM jobs ORDER BY created_at DESC LIMIT ?", (n,))
        return [self._job_row(r) for r in cur]

    def jobs_running(self) -> list[dict]:
        cur = self.db.execute(f"SELECT {self._JOB_COLS} FROM jobs WHERE status='running'")
        return [self._job_row(r) for r in cur]

    def last_job_started(self) -> float | None:
        r = self.db.execute("SELECT MAX(created_at) FROM jobs").fetchone()
        return r[0] if r else None

    def close(self) -> None:
        self.db.close()
