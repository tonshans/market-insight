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
    if summary_bias and tf_results:
        dirs = [r.get("risk", {}).get("direction", "NEUTRAL")
                for r in tf_results.values()]
        n_tf = len(dirs)
        # Arah bias ditentukan EKSPLISIT dari nilai canonical label confluence
        # ("UP"/"DOWN"/"UPTREND"/"DOWNTREND"/"SIDEWAYS"/"CONFLICTED"), BUKAN
        # substring matching yang rapuh (bug lama: `"UP" in "CONFLICTED"` dsb).
        bias_norm = str(summary_bias).strip().upper()
        bias_up = bias_norm in ("UP", "UPTREND")
        bias_down = bias_norm in ("DOWN", "DOWNTREND")
        if bias_up or bias_down:
            # Bug inti yang diperbaiki di sini: deteksi konflik dihitung dari
            # FRAKSI TF yang SEARAH dengan bias, bukan dari string matching
            # arah bias dan bukan dari perbandingan `d != expected` yang
            # menjadikan semua TF NEUTRAL sebagai "berlawanan".
            #   n_sup / n_tf  -> fraksi TF yang benar-benar mendukung bias
            # Konflik nyata bila mayoritas kecil (<= ambang MINORITY, selaras
            # core/analysis.py) atau nol TF yang mendukung bias:
            #   - n_sup == 0                  -> tidak ada dukungan sama sekali
            #   - n_sup * 3 <= n_tf           -> < ~1/3 TF searah (MINORITY)
            # Pesan memakai denominator DINAMIS `n_tf` (bukan hardcode "/3")
            # dan menghitung hanya TF berlawanan-arah (LONG vs SHORT); TF
            # NEUTRAL dilaporkan terpisah agar tidak memicu "konflik semu".
            supported = "LONG" if bias_up else "SHORT"
            opposed = "SHORT" if bias_up else "LONG"
            n_sup = dirs.count(supported)
            n_opp = dirs.count(opposed)
            # Ambang konflik berdenominator DINAMIS (bukan hardcode "/3"):
            # dihitung dari fraksi TF yang SEARAH dengan bias (n_sup/n_tf).
            # Konflik dilaporkan bila TF sejalan bukan mayoritas ketat
            # (n_sup * 2 <= n_tf, termasuk kasus nol dukungan) dan ada
            # oposisi nyata (n_opp >= 1). Ini menangkap dua mode kegagalan:
            #   - arah berlawanan mendominasi/menyamai (mis. DOWNTREND tapi
            #     hanya 0/3 TF SHORT sejalan);
            #   - mayoritas semu karena banyak TF netral tanpa dukungan penuh.
            # Sebaliknya, bias yang didukung mayoritas TF sejalan tidak boleh
            # memicu konflik meski 1 TF berdiri sendiri di sisi berlawanan
            # (itu sekadar outlier, bukan konflik confluence).
            # TF NEUTRAL tidak pernah dihitung sebagai "berlawanan" (bug lama:
            # `d != expected` membuat semua TF netral ikut dianggap konflik).
            if n_sup * 2 <= n_tf and n_opp >= 1:
                warns.append(_w(
                    "TF_CONFLICT", "HIGH",
                    f"Bias {summary_bias} tapi {n_opp}/{n_tf} TF "
                    "berlawanan; hanya observasi, bukan setup."))
        elif bias_norm == "CONFLICTED":
            # konflik nyata antar TF: deteksi langsung dari arah, BUKAN dari
            # string matching nama bias ("UP" in "CONFLICTED" -> False dulu
            # membuat semua TF LONG dianggap berlawanan).
            n_long = dirs.count("LONG")
            n_short = dirs.count("SHORT")
            if min(n_long, n_short) >= 1:
                warns.append(_w("TF_CONFLICT", "HIGH",
                                f"Bias {summary_bias}: {n_long} TF LONG vs "
                                f"{n_short} TF SHORT dari {n_tf} TF; "
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
