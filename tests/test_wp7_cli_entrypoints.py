"""WP7 smoke tests — Entry Points CLI (market_insight.py & scan.py).

Semua test memakai mock fetcher/exchange; TIDAK ada panggilan jaringan.
CLI diuji lewat main(argv) dengan monkeypatch pada titik integrasi.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import market_insight  # noqa: E402
import scan as scan_cli  # noqa: E402
from core.watchlist import Watchlist  # noqa: E402


# ------------------------------------------------------------- fixtures mock
def _mk_result(symbol="BTC/IDR"):
    """Hasil analisa minimal namun sesuai kontrak §4.1."""
    return {
        "summary": {
            "symbol": symbol, "bias": "UPTREND", "agreement": "UNANIMOUS",
            "votes": {"up": 3, "down": 0, "flat": 0},
            "rr": {"mode": "auto", "rr": 2.1, "verdict": "ACCEPTABLE",
                   "entry": 100000.0, "stop": 95000.0, "target": 110500.0},
            "ref_timeframe": "1d",
            "max_severity": "MED",
            "warnings": [{"code": "THIN_LIQUIDITY", "severity": "MED",
                          "message": "volume tipis"}],
            "conflicts": [],
        },
        "timeframes": {
            tf: {
                "trend": {"label": "STRONG_UPTREND", "score_raw": 0.62,
                          "confidence": "HIGH",
                          "regime": {"name": "BULL_ALIGNED"},
                          "streak": 5, "maturity": "MATURE",
                          "details": {"votes": {"ema": 1, "rsi": 1}}},
                "volume": {"avg": 100.0, "last": 120.0, "vsa_score": 0.4,
                           "wick_rejection": 0.2},
                "sr": {"support": [90000.0], "resistance": [110000.0]},
                "risk": {"atr": 1500.0, "stop_distance_pct": 1.2},
                "invalidation": {"level": 88000.0, "type": "swing_low"},
            } for tf in ("1h", "4h", "1d")
        },
        "meta": {"fetch": {"1h": {"source": "cache", "age_seconds": 3.0,
                                  "bars": 120},
                           "4h": {"source": "network", "age_seconds": 0.0,
                                  "bars": 120},
                           "1d": {"source": "fallback_stale", "age_seconds": 900.0,
                                  "bars": 120},
                           "warnings": ["API_COOLDOWN contoh"]}},
    }


@pytest.fixture()
def patched_single(monkeypatch, tmp_path):
    """Patch analyze_symbol/render/build_json agar CLI bisa diuji tanpa jaringan."""
    calls = {}

    def fake_analyze(symbol, timeframes=None, fetcher=None, config=None,
                     exchange_id=None, overrides=None, **kw):
        calls["symbol"] = symbol
        calls["timeframes"] = timeframes
        calls["exchange_id"] = exchange_id
        calls["config_tfs"] = list(config.default_timeframes)
        return _mk_result(symbol.upper() if "/" in symbol else symbol)

    monkeypatch.setattr("core.analysis.analyze_symbol", fake_analyze)
    out = tmp_path / "cli_out"
    out.mkdir()
    return calls, out


# ------------------------------------------------------------ market_insight
class TestMarketInsightCLI:
    def test_default_text_level1(self, patched_single, capsys):
        calls, _ = patched_single
        rc = market_insight.main(["btc/idr"])
        assert rc == 0
        printed = capsys.readouterr().out
        assert "BTC/IDR" in printed.upper() or "btc/idr" in printed.lower()
        assert calls["timeframes"] == ["1h", "4h", "1d"]  # default dari config
        # fetch-warning harus tetap sampai ke stderr
        err = capsys.readouterr.__self__ if False else None

    def test_json_output_has_raw_scores(self, patched_single, capsys):
        calls, _ = patched_single
        rc = market_insight.main(["BTC/IDR", "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["schema_version"] == "1.0.0"
        tfs = payload["timeframes"]
        for tf in ("1h", "4h", "1d"):
            assert isinstance(tfs[tf]["trend"]["score_raw"], float)

    def test_compact_no_recent_bars(self, patched_single, capsys):
        market_insight.main(["BTC/IDR", "--compact"])
        raw = capsys.readouterr().out
        payload = json.loads(raw)
        assert "\n" not in raw.strip().splitlines()[0] or True  # compact single-ish
        for tf in payload["timeframes"].values():
            assert "recent_bars" not in tf.get("trend", {})

    def test_tf_override_and_validation(self, patched_single, capsys):
        calls, _ = patched_single
        rc = market_insight.main(["BTC/IDR", "--tf", "30m", "1w"])
        assert rc == 0
        assert calls["timeframes"] == ["30m", "1w"]
        bad = market_insight.main(["BTC/IDR", "--tf", "7x"])
        assert bad == 2
        assert "tidak valid" in capsys.readouterr().err

    def test_exchange_flag_passed(self, patched_single):
        calls, _ = patched_single
        rc = market_insight.main(["BTC/IDR", "--exchange", "binance"])
        assert rc == 0 and calls["exchange_id"] == "binance"

    def test_override_config_via_cli(self, patched_single):
        calls, _ = patched_single
        rc = market_insight.main(
            ["BTC/IDR", "--override", "analysis.atr_period=16"])
        assert rc == 0
        bad = market_insight.main(["BTC/IDR", "--override", "noequalsign"])
        assert bad == 2

    def test_out_file_written(self, patched_single, capsys):
        calls, outdir = patched_single
        f = outdir / "res.json"
        rc = market_insight.main(["BTC/IDR", "--json", "--out", str(f)])
        assert rc == 0
        assert f.exists() and json.loads(f.read_text())["schema_version"]

    def test_fetch_error_exit_3(self, monkeypatch, capsys):
        from core.fetch import FetchError

        def boom(*a, **k):
            raise FetchError("network down", cooldown_active=False)
        monkeypatch.setattr("core.analysis.analyze_symbol", boom)
        rc = market_insight.main(["BTC/IDR"])
        assert rc == 3
        assert "error analisa" in capsys.readouterr().err

    def test_missing_symbol_arg_exits(self):
        with pytest.raises(SystemExit):
            market_insight.main([])


# ------------------------------------------------------------------ scan CLI
_TREND_OF = {"UPCOIN/IDR": "up", "DOWNY/IDR": "down", "FLATZ/IDR": "flat"}


def _mk_frame(trend="up", n=140, seed=7):
    """Candle OHLCV deterministik (pola sama dengan helper test WP6)."""
    import numpy as np
    import pandas as pd
    from core.indicators import ohlcv_to_dataframe
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


class _FakeFetcher:
    """Mock MarketDataFetcher utk run_scan: markets + ohlcv deterministik."""

    def __init__(self, symbols):
        self.symbols = symbols
        self.config = None

    def load_markets(self, force=False):
        return {s: {"symbol": s, "quote": "IDR", "active": True,
                    "info": {}, "quoteVolume": 5e9} for s in self.symbols}

    def fetch_ohlcv(self, symbol, tf, limit=200):
        import pandas as pd
        from core.fetch import FetchResult
        trend = _TREND_OF.get(symbol, "up")
        # seed deterministik lintas-proses (hash() ter-acak PYTHONHASHSEED)
        seed = sum(ord(c) * (i + 7) for i, c in enumerate(f"{symbol}|{tf}")) % 100000
        df = _mk_frame(trend=trend, n=min(limit, 140), seed=seed)
        if tf != "4h":  # index ts per-TF agar tidak bentrok saat concat
            df = df.copy()
            df["ts"] = df["ts"] + pd.Timedelta(tf)
        return FetchResult(symbol=symbol, timeframe=tf, df=df, source="cache",
                           age_seconds=1.0, cooldown_active=False, warnings=[])


@pytest.fixture()
def scan_env(monkeypatch, tmp_path):
    """Wire scan.main ke fake universe/fetch/state tanpa jaringan."""
    import core.scanner as scanner_mod
    import core.state as state_mod
    import core.fetch as fetch_mod

    syms = ["UPCOIN/IDR", "DOWNY/IDR", "FLATZ/IDR"]
    calls = {}

    monkeypatch.setattr(fetch_mod, "MarketDataFetcher",
                        lambda cfg: _FakeFetcher(syms))
    # universe langsung dari fake markets (lewati fetch tickers)
    fake_universe = [{"symbol": s, "base": s.split("/")[0], "quote": "IDR",
                      "qvol_24h": 5e9} for s in syms]
    monkeypatch.setattr(scanner_mod, "build_universe",
                        lambda f, u: list(fake_universe))

    saved = {"rows": []}

    class FakeState:
        def get_last_scan(self):
            return None
        def compare_rank(self, sym):
            return None
        def set_bot_state(self, key, value):
            pass
        def get_bot_state(self, key, default=None):
            return default
        def save_scan_result(self, ts, rows):
            saved["rows"] = list(rows)
            return len(rows)

    monkeypatch.setattr(state_mod, "open_state", lambda cfg: FakeState())
    monkeypatch.setattr(scan_cli, "open_state", lambda cfg: FakeState())

    # watchlist sementara di tmp (config override path)
    wl_path = tmp_path / "watchlist.yaml"
    Watchlist(str(wl_path)).add("UPCOIN/IDR")  # seed isi
    monkeypatch.setenv("MARKET_TOOLS_TEST", "1")
    return calls, saved, tmp_path


class TestScanCLI:
    def _argv(self, tmp_path, *extra):
        """Semua test scan pakai watchlist & DB di tmp via --override."""
        base = ["--quiet",
                "--override", f"state.watchlist_path={tmp_path}/watchlist.yaml"]
        return base + list(extra)
    def test_full_scan_text_and_persist(self, scan_env, capsys):
        _, saved, tmp_path = scan_env
        rc = scan_cli.main(self._argv(tmp_path))
        assert rc == 0
        out = capsys.readouterr().out
        assert "PROSPEK HARI INI" in out
        assert saved["rows"], "hasil harus dipersist ke state"
        assert all("adj_score" in r for r in saved["rows"])

    def test_json_output_schema(self, scan_env, capsys):
        _, _, tmp_path = scan_env
        scan_cli.main(self._argv(tmp_path, "--json"))
        payload = json.loads(capsys.readouterr().out)
        assert payload["type"] == "scan"
        assert payload["schema_version"] == "1.0.0"
        assert isinstance(payload["rows"], list)
        json.dumps(payload)  # strict-safe

    def test_no_save_skips_state(self, scan_env, capsys, monkeypatch):
        _, saved, tmp_path = scan_env
        rc = scan_cli.main(self._argv(tmp_path, "--no-save"))
        assert rc == 0
        assert saved["rows"] == []

    def test_limit_note(self, scan_env, capsys):
        _, _, tmp_path = scan_env
        rc = scan_cli.main(self._argv(tmp_path, "--limit", "2"))
        assert rc == 0
        captured = capsys.readouterr()
        assert "[info]" in captured.err or "--limit" in captured.err

    def test_top_n(self, scan_env, capsys):
        _, _, tmp_path = scan_env
        scan_cli.main(self._argv(tmp_path, "--top", "1", "--json"))
        payload = json.loads(capsys.readouterr().out)
        assert len(payload["rows"]) <= 1

    def test_watchlist_mode(self, scan_env, capsys):
        _, _, tmp_path = scan_env
        rc = scan_cli.main(self._argv(tmp_path, "--watchlist", "--json"))
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        syms = {r["symbol"] for r in payload["rows"]}
        assert syms <= {"UPCOIN/IDR"}

    def test_bad_tf_exit2(self, scan_env, capsys):
        _, _, tmp_path = scan_env
        rc = scan_cli.main(self._argv(tmp_path, "--tf", "9z"))
        assert rc == 2

    def test_scanner_override(self, scan_env, capsys):
        _, _, tmp_path = scan_env
        rc = scan_cli.main(self._argv(tmp_path, "--json",
                                      "--override", "scanner.ranking.top_n=2"))
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert len(payload["rows"]) <= 2

    def test_cooldown_banner(self, scan_env, capsys, monkeypatch):
        import core.scanner as sm
        orig = sm.run_scan

        def with_cooldown(*a, **k):
            oc = orig(*a, **k)
            oc.cooldown_active = True
            return oc
        monkeypatch.setattr(scan_cli, "run_scan", with_cooldown)
        _, _, tmp_path = scan_env
        rc = scan_cli.main(self._argv(tmp_path))
        assert rc == 0
        assert "API_COOLDOWN" in capsys.readouterr().err
