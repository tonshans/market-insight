# -*- coding: utf-8 -*-
"""Smoke & unit tests WP5 — Orchestration (analysis) + Export (json/text).

Menjalankan pipeline lengkap dengan mock fetcher (deterministik, tanpa
jaringan), lalu memverifikasi:
- struktur data model §4.1 (termasuk raw scores wajib FR-3/D-3)
- confluence bias aggregation & conflict detection (F2)
- JSON export: valid, json.dump-able, ada trend_score_raw/vsa/wick
- Text export Level 1/2 + auto-chunking Telegram (FR-5)
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pandas as pd
import pytest

from core.analysis import (aggregate_bias, analyze_symbol, detect_conflicts)
from core.config import load_config
from core.fetch import FetchResult, SOURCE_CACHE, SOURCE_NETWORK
from core.json_export import build_json, dumps
from core.text_export import chunk_message, render


# --------------------------------------------------------------------------
# synthetic OHLCV generator (deterministic)
# --------------------------------------------------------------------------
def make_df(n=120, mode="up", seed=7, base=100.0):
    rng = np.random.default_rng(seed)
    drift = {"up": 0.004, "down": -0.004, "flat": 0.0}[mode]
    noise = rng.normal(0, 0.004 if mode == "flat" else 0.002, n)
    close = base * np.exp(np.cumsum(drift + noise))
    high = close * (1 + np.abs(rng.normal(0, 0.003, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.003, n)))
    open_ = np.roll(close, 1); open_[0] = close[0]
    vol = rng.uniform(800, 1600, n)
    if mode == "up":                      # volume naik mengikuti tren
        vol = vol * np.linspace(0.8, 1.6, n)
    ts = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame({"ts": ts, "open": open_, "high": high,
                         "low": low, "close": close, "volume": vol})


class MockFetcher:
    def __init__(self, frames: dict[str, pd.DataFrame]):
        self.frames = frames
        self.calls = []

    def fetch_ohlcv(self, symbol, timeframe, limit=None,
                    allow_stale_fallback=True):
        self.calls.append((symbol, timeframe))
        df = self.frames[timeframe]
        src = SOURCE_NETWORK if len(self.calls) <= 3 else SOURCE_CACHE
        return FetchResult(symbol, timeframe, df.copy(), src)


@pytest.fixture()
def config():
    return load_config(os.path.join(os.path.dirname(__file__), "..",
                                    "config.yaml"))


@pytest.fixture()
def up_result(config):
    frames = {"1h": make_df(mode="up", seed=1),
              "4h": make_df(mode="up", seed=2),
              "1d": make_df(mode="up", seed=3)}
    fetcher = MockFetcher(frames)
    return analyze_symbol("TEST/IDR", ["1h", "4h", "1d"], fetcher, config)


# ---------------------------------------------------------------- analysis
class TestAnalysis:
    def test_schema_shape(self, up_result):
        assert up_result["schema_version"] == "1.0.0"
        for key in ("summary", "timeframes", "meta"):
            assert key in up_result
        s = up_result["summary"]
        for key in ("symbol", "bias", "votes", "rr", "warnings"):
            assert key in s

    def test_per_tf_required_fields(self, up_result):
        for tf, r in up_result["timeframes"].items():
            assert isinstance(r["trend"]["score_raw"], float)
            assert -1.0 <= r["trend"]["score_raw"] <= 1.0
            assert r["trend"]["label"] in (
                "STRONG_UPTREND", "UPTREND", "WEAK_UPTREND", "SIDEWAYS",
                "WEAK_DOWNTREND", "DOWNTREND", "STRONG_DOWNTREND")
            assert "vsa_score" in r["volume"]
            assert "wick_rejection_score" in r["price_action"]
            assert "invalidation" in r["risk"] or r["risk"]["direction"] == "NEUTRAL"
            assert "supports" in r["sr"] and "resistances" in r["sr"]

    def test_uptrend_bias_unanimous(self, up_result):
        s = up_result["summary"]
        assert s["bias"] in ("UPTREND",)
        assert s["agreement"] in ("UNANIMOUS", "MAJORITY")

    def test_downtrend_bias(self, config):
        frames = {tf: make_df(mode="down", seed=i)
                  for i, tf in enumerate(["1h", "4h", "1d"])}
        res = analyze_symbol("TEST/IDR", ["1h", "4h", "1d"],
                             MockFetcher(frames), config)
        assert res["summary"]["bias"] in ("DOWNTREND", "SIDEWAYS")
        assert all(v < 0.1 for v in res["summary"]["trend_scores_raw"].values())

    def test_fetch_meta_recorded(self, up_result):
        meta = up_result["meta"]["fetch"]
        assert set(meta) == {"1h", "4h", "1d"}
        assert meta["1h"]["bars"] > 0

    def test_rr_computed_on_ref_tf(self, up_result):
        rr = up_result["summary"]["rr"]
        assert up_result["summary"]["ref_timeframe"] == "1d"
        if rr["verdict"] != "NOT_APPLICABLE":
            assert rr["entry"] and rr["stop"] and rr["target"]
            assert rr["rr"] is None or rr["rr"] > 0

    def test_manual_rr_mode(self, config):
        frames = {"1d": make_df(mode="up", seed=9)}
        f = MockFetcher({"1h": frames["1d"], "4h": frames["1d"], **frames})
        res = analyze_symbol("TEST/IDR", ["1h", "4h", "1d"], f, config,
                             rr_mode="manual", stop=90.0, target=130.0)
        rr = res["summary"]["rr"]
        assert rr["mode"] == "manual"
        assert rr["stop"] == 90.0 and rr["target"] == 130.0
        assert rr["rr"] is not None

    def test_invalidation_min_atr(self, up_result):
        """FR-3: invalidation LONG harus di bawah harga."""
        for tf, r in up_result["timeframes"].items():
            risk = r["risk"]
            if risk["direction"] == "LONG":
                assert risk["invalidation"] < r["last_close"]
            elif risk["direction"] == "SHORT":
                assert risk["invalidation"] > r["last_close"]


# ------------------------------------------------------------- confluence F2
class TestConfluence:
    def test_aggregate_all_up(self):
        tfs = {tf: {"score_raw": 0.6} for tf in ("1h", "4h", "1d")}
        bias, agree = aggregate_bias(tfs)
        assert bias == "UPTREND" and agree == "UNANIMOUS"

    def test_aggregate_all_down(self):
        tfs = {tf: {"score_raw": -0.6} for tf in ("1h", "4h", "1d")}
        bias, agree = aggregate_bias(tfs)
        assert bias == "DOWNTREND" and agree == "UNANIMOUS"

    def test_aggregate_conflicted(self):
        tfs = {"1h": {"score_raw": 0.8}, "4h": {"score_raw": -0.8},
               "1d": {"score_raw": 0.0}}
        bias, _ = aggregate_bias(tfs)
        assert bias == "CONFLICTED"

    def test_aggregate_sideways(self):
        tfs = {"1h": {"score_raw": 0.02}, "4h": {"score_raw": -0.01},
               "1d": {"score_raw": 0.03}}
        bias, _ = aggregate_bias(tfs)
        assert bias == "SIDEWAYS"

    def test_detect_conflicts_pairs(self):
        tfs = {"1h": {"risk": {"direction": "LONG"}},
               "1d": {"risk": {"direction": "SHORT"}}}
        c = detect_conflicts(tfs)
        assert len(c) == 1 and set(c[0]["timeframes"]) == {"1h", "1d"}

    def test_no_conflict_same_dir(self):
        tfs = {"1h": {"risk": {"direction": "LONG"}},
               "1d": {"risk": {"direction": "LONG"}}}
        assert detect_conflicts(tfs) == []


# ------------------------------------------------------------------ json F
class TestJsonExport:
    def test_build_contains_raw_scores(self, up_result):
        payload = build_json(up_result)
        assert payload["schema_version"] == "1.0.0"
        assert "generated_at_utc" in payload
        # summary-level alias (siap utk Fase 2 / D-3)
        for tf, v in payload["summary"]["trend_scores_raw"].items():
            assert isinstance(v, float) and -1.0 <= v <= 1.0
        for tf, r in payload["timeframes"].items():
            assert r["trend"]["score_raw"] is not None      # WAJIB FR-3
            assert r["volume"]["vsa_score"] is not None     # WAJIB §3.4
            assert r["price_action"]["wick_rejection_score"] is not None
            assert "invalidation" in r and "level" in r["invalidation"]

    def test_dumps_is_strict_valid_json(self, up_result):
        text = dumps(up_result)
        parsed = json.loads(text)          # allow_nan=False => strict
        assert parsed["summary"]["symbol"] == "TEST/IDR"
        assert "raw_score_contract" in parsed["summary"]

    def test_no_bare_nan_inf(self, up_result):
        text = dumps(up_result)
        assert "NaN" not in text and "Infinity" not in text

    def test_compact_mode_drops_recent_bars(self, up_result):
        payload = build_json(up_result, include_recent_bars=False)
        for r in payload["timeframes"].values():
            assert r["volume"].get("recent_bars") is None
            assert r["price_action"].get("rejection_bars") is None


# ------------------------------------------------------------------ text F
class TestTextExport:
    def test_level1_short_and_readable(self, up_result):
        out = render(up_result, level=1, ansi=False, emoji=False)
        assert "TEST/IDR" in out
        assert "Bias:" in out and "raw=" in out
        assert len(out) < 2000            # target baca < 10 detik

    def test_level2_has_more_detail(self, up_result):
        l1 = render(up_result, level=1, ansi=False, emoji=False)
        l2 = render(up_result, level=2, ansi=False, emoji=False)
        assert len(l2) > len(l1)
        assert "invalidation=" in l2
        assert "votes=" in l2

    def test_ansi_flag_controls_codes(self, up_result):
        plain = render(up_result, level=1, ansi=False, emoji=False)
        color = render(up_result, level=1, ansi=True, emoji=False)
        assert "\033[" not in plain
        assert "\033[" in color

    def test_emoji_toggle(self, up_result):
        with_em = render(up_result, level=1, ansi=False, emoji=True)
        without = render(up_result, level=1, ansi=False, emoji=False)
        assert "📈" in with_em or "➖" in with_em or "⚠️" in with_em
        assert "📈" not in without

    def test_chunk_small_single(self):
        assert chunk_message("halo") == ["halo"]

    def test_chunk_respects_limit_line_boundary(self):
        text = "\n".join(["x" * 100] * 50)      # ~5049 chars
        chunks = chunk_message(text, limit=1024)
        assert all(len(c) <= 1024 for c in chunks)
        assert sum(len(c) + 1 for c in chunks) - 1 == len(text)  # tidak hilang
        # pecah di batas baris -> tiap chunk kelipatan 100+newline
        assert chunks[0].endswith("xxx")

    def test_chunk_hard_split_long_line(self):
        text = "y" * 3000
        chunks = chunk_message(text, limit=1024)
        assert all(len(c) <= 1024 for c in chunks)
        assert "".join(chunks) == text


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
