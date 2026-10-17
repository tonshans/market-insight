# -*- coding: utf-8 -*-
"""SQLite wrapper untuk dynamic state (D-1, NFR-2, F3/F4, ARCHITECTURE.md §3.3).

Prinsip (config vs state):
    - YAML  : konfigurasi statis + watchlist (core/watchlist.py)
    - SQLite: state dinamis -> riwayat scan & key/value bot state
              (shared antara CLI dan Telegram bot).

Keamanan di Raspberry Pi:
    - WAL mode (journal_mode=WAL) -> write tidak memblokir read, lebih tahan
      terhadap corruption pada SD card (R-2).
    - `synchronous=NORMAL` -> kompromi durability/IO yang wajar utk RPi.
    - Semua operasi memakai context manager; koneksi singkat per operasi
      sehingga aman dipakai bergantian dari thread bot.

Skema sesuai §4.2 dokumen arsitektur.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
from typing import Any, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scan_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    symbol TEXT NOT NULL,
    rank INTEGER,
    prospect REAL,
    risk REAL,
    adj_score REAL,
    raw_scores_json TEXT -- dump raw scores utk Fase 2 (D-3)
);
CREATE INDEX IF NOT EXISTS idx_symbol_ts ON scan_history(symbol, ts_utc);

CREATE TABLE IF NOT EXISTS bot_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def _json_safe(v: Any) -> Any:
    """NaN/Inf tidak valid dalam JSON strict -> ubah jadi None (null)."""
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if isinstance(v, dict):
        return {k: _json_safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_json_safe(x) for x in v]
    return v


class StateStore:
    """Wrapper tipis di di atas sqlite3 (built-in, tanpa dependency eksternal)."""

    def __init__(self, db_path: str = "state/market_tools.db"):
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._init_db()

    # ------------------------------------------------------------- plumbing
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.executescript(_SCHEMA)

    # ------------------------------------------------------- scan history
    def save_scan_result(self, ts_utc: str, rows: list[dict[str, Any]]) -> int:
        """Simpan satu hasil scan (list ranking row). Return jumlah baris.

        Row minimal berisi: symbol; opsional rank/prospect/risk/adj_score
        dan field apa pun yang akan di-dump ke raw_scores_json (D-3).
        """
        if not rows:
            return 0
        payload = []
        for r in rows:
            raw = {k: v for k, v in r.items()
                   if k not in ("symbol", "rank", "prospect", "risk", "adj_score")}
            payload.append((
                ts_utc,
                str(r["symbol"]),
                int(r["rank"]) if r.get("rank") is not None else None,
                float(r["prospect"]) if r.get("prospect") is not None else None,
                float(r["risk"]) if r.get("risk") is not None else None,
                float(r["adj_score"]) if r.get("adj_score") is not None else None,
                json.dumps(_json_safe(raw), default=str, allow_nan=False),
            ))
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO scan_history (ts_utc, symbol, rank, prospect, risk,"
                " adj_score, raw_scores_json) VALUES (?,?,?,?,?,?,?)", payload)
        return len(payload)

    def get_last_scan(self) -> Optional[dict[str, Any]]:
        """Ambil hasil scan terakhir sebagai {ts_utc, rows:[...]} atau None."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(ts_utc) AS ts FROM scan_history").fetchone()
            if not row or row["ts"] is None:
                return None
            ts = row["ts"]
            rows = conn.execute(
                "SELECT * FROM scan_history WHERE ts_utc = ? ORDER BY rank ASC",
                (ts,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                extra = json.loads(d.pop("raw_scores_json") or "{}")
            except (TypeError, ValueError):
                extra = {}
            d.update(extra)
            out.append(d)
        return {"ts_utc": ts, "rows": out}

    def get_symbol_history(self, symbol: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM scan_history WHERE symbol = ?"
                " ORDER BY ts_utc DESC LIMIT ?", (symbol, int(limit))).fetchall()
        return [dict(r) for r in rows]

    def compare_rank(self, symbol: str) -> Optional[dict[str, Any]]:
        """Bandingkan rank simbol pada scan terakhir vs scan sebelumnya (F3).

        Return None bila data belum dua kali; jika simbol absen di salah satu
        scan, rank-nya None (diinterpretasikan caller sbg ENTERED/EXITED).
        """
        with self._connect() as conn:
            scans = conn.execute(
                "SELECT DISTINCT ts_utc FROM scan_history"
                " ORDER BY ts_utc DESC LIMIT 2").fetchall()
        if len(scans) < 2:
            return None
        cur_ts, prev_ts = scans[0]["ts_utc"], scans[1]["ts_utc"]

        def _rank_at(ts: str) -> Optional[int]:
            with self._connect() as conn:
                r = conn.execute(
                    "SELECT rank FROM scan_history WHERE ts_utc=? AND symbol=?",
                    (ts, symbol)).fetchone()
            return None if r is None else r["rank"]

        cur, prev = _rank_at(cur_ts), _rank_at(prev_ts)
        delta = None
        if cur is not None and prev is not None:
            delta = prev - cur  # positif = naik peringkat
        return {"symbol": symbol, "current_rank": cur, "previous_rank": prev,
                "delta": delta, "current_ts": cur_ts, "previous_ts": prev_ts}

    # ---------------------------------------------------------- bot state
    def set_bot_state(self, key: str, value: Any) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO bot_state(key, value) VALUES(?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, default=str)))

    def get_bot_state(self, key: str, default: Any = None) -> Any:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM bot_state WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return row["value"]

    # --------------------------------------------------------------- misc
    def prune_history(self, keep_days: int = 90) -> int:
        """Hapus riwayat > keep_days (mitigasi R-2: hemat kartu SD)."""
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=int(keep_days))).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM scan_history WHERE ts_utc < ?", (cutoff,))
        return cur.rowcount

    def stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            n_scans = conn.execute(
                "SELECT COUNT(DISTINCT ts_utc) AS c FROM scan_history").fetchone()["c"]
            n_rows = conn.execute(
                "SELECT COUNT(*) AS c FROM scan_history").fetchone()["c"]
            last = conn.execute(
                "SELECT MAX(ts_utc) AS ts FROM scan_history").fetchone()["ts"]
        return {"db_path": self.db_path, "distinct_scans": n_scans,
                "total_rows": n_rows, "last_scan_ts": last,
                "file_size_bytes": os.path.getsize(self.db_path)
                if os.path.exists(self.db_path) else 0}


def open_state(config_or_path) -> StateStore:
    """Factory: terima Config (pakai .db_path) atau path string langsung."""
    if isinstance(config_or_path, str):
        return StateStore(config_or_path)
    return StateStore(getattr(config_or_path, "db_path", "state/market_tools.db"))
