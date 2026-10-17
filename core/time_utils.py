# -*- coding: utf-8 -*-
"""Time utilities: WITA/UTC conversion & timeframe validation.

Bagian dari WP2 (Core Utils). Tidak ada dependensi ke modul core lain.
Zona waktu ditampilkan dalam WITA (Asia/Makassar, UTC+8) untuk kenyamanan
trader Indonesia; seluruh penyimpanan data memakai UTC (ISO-8601).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

try:  # Python 3.9+ punya zoneinfo di stdlib
    from zoneinfo import ZoneInfo

    _WITA = ZoneInfo("Asia/Makassar")
except Exception:  # pragma: no cover - fallback utk lingkungan tanpa tzdata
    _WITA = timezone(timedelta(hours=8), name="WITA")

UTC = timezone.utc

# TF yang didukung (harus konsisten dgn config.yaml timeframes.supported)
SUPPORTED_TIMEFRAMES = ["5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w"]

_MS_IN_HOUR = 3_600_000


class TimeframeError(ValueError):
    """Raised when a timeframe string is not supported."""


def validate_timeframe(tf: str) -> str:
    """Validasi string timeframe ('1h', '4h', '1d', ...).

    Returns normalized lowercase string; raises TimeframeError jika invalid.
    """
    if not isinstance(tf, str):
        raise TimeframeError(f"timeframe must be str, got {type(tf).__name__}")
    norm = tf.strip().lower()
    if norm not in SUPPORTED_TIMEFRAMES:
        raise TimeframeError(
            f"unsupported timeframe {tf!r}; supported: {', '.join(SUPPORTED_TIMEFRAMES)}"
        )
    return norm


def timeframe_to_ms(tf: str) -> int:
    """Konversi timeframe ke durasi milidetik (untuk TTL & staleness check)."""
    tf = validate_timeframe(tf)
    unit, n = tf[-1], int(tf[:-1])
    if unit == "m":
        return n * 60_000
    if unit == "h":
        return n * _MS_IN_HOUR
    if unit == "d":
        return n * 24 * _MS_IN_HOUR
    if unit == "w":
        return n * 7 * 24 * _MS_IN_HOUR
    raise TimeframeError(f"cannot convert timeframe {tf!r}")  # pragma: no cover


def now_utc() -> datetime:
    """Current time, timezone-aware UTC."""
    return datetime.now(timezone.utc)


def to_utc(dt: datetime) -> datetime:
    """Normalize a datetime to timezone-aware UTC (naive assumed UTC)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_wita(dt: datetime) -> datetime:
    """Convert any aware/naive(=UTC) datetime to WITA (UTC+8)."""
    return to_utc(dt).astimezone(_WITA)


def iso_utc(dt: datetime | None = None) -> str:
    """ISO-8601 string in UTC (format penyimpanan SQLite)."""
    return (to_utc(dt) if dt is not None else now_utc()).strftime("%Y-%m-%dT%H:%M:%SZ")


def human_wita(dt: datetime | None = None) -> str:
    """Format tampilan manusia dalam WITA, mis. '17-10 20:15 WITA'."""
    d = to_wita(dt) if dt is not None else to_wita(now_utc())
    return d.strftime("%d-%m %H:%M WITA")


def ms_to_wita(ms: int) -> str:
    """Timestamp epoch-millisecond (OHLCV ccxt) ke string WITA."""
    return human_wita(datetime.fromtimestamp(ms / 1000, tz=timezone.utc))


def candle_is_closed(candle: list, tf: str, now: datetime | None = None) -> bool:
    """True bila candle OHLCV terakhir sudah close (dibuka + durasi TF <= sekarang)."""
    now = to_utc(now) if now is not None else now_utc()
    open_ms = int(candle[0])
    close_ms = open_ms + timeframe_to_ms(tf)
    return now.timestamp() * 1000 >= close_ms
