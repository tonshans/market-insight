# -*- coding: utf-8 -*-
"""Support/Resistance detection adaptif (F1).

Bagian dari WP4. Strategi:
1. Cari swing high/low lokal (fractal) dengan lookback `swing_lookback`.
2. Cluster level-level berdekatan (jarak < cluster_atr x ATR) menjadi ZONE,
   karena S/R riil adalah area, bukan garis tunggal.
3. Skor tiap zone: jumlah touch + usia (semakin baru semakin relevan) +
   kekuatan (range swing yang membentuknya).
4. Adaptive: zona terlalu lebar (> max_width_atr x ATR) di-split; hanya
   top `max_levels` per sisi yang diekspor.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from . import indicators as ta


def find_swings(df: pd.DataFrame, lookback: int = 20) -> dict[str, list[tuple[int, float]]]:
    """Swing high/low pada dua resolusi.

    - Mayor (index utama utk S/R & invalidation): pivot ekstrem dalam window
      +-min(lookback//2, 5) bar per sisi — hanya swing yang benar-benar lokal.
    - Minor (fallback utk market halus/sideways): pivot window +-2 bar.
    Return {"highs": [...], "lows": [...]} berisi tuple (index, harga) hasil
    mayor; key "highs_minor"/"lows_minor" untuk fallback.
    """
    n = len(df)

    def pivots(width: int) -> tuple[list, list]:
        highs, lows = [], []
        h = df["high"].astype(float).tolist()
        l = df["low"].astype(float).tolist()
        for i in range(width, n - width):
            win_h = h[i - width: i + width + 1]
            if h[i] == max(win_h) and win_h.count(h[i]) == 1:
                highs.append((i, h[i]))
            win_l = l[i - width: i + width + 1]
            if l[i] == min(win_l) and win_l.count(l[i]) == 1:
                lows.append((i, l[i]))
        return highs, lows

    major_w = max(3, min(lookback // 2, 5))
    highs, lows = pivots(major_w)
    highs_m, lows_m = pivots(2)
    return {"highs": highs, "lows": lows,
            "highs_minor": highs_m, "lows_minor": lows_m}


def _cluster(levels: list[tuple[int, float]], atr_val: float, threshold: float
             ) -> list[dict[str, Any]]:
    """Cluster level berdekatan (<= threshold*ATR) jadi satu zone."""
    if not levels or atr_val <= 0:
        return []
    ordered = sorted(levels, key=lambda t: t[1])
    clusters: list[list[tuple[int, float]]] = [[ordered[0]]]
    for idx, price in ordered[1:]:
        if price - clusters[-1][-1][1] <= threshold * atr_val:
            clusters[-1].append((idx, price))
        else:
            clusters.append([(idx, price)])
    out = []
    for c in clusters:
        prices = [p for _, p in c]
        newest = max(i for i, _ in c)
        out.append({
            "center": sum(prices) / len(prices),
            "lo": min(prices),
            "hi": max(prices),
            "touches": len(c),
            "newest_index": newest,
        })
    return out


def detect_sr(df: pd.DataFrame, params: dict[str, Any],
              current_price: float | None = None) -> dict[str, Any]:
    """Deteksi zone support & resistance. Return dict siap-ditempatkan §4.1."""
    lookback = int(params.get("swing_lookback", 20))
    cluster_atr = float(params.get("sr_cluster_atr", 0.5))
    max_levels = int(params.get("sr_max_levels", 3))
    if len(df) < lookback + 2:
        raise ValueError(f"data terlalu pendek untuk S/R: {len(df)} bar")

    atr_series = ta.atr(df, params["atr_period"])
    atr_val = float(atr_series.iloc[-1])
    price = float(current_price if current_price is not None else df["close"].iloc[-1])
    swings = find_swings(df, lookback)
    n = len(df)

    def _zones(pts_key: str, kind: str) -> list[dict[str, Any]]:
        pts = swings.get(pts_key) or []
        out = []
        for z in _cluster(pts, atr_val, cluster_atr):
            strength = z["touches"] * 0.6 + (z["newest_index"] / max(n, 1)) * 0.4
            if pts is swings.get("highs_minor") or pts is swings.get("lows_minor"):
                strength *= 0.7   # minor pivot kurang relevan
            out.append({
                "type": kind,
                "level": round(z["center"], 8),
                "zone_low": round(z["lo"], 8),
                "zone_high": round(z["hi"], 8),
                "touches": z["touches"],
                "strength": round(min(strength, 1.0), 3),
                "distance_pct": round((z["center"] - price) / price * 100, 3),
            })
        return out

    zones = _zones("highs", "resistance") + _zones("lows", "support")
    sup_pool = [z for z in zones if z["type"] == "support" and z["level"] < price]
    res_pool = [z for z in zones if z["type"] == "resistance" and z["level"] >= price]
    # adaptive fallback: bila market terlalu halus utk pivot mayor, pakai minor
    if not sup_pool:
        sup_pool = _zones("lows_minor", "support")
        sup_pool = [z for z in sup_pool if z["level"] < price]
    if not res_pool:
        res_pool = _zones("highs_minor", "resistance")
        res_pool = [z for z in res_pool if z["level"] >= price]

    supports = sorted(sup_pool, key=lambda z: -z["level"])[:max_levels]
    resistances = sorted(res_pool, key=lambda z: z["level"])[:max_levels]

    nearest_support = supports[0]["level"] if supports else None
    nearest_resistance = resistances[0]["level"] if resistances else None
    return {
        "supports": supports,
        "resistances": resistances,
        "nearest_support": nearest_support,
        "nearest_resistance": nearest_resistance,
        "position_in_range": _position_in_range(price, nearest_support,
                                                nearest_resistance),
        "atr": round(atr_val, 8),
    }


def _position_in_range(price: float, sup: float | None, res: float | None) -> float | None:
    """0.0 = di support, 1.0 = di resistance (context utk R:R & warnings)."""
    if sup is None or res is None or res <= sup:
        return None
    return round(max(0.0, min(1.0, (price - sup) / (res - sup))), 3)
