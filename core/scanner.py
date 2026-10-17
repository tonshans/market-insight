# -*- coding: utf-8 -*-
"""Scanner pasar (F3, FR-4): universe filter -> pre-filter ADX -> skor -> rank.

Alur (ringan di jaringan, sesuai NFR-1 target < 5 menit first-run):
    1. UNIVERSE   : markets ccxt, quote IDR, buang stablecoin base & volume
                    24h rendah; dibatasi max_symbols (protect RPi).
    2. PREFILTER  : fetch OHLCV HANYA TF acuan pre-filter (default 4h);
                    buang pair dengan ADX < min_adx (tanpa trend jelas).
    3. ANALYSE    : untuk kandidat lolos, jalankan pipeline indikator murni
                    pada data yang SUDAH ada di cache memori scan ini + TF
                    lain dari disk-cache/fetch (cache agresif = panggilan
                    kedua hampir selalu HIT).
    4. SCORING    : prospect (bobot scanner.yaml), risk (atr_pct/distance/
                    liquidity), adj_score = prospect - risk*risk_penalty.
    5. PERSIST    : simpan ranking ke SQLite scan_history + bandingkan rank
                    vs scan sebelumnya (compare_rank).

Semua nilai mentah pembentuk skor ikut disimpan di raw_scores_json (D-3)
untuk feature engineering Fase 2.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import pandas as pd

from . import indicators as ta
from .fetch import FetchError
from .price_action import analyze_price_action
from .risk import compute_risk, direction_from_bias
from .sr import detect_sr
from .time_utils import iso_utc
from .trend import analyze_trend
from .volume import analyze_volume

logger = logging.getLogger("market_tools.scanner")

STABLE_BASES_FALLBACK = {"USDT", "USDC", "BUSD", "TUSD", "FDUSD", "DAI"}


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return lo
    return max(lo, min(hi, v))


# --------------------------------------------------------------------- data
@dataclass
class ScanOutcome:
    """Hasil satu scan penuh, siap render (CLI/bot) dan persist (SQLite)."""
    ts_utc: str
    rows: list[dict[str, Any]]            # terurut, sudah dinomori `rank`
    universe_size: int
    prefiltered_size: int
    analysed_size: int
    errors: dict[str, str]                # symbol -> pesan singkat
    cooldown_active: bool = False
    skipped_watchlist: bool = False
    limit_reached_note: Optional[str] = None  # info bila --limit memotong kandidat
    duration_seconds: float = 0.0
    previous_ts: Optional[str] = None     # utk keterangan rank-change
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def top(self) -> list[dict[str, Any]]:
        return self.rows


# ------------------------------------------------------------- universe step
def build_universe(fetcher, universe: dict[str, Any]) -> list[dict[str, Any]]:
    """Filter markets ccxt: active, spot, quote cocok, bukan stablecoin base,
    quoteVolume 24h >= minimum. Return list [{symbol, base, quote, qvol}]."""
    uni = universe
    quote = str(uni["quote_currency"]).upper()
    exclude = {str(b).upper() for b in uni.get("exclude_bases", [])} | STABLE_BASES_FALLBACK
    min_qvol = float(uni.get("min_quote_volume_24h", 0))
    max_symbols = int(uni.get("max_symbols", 200))

    markets = fetcher.load_markets()
    out: list[dict[str, Any]] = []
    for sym, m in markets.items():
        try:
            if not m.get("active", True) or m.get("swap") or m.get("future"):
                continue
            if m.get("quote") != quote:
                continue
            if m.get("base") in exclude:
                continue
            qvol = (m.get("info") or {}).get("quote_volume") or m.get("quoteVolume")
            qvol = float(qvol) if qvol not in (None, "") else 0.0
            if qvol < min_qvol:
                continue
            out.append({"symbol": sym, "base": m.get("base"), "quote": quote,
                        "qvol_24h": qvol})
        except Exception:  # market entry anomali -> lewati, jangan crash
            continue
    # prioritas likuiditas tertinggi saat kena batas max_symbols
    out.sort(key=lambda x: x["qvol_24h"], reverse=True)
    return out[:max_symbols]


# ------------------------------------------------------------ prefilter step
def prefilter_adx(fetcher, symbols: list[str], scfg: dict[str, Any],
                  params: dict[str, Any],
                  progress: Optional[Callable[[int, int, str], None]] = None
                  ) -> tuple[list[str], dict[str, str], dict[str, dict], bool]:
    """Fetch TF pre-filter utk semua symbol; kembalikan kandidat dgn ADX ok.

    Return (candidates, errors, ohlcv_cache, cooldown_seen).
    """
    pf = scfg["prefilter"]
    tf = pf["timeframe"]
    adx_period = int(pf.get("adx_period", params.get("adx_period", 14)))
    min_adx = float(pf.get("min_adx", 15))
    limit = max(int(params.get("ema_slow", 50)), adx_period * 3) + 10

    candidates: list[str] = []
    errors: dict[str, str] = {}
    frames: dict[str, pd.DataFrame] = {}
    cooldown_seen = False
    n = len(symbols)
    for i, sym in enumerate(symbols):
        if progress:
            progress(i + 1, n, sym)
        try:
            res = fetcher.fetch_ohlcv(sym, tf, limit=limit)
            cooldown_seen = cooldown_seen or res.cooldown_active
            df = res.df
            if len(df) < adx_period * 3:
                errors[sym] = f"data pendek ({len(df)} bar)"
                continue
            adx_df = ta.adx(df, adx_period)
            last_adx = float(adx_df["adx"].iloc[-1])
            if math.isnan(last_adx):
                errors[sym] = "ADX tidak bisa dihitung"
                continue
            if last_adx >= min_adx:
                candidates.append(sym)
                frames[sym] = df
        except FetchError as exc:
            errors[sym] = str(exc)[:120]
            cooldown_seen = cooldown_seen or exc.cooldown_active
            if exc.cooldown_active:
                # breaker open -> sisa symbol langsung dicatat tanpa request
                for rest in symbols[i + 1:]:
                    errors.setdefault(rest, "skipped: API_COOLDOWN aktif")
                break
        except Exception as exc:  # symbol anomali (delist dsb.)
            errors[sym] = f"{type(exc).__name__}: {exc}"[:120]
    return candidates, errors, frames, cooldown_seen


# ------------------------------------------------------------------ scoring
def score_symbol(df: pd.DataFrame, symbol: str, qvol_24h: float,
                 params: dict[str, Any], scfg: dict[str, Any],
                 extra_frames: Optional[dict[str, pd.DataFrame]] = None
                 ) -> dict[str, Any]:
    """Hitung prospect/risk/adj_score SATU symbol dari OHLCV yang tersedia.

    `extra_frames`: {tf: df} hasil fetch TF confluence tambahan (boleh kosong).
    Semua komponen mentah dieksport (raw scores, D-3).
    """
    weights = scfg["scoring"].get("weights", {})
    risk_weights = scfg["scoring"].get("risk_weights", {})
    penalty = float(scfg["ranking"].get("risk_penalty", 0.5))

    trend = analyze_trend(df, params)
    sr = detect_sr(df, params)
    vol = analyze_volume(df, params,
                         trend_direction=direction_from_bias(trend["label"]))
    pa = analyze_price_action(df, params,
                              trend_direction=direction_from_bias(trend["label"]))
    risk = compute_risk(df, params, trend["label"], sr=sr)

    price = float(df["close"].iloc[-1])
    atr_val = float(risk.get("atr") or 0.0)
    atr_pct = risk.get("atr_pct")

    # extra TF dianalisa SEKALI (hemat CPU di RPi), hasilnya dipakai bersama
    extra_trends = [analyze_trend(f, params)
                    for f in (extra_frames or {}).values()]

    # --- components (0..1) ---------------------------------------------------
    raw_scores = [trend["score_raw"]] + [t["score_raw"] for t in extra_trends]
    best_abs = max(abs(s) for s in raw_scores)
    c_trend = _clamp(best_abs / 0.75)          # ~0.75 raw ≈ tren sangat kuat

    # confluence: kesepakatan arah antar TF (termasuk TF tambahan bila ada)
    labels = [trend["label"]] + [t["label"] for t in extra_trends]
    dirs = [direction_from_bias(l) for l in labels]
    pos, neg = dirs.count("LONG"), dirs.count("SHORT")
    agree = max(pos, neg)
    c_confl = _clamp(agree / len(dirs)) if agree else 0.0

    det = trend["details"]
    hist = float(det.get("macd_hist") or 0.0)
    lr_slope = float(det.get("lr_slope_norm") or 0.0)
    macd_ref = atr_val / max(price, 1e-9)      # skala histogram thd ATR-price
    c_momentum = _clamp(0.5 * _clamp(abs(lr_slope) / 0.02) +
                        0.5 * _clamp(abs(hist) / max(macd_ref, 1e-9) / 2))

    c_volume = _clamp(0.5 * _clamp((float(vol["vsa_score"]) + 0.5)) +
                      0.5 * _clamp(float(pa["wick_rejection_score"]) + 0.5))

    rr_comp = 0.0
    from .rr import compute_rr  # lokal agar tidak circular-import tak perlu
    direction = direction_from_bias(trend["label"])
    rr = compute_rr(direction, price, risk.get("invalidation"), sr,
                    mode="auto", df=df)
    if rr.get("rr") is not None:
        rr_comp = _clamp(float(rr["rr"]) / 3.0)  # R:R >= 3 -> komponen penuh

    w_sum = sum(weights.values()) or 1.0
    prospect = 100.0 * (
        weights.get("trend_strength", 0.35) * c_trend +
        weights.get("confluence", 0.25) * c_confl +
        weights.get("momentum", 0.15) * c_momentum +
        weights.get("volume", 0.15) * c_volume +
        weights.get("risk_reward", 0.10) * rr_comp) / w_sum

    # --- risk score (0..100, makin tinggi makin berisiko) --------------------
    c_atr = _clamp((atr_pct or 0.0) / 10.0)     # ATR 10%+ dari harga = ekstrem
    dist_sup = sr.get("nearest_support")
    dist_res = sr.get("nearest_resistance")
    near = None
    # nearest_* berupa float level (lihat core/sr.detect_sr)
    if direction == "LONG" and dist_sup:
        near = (price - float(dist_sup)) / max(atr_val, 1e-9)
    elif direction == "SHORT" and dist_res:
        near = (float(dist_res) - price) / max(atr_val, 1e-9)
    c_dist = _clamp(1.0 - (near / 3.0)) if near is not None else 0.6  # netral-ish
    liq_ref = 1e10                                # 10 miliar IDR = likuid penuh
    c_liq = _clamp(math.log10(max(qvol_24h, 1.0)) / math.log10(liq_ref)) if qvol_24h > 1 else 0.0
    rw_sum = sum(risk_weights.values()) or 1.0
    risk_score = 100.0 * (
        risk_weights.get("atr_pct", 0.5) * c_atr +
        risk_weights.get("distance_to_sr", 0.3) * c_dist +
        risk_weights.get("liquidity", 0.2) * (1.0 - c_liq)) / rw_sum

    adj = prospect - risk_score * penalty

    return {
        "symbol": symbol,
        "price": round(price, 8),
        "qvol_24h": qvol_24h,
        "trend_label": trend["label"],
        "trend_score_raw": trend["score_raw"],
        "regime": trend["regime"]["name"],
        "streak": trend["streak"],
        "maturity": trend["maturity"],
        "confidence": trend["confidence"],
        "adx": round(float(det.get("adx") or 0.0), 2),
        "rsi": round(float(det.get("rsi") or 0.0), 2),
        "atr_pct": round(atr_pct, 3) if atr_pct is not None else None,
        "vsa_score": vol["vsa_score"],
        "wick_rejection_score": pa["wick_rejection_score"],
        "vol_ratio": vol["ratio"],
        "rr": rr.get("rr"),
        "rr_verdict": rr.get("verdict"),
        "direction": direction,
        "prospect": round(prospect, 2),
        "risk": round(risk_score, 2),
        "adj_score": round(adj, 2),
        "components": {"trend": round(c_trend, 4), "confluence": round(c_confl, 4),
                       "momentum": round(c_momentum, 4), "volume": round(c_volume, 4),
                       "rr": round(rr_comp, 4), "atr": round(c_atr, 4),
                       "dist_sr": round(c_dist, 4), "liq": round(c_liq, 4)},
    }


# ------------------------------------------------------------------ driver
def run_scan(fetcher, config, scanner_config, state=None,
             watchlist_only: bool = False,
             timeframes: Optional[list[str]] = None,
             progress: Optional[Callable[[int, int, str], None]] = None,
             top_n: Optional[int] = None,
             limit: Optional[int] = None) -> ScanOutcome:
    """Eksekusi scanner end-to-end (F3). Persistensi via `state` (opsional)."""
    import time as _t
    started = _t.time()
    scfg = {
        "universe": scanner_config.universe,
        "prefilter": scanner_config.prefilter,
        "scoring": scanner_config.scoring,
        "ranking": scanner_config.ranking,
    }
    params = config.analysis
    conf_tf = timeframes or config.default_timeframes

    # 1. universe --------------------------------------------------------------
    if watchlist_only:
        from .watchlist import Watchlist  # lokal hindari cycle tak perlu
        wl_path = getattr(config, "get", lambda k, d=None: d)("state.watchlist_path",
                                                              "state/watchlist.yaml")
        wl = Watchlist(wl_path)
        markets = [{"symbol": s, "base": s.split("/")[0], "quote": s.split("/")[1],
                    "qvol_24h": 0.0} for s in wl.symbols]
    else:
        markets = build_universe(fetcher, scfg["universe"])
    universe_size = len(markets)
    logger.info("universe: %d simbol", universe_size)

    # 2. pre-filter ADX ---------------------------------------------------------
    symbols = [m["symbol"] for m in markets]
    qvol_map = {m["symbol"]: m["qvol_24h"] for m in markets}
    candidates, errors, prefetched, cooldown_seen = prefilter_adx(
        fetcher, symbols, scfg, params, progress=progress)
    prefiltered_size = len(candidates)
    logger.info("lolos pre-filter ADX: %d/%d", prefiltered_size, universe_size)

    # batas opsional jumlah analisa (hemat rate-limit / RPi) — urut qvol agar
    # yang dipotong adalah kandidat paling tidak likuid
    limit_reached_note = None
    if limit is not None and 0 < limit < len(candidates):
        candidates.sort(key=lambda s: qvol_map.get(s, 0.0), reverse=True)
        limit_reached_note = (f"--limit {limit}: {len(candidates)} kandidat lolos "
                              f"ADX, hanya {limit} teratas (likuiditas) yang dianalisa")
        candidates = candidates[:limit]

    # 3+4. analisa & skor per kandidat ------------------------------------------
    rows: list[dict[str, Any]] = []
    n_cand = len(candidates)
    for ci, sym in enumerate(candidates, 1):
        if progress:
            progress(ci, n_cand, f"{sym} (scoring {ci}/{n_cand})")
        df_base = prefetched[sym]
        extras: dict[str, pd.DataFrame] = {}
        for tf in conf_tf:
            if tf == scfg["prefilter"]["timeframe"]:
                continue
            try:
                res = fetcher.fetch_ohlcv(sym, tf,
                                          limit=max(config.max_candles,
                                                    params["ema_slow"] + 10))
                extras[tf] = res.df
                cooldown_seen = cooldown_seen or res.cooldown_active
            except Exception as exc:
                logger.debug("confluence tf %s %s gagal: %s", sym, tf, exc)
        try:
            row = score_symbol(df_base, sym, qvol_map.get(sym, 0.0), params,
                               scfg, extra_frames=extras)
            rows.append(row)
        except Exception as exc:
            errors[sym] = f"scoring error: {type(exc).__name__}: {exc}"[:120]

    # 5. ranking ----------------------------------------------------------------
    rows.sort(key=lambda r: r["adj_score"], reverse=True)
    limit_n = top_n if top_n is not None else int(scfg["ranking"].get("top_n", 15))
    ranked_all = rows
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    display_rows = ranked_all[:limit_n]

    # rank change vs scan sebelumnya (sebelum persist scan baru) ----------------
    prev_ts = None
    if state is not None:
        last = state.get_last_scan()
        prev_ts = last["ts_utc"] if last else None
        if prev_ts:
            prev_ranks = {r["symbol"]: r.get("rank") for r in last["rows"]}
            for r in display_rows:
                pr = prev_ranks.get(r["symbol"])
                r["prev_rank"] = pr
                if pr is None:
                    r["rank_change"] = "NEW"
                else:
                    d = pr - r["rank"]
                    r["rank_change"] = "0" if d == 0 else f"{'+' if d > 0 else ''}{d}"
                    r["rank_delta"] = d

    ts = iso_utc()
    if state is not None and scfg["ranking"].get("persist_history", True):
        state.save_scan_result(ts, display_rows)
        state.set_bot_state("last_scan", {"ts_utc": ts,
                                          "n_candidates": prefiltered_size,
                                          "n_universe": universe_size})

    return ScanOutcome(
        ts_utc=ts, rows=display_rows, universe_size=universe_size,
        prefiltered_size=prefiltered_size, analysed_size=len(rows),
        errors=errors, cooldown_active=cooldown_seen,
        skipped_watchlist=False, duration_seconds=round(_t.time() - started, 2),
        limit_reached_note=limit_reached_note,
        previous_ts=prev_ts,
        meta={"timeframes": conf_tf, "prefilter_tf": scfg["prefilter"]["timeframe"],
              "min_adx": scfg["prefilter"]["min_adx"],
              "formula": scfg["ranking"].get("formula", ""),
              "persisted": bool(state is not None and
                                scfg["ranking"].get("persist_history", True))},
    )


# --------------------------------------------------------------- text render
def format_scan_text(outcome: ScanOutcome, emoji: bool = True,
                     title: str = "PROSPEK HARI INI") -> str:
    """Render Level-1 ringkas utk CLI/bot (< 10 detik baca)."""
    up, down, neu = "🟢", "🔴", "⚪"
    arrow = {"UPTREND": up, "STRONG_UPTREND": up, "WEAK_UPTREND": up,
             "DOWNTREND": down, "STRONG_DOWNTREND": down, "WEAK_DOWNTREND": down}

    def sym_icon(label: str) -> str:
        return arrow.get(label, neu)

    lines = [f"📊 {title} — {outcome.ts_utc}",
             f"Universe {outcome.universe_size} → lolos ADX {outcome.prefiltered_size}"
             f" → dianalisa {outcome.analysed_size} "
             f"({outcome.duration_seconds:.1f}s)", ""]
    if not outcome.rows:
        lines.append("(tidak ada kandidat lolos pre-filter)")
    header = f"{'#':>2} {'PAIR':<14} {'ADJ':>6} {'PROSP':>6} {'RISK':>5} " \
             f"{'TREND':<16} {'ADX':>5} {'R:R':>5} {'Δ':>4}"
    lines.append(header)
    lines.append("-" * len(header))
    for r in outcome.rows:
        delta = r.get("rank_change", "")
        rr = f"{r['rr']:.1f}" if r.get("rr") else "-"
        lines.append(
            f"{r['rank']:>2} {sym_icon(r['trend_label'])} {r['symbol']:<12} "
            f"{r['adj_score']:>6.1f} {r['prospect']:>6.1f} {r['risk']:>5.1f} "
            f"{r['trend_label']:<16} {r['adx']:>5.1f} {rr:>5} {delta:>4}")
    if outcome.cooldown_active:
        lines.append("")
        lines.append("⚠️ API_COOLDOWN aktif selama scan — sebagian data berasal "
                     "dari cache lama.")
    if outcome.errors:
        lines.append("")
        lines.append(f"⚠️ {len(outcome.errors)} symbol dilewati "
                     "(contoh: " + "; ".join(
                         f"{k}: {v}" for k, v in list(outcome.errors.items())[:3]) + ")")
    return "\n".join(lines)
