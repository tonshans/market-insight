# -*- coding: utf-8 -*-
"""ccxt wrapper + Circuit Breaker (FR-2, F5, ARCHITECTURE.md §3.1 & §5.1).

4-level resolution untuk setiap permintaan OHLCV:
    1. CACHE   : Parquet HIT (umur < TTL)            -> source="cache"
    2. NETWORK : fetch live via ccxt                 -> source="network"
    3. FALLBACK: cache STALE dipakai krn network gagal
                 ATAU circuit breaker sedang cooldown -> source="fallback_stale"|"cooldown_stale"
    4. STALE/ERROR: tidak ada apa-apa -> FetchError

CIRCUIT BREAKER (§5.1):
    - failure_threshold kegagalan *berturut-turut* dalam failure_window_seconds
      -> trigger cooldown cooldown_seconds (default 3x dlm 60s -> pause 5 menit).
    - Selama cooldown: TIDAK ada request jaringan; return cache lama + flag
      API_COOLDOWN pada result (bot menampilkan warning ke user, PRD FR-2).
    - Satu sukses me-reset counter.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from .cache import ParquetCache, STATUS_HIT
from .indicators import ohlcv_to_dataframe
from .time_utils import validate_timeframe

logger = logging.getLogger("market_tools.fetch")

# ------------------------------------------------------------------ sources
SOURCE_CACHE = "cache"
SOURCE_NETWORK = "network"
SOURCE_FALLBACK_STALE = "fallback_stale"   # network gagal -> pakai stale
SOURCE_COOLDOWN_STALE = "cooldown_stale"   # breaker open -> pakai stale


class FetchError(Exception):
    """Tidak ada data sama sekali (network gagal DAN tidak ada cache)."""

    def __init__(self, message: str, cooldown_active: bool = False):
        super().__init__(message)
        self.cooldown_active = cooldown_active


@dataclass
class FetchResult:
    symbol: str
    timeframe: str
    df: pd.DataFrame
    source: str                 # lihat konstanta SOURCE_* di atas
    age_seconds: float = 0.0    # umur data (0 utk fresh network)
    cooldown_active: bool = False
    warnings: list = field(default_factory=list)

    @property
    def is_live(self) -> bool:
        return self.source == SOURCE_NETWORK


# ------------------------------------------------------------- cb classification
def _is_breaker_failure(exc: Exception) -> bool:
    """Hitung sebagai kegagalan exchange hanya utk network/5xx (PRD FR-2).

    Error request/permintaan salah (4xx, invalid symbol) TIDAK memicu breaker.
    """
    try:
        import ccxt
    except Exception:  # pragma: no cover - ccxt selalu ada di runtime
        return True

    # Network-level & server-side -> dihitung.
    if isinstance(exc, (ccxt.NetworkError, ccxt.ExchangeNotAvailable,
                        ccxt.RequestTimeout, ccxt.DDoSProtection)):
        return True
    # Client-side / konfigurasi -> tidak dihitung.
    if isinstance(exc, (ccxt.BadSymbol, ccxt.AuthenticationError,
                        ccxt.InsufficientFunds, ccxt.PermissionDenied,
                        ccxt.BadRequest, ccxt.NotSupported)):
        return False
    # requests-style errors juga dianggap kegagalan jaringan.
    try:
        import requests
        if isinstance(exc, (requests.exceptions.ConnectionError,
                            requests.exceptions.Timeout)):
            return True
    except Exception:
        pass
    http_code = getattr(exc, "http_status", None) or getattr(exc, "status_code", None)
    if isinstance(http_code, int) and http_code >= 500:
        return True
    # Default konservatif: hitung sebagai kegagalan.
    return True


class CircuitBreaker:
    """In-memory breaker: N kegagalan beruntun dalam window -> cooldown."""

    def __init__(self, failure_threshold: int = 3,
                 failure_window_seconds: int = 60,
                 cooldown_seconds: int = 300):
        self.failure_threshold = int(failure_threshold)
        self.failure_window_seconds = int(failure_window_seconds)
        self.cooldown_seconds = int(cooldown_seconds)

        self._lock = threading.Lock()
        self._failures: list[float] = []     # timestamp kegagalan (beruntun)
        self._cooldown_until: float = 0.0
        self.trigger_count = 0               # utk observability /status bot

    # ------------------------------------------------------------------ state
    @property
    def in_cooldown(self) -> bool:
        with self._lock:
            return time.time() < self._cooldown_until

    def remaining_cooldown(self) -> float:
        with self._lock:
            return max(0.0, self._cooldown_until - time.time())

    def status(self) -> dict:
        return {
            "in_cooldown": self.in_cooldown,
            "remaining_seconds": round(self.remaining_cooldown(), 1),
            "consecutive_failures": len(self.recent_failures()),
            "trigger_count": self.trigger_count,
        }

    def recent_failures(self) -> list[float]:
        cutoff = time.time() - self.failure_window_seconds
        with self._lock:
            return [t for t in self._failures if t >= cutoff]

    # ----------------------------------------------------------------- events
    def record_success(self) -> None:
        with self._lock:
            self._failures.clear()

    def record_failure(self) -> bool:
        """Return True jika kegagalan ini MENTRIGGER cooldown."""
        now = time.time()
        with self._lock:
            cutoff = now - self.failure_window_seconds
            self._failures = [t for t in self._failures if t >= cutoff]
            self._failures.append(now)
            triggered = len(self._failures) >= self.failure_threshold
            if triggered:
                self._cooldown_until = now + self.cooldown_seconds
                self._failures.clear()
                self.trigger_count += 1
        if triggered:
            logger.warning(
                "Exchange API unresponsive. Cooldown %d minutes.",
                self.cooldown_seconds // 60,
            )
        return triggered


class MarketDataFetcher:
    """Facade: config-aware OHLCV fetch dengan cache + circuit breaker."""

    def __init__(self, config, cache: Optional[ParquetCache] = None,
                 exchange=None, breaker: Optional[CircuitBreaker] = None):
        self.config = config
        self.cache = cache if cache is not None else ParquetCache(config.cache_dir)
        self.breaker = breaker if breaker is not None else CircuitBreaker(
            **config.circuit_breaker)
        self._exchange = exchange          # lazy-inject utk test/mock
        self._markets: Optional[dict] = None

    # --------------------------------------------------------------- exchange
    def get_exchange(self):
        if self._exchange is None:
            import ccxt  # import lokal: tetap bisa di-test tanpa ccxt instance
            klass = getattr(ccxt, self.config.exchange_id)
            self._exchange = klass({
                "enableRateLimit": bool(self.config.get("exchange.enable_rate_limit", True)),
            })
        return self._exchange

    def set_exchange(self, exchange) -> None:
        """Inject mock/exchange lain (testing/resilience drill)."""
        self._exchange = exchange
        self._markets = None

    # ----------------------------------------------------------------- markets
    def load_markets(self, force: bool = False) -> dict:
        if self._markets is None or force:
            mkts = self.get_exchange().load_markets()
            # Beberapa exchange (mis. indodax via ccxt) TIDAK mengisi
            # quoteVolume di load_markets -> universe filter akan membuang
            # semua pair saat min_quote_volume_24h > 0. Kaya dengan satu
            # panggilan fetch_tickers() hemat-rate-limit; kegagalan di sini
            # tidak fatal (qvol dianggap 0 -> fallback max_symbols per likuiditas
            # tetap jalan karena build_universe memakai nilai info/quoteVolume).
            try:
                if getattr(self.get_exchange(), "has", {}).get("fetchTickers"):
                    tickers = self.get_exchange().fetch_tickers()
                    for sym, t in (tickers or {}).items():
                        if sym in mkts and isinstance(t, dict):
                            qv = t.get("quoteVolume")
                            if qv is not None and not mkts[sym].get("quoteVolume"):
                                mkts[sym]["quoteVolume"] = qv
            except Exception as exc:
                logger.warning("fetch_tickers untuk qvol gagal (%s); "
                               "universe tanpa filter volume.", type(exc).__name__)
            self._markets = mkts
        return self._markets

    # ------------------------------------------------------------------- fetch
    def fetch_ohlcv(self, symbol: str, timeframe: str,
                    limit: Optional[int] = None,
                    allow_stale_fallback: bool = True) -> FetchResult:
        """4-level resolution: Cache -> Network -> Fallback -> Stale/Error."""
        validate_timeframe(timeframe)  # validasi format global (TimeframeError)
        limit = int(limit or self.config.max_candles)
        ttl = self.config.cache_ttl_seconds(timeframe)
        ex_id = self.config.exchange_id

        # ---- level 1: cache fresh ----------------------------------------
        cached = self.cache.get(ex_id, symbol, timeframe, ttl)
        if cached.status == STATUS_HIT:
            return FetchResult(symbol, timeframe, cached.df, SOURCE_CACHE,
                               age_seconds=cached.age_seconds)

        # ---- circuit breaker gate ----------------------------------------
        if self.breaker.in_cooldown:
            warn = (f"API_COOLDOWN: exchange sedang dalam cooldown "
                    f"({self.breaker.remaining_cooldown():.0f}s tersisa).")
            if cached.df is not None and allow_stale_fallback:
                return FetchResult(symbol, timeframe, cached.df,
                                   SOURCE_COOLDOWN_STALE,
                                   age_seconds=cached.age_seconds,
                                   cooldown_active=True, warnings=[warn])
            raise FetchError(warn + " Tidak ada cache tersedia.",
                             cooldown_active=True)

        # ---- level 2: network --------------------------------------------
        try:
            raw = self.get_exchange().fetch_ohlcv(
                symbol, timeframe=timeframe, limit=limit)
            sleep_s = self.config.request_sleep_ms / 1000.0
            if sleep_s > 0:
                time.sleep(sleep_s)          # R-1: hormati rate limit Indodax
            df = ohlcv_to_dataframe(raw)
            if df.empty:
                raise RuntimeError(f"empty OHLCV for {symbol} {timeframe}")
            self.breaker.record_success()
            self.cache.put(ex_id, symbol, timeframe, df,
                           max_candles=self.config.max_candles)
            return FetchResult(symbol, timeframe, df, SOURCE_NETWORK)
        except Exception as exc:
            counted = _is_breaker_failure(exc)
            if counted:
                triggered = self.breaker.record_failure()
            else:
                triggered = False

            # ---- level 3: fallback stale ---------------------------------
            if cached.df is not None and allow_stale_fallback:
                warn = f"Network gagal ({type(exc).__name__}); memakai cache lama."
                if triggered:
                    warn = ("API_COOLDOWN: circuit breaker baru aktif "
                            f"({self.config.circuit_breaker['cooldown_seconds']}s pause). "
                            + warn)
                logger.warning("fetch %s %s failed: %s -> stale fallback",
                               symbol, timeframe, exc)
                return FetchResult(symbol, timeframe, cached.df,
                                   SOURCE_FALLBACK_STALE,
                                   age_seconds=cached.age_seconds,
                                   cooldown_active=triggered or self.breaker.in_cooldown,
                                   warnings=[warn])

            # ---- level 4: error ------------------------------------------
            logger.error("fetch %s %s failed dan tidak ada cache: %s",
                         symbol, timeframe, exc)
            raise FetchError(
                f"Gagal fetch {symbol} {timeframe}: {exc}",
                cooldown_active=self.breaker.in_cooldown,
            ) from exc

    # ------------------------------------------------------------- convenience
    def fetch_many(self, symbols: list[str], timeframe: str,
                   limit: Optional[int] = None) -> dict[str, FetchResult | FetchError]:
        """Fetch beberapa symbol sequential (sleep per-request sudah built-in).

        Return mapping symbol -> FetchResult atau FetchError. Pada saat
        cooldown aktif, symbol berikutnya langsung jatuh ke stale/error
        tanpa percobaan jaringan (hemat resource RPi — D-4).
        """
        out: dict = {}
        for sym in symbols:
            try:
                out[sym] = self.fetch_ohlcv(sym, timeframe, limit=limit)
            except FetchError as exc:
                out[sym] = exc
        return out

    def market_status(self) -> dict:
        return {"circuit_breaker": self.breaker.status(),
                "cache": self.cache.stats()}
