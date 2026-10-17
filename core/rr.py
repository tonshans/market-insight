# -*- coding: utf-8 -*-
"""Risk:Reward calculator (F1).

Dua mode:
- auto   : target = nearest resistance (LONG) / nearest support (SHORT),
           risk dari core/risk.compute_risk.
- manual : entry, stop, target diberikan user (CLI override / bot arg).

Ekspor juga skenario R:R alternatif (target berikutnya) supaya trader bisa
melihat opsi, plus verdict ACCEPTABLE/MARGINAL/POOR terhadap min_rr config.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from . import indicators as ta


def _rr(risk_abs: float, reward_abs: float) -> float | None:
    if risk_abs is None or risk_abs <= 0:
        return None
    return round(reward_abs / risk_abs, 2)


def compute_rr(direction: str, price: float, invalidation: float | None,
               sr: dict[str, Any], mode: str = "auto",
               entry: float | None = None, stop: float | None = None,
               target: float | None = None, min_rr: float = 1.5,
               df: pd.DataFrame | None = None) -> dict[str, Any]:
    """Hitung R:R. `sr` hasil core/sr.detect_sr; direction LONG/SHORT.

    `df` (opsional) dipakai untuk fallback target ATR-projection bila tidak
    ada level S/R di depan entry (harga membuat high/low baru).
    """
    out: dict[str, Any] = {
        "mode": mode, "direction": direction, "entry": None, "stop": None,
        "target": None, "risk_abs": None, "reward_abs": None, "rr": None,
        "scenarios": [], "verdict": "NOT_APPLICABLE", "min_rr": min_rr,
    }
    if direction == "NEUTRAL":
        return out

    e = float(entry if entry is not None else price)
    if mode == "manual":
        if stop is None or target is None:
            raise ValueError("mode manual membutuhkan stop & target")
        e = float(entry if entry is not None else price)
        s, t = float(stop), float(target)
    else:  # auto
        if invalidation is None:
            return out
        e = float(price)
        s = float(invalidation)
        levels = (sr.get("resistances") if direction == "LONG"
                  else sr.get("supports")) or []
        beyond = [z["level"] for z in levels
                  if (z["level"] > e if direction == "LONG" else z["level"] < e)]
        if beyond:
            t = max(beyond) if direction == "LONG" else min(beyond)
        else:
            # Fallback target bila tidak ada S/R di depan entry (mis. harga
            # sedang membuat high/low baru): pakai projection ATR supaya R:R
            # tetap terhitung; sumber ditandai untuk honesty output text/json.
            atr_period = int(sr.get("atr_period", 14))
            a = None
            if df is not None and len(df) >= atr_period + 1:
                atr_val = ta.atr(df, atr_period).iloc[-1]
                a = float(atr_val) if pd.notna(atr_val) else None
            if a is None or a <= 0:
                return out
            proj = float(sr.get("fallback_atr_multiple", 2.0))
            t = e + proj * a if direction == "LONG" else e - proj * a
            out["target_source"] = "ATR_PROJECTION"

    if direction == "LONG":
        risk, reward = e - s, t - e
    else:
        risk, reward = s - e, e - t

    ratio = _rr(risk, reward)
    out.update({
        "entry": round(e, 8), "stop": round(s, 8), "target": round(t, 8),
        "risk_abs": round(risk, 8), "reward_abs": round(reward, 8), "rr": ratio,
    })
    if ratio is None:
        out["verdict"] = "INVALID_STOP"
        return out
    out["verdict"] = ("ACCEPTABLE" if ratio >= min_rr
                      else "MARGINAL" if ratio >= 1.0 else "POOR")

    # ---- skenario target berikutnya ( utk level S/R selanjutnya ) ---------
    scenarios = []
    all_levels = ((sr.get("resistances") if direction == "LONG"
                   else sr.get("supports")) or [])
    farther = ([z["level"] for z in all_levels if z["level"] > t]
               if direction == "LONG" else
               [z["level"] for z in all_levels if z["level"] < t])
    for lv in sorted(farther)[:2]:
        rw = (lv - e) if direction == "LONG" else (e - lv)
        scenarios.append({"target": round(lv, 8), "rr": _rr(risk, rw)})
    out["scenarios"] = scenarios
    return out
