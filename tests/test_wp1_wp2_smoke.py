# -*- coding: utf-8 -*-
"""Smoke test WP1 (Foundation) + WP2 (Core Utils)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import pytest

from core import indicators as ind
from core import time_utils as tu
from core import yaml_io
from core.config import ConfigError, load_config, load_scanner_config


def _sample_df(n=120, trend=0.5):
    rng = np.random.default_rng(42)
    close = 100 + np.cumsum(rng.normal(trend, 1.0, n))
    high = close + np.abs(rng.normal(0.5, 0.3, n))
    low = close - np.abs(rng.normal(0.5, 0.3, n))
    open_ = close + rng.normal(0, 0.3, n)
    vol = np.abs(rng.normal(1000, 200, n))
    ts = (np.arange(n) * 3_600_000 + 1_700_000_000_000).astype("int64")
    df = pd.DataFrame({"ts": ts, "open": open_, "high": high, "low": low, "close": close, "volume": vol})
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df


# ---------- WP1: config ----------
def test_load_config_and_defaults():
    cfg = load_config("config.yaml")
    assert cfg.exchange_id == "indodax"
    assert cfg.default_timeframes == ["1h", "4h", "1d"]
    assert cfg.cache_ttl_seconds("1h") == 30 * 60
    assert cfg.circuit_breaker["failure_threshold"] == 3
    assert cfg.circuit_breaker["cooldown_seconds"] == 300
    a = cfg.analysis
    assert a["ema_fast"] == 20 and a["ema_slow"] == 50 and a["min_atr_distance"] == 0.5


def test_cli_override_deep_merge():
    cfg = load_config("config.yaml", overrides={"exchange": {"id": "binance"}, "analysis": {"ema_fast": 10}})
    assert cfg.exchange_id == "binance"
    assert cfg.analysis["ema_fast"] == 10
    assert cfg.analysis["ema_slow"] == 50  # tidak ikut ter-override


def test_missing_config_raises():
    with pytest.raises(ConfigError):
        load_config("does_not_exist.yaml")


def test_scanner_config():
    sc = load_scanner_config("scanner.yaml")
    assert sc.universe["quote_currency"] == "IDR"
    assert sc.prefilter["min_adx"] == 15
    assert sc.ranking["top_n"] == 15
    assert sc.scoring["weights"]["trend_strength"] == 0.35


def test_bot_yaml_gitignored():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, ".gitignore")) as fh:
        assert "bot.yaml" in fh.read()


# ---------- WP2: time_utils ----------
def test_validate_timeframe():
    assert tu.validate_timeframe("1H") == "1h"
    with pytest.raises(tu.TimeframeError):
        tu.validate_timeframe("7m")


def test_timeframe_to_ms():
    assert tu.timeframe_to_ms("1h") == 3_600_000
    assert tu.timeframe_to_ms("1d") == 86_400_000
    assert tu.timeframe_to_ms("1w") == 604_800_000


def test_wita_conversion():
    dt = pd.Timestamp("2025-01-15 10:00", tz="UTC").to_pydatetime()
    assert tu.human_wita(dt) == "15-01 18:00 WITA"  # UTC+8
    assert tu.iso_utc(dt) == "2025-01-15T10:00:00Z"


def test_candle_is_closed():
    now = pd.Timestamp("2025-01-15 10:30", tz="UTC").to_pydatetime()
    closed = [1_736_932_800_000, 1, 2, 0.5, 1.5, 100]   # open 09:00 UTC, 1h -> closed 10:00
    live = [1_736_936_400_000, 1, 2, 0.5, 1.5, 100]     # open 10:00 UTC -> belum close
    assert tu.candle_is_closed(closed, "1h", now) is True
    assert tu.candle_is_closed(live, "1h", now) is False


# ---------- WP2: indicators ----------
def test_ema_sma_rsi():
    df = _sample_df()
    e = ind.ema(df["close"], 20)
    s = ind.sma(df["close"], 20)
    r = ind.rsi(df["close"], 14)
    assert len(e) == len(df) and not np.isnan(e.iloc[-1])
    assert not np.isnan(s.iloc[-1])
    last_r = r.dropna().iloc[-1]
    assert 0 <= last_r <= 100


def test_rsi_flat_prices_is_neutral_not_100():
    """Regresi: harga flat (sideways) harus RSI 50 (netral), bukan 100.

    Nilai 100 pada data sideways memicu vote bullish palsu di engine.
    """
    flat = pd.Series([100.0] * 30)
    r = ind.rsi(flat, 14)
    assert not np.isnan(r.iloc[-1])
    assert r.iloc[-1] == pytest.approx(50.0)

    # Uptrend murni (tanpa loss, tapi ada gain) tetap 100.
    up = pd.Series(np.arange(1.0, 40.0))
    assert ind.rsi(up, 14).iloc[-1] == pytest.approx(100.0)


def test_macd_atr_adx():
    df = _sample_df()
    m = ind.macd(df["close"])
    assert list(m.columns) == ["macd", "signal", "hist"]
    a = ind.atr(df, 14)
    assert a.iloc[-1] > 0
    x = ind.adx(df, 14)
    assert 0 <= x["adx"].dropna().iloc[-1] <= 100


def test_lr_slope_sign():
    up = pd.Series(np.linspace(100, 200, 50))
    down = pd.Series(np.linspace(200, 100, 50))
    assert ind.linear_regression_slope(up, 20).iloc[-1] > 0
    assert ind.linear_regression_slope(down, 20).iloc[-1] < 0


def test_bollinger_pctb_bounds():
    df = _sample_df()
    bb = ind.bollinger_bands(df["close"])
    assert (bb["lower"].dropna() <= bb["mid"].dropna()).all()
    assert (bb["mid"].dropna() <= bb["upper"].dropna()).all()


def test_ohlcv_to_dataframe_roundtrip():
    raw = [[1_700_000_000_000 + i * 3_600_000, 1.0, 2.0, 0.5, 1.5, 100.0] for i in range(5)]
    df = ind.ohlcv_to_dataframe(raw)
    assert list(df.columns) == ["ts", "open", "high", "low", "close", "volume"]
    assert str(df["ts"].iloc[0]).startswith("2023-11-14")


def test_indicators_reject_bad_input():
    bad = pd.DataFrame({"close": [1, 2, 3]})
    with pytest.raises(ValueError):
        ind.atr(bad)


# ---------- WP2: yaml_io atomic write ----------
def test_atomic_write_yaml(tmp_path):
    p = tmp_path / "sub" / "watchlist.yaml"
    yaml_io.atomic_write_yaml(str(p), {"symbols": ["BTC/IDR", "ETH/IDR"]})
    data = yaml_io.load_yaml(str(p))
    assert data["symbols"] == ["BTC/IDR", "ETH/IDR"]
    assert not any(f.startswith(".tmp_") for f in os.listdir(p.parent))


def test_load_yaml_default_when_missing(tmp_path):
    assert yaml_io.load_yaml(str(tmp_path / "nope.yaml"), default={"a": 1}) == {"a": 1}
