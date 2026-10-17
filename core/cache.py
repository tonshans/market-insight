# -*- coding: utf-8 -*-
"""Parquet cache OHLCV dengan TTL (F5, ARCHITECTURE.md §2 core/cache.py).

Cache agresis per (exchange, symbol, timeframe) — mitigasi R-1 (rate limit
Indodax) & R-2 (wear SD card: file kecil, tmpfs-friendly, path dari config).

Skema file parquet:
    ts        int64   epoch-ms open time candle
    open/high/low/close/volume  float64
    fetched_at int64  epoch-ms saat data di-fetch (untuk audit/staleness)

Status yang dikembalikan `get()`:
    HIT     -> fresh (umur < TTL)
    STALE   -> ada tapi umur >= TTL (masih boleh dipakai sbg fallback terakhir)
    MISS    -> tidak ada file / korup
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Optional

import pandas as pd

STATUS_HIT = "HIT"
STATUS_STALE = "STALE"
STATUS_MISS = "MISS"


@dataclass
class CacheResult:
    status: str                      # HIT | STALE | MISS
    df: Optional[pd.DataFrame]       # None bila MISS
    age_seconds: float               # umur data sejak fetched_at (inf bila MISS)
    ttl_seconds: int
    path: str


def _sanitize(part: str) -> str:
    """Amankan komponen path (symbol 'BTC/IDR' -> 'BTC_IDR')."""
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in part)


class ParquetCache:
    """Simple on-disk Parquet cache keyed by (exchange, symbol, timeframe)."""

    def __init__(self, cache_dir: str = "cache"):
        self.cache_dir = cache_dir
        os.makedirs(self.cache_dir, exist_ok=True)

    # ------------------------------------------------------------------ path
    def key_path(self, exchange: str, symbol: str, timeframe: str) -> str:
        name = f"{_sanitize(exchange)}__{_sanitize(symbol)}__{_sanitize(timeframe)}.parquet"
        return os.path.join(self.cache_dir, name)

    # ------------------------------------------------------------------- get
    def get(
        self,
        exchange: str,
        symbol: str,
        timeframe: str,
        ttl_seconds: int,
        now: Optional[float] = None,
    ) -> CacheResult:
        path = self.key_path(exchange, symbol, timeframe)
        if not os.path.exists(path):
            return CacheResult(STATUS_MISS, None, float("inf"), ttl_seconds, path)
        try:
            df = pd.read_parquet(path)
        except Exception:
            # File korup -> anggap MISS (fetch akan menimpa dengan data baru).
            return CacheResult(STATUS_MISS, None, float("inf"), ttl_seconds, path)
        if df.empty or "ts" not in df.columns:
            return CacheResult(STATUS_MISS, None, float("inf"), ttl_seconds, path)

        # df.attrs TIDAK survive round-trip parquet -> pakai sidecar .meta.json.
        fetched_at = self._read_fetched_at(path) or self._mtime_fallback(path)
        age = max(0.0, (now if now is not None else time.time()) - fetched_at)
        status = STATUS_HIT if age < ttl_seconds else STATUS_STALE
        return CacheResult(status, df, age, ttl_seconds, path)

    @staticmethod
    def _meta_path(path: str) -> str:
        return path + ".meta.json"

    @classmethod
    def _read_fetched_at(cls, path: str) -> float:
        try:
            with open(cls._meta_path(path), "r", encoding="utf-8") as fh:
                return float(json.load(fh).get("fetched_at", 0))
        except Exception:
            return 0.0

    @staticmethod
    def _mtime_fallback(path: str) -> float:
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0.0

    # ------------------------------------------------------------------- put
    def put(
        self,
        exchange: str,
        symbol: str,
        timeframe: str,
        df: pd.DataFrame,
        max_candles: int = 300,
    ) -> str:
        """Persist DataFrame OHLCV ke parquet (atomic: tmp + os.replace)."""
        path = self.key_path(exchange, symbol, timeframe)
        out = df.copy()
        if max_candles and len(out) > max_candles:
            out = out.tail(max_candles)
        out.attrs["fetched_at"] = time.time()

        tmp = path + ".tmp"
        out.to_parquet(tmp, index=False)
        os.replace(tmp, path)  # atomic write (NFR-2)

        # Sidecar metadata: attrs DataFrame tidak survive round-trip parquet.
        with open(self._meta_path(path), "w", encoding="utf-8") as fh:
            json.dump({"fetched_at": out.attrs["fetched_at"],
                       "rows": int(len(out)),
                       "exchange": exchange, "symbol": symbol,
                       "timeframe": timeframe}, fh)
        return path

    # ------------------------------------------------------------- utilities
    def flush(self, exchange: Optional[str] = None) -> int:
        """Hapus semua (atau per-exchange) file cache. Return jumlah file."""
        removed = 0
        if not os.path.isdir(self.cache_dir):
            return 0
        prefix = f"{_sanitize(exchange)}__" if exchange else ""
        for fname in os.listdir(self.cache_dir):
            if not fname.startswith(prefix):
                continue
            if fname.endswith(".parquet") or fname.endswith(".parquet.meta.json"):
                try:
                    os.remove(os.path.join(self.cache_dir, fname))
                    removed += 1
                except OSError:
                    pass
        return removed

    def stats(self) -> dict:
        files = []
        if os.path.isdir(self.cache_dir):
            files = [f for f in os.listdir(self.cache_dir) if f.endswith(".parquet")]
        return {"dir": os.path.abspath(self.cache_dir), "files": len(files)}
