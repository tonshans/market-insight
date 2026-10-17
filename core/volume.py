# -*- coding: utf-8 -*-
"""Volume context & VSA (Volume Spread Analysis) — liquidity proxy (F1, D-2).

Alih-alih fetch order book (rate-limit & latency), kita pakai OHLCV:
- avg volume period vs last volume -> spike/dry-up.
- VSA per candle: klasifikasi bar dari kombinasi SPREAD (range h-l relatif
  ATR) dan VOLUME (relatif rata-rata):
    * No Demand        : spread naik + volume turun  (weakness utk LONG)
    * No Supply        : spread turun + volume tinggi (strength utk LONG)
    * Stopping Volume  : range besar + volume spike di akhir candle
                         (anomali likuiditas)
    * Effort vs Result : volume besar tapi body kecil -> distribusi/akumulasi
- vsa_score agregat [-1..+1]: kontribusi arah volume terhadap bias trend.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from . import indicators as ta


def volume_context(df: pd.DataFrame, params: dict[str, Any]) -> dict[str, float]:
    period = int(params.get("volume_avg_period", 20))
    vol = df["volume"].astype(float)
    avg_s = vol.rolling(period, min_periods=max(3, period // 4)).mean()
    avg = float(avg_s.iloc[-1]) if not pd.isna(avg_s.iloc[-1]) else float(vol.mean())
    last = float(vol.iloc[-1])
    ratio = last / avg if avg > 0 else 1.0
    return {
        "avg": round(avg, 4),
        "last": round(last, 4),
        "ratio": round(ratio, 3),
        "state": ("SPIKE" if ratio >= 2.0 else "HIGH" if ratio >= 1.3
                  else "LOW" if ratio <= 0.7 else "NORMAL"),
    }


def classify_bar(o: float, h: float, l: float, c: float, v: float,
                 avg_v: float, atr_val: float, prev_spread: float
                 ) -> tuple[str, float]:
    """Klasifikasi VSA satu candle. Return (label, score_signed).

    Score signed: positif = mendukung sisi BUYER (bullish strength),
    negatif = mendukung sisi SELLER. |score| ~ kekuatan sinyal (0..1).
    """
    spread = h - l
    body = abs(c - o)
    rng = max(spread, 1e-12)
    rel_spread = spread / atr_val if atr_val > 0 else 1.0
    vol_ratio = v / avg_v if avg_v > 0 else 1.0
    up = c >= o

    # effort vs result: volume besar, hasil (body) kecil
    if vol_ratio >= 1.5 and body / rng < 0.30:
        # wick dominan -> siapa yang menekan? lower wick panjang = buyer defend
        lower_wick = min(o, c) - l
        upper_wick = h - max(o, c)
        if lower_wick > 2 * upper_wick:
            return "STOPS_VOLUME_BUYERS", 0.6
        if upper_wick > 2 * lower_wick:
            return "STOPS_VOLUME_SELLERS", -0.6
        return "INDECISION", 0.0
    if vol_ratio >= 1.5 and rel_spread >= 1.2 and up:
        return "SUCCESSFUL_TEST", 0.5
    if vol_ratio >= 1.5 and rel_spread >= 1.2 and not up:
        return "SELLING_CLIMAX_LIKE", -0.5
    if vol_ratio <= 0.6 and rel_spread >= 1.0:
        # spread naik tapi partisipasi turun
        return ("NO_DEMAND", -0.4) if up else ("DOWNTHRUST_TRAP", 0.2)
    if vol_ratio <= 0.6 and rel_spread < 0.6:
        return "DRY_UP_CONSENSUS", 0.0
    if prev_spread > 0 and rel_spread < 0.7 and vol_ratio >= 1.2:
        return "NO_SUPPLY", 0.3
    return "BALANCE", 0.0


def analyze_volume(df: pd.DataFrame, params: dict[str, Any],
                   trend_direction: str = "NEUTRAL") -> dict[str, Any]:
    """VSA ringkas pada N candle terakhir + skor agregat [-1..+1]."""
    lookback = int(params.get("vsa_lookback", 6))
    ctx = volume_context(df, params)
    atr_series = ta.atr(df, params["atr_period"])
    atr_val = float(atr_series.iloc[-1])
    avg_v = ctx["avg"]

    bars = []
    scores: list[float] = []
    n = len(df)
    start = max(1, n - lookback)
    spreads = (df["high"] - df["low"]).tolist()
    for i in range(start, n):
        row = df.iloc[i]
        label, score = classify_bar(
            float(row["open"]), float(row["high"]), float(row["low"]),
            float(row["close"]), float(row["volume"]), avg_v, atr_val,
            spreads[i - 1] if i > 0 else spreads[i])
        ts = row["ts"]
        bars.append({
            "ts": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
            "label": label, "score": score,
            "vol_ratio": round(float(row["volume"]) / avg_v, 2) if avg_v > 0 else None,
        })
        # bobot penuh bila pola searah dengan bias TF, setengah bila melawan
        weights_full = score > 0 if trend_direction == "LONG" else score < 0
        scores.append(score if weights_full else score * 0.5)

    vsa_score = round(max(-1.0, min(1.0, sum(scores) / max(len(scores), 1))), 4) \
        if scores else 0.0
    return {
        "avg": ctx["avg"],
        "last": ctx["last"],
        "ratio": ctx["ratio"],
        "state": ctx["state"],
        "vsa_score": vsa_score,                       # ARCH §3.4: WAJIB diekspor
        "pattern_counts": _counts(bars),
        "recent_bars": bars[-lookback:],
    }


def _counts(bars: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for b in bars:
        out[b["label"]] = out.get(b["label"], 0) + 1
    return out
