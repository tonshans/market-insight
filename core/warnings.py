# -*- coding: utf-8 -*-
"""Warning engine dengan severity (F1).

Kumpulan aturan heuristik yang dievaluasi terhadap hasil analisa per TF dan
hasil confluence. Setiap warning: {code, severity, message}.
Severity: INFO < WARN < HIGH  (HIGH = sebaiknya tidak entry / observasi saja).
"""
from __future__ import annotations

from typing import Any

SEVERITY_ORDER = {"INFO": 0, "WARN": 1, "HIGH": 2}


def _w(code: str, severity: str, message: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "message": message}


def evaluate(tf_results: dict[str, dict[str, Any]],
             summary_bias: str | None = None,
             rr_summary: dict[str, Any] | None = None,
             params: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """Evaluasi semua aturan terhadap `timeframes` results + konteks global.

    tf_results: mapping tf -> dict trend/volume/sr/risk/pa satu TF.
    """
    p = params or {}
    warns: list[dict[str, str]] = []

    for tf, r in tf_results.items():
        trend = r.get("trend", {})
        vol = r.get("volume", {})
        pa = r.get("price_action", {})
        risk = r.get("risk", {})
        label = trend.get("label", "SIDEWAYS")
        score_raw = trend.get("score_raw", 0.0)
        maturity = trend.get("maturity", "EARLY")
        streak = trend.get("streak", 0)
        votes = trend.get("votes", {})
        direction = risk.get("direction", "NEUTRAL")

        # ---- voting internal tidak kompak ----------------------------------
        pos = sum(1 for v in votes.values() if v > 0)
        neg = sum(1 for v in votes.values() if v < 0)
        if abs(score_raw) >= 0.3 and min(pos, neg) >= 2:
            warns.append(_w(
                "VOTE_SPLIT", "WARN",
                f"{tf}: indikator terbelah ({pos} bull vs {neg} bear) meski "
                f"label {label}; tunggu konfirmasi."))

        # ---- MATURE + volume dry-up = tren kehabisan bahan bakar -----------
        if maturity == "MATURE" and vol.get("ratio", 1.0) <= 0.7:
            warns.append(_w(
                "TREND_EXHAUSTION", "HIGH",
                f"{tf}: tren {label} sudah MATURE (streak {streak}) tapi "
                f"volume turun (ratio {vol.get('ratio')}); risiko reversal."))

        # ---- wick rejection melawan arah bias -------------------------------
        wrs = pa.get("wick_rejection_score", 0.0) if isinstance(pa, dict) else 0.0
        if direction == "LONG" and wrs <= -0.3:
            warns.append(_w("REJECTION_AGAINST_BIAS", "WARN",
                            f"{tf}: seller aktif menolak kenaikan "
                            f"(wick_rejection={wrs}) padahal bias bullish."))
        if direction == "SHORT" and wrs >= 0.3:
            warns.append(_w("REJECTION_AGAINST_BIAS", "WARN",
                            f"{tf}: buyer aktif menolak penurunan "
                            f"(wick_rejection={wrs}) padahal bias bearish."))

        # ---- harga di ujung range melawan arah ------------------------------
        zone = (pa.get("range_position") or {}).get("zone") if isinstance(pa, dict) else None
        if direction == "LONG" and zone == "TOP":
            warns.append(_w("AT_RANGE_TOP", "WARN",
                            f"{tf}: harga di TOP range {p.get('range_lookback', 50)} bar; "
                            "raw momentum chase berisiko."))
        if direction == "SHORT" and zone == "BOTTOM":
            warns.append(_w("AT_RANGE_BOTTOM", "WARN",
                            f"{tf}: harga di BOTTOM range; raw momentum chase berisiko."))

        # ---- RSI ekstrem -----------------------------------------------------
        rsi_val = (trend.get("details") or {}).get("rsi")
        if rsi_val is not None:
            if direction == "LONG" and rsi_val >= 75:
                warns.append(_w("RSI_OVERBOUGHT", "INFO",
                                f"{tf}: RSI {rsi_val:.0f} overbought; "
                                "tren valid tapi hindari entry baru."))
            if direction == "SHORT" and rsi_val <= 25:
                warns.append(_w("RSI_OVERSOLD", "INFO",
                                f"{tf}: RSI {rsi_val:.0f} oversold; "
                                "tren valid tapi hindari entry baru."))

        # ---- likuiditas tipis -------------------------------------------------
        if vol.get("state") == "LOW" and direction != "NEUTRAL":
            warns.append(_w("THIN_LIQUIDITY", "WARN",
                            f"{tf}: volume ratio {vol.get('ratio')} (<0.7); "
                            "slippage & false break lebih mungkin (proxy D-2)."))

        # ---- risk terlalu besar ----------------------------------------------
        if risk.get("risk_class") == "HIGH":
            warns.append(_w("WIDE_STOP", "WARN",
                            f"{tf}: invalidation {risk.get('risk_pct')}% dari harga; "
                            "position sizing harus diperkecil."))

    # ---- confluence-level (summary) -----------------------------------------
    if summary_bias:
        dirs = [r.get("risk", {}).get("direction", "NEUTRAL")
                for r in tf_results.values()]
        opp = sum(1 for d in dirs
                  if d != "NEUTRAL" and d != ("LONG" if "UP" in summary_bias
                                              else "SHORT"))
        if opp >= 2:
            warns.append(_w("TF_CONFLICT", "HIGH",
                            f"Bias {summary_bias} tapi {opp}/3 TF berlawanan; "
                            "hanya observasi, bukan setup."))

    # ---- R:R global ------------------------------------------------------------
    if rr_summary and rr_summary.get("rr") is not None:
        if rr_summary["rr"] < 1.0:
            warns.append(_w("BAD_RR", "HIGH",
                            f"R:R {rr_summary['rr']} < 1 — target terdekat lebih "
                            "dekat daripada stop."))
        elif rr_summary["rr"] < rr_summary.get("min_rr", 1.5):
            warns.append(_w("MARGINAL_RR", "WARN",
                            f"R:R {rr_summary['rr']} di bawah standar "
                            f"{rr_summary.get('min_rr', 1.5)}."))

    warns.sort(key=lambda w: -SEVERITY_ORDER.get(w["severity"], 0))
    return warns


def highest_severity(warnings: list[dict[str, str]]) -> str:
    if not warnings:
        return "NONE"
    return max((w["severity"] for w in warnings),
               key=lambda s: SEVERITY_ORDER.get(s, 0))
