# -*- coding: utf-8 -*-
"""Trend detection per timeframe — voting 5 indikator + raw score (F1).

Bagian dari WP4 (Analysis Engine). Sesuai PRD FR-3 & ARCHITECTURE §3.2:
- Hasil SELALU menyertakan `score_raw` (float -1.0..+1.0) untuk logging
  mentah demi Fase 2 (D-3).
- Regime ditentukan dari posisi relatif EMA fast/slow terhadap harga.
- Streak = jumlah candle close berturut-turut yang konsisten dengan label;
  trend maturity diklasifikasikan EARLY / MID / MATURE dari streak tersebut.

Voting 5 indikator (masing-masing memberi vote -1 / 0 / +1):
  1. ema      : EMA(fast) vs EMA(slow) dan posisi close terhadap keduanya
  2. adx      : arah DI+ vs DI-, divalidasi kekuatan tren via ADX
  3. macd     : histogram MACD (+ konteks garis vs signal)
  4. rsi      : bias RSI di sekitar 50, dinormalisasi pada pita 30..70
  5. lr       : slope linear-regression ternormalisasi per bar
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from . import indicators as ta

LABELS = (
    "STRONG_UPTREND",
    "UPTREND",
    "WEAK_UPTREND",
    "SIDEWAYS",
    "WEAK_DOWNTREND",
    "DOWNTREND",
    "STRONG_DOWNTREND",
)


def label_from_score(score_raw: float) -> str:
    """Petakan skor mentah [-1, +1] ke label trend 7 tingkat."""
    if score_raw >= 0.60:
        return "STRONG_UPTREND"
    if score_raw >= 0.30:
        return "UPTREND"
    if score_raw >= 0.10:
        return "WEAK_UPTREND"
    if score_raw <= -0.60:
        return "STRONG_DOWNTREND"
    if score_raw <= -0.30:
        return "DOWNTREND"
    if score_raw <= -0.10:
        return "WEAK_DOWNTREND"
    return "SIDEWAYS"


def confidence_from_votes(votes: dict[str, int], score_raw: float) -> str:
    """HIGH bila >=4/5 indikator searah & |skor|>=0.5; LOW bila <=2/5."""
    direction = 1 if score_raw > 0 else (-1 if score_raw < 0 else 0)
    agree = sum(1 for v in votes.values() if v == direction and direction != 0)
    if direction == 0 or abs(score_raw) < 0.30:
        return "LOW" if agree <= 2 else "MEDIUM"
    if agree >= 4 and abs(score_raw) >= 0.50:
        return "HIGH"
    if agree >= 3:
        return "MEDIUM"
    return "LOW"


def _vote_ema(close: pd.Series, e_fast: pd.Series, e_slow: pd.Series) -> int:
    f, s, c = e_fast.iloc[-1], e_slow.iloc[-1], close.iloc[-1]
    if pd.isna(f) or pd.isna(s):
        return 0
    if f > s and c > f:
        return 1
    if f < s and c < f:
        return -1
    if f > s or c > f:
        return 1 if (f - s) / max(s, 1e-9) > 0.001 else 0
    return -1 if (s - f) / max(s, 1e-9) > 0.001 else 0


def _vote_adx(adx_df: pd.DataFrame, min_adx: float = 18.0) -> tuple[int, float]:
    row = adx_df.iloc[-1]
    plus, minus, adx = row["plus_di"], row["minus_di"], row["adx"]
    if pd.isna(adx):
        return 0, 0.0
    denom = plus + minus
    spread = (plus - minus) / denom if denom > 0 else 0.0
    strength = max(0.0, min(1.0, (adx - min_adx) / 25.0))
    if spread > 0.05:
        return (1 if strength > 0 else 0), strength * min(abs(spread) * 2, 1.0)
    if spread < -0.05:
        return (-1 if strength > 0 else 0), strength * min(abs(spread) * 2, 1.0)
    return 0, 0.0


def _vote_macd(macd_df: pd.DataFrame) -> int:
    hist = macd_df["hist"].iloc[-1]
    line, sig = macd_df["macd"].iloc[-1], macd_df["signal"].iloc[-1]
    if pd.isna(hist):
        return 0
    if hist > 0 and line > sig:
        return 1
    if hist < 0 and line < sig:
        return -1
    return 1 if hist > 0 else (-1 if hist < 0 else 0)


def _vote_rsi(rsi_series: pd.Series, period: int = 14) -> tuple[int, float]:
    r = rsi_series.iloc[-1]
    if pd.isna(r):
        return 0, 0.0
    # bias ter-normalisasi: RSI 50 -> 0, RSI 70/30 -> +-1 (clipped)
    bias = max(-1.0, min(1.0, (r - 50.0) / 20.0))
    vote = 1 if r > 55 else (-1 if r < 45 else 0)
    return vote, bias


def _vote_lr(lr_slope: pd.Series, threshold: float = 0.0005) -> tuple[int, float]:
    s = lr_slope.iloc[-1]
    if pd.isna(s):
        return 0, 0.0
    norm = max(-1.0, min(1.0, s / (threshold * 10)))  # ~10x threshold => +-1
    vote = 1 if s > threshold else (-1 if s < -threshold else 0)
    return vote, norm


def compute_streak(df: pd.DataFrame, label: str) -> int:
    """Jumlah candle close terakhir yang konsisten dengan arah label."""
    direction = 1 if "UP" in label else (-1 if "DOWN" in label else 0)
    closes = df["close"].astype(float)
    diffs = closes.diff().dropna()
    if direction == 0:
        # sideways: streak = candle yang bergerak < 0.25 ATR dari prev close
        atr_series = ta.atr(df, 14)
        a = float(atr_series.iloc[-1]) if not pd.isna(atr_series.iloc[-1]) else 0.0
        if a <= 0:
            return 0
        n = 0
        for d in reversed(diffs.tolist()):
            if abs(d) <= 0.25 * a:
                n += 1
            else:
                break
        return n
    n = 0
    for d in reversed(diffs.tolist()):
        if (d > 0 and direction == 1) or (d < 0 and direction == -1):
            n += 1
        else:
            break
    return n


def maturity_from_streak(streak: int, label: str) -> str:
    if "SIDEWAYS" in label:
        return "NONE"
    if streak <= 3:
        return "EARLY"
    if streak <= 8:
        return "MID"
    return "MATURE"


def regime(df: pd.DataFrame, params: dict[str, Any]) -> dict[str, Any]:
    """Regime berdasarkan EMA fast/slow (F1): harga vs EMA20/EMA50."""
    fast_p, slow_p = params["ema_fast"], params["ema_slow"]
    close = df["close"].astype(float)
    e_fast, e_slow = ta.ema(close, fast_p), ta.ema(close, slow_p)
    c, f, s = close.iloc[-1], e_fast.iloc[-1], e_slow.iloc[-1]
    if pd.isna(f) or pd.isna(s):
        return {"name": "UNKNOWN", "ema_fast": None, "ema_slow": None,
                "price_above_fast": None, "price_above_slow": None}
    if c > f > s:
        name = "BULLISH_ALIGNED"
    elif c < f < s:
        name = "BEARISH_ALIGNED"
    elif c > s:
        name = "TRANSITIONAL_BULLISH"
    else:
        name = "TRANSITIONAL_BEARISH"
    return {
        "name": name,
        "ema_fast": round(float(f), 8),
        "ema_slow": round(float(s), 8),
        "price_above_fast": bool(c > f),
        "price_above_slow": bool(c > s),
    }


def analyze_trend(df: pd.DataFrame, params: dict[str, Any]) -> dict[str, Any]:
    """Hitung trend lengkap satu TF. Return dict sesuai data model §4.1.

    Keys: label, score_raw (WAJIB FR-3), confidence, votes, details,
          regime, streak, maturity.
    """
    if len(df) < max(params["ema_slow"], params["lr_period"]) + 2:
        raise ValueError(
            f"data terlalu pendek untuk analisa trend: {len(df)} bar "
            f"(butuh >= {max(params['ema_slow'], params['lr_period']) + 2})")

    # ---- anchor tren persisten (bukan noise/chop) --------------------------
    # Tren sejati: harga berubah NETTO sebanding dengan total jarak yang
    # ditempuh (efficiency ratio / Kaufman). Market sideways bolak-balik punya
    # net kecil tapi total movement besar -> rasio rendah -> skor dipangkas.
    # Catatan: pada data ber-noise, efficiency bisa tinggi karena satu swing
    # acak; karena itu dipakai juga anchor drift bersih per-bar (slope LR
    # ternormalisasi ATR) — sideways murni menghasilkan ~0, uptrend nyata
    # menghasilkan >> ambang.
    lr_p = int(params["lr_period"])
    last_atr_pre = float(ta.atr(df, params["atr_period"]).iloc[-1]) or 1e-9
    close = df["close"].astype(float)
    n = len(close)
    win = close.iloc[-(lr_p + 1):]          # +1 bar utk menghitung diffs window
    w_first, w_last = float(win.iloc[0]), float(win.iloc[-1])
    w_mean = abs(float(win.mean())) or 1e-9
    net_frac = abs(w_last - w_first) / w_mean
    total_movement = float(win.diff().abs().sum())
    efficiency = abs(w_last - w_first) / max(total_movement, 1e-12)  # 0..1
    direction_anchor = 1 if w_last >= w_first else -1
    e_fast = ta.ema(close, params["ema_fast"])
    e_slow = ta.ema(close, params["ema_slow"])
    adx_df = ta.adx(df, params["adx_period"])
    macd_df = ta.macd(close, **params["macd"])
    rsi_s = ta.rsi(close, params["rsi_period"])
    lr_s = ta.linear_regression_slope(close, lr_p)
    # drift bersih per bar, dinormalisasi ATR (bebas dari swing acak sesaat)
    slope_norm = float(lr_s.iloc[-1]) / last_atr_pre if last_atr_pre > 0 else 0.0

    v_ema = _vote_ema(close, e_fast, e_slow)
    v_adx, w_adx = _vote_adx(adx_df)
    v_macd = _vote_macd(macd_df)
    v_rsi, w_rsi = _vote_rsi(rsi_s, params["rsi_period"])
    v_lr, w_lr = _vote_lr(lr_s)
    votes = {"ema": v_ema, "adx": v_adx, "macd": v_macd, "rsi": v_rsi, "lr": v_lr}

    # ---- raw score: rata-rata kontribusi ternormalisasi 5 indikator -------
    last_hist = float(macd_df["hist"].iloc[-1]) if not pd.isna(macd_df["hist"].iloc[-1]) else 0.0
    last_atr = last_atr_pre
    w_macd = max(-1.0, min(1.0, last_hist / (0.25 * last_atr))) if last_atr > 0 else 0.0
    w_ema = 0.0
    if not pd.isna(e_fast.iloc[-1]) and not pd.isna(e_slow.iloc[-1]):
        gap = (e_fast.iloc[-1] - e_slow.iloc[-1]) / max(e_slow.iloc[-1], 1e-9)
        w_ema = max(-1.0, min(1.0, gap / 0.02))  # gap EMA 2% terhadap harga => +-1
    contributions = [w_ema, w_adx, w_macd, w_rsi, w_lr]
    score_raw = sum(contributions) / len(contributions)

    # ---- koreksi anti-chop --------------------------------------------------
    # Anchor tren persisten (net-change vs swing-range window LR + ADX).
    # Pada market sideways, indikator momentum (macd/lr/rsi) bisa kebetulan
    # searah; bila anchor lemah, skor dipangkas agar label jujur.
    adx_last = float(adx_df["adx"].iloc[-1]) if not pd.isna(adx_df["adx"].iloc[-1]) else 0.0
    strong_trend = (abs(slope_norm) >= 0.15 and adx_last >= 22.0)
    moderate_trend = (abs(slope_norm) >= 0.08 and adx_last >= 18.0)
    if not strong_trend:
        trust = 1.0 if moderate_trend else 0.30
        anchored = []
        for val in contributions:
            aligned = ((val > 0) == (direction_anchor > 0)) and abs(val) > 0.05
            anchored.append(val * (max(trust, 0.6) if aligned else trust))
        score_raw = sum(anchored) / len(anchored)
        # chop total: drift bersih sangat kecil => paksa mendekati netral
        if abs(slope_norm) < 0.05 and adx_last < 25.0:
            score_raw *= 0.4
    # voting mayoritas harus tercermin di skor mentah (konsistensi internal),
    # TAPI hanya bila anchor trend juga mendukung arah voting tersebut.
    pos = sum(1 for v in votes.values() if v > 0)
    neg = sum(1 for v in votes.values() if v < 0)
    net_votes = pos - neg
    if abs(net_votes) >= 4 and strong_trend:
        sign = 1 if net_votes > 0 else -1
        if abs(score_raw) < 0.30:
            score_raw = sign * 0.32
    elif abs(net_votes) >= 3 and moderate_trend:
        sign = 1 if net_votes > 0 else -1
        if abs(score_raw) < 0.10:
            score_raw = sign * 0.12
    score_raw = round(max(-1.0, min(1.0, score_raw)), 4)

    label = label_from_score(score_raw)
    confidence = confidence_from_votes(votes, score_raw)
    streak = compute_streak(df, label)

    return {
        "label": label,
        "score_raw": score_raw,                      # FR-3/D-3: WAJIB ada
        "confidence": confidence,
        "votes": votes,
        "regime": regime(df, params),
        "streak": streak,
        "maturity": maturity_from_streak(streak, label),
        "details": {
            "close": round(float(close.iloc[-1]), 8),
            "ema_fast": round(float(e_fast.iloc[-1]), 8) if not pd.isna(e_fast.iloc[-1]) else None,
            "ema_slow": round(float(e_slow.iloc[-1]), 8) if not pd.isna(e_slow.iloc[-1]) else None,
            "rsi": round(float(rsi_s.iloc[-1]), 2) if not pd.isna(rsi_s.iloc[-1]) else None,
            "adx": round(float(adx_df["adx"].iloc[-1]), 2) if not pd.isna(adx_df["adx"].iloc[-1]) else None,
            "plus_di": round(float(adx_df["plus_di"].iloc[-1]), 2),
            "minus_di": round(float(adx_df["minus_di"].iloc[-1]), 2),
            "macd_hist": round(last_hist, 8),
            "lr_slope_norm": round(float(lr_s.iloc[-1]), 6) if not pd.isna(lr_s.iloc[-1]) else None,
            "indicator_weights": {
                "ema": round(w_ema, 4), "adx": round(w_adx, 4),
                "macd": round(w_macd, 4), "rsi": round(w_rsi, 4),
                "lr": round(w_lr, 4),
            },
        },
    }
