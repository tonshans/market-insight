# -*- coding: utf-8 -*-
"""Smoke test WP3: Data Layer — core/cache.py (Parquet+TTL) & core/fetch.py (ccxt+CircuitBreaker).

Menjalankan resilience drill mini (§6.2): exchange mock yang gagal 3x harus
memicu cooldown, dan fetch selama cooldown TIDAK menyentuh jaringan.
"""
from __future__ import annotations

import os
import shutil
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd  # noqa: E402

from core.cache import ParquetCache, STATUS_HIT, STATUS_MISS, STATUS_STALE  # noqa: E402
from core.config import load_config  # noqa: E402
from core.fetch import (  # noqa: E402
    CircuitBreaker,
    FetchError,
    MarketDataFetcher,
    SOURCE_CACHE,
    SOURCE_COOLDOWN_STALE,
    SOURCE_FALLBACK_STALE,
    SOURCE_NETWORK,
)

CFG = load_config(os.path.join(os.path.dirname(__file__), "..", "config.yaml"))

def fresh_tmp(name: str) -> str:
    """Dir uji yang selalu bersih (idempotent antar-run & antar-test-file)."""
    path = os.path.join("/tmp", name)
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)
    return path



def make_ohlcv(n: int = 50, start: float = 1_700_000_000_000) -> list:
    out = []
    ts = start
    for i in range(n):
        o = 100 + i
        out.append([ts, o, o + 2, o - 1, o + 1, 1000 + i])
        ts += 3_600_000
    return out


class MockExchange:
    def __init__(self, ohlcv=None, fail_with=None):
        self.calls = 0
        self.ohlcv = ohlcv if ohlcv is not None else make_ohlcv()
        self.fail_with = fail_with  # exception instance; None => sukses

    def fetch_ohlcv(self, symbol, timeframe="1h", limit=None):
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        return self.ohlcv[:limit] if limit else self.ohlcv


# ---------------------------------------------------------------- cache tests
def test_cache_miss_hit_stale(tmpdir=fresh_tmp("wp3_cache")):
    c = ParquetCache(tmpdir)
    r = c.get("indodax", "BTC/IDR", "1h", ttl_seconds=3600)
    assert r.status == STATUS_MISS and r.df is None

    df = pd.DataFrame(make_ohlcv(10), columns=["ts", "open", "high", "low", "close", "volume"])
    c.put("indodax", "BTC/IDR", "1h", df, max_candles=300)

    r = c.get("indodax", "BTC/IDR", "1h", ttl_seconds=3600)
    assert r.status == STATUS_HIT and len(r.df) == 10 and r.age_seconds < 5

    # TTL 0 detik -> STALE (bukan MISS; data tetap dikembalikan utk fallback)
    r2 = c.get("indodax", "BTC/IDR", "1h", ttl_seconds=0)
    assert r2.status == STATUS_STALE and r2.df is not None


def test_cache_truncates_and_sanitizes(tmpdir=fresh_tmp("wp3_cache2")):
    c = ParquetCache(tmpdir)
    df = pd.DataFrame(make_ohlcv(400), columns=["ts", "open", "high", "low", "close", "volume"])
    c.put("indodax", "ETH/IDR", "4h", df, max_candles=300)
    path = c.key_path("indodax", "ETH/IDR", "4h")
    assert "ETH_IDR" in path and os.path.exists(path)
    got = c.get("indodax", "ETH/IDR", "4h", 3600)
    assert len(got.df) == 300  # tail(max_candles)


def test_cache_corrupt_file_is_miss(tmpdir=fresh_tmp("wp3_cache3")):
    c = ParquetCache(tmpdir)
    path = c.key_path("indodax", "X/IDR", "1d")
    with open(path, "w") as fh:
        fh.write("not a parquet")
    r = c.get("indodax", "X/IDR", "1d", 3600)
    assert r.status == STATUS_MISS


def test_cache_flush(tmpdir=fresh_tmp("wp3_cache4")):
    c = ParquetCache(tmpdir)
    df = pd.DataFrame(make_ohlcv(5), columns=["ts", "open", "high", "low", "close", "volume"])
    c.put("indodax", "A/IDR", "1h", df)
    c.put("binance", "B/USDT", "1h", df)
    assert c.stats()["files"] == 2
    n = c.flush("indodax")          # hanya exchange tsb (parquet + meta)
    assert n == 2 and c.stats()["files"] == 1
    assert c.flush() == 2           # sisanya
    assert c.stats()["files"] == 0


# ------------------------------------------------------------------ cb tests
def test_circuit_breaker_unit():
    cb = CircuitBreaker(failure_threshold=3, failure_window_seconds=60, cooldown_seconds=300)
    assert not cb.in_cooldown
    assert cb.record_failure() is False
    assert cb.record_failure() is False
    assert cb.record_failure() is True          # 3x -> trigger
    assert cb.in_cooldown and cb.remaining_cooldown() > 290
    # success setelah trigger tidak menghapus cooldown (hanya reset counter)
    cb.record_success()
    assert cb.in_cooldown
    st = cb.status()
    assert st["trigger_count"] == 1 and st["consecutive_failures"] == 0


def test_circuit_breaker_success_resets_counter():
    cb = CircuitBreaker(failure_threshold=3, failure_window_seconds=60, cooldown_seconds=5)
    cb.record_failure(); cb.record_failure()
    cb.record_success()                          # reset beruntun
    assert cb.record_failure() is False          # mulai lagi dr 1
    assert not cb.in_cooldown


# ---------------------------------------------------------------- fetch tests
def _fetcher(tmpdir, exchange):
    os.makedirs(tmpdir, exist_ok=True)
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config.yaml"),
                      overrides={"exchange": {"request_sleep_ms": 0},
                                 "cache": {"dir": tmpdir}})
    f = MarketDataFetcher(cfg, exchange=exchange)
    return f


def test_fetch_network_then_cache_hit(tmpdir=fresh_tmp("wp3_fetch1")):
    ex = MockExchange()
    f = _fetcher(tmpdir, ex)
    r = f.fetch_ohlcv("BTC/IDR", "1h", limit=30)
    assert r.source == SOURCE_NETWORK and ex.calls == 1 and len(r.df) == 30
    assert os.path.exists(f.cache.key_path(CFG.exchange_id, "BTC/IDR", "1h"))

    r2 = f.fetch_ohlcv("BTC/IDR", "1h", limit=30)
    assert r2.source == SOURCE_CACHE and ex.calls == 1   # level 1: tanpa jaringan


def test_fetch_fallback_stale_on_network_error(tmpdir=fresh_tmp("wp3_fetch2")):
    import ccxt
    good = MockExchange()
    f = _fetcher(tmpdir, good)
    f.fetch_ohlcv("ETH/IDR", "1h")                       # isi cache (fresh)

    # paksa cache jadi stale dgn TTL 0 utk symbol ini via config override lain:
    bad = MockExchange(fail_with=ccxt.ExchangeNotAvailable("503 down"))
    f2 = _fetcher(tmpdir, bad)
    f2.breaker = CircuitBreaker(failure_threshold=99, failure_window_seconds=60, cooldown_seconds=300)
    # ubah ttl symbol tsb agar cache dianggap STALE -> pakai jalur fallback
    f2.config.raw["cache"]["ttl_minutes"]["1h"] = 0
    r = f2.fetch_ohlcv("ETH/IDR", "1h")
    assert r.source == SOURCE_FALLBACK_STALE and r.df is not None
    assert any("Network gagal" in w for w in r.warnings)


def test_fetch_triggers_cooldown_after_3_failures(tmpdir=fresh_tmp("wp3_fetch3")):
    """Resilience drill §6.2: 3x gagal beruntun -> cooldown, no network calls."""
    import ccxt
    f = _fetcher(tmpdir, MockExchange(fail_with=ccxt.RequestTimeout("timeout")))
    f.breaker = CircuitBreaker(failure_threshold=3, failure_window_seconds=60, cooldown_seconds=300)

    for _ in range(3):
        try:
            f.fetch_ohlcv("DOGE/IDR", "1h")
            raise AssertionError("should have raised FetchError (no cache)")
        except FetchError as exc:
            assert "Gagal fetch" in str(exc)

    assert f.breaker.in_cooldown
    calls_at_trigger = f._exchange.calls
    assert calls_at_trigger == 3

    # fetch berikutnya SELAMA cooldown tidak menyentuh jaringan, dan flag aktif
    try:
        f.fetch_ohlcv("XRP/IDR", "1h")
        raise AssertionError("expected FetchError with cooldown flag")
    except FetchError as exc:
        assert exc.cooldown_active is True
    assert f._exchange.calls == calls_at_trigger  # tidak ada request baru

    st = f.market_status()["circuit_breaker"]
    assert st["in_cooldown"] and st["trigger_count"] == 1


def test_fetch_cooldown_returns_stale_cache(tmpdir=fresh_tmp("wp3_fetch4")):
    import ccxt
    ok = MockExchange()
    f = _fetcher(tmpdir, ok)
    f.fetch_ohlcv("ADA/IDR", "1h")                     # cache terisi

    # ganti ke exchange gagal + pancing cooldown
    f.set_exchange(MockExchange(fail_with=ccxt.NetworkError("conn refused")))
    f.breaker = CircuitBreaker(failure_threshold=1, failure_window_seconds=60, cooldown_seconds=300)
    f.config.raw["cache"]["ttl_minutes"]["1h"] = 0     # cache dianggap stale
    r = f.fetch_ohlcv("ADA/IDR", "1h")                 # gagal -> fallback stale (+trigger)
    assert r.source == SOURCE_FALLBACK_STALE and r.cooldown_active
    r2 = f.fetch_ohlcv("ADA/IDR", "1h")                # sekarang dlm cooldown
    assert r2.source == SOURCE_COOLDOWN_STALE and r2.cooldown_active
    assert any("API_COOLDOWN" in w for w in r2.warnings)
    assert "circuit breaker" in r.warnings[0].lower() or "API_COOLDOWN" in r.warnings[0]


def test_client_side_error_not_counted(tmpdir=fresh_tmp("wp3_fetch5")):
    import ccxt
    f = _fetcher(tmpdir, MockExchange(fail_with=ccxt.BadSymbol("no market")))
    try:
        f.fetch_ohlcv("FAKE/IDR", "1h")
        raise AssertionError("expected FetchError")
    except FetchError:
        pass
    assert f.breaker.status()["consecutive_failures"] == 0
    assert not f.breaker.in_cooldown


def test_invalid_timeframe_rejected(tmpdir=fresh_tmp("wp3_fetch6")):
    from core.time_utils import TimeframeError
    f = _fetcher(tmpdir, MockExchange())
    try:
        f.fetch_ohlcv("BTC/IDR", "7x")
        raise AssertionError("expected TimeframeError")
    except TimeframeError:
        pass


def test_fetch_many_collects_results(tmpdir=fresh_tmp("wp3_fetch7")):
    f = _fetcher(tmpdir, MockExchange())
    res = f.fetch_many(["A/IDR", "B/IDR"], "1h", limit=10)
    assert set(res) == {"A/IDR", "B/IDR"}
    assert all(r.source == SOURCE_NETWORK for r in res.values())


if __name__ == "__main__":
    failures = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                failures.append(name)
                print(f"FAIL {name}: {exc}")
    print("-" * 50)
    print(f"{len(failures)} failure(s)")
    sys.exit(1 if failures else 0)
