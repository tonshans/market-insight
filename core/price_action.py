# -*- coding: utf-8 -*-
"""Price action context: posisi dalam range & Wick Rejection (F1, D-2).

Wick Rejection = proxy anomali likuiditas tanpa order book:
- Rasio upper/lower wick terhadap body & range candle.
- Candle dengan wick penolakan dominan + volume >= rata-rata dianggap
  "rejection bar" -> siapa yang menolak (buyer di bawah / seller di atas).
- wick_rejection_score [-1..+1] agregat lookback pendek (ARCH §3.4).
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from . import indicators as ta


def range_position(df: pd.DataFrame, lookback: int = 50) -> dict[str, Any]:
    """Posisi harga dalam high-low range N bar terakhir (0=bottom,1=top)."""
    n = min(lookback, len(df))
    window = df.iloc[-n:]
    hi, lo = float(window["high"].max()), float(window["low"].min())
    c = float(df["close"].iloc[-1])
    pos = (c - lo) / (hi - lo) if hi > lo else 0.5
    zone = ("TOP" if pos >= 0.8 else "HIGH_MID" if pos >= 0.6
            else "MID" if pos >= 0.4 else "LOW_MID" if pos >= 0.2 else "BOTTOM")
    return {
        "lookback": n,
        "range_high": round(hi, 8),
        "range_low": round(lo, 8),
        "position": round(pos, 3),
        "zone": zone,
    }


def wick_metrics(row: pd.Series) -> dict[str, float]:
    o, h, l, c = (float(row["open"]), float(row["high"]),
                  float(row["low"]), float(row["close"]))
    rng = max(h - l, 1e-12)
    body = abs(c - o)
    upper = h - max(o, c)
    lower = min(o, c) - l
    return {"range": rng, "body": body, "upper_wick": upper, "lower_wick": lower,
            "upper_ratio": upper / rng, "lower_ratio": lower / rng,
            "body_ratio": body / rng}


def analyze_price_action(df: pd.DataFrame, params: dict[str, Any],
                         trend_direction: str = "NEUTRAL",
                         lookback: int = 10) -> dict[str, Any]:
    """Range position + deteksi rejection bars + wick_rejection_score."""
    rp = range_position(df, int(params.get("range_lookback", 50)))
    avg_vol = float(df["volume"].astype(float).tail(
        int(params.get("volume_avg_period", 20))).mean() or 0.0)

    n = len(df)
    start = max(0, n - lookback)
    rejections: list[dict[str, Any]] = []
    scores: list[float] = []
    for i in range(start, n):
        row = df.iloc[i]
        m = wick_metrics(row)
        vol_ok = avg_vol > 0 and float(row["volume"]) >= avg_vol * 0.9
        # rejection = wick >= 60% range, body <= 25%, disertai partisipasi volume
        if m["lower_ratio"] >= 0.60 and m["body_ratio"] <= 0.25 and vol_ok:
            strength = min(1.0, m["lower_ratio"] / 0.8)
            rejections.append({"index": i, "side": "BUYER_REJECTION",
                               "strength": round(strength, 2)})
            scores.append(strength)
        elif m["upper_ratio"] >= 0.60 and m["body_ratio"] <= 0.25 and vol_ok:
            strength = min(1.0, m["upper_ratio"] / 0.8)
            rejections.append({"index": i, "side": "SELLER_REJECTION",
                               "strength": round(strength, 2)})
            scores.append(-strength)
        else:
            scores.append(0.0)

    score = round(max(-1.0, min(1.0, sum(scores) / max(len(scores), 1))), 4)
    last_bar = wick_metrics(df.iloc[-1])
    return {
        "range_position": rp,
        "last_bar": {k: round(v, 6) for k, v in last_bar.items()},
        "rejection_bars": rejections,
        "wick_rejection_score": score,               # ARCH §3.4: WAJIB diekspor
        "interpretation": _interpret(score, rp["zone"], trend_direction),
    }


def _interpret(score: float, zone: str, direction: str) -> str:
    if abs(score) < 0.15:
        return "netral: tidak ada penolakan wick signifikan"
    dominant = "buyer" if score > 0 else "seller"
    if direction == "LONG":
        if score > 0:
            return "mendukung long: buyer aktif menolak penurunan"
        return f"melawan long: {dominant} menekan dari atas (zona {zone})"
    if direction == "SHORT":
        if score < 0:
            return "mendukung short: seller aktif menolak kenaikan"
        return f"melawan short: {dominant} defend di bawah (zona {zone})"
    return f"{dominant} dominan dalam {int(abs(score) * 100)}% konteks lookback"
