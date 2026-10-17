# -*- coding: utf-8 -*-
"""Smoke & unit tests WP6: state.py (SQLite WAL), watchlist.py (YAML CRUD),
scanner.py (universe/prefilter/skor/rank/rank-change/persist)."""
from __future__ import annotations

import json
import math
import os
import sys
import tempfile

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.config import load_config, load_scanner_config
from core.fetch import FetchError, FetchResult, SOURCE_NETWORK
from core.indicators import ohlcv_to_dataframe
from core.scanner import (ScanOutcome, build_universe, format_scan_text,
                          prefilter_adx, run_scan, score_symbol)
from core.state import StateStore, open_state
from core.time_utils import iso_utc
from core.watchlist import Watchlist, normalize_symbol


# ------------------------------------------------------------------ fixtures
def make_df(trend="up", n=140, seed=7):
    rng = np.random.default_rng(seed)
    drift = {"up": 0.35, "down": -0.35, "flat": 0.0}[trend]
    close = 100 + np.cumsum(rng.normal(drift, 1.0, n))
    open_ = close + rng.normal(0, 0.4, n)
    high = np.maximum(open_, close) + abs(rng.normal(0, 0.8, n))
    low = np.minimum(open_, close) - abs(rng.normal(0, 0.8, n))
    vol = rng.uniform(50, 250, n)
    ts = [int(t.timestamp() * 1000)
          for t in pd.date_range("2025-01-01", periods=n, freq="4h")]
    return ohlcv_to_dataframe([[t, o, h, l, c, v] for t, o, h, l, c, v
                               in zip(ts, open_, high, low, close, vol)])


class MockFetcher:
    """Fetcher minimal interface-compatible utk scanner tests."""

    def __init__(self, frames: dict[str, dict[str, pd.DataFrame]],
                 markets: dict | None = None, fail_symbols=()):
        self.frames = frames            # symbol -> {tf: df}
        self._markets = markets or {}
        self.fail_symbols = set(fail_symbols)
        self.calls: list[tuple[str, str]] = []

    def load_markets(self, force=False):
        return self._markets

    def fetch_ohlcv(self, symbol, timeframe, limit=None,
                    allow_stale_fallback=True):
        self.calls.append((symbol, timeframe))
        if symbol in self.fail_symbols:
            raise FetchError(f"simulated network failure {symbol}",
                             cooldown_active=False)
        df = self.frames.get(symbol, {}).get(timeframe)
        if df is None:
            raise FetchError(f"no data {symbol} {timeframe}")
        return FetchResult(symbol, timeframe, df, SOURCE_NETWORK)


@pytest.fixture
def cfg():
    return load_config(os.path.join(os.path.dirname(__file__), "..", "config.yaml"))


@pytest.fixture
def scfg():
    return load_scanner_config(os.path.join(os.path.dirname(__file__), "..",
                                            "scanner.yaml"))


# ====================================================================== state
class TestStateStore:
    def test_wal_mode_enabled(self, tmp_path):
        st = StateStore(str(tmp_path / "state" / "mt.db"))
        import sqlite3
        conn = sqlite3.connect(st.db_path)
        mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        conn.close()
        assert mode.lower() == "wal"

    def test_schema_matches_arch(self, tmp_path):
        st = StateStore(str(tmp_path / "mt.db"))
        import sqlite3
        conn = sqlite3.connect(st.db_path)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(scan_history)")]
        idx = [r[1] for r in conn.execute("PRAGMA index_list(scan_history)")]
        bot_cols = [r[1] for r in conn.execute("PRAGMA table_info(bot_state)")]
        conn.close()
        assert cols == ["id", "ts_utc", "symbol", "rank", "prospect", "risk",
                        "adj_score", "raw_scores_json"]
        assert "idx_symbol_ts" in idx
        assert bot_cols == ["key", "value"]

    def test_save_and_get_last_scan_roundtrip(self, tmp_path):
        st = StateStore(str(tmp_path / "mt.db"))
        rows = [{"symbol": "BTC/IDR", "rank": 1, "prospect": 71.5, "risk": 30.0,
                 "adj_score": 56.5, "trend_score_raw": 0.62, "adx": 28.1},
                {"symbol": "ETH/IDR", "rank": 2, "prospect": 60.0, "risk": 25.0,
                 "adj_score": 47.5, "trend_score_raw": 0.4}]
        ts = iso_utc()
        assert st.save_scan_result(ts, rows) == 2
        last = st.get_last_scan()
        assert last["ts_utc"] == ts and len(last["rows"]) == 2
        assert last["rows"][0]["symbol"] == "BTC/IDR"
        # raw extras survive round-trip via raw_scores_json (D-3)
        assert last["rows"][0]["trend_score_raw"] == 0.62
        assert last["rows"][0]["adx"] == 28.1

    def test_get_last_scan_empty_returns_none(self, tmp_path):
        st = StateStore(str(tmp_path / "mt.db"))
        assert st.get_last_scan() is None

    def test_compare_rank_delta(self, tmp_path):
        st = StateStore(str(tmp_path / "mt.db"))
        st.save_scan_result("2025-01-01T00:00:00+00:00",
                            [{"symbol": "A/IDR", "rank": 3},
                             {"symbol": "B/IDR", "rank": 1}])
        st.save_scan_result("2025-01-02T00:00:00+00:00",
                            [{"symbol": "A/IDR", "rank": 1},
                             {"symbol": "C/IDR", "rank": 2}])
        cmp_a = st.compare_rank("A/IDR")
        assert cmp_a["current_rank"] == 1 and cmp_a["previous_rank"] == 3
        assert cmp_a["delta"] == 2                       # naik 2 peringkat
        cmp_c = st.compare_rank("C/IDR")
        assert cmp_c["previous_rank"] is None and cmp_c["delta"] is None
        assert st.compare_rank("Z/IDR")["current_rank"] is None

    def test_compare_rank_single_scan_returns_none(self, tmp_path):
        st = StateStore(str(tmp_path / "mt.db"))
        st.save_scan_result(iso_utc(), [{"symbol": "A/IDR", "rank": 1}])
        assert st.compare_rank("A/IDR") is None

    def test_bot_state_keyvalue(self, tmp_path):
        st = StateStore(str(tmp_path / "mt.db"))
        assert st.get_bot_state("missing", default={"a": 1}) == {"a": 1}
        st.set_bot_state("last_scan", {"ts_utc": "x", "n": 5})
        got = st.get_bot_state("last_scan")
        assert got == {"ts_utc": "x", "n": 5}
        st.set_bot_state("last_scan", {"ts_utc": "y"})   # upsert
        assert st.get_bot_state("last_scan")["ts_utc"] == "y"

    def test_prune_history(self, tmp_path):
        st = StateStore(str(tmp_path / "mt.db"))
        st.save_scan_result("2000-01-01T00:00:00+00:00", [{"symbol": "OLD/IDR",
                                                           "rank": 1}])
        st.save_scan_result(iso_utc(), [{"symbol": "NEW/IDR", "rank": 1}])
        removed = st.prune_history(keep_days=90)
        assert removed == 1
        assert st.get_last_scan()["rows"][0]["symbol"] == "NEW/IDR"

    def test_open_state_factory_with_config(self, cfg, tmp_path):
        cfg.raw.setdefault("state", {})["db_path"] = str(tmp_path / "sub" / "x.db")
        st = open_state(cfg)
        assert os.path.exists(st.db_path)

    def test_nan_safe_persistence(self, tmp_path):
        st = StateStore(str(tmp_path / "mt.db"))
        st.save_scan_result(iso_utc(), [{"symbol": "X/IDR", "rank": 1,
                                         "some_float": float("nan")}])
        # raw_scores_json ditulis dengan allow_nan=False -> NaN jadi string? 
        # json.dumps default str converter tidak dipakai utk float nan; kita
        # pastikan tidak crash dan bisa dibaca kembali.
        last = st.get_last_scan()
        assert last["rows"][0]["symbol"] == "X/IDR"


# ================================================================= watchlist
class TestWatchlist:
    def test_normalize(self):
        assert normalize_symbol("btc/idr") == "BTC/IDR"
        assert normalize_symbol(" DOGE-IDR ") == "DOGE/IDR"
        with pytest.raises(ValueError):
            normalize_symbol("BTCIDR")
        with pytest.raises(ValueError):
            normalize_symbol("")

    def test_crud_roundtrip(self, tmp_path):
        p = str(tmp_path / "watchlist.yaml")
        wl = Watchlist(p)
        assert len(wl) == 0
        wl.add("btc/idr", note="leader")
        wl.add("ETH/IDR")
        wl.add("eth-idr")                      # duplikat dinormalisasi
        assert wl.symbols == ["BTC/IDR", "ETH/IDR"]
        # __contains__ dinormalisasi: varian ejaan dianggap sama (fitur),
        # tapi hanya bentuk kanonikal yang tersimpan di list.
        assert "BTC/IDR" in wl and "btc/idr" in wl and "eth-idr" in wl
        assert "SOL/USDT" not in wl
        assert all(s in ("BTC/IDR", "ETH/IDR") for s in wl.symbols)
        # persist ke disk, reload = sama
        wl2 = Watchlist(p)
        assert wl2.symbols == ["BTC/IDR", "ETH/IDR"]
        assert wl2.entries()[0]["note"] == "leader"
        assert wl2.remove("BTC/IDR") is True
        assert wl2.remove("BTC/IDR") is False
        assert Watchlist(p).symbols == ["ETH/IDR"]
        assert Watchlist(p).clear() == 1
        assert Watchlist(p).symbols == []

    def test_atomic_write_no_tmp_leftover(self, tmp_path):
        p = str(tmp_path / "wl.yaml")
        wl = Watchlist(p)
        wl.add("BTC/IDR")
        leftovers = [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]
        assert leftovers == []
        assert os.path.exists(p)

    def test_corrupt_entries_skipped(self, tmp_path):
        p = tmp_path / "wl.yaml"
        p.write_text("symbols:\n  - symbol: 'BAD SYMBOL'\n  - symbol: BTC/IDR\n")
        wl = Watchlist(str(p))
        assert wl.symbols == ["BTC/IDR"]      # entri invalid dilewati, tidak crash


# =================================================================== scanner
MARKETS = {
    "BTC/IDR": {"active": True, "base": "BTC", "quote": "IDR",
                "info": {"quote_volume": 5e10}},
    "ETH/IDR": {"active": True, "base": "ETH", "quote": "IDR",
                "info": {"quote_volume": 2e10}},
    "USDT/IDR": {"active": True, "base": "USDT", "quote": "IDR",
                 "info": {"quote_volume": 9e10}},         # stablecoin base
    "SOL/USDT": {"active": True, "base": "SOL", "quote": "USDT",
                 "info": {"quote_volume": 8e10}},         # quote salah
    "KRYPT/IDR": {"active": True, "base": "KRYPT", "quote": "IDR",
                  "info": {"quote_volume": 1e6}},         # volume terlalu kecil
    "DEAD/IDR": {"active": False, "base": "DEAD", "quote": "IDR",
                 "info": {"quote_volume": 5e9}},          # inactive
}


def _frames():
    return {
        "BTC/IDR": {"4h": make_df("up"), "1h": make_df("up", seed=1),
                    "1d": make_df("up", seed=2)},
        "ETH/IDR": {"4h": make_df("down"), "1h": make_df("down", seed=3),
                    "1d": make_df("down", seed=4)},
    }


class TestUniverseAndPrefilter:
    def test_build_universe_filters(self, scfg):
        f = MockFetcher(_frames(), markets=MARKETS)
        uni = build_universe(f, scfg.universe)
        syms = {m["symbol"] for m in uni}
        assert "BTC/IDR" in syms and "ETH/IDR" in syms
        assert "USDT/IDR" not in syms       # stablecoin excluded
        assert "SOL/USDT" not in syms       # quote != IDR
        assert "KRYPT/IDR" not in syms      # qvol < min
        assert "DEAD/IDR" not in syms       # inactive
        vols = [m["qvol_24h"] for m in uni]
        assert vols == sorted(vols, reverse=True)

    def test_max_symbols_cap(self, scfg):
        big = {f"C{i}/IDR": {"active": True, "base": f"C{i}", "quote": "IDR",
                             "info": {"quote_volume": 1e10 + i}}
               for i in range(50)}
        f = MockFetcher({}, markets=big)
        u = dict(scfg.universe); u["max_symbols"] = 10
        uni = build_universe(f, u)
        assert len(uni) == 10

    def test_prefilter_adx_keeps_trending_only(self, cfg, scfg):
        frames = _frames()
        # seed 13 -> ADX(14) ~9.0 < min_adx 15 (deterministik, sudah diverifikasi)
        frames["FLAT/IDR"] = {"4h": make_df("flat", seed=13)}
        mk = dict(MARKETS)
        mk["FLAT/IDR"] = {"active": True, "base": "FLAT", "quote": "IDR",
                          "info": {"quote_volume": 3e9}}
        f = MockFetcher(frames, markets=mk)
        cands, errs, cached, cool = prefilter_adx(
            f, ["BTC/IDR", "ETH/IDR", "FLAT/IDR"],
            {"prefilter": scfg.prefilter}, cfg.analysis)
        assert "BTC/IDR" in cands and "ETH/IDR" in cands
        assert "FLAT/IDR" not in cands      # ADX flat < min_adx
        assert cool is False
        assert "FLAT/IDR" not in cached     # hanya kandidat lolos yg di-cache

    def test_prefilter_cooldown_short_circuits(self, cfg, scfg):
        class RaisingFetcher(MockFetcher):
            def fetch_ohlcv(self, *a, **k):
                raise FetchError("API_COOLDOWN: cooldown aktif",
                                 cooldown_active=True)
        f = RaisingFetcher({}, markets={})
        cands, errs, _, cool = prefilter_adx(
            f, ["A/IDR", "B/IDR", "C/IDR"],
            {"prefilter": scfg.prefilter}, cfg.analysis)
        assert cands == [] and cool is True
        # sisa symbol langsung ditandai tanpa mencoba fetch lagi
        assert len(errs) == 3
        # MockFetcher override mencatat call sendiri -> f.calls kosong;
        # bukti short-circuit: errs terisi 3 padahal hanya 1 fetch dicoba
        assert errs["A/IDR"].startswith("API_COOLDOWN")
        assert errs["B/IDR"].startswith("skipped")
        assert errs["C/IDR"].startswith("skipped")


class TestScoring:
    def test_score_symbol_fields_and_bounds(self, cfg, scfg):
        row = score_symbol(make_df("up"), "BTC/IDR", 5e10, cfg.analysis,
                           {"scoring": scfg.scoring, "ranking": scfg.ranking})
        for k in ("prospect", "risk", "adj_score", "trend_score_raw",
                  "vsa_score", "wick_rejection_score", "components"):
            assert k in row
        assert 0 <= row["prospect"] <= 100
        assert 0 <= row["risk"] <= 100
        expected = row["prospect"] - row["risk"] * \
            scfg.ranking["risk_penalty"]
        assert abs(row["adj_score"] - round(expected, 2)) < 0.01
        assert all(0 <= v <= 1 for v in row["components"].values())

    def test_uptrend_beats_flat_on_prospect(self, cfg, scfg):
        s = {"scoring": scfg.scoring, "ranking": scfg.ranking}
        up = score_symbol(make_df("up"), "UP/IDR", 5e10, cfg.analysis, s)
        fl = score_symbol(make_df("flat"), "FL/IDR", 5e10, cfg.analysis, s)
        assert up["prospect"] > fl["prospect"]
        assert up["adj_score"] > fl["adj_score"]

    def test_low_liquidity_raises_risk(self, cfg, scfg):
        s = {"scoring": scfg.scoring, "ranking": scfg.ranking}
        df = make_df("up")
        liq = score_symbol(df, "X/IDR", 5e10, cfg.analysis, s)
        illi = score_symbol(df, "X/IDR", 6e8, cfg.analysis, s)
        assert illi["risk"] > liq["risk"]


class TestRunScanEndToEnd:
    def _fetcher(self):
        frames = _frames()
        frames["PECAH/IDR"] = {"4h": make_df("up", n=30)}  # terlalu pendek
        mk = dict(MARKETS)
        mk["PECAH/IDR"] = {"active": True, "base": "PECAH", "quote": "IDR",
                           "info": {"quote_volume": 4e9}}
        return MockFetcher(frames, markets=mk)

    def test_full_flow_persist_and_rankchange(self, cfg, scfg, tmp_path):
        cfg.raw.setdefault("state", {})["db_path"] = str(tmp_path / "mt.db")
        st = open_state(cfg)
        f = self._fetcher()
        out1 = run_scan(f, cfg, scfg, state=st, top_n=5)
        assert isinstance(out1, ScanOutcome)
        assert out1.universe_size == 3         # BTC, ETH & PECAH lolos universe
                                               # (PECAH gugur di pre-filter: data pendek)
        assert out1.prefiltered_size == 2      # hanya BTC/ETH yang punya >= 3*adx_period bar
        assert "PECAH/IDR" in out1.errors
        assert [r["rank"] for r in out1.rows] == list(range(1, len(out1.rows) + 1))
        adj = [r["adj_score"] for r in out1.rows]
        assert adj == sorted(adj, reverse=True)
        assert out1.previous_ts is None          # scan pertama
        # persisted ke SQLite
        last = st.get_last_scan()
        assert last and last["ts_utc"] == out1.ts_utc
        assert last["rows"][0]["trend_score_raw"] is not None  # D-3 raw saved

        # scan kedua -> rank change dihitung thd scan pertama
        out2 = run_scan(f, cfg, scfg, state=st, top_n=5)
        assert out2.previous_ts == out1.ts_utc
        for r in out2.rows:
            assert "prev_rank" in r and "rank_change" in r

        # bot_state ikut ter-update
        assert st.get_bot_state("last_scan")["ts_utc"] == out2.ts_utc

    def test_errors_do_not_crash_scan(self, cfg, scfg, tmp_path):
        cfg.raw.setdefault("state", {})["db_path"] = str(tmp_path / "mt.db")
        st = open_state(cfg)
        f = self._fetcher()
        f.fail_symbols = {"PECAH/IDR"}
        out = run_scan(f, cfg, scfg, state=st)
        assert "PECAH/IDR" in out.errors
        assert out.prefiltered_size == 2          # BTC/ETH tetap jalan

    def test_watchlist_only_mode(self, cfg, scfg, tmp_path):
        cfg.raw.setdefault("state", {})["db_path"] = str(tmp_path / "mt.db")
        cfg.raw["state"]["watchlist_path"] = str(tmp_path / "wl.yaml")
        wl = Watchlist(str(tmp_path / "wl.yaml"))
        wl.add("BTC/IDR")
        out = run_scan(self._fetcher(), cfg, scfg, state=open_state(cfg),
                       watchlist_only=True)
        assert out.universe_size == 1
        assert out.rows[0]["symbol"] == "BTC/IDR"

    def test_format_scan_text_smoke(self, cfg, scfg, tmp_path):
        cfg.raw.setdefault("state", {})["db_path"] = str(tmp_path / "mt.db")
        out = run_scan(self._fetcher(), cfg, scfg, state=open_state(cfg))
        txt = format_scan_text(out)
        assert "PROSPEK HARI INI" in txt
        assert "BTC/IDR" in txt and "ETH/IDR" in txt
        assert "adj" in txt.lower() or "ADJ" in txt

    def test_json_serializable_rows(self, cfg, scfg, tmp_path):
        cfg.raw.setdefault("state", {})["db_path"] = str(tmp_path / "mt.db")
        out = run_scan(self._fetcher(), cfg, scfg, state=open_state(cfg))
        blob = json.dumps([{"symbol": r["symbol"], "rank": r["rank"],
                            "prospect": r["prospect"], "risk": r["risk"],
                            "adj_score": r["adj_score"]} for r in out.rows],
                          allow_nan=False)
        assert json.loads(blob)[0]["symbol"]
