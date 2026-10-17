# -*- coding: utf-8 -*-
"""Smoke tests WP4 — Analysis Engine (trend, sr, risk, rr, volume, price_action, warnings).

Menjalankan: python -m pytest tests/test_wp4_analysis_engine.py -q
Tanpa pytest:  python tests/test_wp4_analysis_engine.py
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from core import trend as tr
from core import sr as sr_mod
from core import risk as risk_mod
from core import rr as rr_mod
from core import volume as vol_mod
from core import price_action as pa_mod
from core import warnings as warn_mod

PARAMS = {
    "ema_fast": 20, "ema_slow": 50, "atr_period": 14, "adx_period": 14,
    "rsi_period": 14, "macd": {"fast": 12, "slow": 26, "signal": 9},
    "lr_period": 20, "swing_lookback": 20, "min_atr_distance": 0.5,
    "volume_avg_period": 20,
}


def synth(n: int = 200, mode: str = "up", seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    noise = float(rng.normal(1.0, 0.05))          # skala noise per-seed
    if mode == "up":
        base = 100 * np.exp(0.004 * t) + rng.normal(0, 0.6, n)
        step = 0.004
    elif mode == "down":
        base = 100 * np.exp(-0.004 * t) + rng.normal(0, 0.6, n)
        step = -0.004
    else:
        # sideways MURNI: tidak ada drift — sinyal indikator harus kecil.
        # Amplitudo dipilih agar ATR ~ 1.3x swing fractal (>= 0.4*ATR) sehingga
        # swing high/low terdeteksi di kedua sisi harga.
        amp = 1.8 * noise
        step = 0.0
        base = 100 + amp * np.sin(t / 5.0) + rng.normal(0, 0.15, n)
    close = base
    open_ = np.concatenate([[close[0]], close[:-1]])
    spread = np.abs(rng.normal(0.8, 0.3, n))
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    volume = np.full(n, 1000.0) + rng.normal(0, 50, n)
    df = pd.DataFrame({
        "ts": pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC"),
        "open": open_, "high": high, "low": low, "close": close,
        "volume": np.clip(volume, 10, None),
    })
    df.attrs["drift_step"] = step
    return df


def assert_consistent_with_drift(r: dict, df: pd.DataFrame) -> None:
    """Guard deterministik: arah score_raw harus sesuai arah drift data."""
    step = df.attrs.get("drift_step", None)
    if step is None:
        return
    if step > 0:
        assert r["score_raw"] > 0.1 and "UP" in r["label"], \
            f"data naik tapi hasil {r['label']} ({r['score_raw']})"
    elif step < 0:
        assert r["score_raw"] < -0.1 and "DOWN" in r["label"], \
            f"data turun tapi hasil {r['label']} ({r['score_raw']})"
    else:
        assert abs(r["score_raw"]) < 0.30, \
            f"data sideways tapi |score| {abs(r['score_raw'])} >= 0.30"


# ------------------------------------------------------------------ trend
def test_trend_uptrend_has_raw_score_and_label():
    r = tr.analyze_trend(synth(mode="up"), PARAMS)
    assert isinstance(r["score_raw"], float), "FR-3: score_raw wajib float"
    assert -1.0 <= r["score_raw"] <= 1.0
    assert r["label"] in tr.LABELS
    assert r["score_raw"] > 0.1 and "UP" in r["label"]
    assert set(r["votes"]) == {"ema", "adx", "macd", "rsi", "lr"}
    assert all(v in (-1, 0, 1) for v in r["votes"].values())
    assert r["regime"]["name"] == "BULLISH_ALIGNED"
    assert r["streak"] >= 0 and r["maturity"] in ("EARLY", "MID", "MATURE")
    assert "indicator_weights" in r["details"]  # D-3 raw logging


def test_trend_downtrend_and_sideways():
    d = tr.analyze_trend(synth(mode="down"), PARAMS)
    assert d["score_raw"] < -0.1 and "DOWN" in d["label"]
    s = tr.analyze_trend(synth(mode="flat"), PARAMS)
    assert abs(s["score_raw"]) < 0.35
    assert s["maturity"] in ("EARLY", "MID", "MATURE", "NONE")


def test_trend_deterministic():
    a = tr.analyze_trend(synth(mode="up"), PARAMS)
    b = tr.analyze_trend(synth(mode="up"), PARAMS)
    assert a["score_raw"] == b["score_raw"]  # konsistensi output 100%


def test_trend_rejects_short_data():
    try:
        tr.analyze_trend(synth(n=30), PARAMS)
        assert False, "should raise"
    except ValueError:
        pass


def test_label_thresholds():
    assert tr.label_from_score(0.8) == "STRONG_UPTREND"
    assert tr.label_from_score(0.4) == "UPTREND"
    assert tr.label_from_score(0.15) == "WEAK_UPTREND"
    assert tr.label_from_score(0.0) == "SIDEWAYS"
    assert tr.label_from_score(-0.15) == "WEAK_DOWNTREND"
    assert tr.label_from_score(-0.45) == "DOWNTREND"
    assert tr.label_from_score(-0.9) == "STRONG_DOWNTREND"


# ------------------------------------------------------------------ sr
def test_sr_zones_structure():
    out = sr_mod.detect_sr(synth(mode="up"), PARAMS)
    assert -1.0 <= 0  # sanity
    assert isinstance(out["supports"], list)
    assert isinstance(out["resistances"], list)
    price = float(synth(mode="up")["close"].iloc[-1])
    for z in out["supports"]:
        assert z["level"] < price
        assert z["zone_low"] <= z["level"] <= z["zone_high"]
    for z in out["resistances"]:
        assert z["level"] >= price
    if out["nearest_support"] and out["nearest_resistance"]:
        assert out["nearest_support"] < price <= out["nearest_resistance"]
        assert 0.0 <= out["position_in_range"] <= 1.0
    assert out["atr"] > 0


def test_sr_flat_market_finds_both_sides():
    out = sr_mod.detect_sr(synth(mode="flat"), PARAMS)
    assert len(out["supports"]) >= 1 and len(out["resistances"]) >= 1


# ------------------------------------------------------------------ risk
def test_risk_invalidation_min_half_atr():
    df = synth(mode="up")
    price = float(df["close"].iloc[-1])
    t = tr.analyze_trend(df, PARAMS)
    sr_out = sr_mod.detect_sr(df, PARAMS)
    r = risk_mod.compute_risk(df, PARAMS, t["label"], sr=sr_out)
    assert r["direction"] == "LONG"
    assert r["invalidation"] is not None
    assert r["invalidation"] < price
    dist = price - r["invalidation"]
    from core.indicators import atr as real_atr
    a = float(real_atr(df, 14).iloc[-1])
    # FR-3: stop ideal >= 0.5*ATR; kalau swing terdekat lebih rapat dari itu,
    # stop_type harus menandainya (transparan ke user), bukan diam-diam.
    if dist < 0.5 * a - 1e-9:
        assert r["stop_type"] == "NEAREST_SWING_BELOW_MIN_ATR", \
            f"dist {dist} < 0.5*ATR {a} tapi stop_type={r['stop_type']}"
    else:
        assert r["stop_type"] in ("SWING_LOW", "MIN_ATR_DISTANCE")
    assert r["risk_pct"] > 0
    assert r["risk_class"] in ("LOW", "MEDIUM", "HIGH")


def test_risk_stop_prefers_nearest_swing_beyond_min_atr():
    """Bila ada swing >= 0.5 ATR, stop HARUS swing terdekat yang memenuhi."""
    df = synth(mode="up")
    price = float(df["close"].iloc[-1])
    from core.indicators import atr as real_atr
    a = float(real_atr(df, 14).iloc[-1])
    r = risk_mod.compute_risk(df, PARAMS, "UPTREND", sr=None)
    assert r["invalidation"] <= price - 0.0000001
    assert r["risk_abs"] >= 0.0
    # kandidat swing low yang valid (>= 0.5 ATR) -> stop tidak boleh lebih
    # jauh daripada perlu: pilih TERDEKAT yang memenuhi
    sw = sorted([p for _, p in sr_mod.find_swings(df, 20)["lows"]
                 if price - p >= 0.5 * a], reverse=True)
    if sw:
        assert abs(r["invalidation"] - round(sw[0], 8)) < 1e-6 or \
            r["stop_type"] != "SWING_LOW"


def test_risk_neutral_bias():
    df = synth(mode="flat")
    t = tr.analyze_trend(df, PARAMS)
    r = risk_mod.compute_risk(df, PARAMS, t["label"])
    if t["label"] == "SIDEWAYS":
        assert r["direction"] == "NEUTRAL"
        assert r["stop_type"] == "NOT_APPLICABLE"


def test_risk_short_side():
    df = synth(mode="down")
    t = tr.analyze_trend(df, PARAMS)
    r = risk_mod.compute_risk(df, PARAMS, t["label"])
    assert r["direction"] == "SHORT"
    assert r["invalidation"] > float(df["close"].iloc[-1])


# ------------------------------------------------------------------ rr
def test_rr_auto_long():
    df = synth(mode="up")
    price = float(df["close"].iloc[-1])
    t = tr.analyze_trend(df, PARAMS)
    sr_out = sr_mod.detect_sr(df, PARAMS)
    rk = risk_mod.compute_risk(df, PARAMS, t["label"])
    rr = rr_mod.compute_rr("LONG", price, rk["invalidation"], sr_out,
                           min_rr=1.5, df=df)
    assert rr["entry"] == round(price, 8)
    assert rr["stop"] < rr["entry"] < rr["target"] or rr["rr"] is None
    if rr["rr"] is not None:
        assert rr["verdict"] in ("ACCEPTABLE", "MARGINAL", "POOR")
        assert abs(rr["rr"] - round((rr["target"] - rr["entry"])
                                    / (rr["entry"] - rr["stop"]), 2)) < 0.01


def test_rr_atr_fallback_does_not_clobber_stop():
    """Regresi bug: di jalur fallback ATR-projection, variabel stop (`s`)
    pernah ditimpa oleh nilai ATR sehingga risk/reward & verdict salah total.

    Stop harus tetap sama dengan invalidation; hanya target yang diproyeksikan.
    """
    n = 40
    close = np.linspace(100.0, 110.0, n)
    df = pd.DataFrame({
        "ts": pd.date_range("2026-01-01", periods=n, tz="UTC", freq="h"),
        "open": close, "high": close + 0.5, "low": close - 0.5,
        "close": close, "volume": [1000.0] * n,
    })
    from core import indicators as ind
    atr_last = float(ind.atr(df, 14).iloc[-1])
    sr_out = {"resistances": [], "supports": [{"level": 100.0}],
              "atr_period": 14, "fallback_atr_multiple": 2.0}
    price, invalidation = 110.0, 108.0
    rr = rr_mod.compute_rr("LONG", price, invalidation, sr_out, df=df)
    assert rr["stop"] == invalidation          # stop TIDAK boleh jadi ATR
    assert rr["target_source"] == "ATR_PROJECTION"
    assert rr["target"] == round(price + 2.0 * atr_last, 8)
    assert rr["risk_abs"] == round(price - invalidation, 8)
    assert rr["rr"] == round((price + 2.0 * atr_last - price)
                             / (price - invalidation), 2)


def test_rr_manual_and_neutral():
    sr_out = {"supports": [{"level": 90.0}], "resistances": [{"level": 120.0}]}
    rr = rr_mod.compute_rr("LONG", 100.0, None, sr_out, mode="manual",
                           stop=95.0, target=115.0)
    assert rr["rr"] == 3.0 and rr["verdict"] == "ACCEPTABLE"
    rr_n = rr_mod.compute_rr("NEUTRAL", 100.0, None, sr_out)
    assert rr_n["verdict"] == "NOT_APPLICABLE" and rr_n["rr"] is None
    try:
        rr_mod.compute_rr("LONG", 100.0, None, sr_out, mode="manual", stop=None)
        assert False
    except ValueError:
        pass


# ------------------------------------------------------------------ volume
def test_volume_context_and_vsa_score():
    df = synth(mode="up")
    out = vol_mod.analyze_volume(df, PARAMS, trend_direction="LONG")
    assert out["avg"] > 0 and out["last"] > 0
    assert out["state"] in ("SPIKE", "HIGH", "NORMAL", "LOW")
    assert -1.0 <= out["vsa_score"] <= 1.0          # ARCH §3.4
    assert isinstance(out["pattern_counts"], dict)
    assert 1 <= len(out["recent_bars"]) <= 6


def test_vsa_spike_detection():
    df = synth(mode="up")
    df.loc[df.index[-1], "volume"] = df["volume"].mean() * 3
    out = vol_mod.analyze_volume(df, PARAMS, "LONG")
    assert out["state"] == "SPIKE"


# ------------------------------------------------------------------ price action
def test_price_action_wick_rejection():
    df = synth(mode="up")
    out = pa_mod.analyze_price_action(df, PARAMS, "LONG")
    rp = out["range_position"]
    assert 0.0 <= rp["position"] <= 1.0
    assert rp["zone"] in ("TOP", "HIGH_MID", "MID", "LOW_MID", "BOTTOM")
    assert -1.0 <= out["wick_rejection_score"] <= 1.0   # ARCH §3.4
    assert "interpretation" in out


def test_wick_buyer_rejection_detected():
    df = synth(mode="flat", n=100)
    i = df.index[-2]
    c = float(df.loc[i, "close"])
    # pahat satu candle: lower wick panjang, body kecil, volume tinggi
    df.loc[i, ["open", "high", "low", "close"]] = [c, c + 1, c - 10, c + 0.5]
    df.loc[i, "volume"] = df["volume"].mean() * 1.5
    out = pa_mod.analyze_price_action(df, PARAMS, "LONG")
    assert any(b["side"] == "BUYER_REJECTION" for b in out["rejection_bars"])
    assert out["wick_rejection_score"] > 0


# ------------------------------------------------------------------ warnings
def _full_tf(tf_result):
    return tf_result


def test_warnings_sorted_by_severity_and_codes():
    df = synth(mode="up")
    t = tr.analyze_trend(df, PARAMS)
    rk = risk_mod.compute_risk(df, PARAMS, t["label"])
    v = vol_mod.analyze_volume(df, PARAMS, rk["direction"])
    p = pa_mod.analyze_price_action(df, PARAMS, rk["direction"])
    tf_results = {"1h": {"trend": t, "risk": rk, "volume": v, "price_action": p}}
    warns = warn_mod.evaluate(tf_results, summary_bias=t["label"], params=PARAMS)
    order = [warn_mod.SEVERITY_ORDER[w["severity"]] for w in warns]
    assert order == sorted(order, reverse=True)
    for w in warns:
        assert {"code", "severity", "message"} <= set(w)
    assert warn_mod.highest_severity(warns) in ("NONE", "INFO", "WARN", "HIGH")


def test_warnings_exhaustion_and_conflict():
    fake = {"1d": {
        "trend": {"label": "STRONG_UPTREND", "score_raw": 0.7, "maturity": "MATURE",
                  "streak": 12, "votes": {"ema": 1, "adx": 1, "macd": 1, "rsi": 1, "lr": 1},
                  "details": {"rsi": 80}},
        "risk": {"direction": "LONG", "risk_class": "LOW", "risk_pct": 1.0},
        "volume": {"ratio": 0.5, "state": "LOW"},
        "price_action": {"wick_rejection_score": -0.5,
                         "range_position": {"zone": "TOP"}},
    }}
    warns = warn_mod.evaluate(fake, summary_bias="SIDEWAYS", params=PARAMS)
    codes = {w["code"] for w in warns}
    assert "TREND_EXHAUSTION" in codes      # MATURE + volume dry-up => HIGH
    assert "RSI_OVERBOUGHT" in codes
    assert "THIN_LIQUIDITY" in codes
    assert "REJECTION_AGAINST_BIAS" in codes
    exhaust = next(w for w in warns if w["code"] == "TREND_EXHAUSTION")
    assert exhaust["severity"] == "HIGH"


def test_warnings_rr_bad():
    rr_summary = {"rr": 0.8, "min_rr": 1.5}
    warns = warn_mod.evaluate({}, summary_bias=None, rr_summary=rr_summary)
    assert any(w["code"] == "BAD_RR" and w["severity"] == "HIGH" for w in warns)


ALL_TESTS = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and isinstance(v, types.FunctionType)]

if __name__ == "__main__":
    failed = 0
    for fn in ALL_TESTS:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(ALL_TESTS) - failed}/{len(ALL_TESTS)} passed")
    sys.exit(1 if failed else 0)
