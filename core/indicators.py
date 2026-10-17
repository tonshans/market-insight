# -*- coding: utf-8 -*-
"""Technical indicators computed from an OHLCV pandas DataFrame.

Bagian dari WP2 (Core Utils). Konvensi:
- Input `df` minimal berisi kolom: ts, open, high, low, close, volume
  (bentuk standar hasil ccxt.fetch_ohlcv yang dikonversi ke DataFrame).
- Semua fungsi mengembalikan Series/DataFrame dengan index sama dengan df,
  NaN pada warm-up period (dibiarkan, bukan diisi nol — penting agar voting
  indikator tidak bias saat data pendek).
- Murni numpy/pandas, tanpa dependensi TA-Lib (portable ke Raspberry Pi).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _check(df: pd.DataFrame, min_len: int = 1) -> None:
    required = {"open", "high", "low", "close"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"OHLCV dataframe missing columns: {sorted(missing)}")
    if len(df) < min_len:
        raise ValueError(f"dataframe too short: need >= {min_len} rows, got {len(df)}")


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average."""
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple moving average."""
    return series.rolling(period, min_periods=period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index (Wilder's smoothing)."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100 - (100 / (1 + rs))
    # Kasus khusus: avg_loss == 0 (tidak ada penurunan sama sekali).
    # - avg_gain > 0  -> RSI = 100 (uptrend murni).
    # - avg_gain == 0 -> harga flat/sideways; RSI klasik didefinisikan 50
    #   (netral), BUKAN 100. Mengembalikan 100 di sini memicu vote bullish
    #   palsu pada data sideways.
    no_loss = avg_loss == 0
    out = out.mask(no_loss & (avg_gain == 0), 50.0)
    out = out.mask(no_loss & (avg_gain > 0), 100.0)
    return out.where(avg_gain.notna() | avg_loss.notna(), np.nan)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """MACD line, signal line & histogram."""
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return pd.DataFrame(
        {"macd": macd_line, "signal": signal_line, "hist": macd_line - signal_line}
    )


def true_range(df: pd.DataFrame) -> pd.Series:
    """True Range per bar."""
    prev_close = df["close"].shift(1)
    ranges = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range (Wilder's smoothing)."""
    _check(df)
    return true_range(df).ewm(alpha=1 / period, adjust=False).mean()


def adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Average Directional Index dengan +DI/-DI.

    Returns DataFrame kolom: plus_di, minus_di, adx.
    """
    _check(df, min_len=period + 1)
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)

    alpha = 1 / period
    tr_smooth = true_range(df).ewm(alpha=alpha, adjust=False).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=alpha, adjust=False).mean() / tr_smooth)
    minus_di = 100 * (minus_dm.ewm(alpha=alpha, adjust=False).mean() / tr_smooth)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    adx_series = dx.ewm(alpha=alpha, adjust=False).mean()
    return pd.DataFrame({"plus_di": plus_di, "minus_di": minus_di, "adx": adx_series})


def linear_regression_slope(series: pd.Series, period: int = 20) -> pd.Series:
    """Normalized slope of linear regression over rolling window.

    Di-normalisasi terhadap mean harga window sehingga nilainya scale-free
    (mis. 0.01 = naik ~1% per bar secara rata-rata). Berguna utk LR vote.
    """
    x = np.arange(period, dtype=float)
    x_mean = x.mean()
    denom = ((x - x_mean) ** 2).sum()

    def _slope(window: np.ndarray) -> float:
        y_mean = window.mean()
        if y_mean == 0:
            return np.nan
        beta = ((x - x_mean) * (window - y_mean)).sum() / denom
        return beta / y_mean  # normalized per-bar slope

    return series.rolling(period, min_periods=period).apply(_slope, raw=True)


def bollinger_bands(close: pd.Series, period: int = 20, num_std: float = 2.0) -> pd.DataFrame:
    """Bollinger bands (mid/upper/lower + %B position)."""
    mid = sma(close, period)
    std = close.rolling(period, min_periods=period).std(ddof=0)
    upper = mid + num_std * std
    lower = mid - num_std * std
    width = (upper - lower).replace(0.0, np.nan)
    pct_b = (close - lower) / width
    return pd.DataFrame(
        {"mid": mid, "upper": upper, "lower": lower, "pct_b": pct_b, "bandwidth": width / mid}
    )


def ohlcv_to_dataframe(ohlcv: list) -> pd.DataFrame:
    """Konversi hasil ccxt.fetch_ohlcv (list of [ts,o,h,l,c,v]) ke DataFrame."""
    cols = ["ts", "open", "high", "low", "close", "volume"]
    df = pd.DataFrame(ohlcv, columns=cols)
    if not df.empty:
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df
