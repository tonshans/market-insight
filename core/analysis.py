# -*- coding: utf-8 -*-
"""Orkestrasi analisa single-pair multi-timeframe (F1 + F2, ARCH §3).

Bagian dari WP5 (Orchestration & Export). Alur:
    1. Validasi symbol & daftar TF (FR-1 override CLI dimungkinkan).
    2. Fetch OHLCV per TF via core/fetch (Cache -> Network -> Stale -> Error).
    3. Per TF: trend (voting 5 indikator + score_raw), regime EMA20/50,
       streak/maturity, S/R adaptive, risk+invalidation (>=0.5 x ATR, FR-3),
       volume/VSA + wick-rejection (proxy likuiditas D-2), price action.
    4. Confluence lintas TF (F2): bias aggregation + deteksi konflik.
    5. R:R calculator pada TF acuan (default TF terbesar).
    6. Warnings ber-severity (lintas TF + global).

Data model keluaran mengikuti ARCHITECTURE.md §4.1 — WAJIB berisi
`trend.score_raw`, `volume.vsa_score`, `price_action.wick_rejection_score`
agar siap diekspor ke JSON untuk Fase 2 (D-3).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

SCHEMA_VERSION = "1.0.0"

logger = logging.getLogger("market_tools.analysis")

# urutan label dari bullish -> bearish utk agregasi bias
_LABEL_ORDER = [
    "STRONG_UPTREND", "UPTREND", "WEAK_UPTREND",
    "SIDEWAYS",
    "WEAK_DOWNTREND", "DOWNTREND", "STRONG_DOWNTREND",
]


def _label_rank(label: str) -> int:
    try:
        return _LABEL_ORDER.index(label)
    except ValueError:
        return _LABEL_ORDER.index("SIDEWAYS")


def aggregate_bias(tf_trends: dict[str, dict[str, Any]]) -> tuple[str, str]:
    """Agregasi bias confluence (F2). Return (bias, agreement).

    bias      : label kanonik (mis. UPTREND / SIDEWAYS / CONFLICTED)
    agreement : UNANIMOUS / MAJORITY / MINORITY / NONE
    """
    if not tf_trends:
        return "SIDEWAYS", "NONE"
    scores = [t.get("score_raw", 0.0) for t in tf_trends.values()]
    avg = sum(scores) / len(scores)
    directions = ["UP" if s >= 0.10 else ("DOWN" if s <= -0.10 else "FLAT")
                  for s in scores]
    n_up = directions.count("UP")
    n_down = directions.count("DOWN")
    n_flat = directions.count("FLAT")

    if n_up and n_down and abs(avg) < 0.15:
        # konflik nyata antar TF dengan rata-rata bersih netral
        return "CONFLICTED", "NONE"

    if avg >= 0.10:
        base = "UPTREND"
        agree_with_bias = n_up
    elif avg <= -0.10:
        base = "DOWNTREND"
        agree_with_bias = n_down
    else:
        return "SIDEWAYS", ("NONE" if (n_up and n_down) else "MAJORITY")

    total = len(directions)
    if agree_with_bias == total:
        agreement = "UNANIMOUS"
    elif agree_with_bias * 2 > total:
        agreement = "MAJORITY"
    else:
        agreement = "MINORITY"
    return base, agreement


def detect_conflicts(tf_results: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Deteksi pasangan TF yang arahnya bertentangan secara eksplisit (F2)."""
    conflicts: list[dict[str, Any]] = []
    items = list(tf_results.items())
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            (tf_a, ra), (tf_b, rb) = items[i], items[j]
            da = (ra.get("risk") or {}).get("direction", "NEUTRAL")
            db = (rb.get("risk") or {}).get("direction", "NEUTRAL")
            if {da, db} == {"LONG", "SHORT"}:
                conflicts.append({
                    "timeframes": [tf_a, tf_b],
                    "directions": {tf_a: da, tf_b: db},
                    "note": (f"{tf_a} {da} vs {tf_b} {db}: momentum tidak "
                             "searah; entry melawan TF lebih tinggi berisiko."),
                })
    return conflicts


def analyze_symbol(symbol: str, timeframes: list[str], fetcher, config,
                   rr_mode: str = "auto", entry: Optional[float] = None,
                   stop: Optional[float] = None, target: Optional[float] = None,
                   min_rr: float = 1.5) -> dict[str, Any]:
    """Analisa lengkap satu pair multi-TF. Return dict sesuai §4.1.

    Parameter:
        fetcher : core.fetch.MarketDataFetcher (atau mock dgn interface sama)
        config  : core.config.Config
        rr_mode : auto | manual (manual butuh stop & target)
    """
    from .price_action import analyze_price_action
    from .risk import compute_risk, direction_from_bias
    from .rr import compute_rr
    from .sr import detect_sr
    from .trend import analyze_trend
    from .volume import analyze_volume
    from .warnings import evaluate, highest_severity

    params = config.analysis
    tf_list = list(dict.fromkeys(timeframes))  # unik, jaga urutan

    # panjang minimum agar semua indikator (EMA slow/LR/S/R) valid
    min_bars = max(params["ema_slow"], params["lr_period"],
                   params.get("swing_lookback", 20)) + 5
    limit = max(int(config.max_candles), min_bars)

    timeframes_out: dict[str, Any] = {}
    fetch_meta: dict[str, Any] = {}
    data_warnings: list[dict[str, str]] = []
    errors: dict[str, str] = {}

    for tf in tf_list:
        res = fetcher.fetch_ohlcv(symbol, tf, limit=limit)
        df = res.df
        if len(df) < min_bars:
            raise ValueError(
                f"{symbol} {tf}: data terlalu pendek ({len(df)} bar, "
                f"butuh >= {min_bars}) — tidak bisa analisa.")
        fetch_meta[tf] = {
            "source": res.source,
            "age_seconds": round(res.age_seconds, 1),
            "bars": int(len(df)),
            "cooldown_active": bool(res.cooldown_active),
        }
        for w in res.warnings:
            data_warnings.append({"code": "DATA_SOURCE", "severity": "WARN",
                                  "message": f"{tf}: {w}"})

        trend = analyze_trend(df, params)
        sr = detect_sr(df, params)
        vol = analyze_volume(df, params,
                             trend_direction=direction_from_bias(trend["label"]))
        pa = analyze_price_action(df, params,
                                  trend_direction=direction_from_bias(trend["label"]))
        risk = compute_risk(df, params, trend["label"], sr=sr)

        timeframes_out[tf] = {
            "trend": trend,
            "sr": sr,
            "risk": risk,
            "volume": vol,
            "price_action": pa,
            "last_close": round(float(df["close"].iloc[-1]), 8),
            "last_ts": str(df["ts"].iloc[-1]),
        }

    # ---- confluence (F2) ----------------------------------------------------
    tf_trends = {tf: r["trend"] for tf, r in timeframes_out.items()}
    bias, agreement = aggregate_bias(tf_trends)
    conflicts = detect_conflicts(timeframes_out)

    # ---- R:R pada TF acuan (TF terakhir = terbesar di urutan default) -------
    ref_tf = tf_list[-1]
    ref = timeframes_out[ref_tf]
    direction = direction_from_bias(bias if bias not in ("CONFLICTED",) else
                                    ref["trend"]["label"])
    rr_df = None
    try:
        rr_res = fetcher.fetch_ohlcv(symbol, ref_tf)
        rr_df = rr_res.df
    except Exception:  # pragma: no cover - sudah ter-fetch di loop atas
        pass
    if rr_mode == "manual":
        direction = "LONG" if (target is not None and stop is not None
                               and target > stop) else "SHORT"
    rr = compute_rr(direction, ref["last_close"], ref["risk"].get("invalidation"),
                    ref["sr"], mode=rr_mode, entry=entry, stop=stop,
                    target=target, min_rr=min_rr, df=rr_df)

    # ---- warnings -------------------------------------------------------------
    warn_list = evaluate(timeframes_out, summary_bias=bias, rr_summary=rr,
                         params=params) + data_warnings

    summary = {
        "symbol": symbol,
        "exchange": getattr(config, "exchange_id", "?"),
        "bias": bias,
        "agreement": agreement,
        "conflicts": conflicts,
        "votes": {tf: t["votes"] for tf, t in tf_trends.items()},
        "trend_scores_raw": {tf: t["score_raw"] for tf, t in tf_trends.items()},
        "regime": {tf: t["regime"]["name"] for tf, t in tf_trends.items()},
        "maturity": {tf: t["maturity"] for tf, t in tf_trends.items()},
        "rr": rr,
        "ref_timeframe": ref_tf,
        "warnings": warn_list,
        "max_severity": highest_severity(warn_list),
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "summary": summary,
        "timeframes": timeframes_out,
        "meta": {
            "fetch": fetch_meta,
            "errors": errors,
            "params": params,
        },
    }
