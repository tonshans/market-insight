# -*- coding: utf-8 -*-
"""Risk per timeframe + invalidation level (F1, FR-3).

Aturan PRD FR-3:
- Invalidation = swing low (long) / swing high (short) TERDEKAT di bawah/atas
  harga, dengan JARAK MINIMUM 0.5 x ATR dari harga sekarang. Bila swing
  terdekat lebih dekat dari 0.5*ATR, dipakai fallback 0.5*ATR (swing berikutnya
  dicek; jika masih terlalu dekat, gunakan batas minimum).
- Risk % = |harga - invalidation| / harga * 100.
- Risk class LOW/MEDIUM/HIGH berdasarkan band ATR% terhadap harga.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from . import indicators as ta
from .sr import find_swings


def direction_from_bias(bias: str) -> str:
    """Petakan trend label/bias ke arah posisi yang dianalisa."""
    if "UP" in bias:
        return "LONG"
    if "DOWN" in bias:
        return "SHORT"
    return "NEUTRAL"


def compute_risk(df: pd.DataFrame, params: dict[str, Any], bias: str,
                 sr: dict[str, Any] | None = None,
                 current_price: float | None = None) -> dict[str, Any]:
    """Hitung invalidation + risk per TF. `bias` = trend label TF tersebut."""
    min_atr_mult = float(params.get("min_atr_distance", 0.5))
    lookback = int(params.get("swing_lookback", 20))
    atr_val = float(ta.atr(df, params["atr_period"]).iloc[-1])
    price = float(current_price if current_price is not None else df["close"].iloc[-1])
    direction = direction_from_bias(bias)

    result: dict[str, Any] = {
        "direction": direction,
        "atr": round(atr_val, 8),
        "atr_pct": round(atr_val / price * 100, 3) if price else None,
        "stop_type": None,
        "invalidation": None,
        "risk_abs": None,
        "risk_pct": None,
        "risk_class": None,
    }
    if direction == "NEUTRAL":
        # tetap ekspor jarak stop hipotetis utk konteks scanner
        result["stop_type"] = "NOT_APPLICABLE"
        return result

    swings = find_swings(df, lookback)
    min_dist = min_atr_mult * atr_val

    if direction == "LONG":
        # kandidat: swing low di bawah harga ATAU zona support S/R terdekat
        cands = [p for _, p in swings["lows"] if p < price - 1e-12]
        if sr:
            for z in (sr.get("supports") or []):
                lv = z.get("level")
                if lv is not None and lv < price - 1e-12:
                    cands.append(float(lv))
        candidates = sorted(set(cands), reverse=True)
        # prioritas 1: swing/zon TERDEKAT yang memenuhi jarak minimum 0.5 x ATR
        stop = next((c for c in candidates if price - c >= min_dist), None)
        stop_type = "SWING_LOW"
        if stop is None and candidates:
            # tidak ada yg memenuhi min distance -> pakai swing terdekat
            stop = candidates[0]
            stop_type = "NEAREST_SWING_BELOW_MIN_ATR"
        elif stop is None:
            stop = price - min_dist
            stop_type = "MIN_ATR_DISTANCE"
        result["invalidation"] = round(stop, 8)
        result["risk_abs"] = round(price - stop, 8)
    else:  # SHORT
        cands = [p for _, p in swings["highs"] if p > price + 1e-12]
        if sr:
            for z in (sr.get("resistances") or []):
                lv = z.get("level")
                if lv is not None and lv > price + 1e-12:
                    cands.append(float(lv))
        candidates = sorted(set(cands))
        stop = next((c for c in candidates if c - price >= min_dist), None)
        stop_type = "SWING_HIGH"
        if stop is None and candidates:
            stop = candidates[0]
            stop_type = "NEAREST_SWING_BELOW_MIN_ATR"
        elif stop is None:
            stop = price + min_dist
            stop_type = "MIN_ATR_DISTANCE"
        result["invalidation"] = round(stop, 8)
        result["risk_abs"] = round(stop - price, 8)

    result["stop_type"] = stop_type
    result["risk_pct"] = round(result["risk_abs"] / price * 100, 3) if price else None
    rp = result["risk_pct"] or 0.0
    result["risk_class"] = "LOW" if rp <= 2.0 else ("MEDIUM" if rp <= 5.0 else "HIGH")
    return result
