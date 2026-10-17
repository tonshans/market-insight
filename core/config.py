# -*- coding: utf-8 -*-
"""Load/validate config.yaml & scanner.yaml, deep-merge CLI overrides.

Bagian dari WP1 (Foundation). Prinsip "Config-first" (ARCHITECTURE.md §1):
modul core lain selalu menerima nilai dari objek Config, tidak membaca file
langsung dan tidak memakai magic number tersembunyi.
"""
from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = "config.yaml"
DEFAULT_SCANNER_PATH = "scanner.yaml"


class ConfigError(Exception):
    """Raised when a configuration file is missing or invalid."""


def _deep_merge(base: dict, override: dict) -> dict:
    """Rekursif merge `override` ke `base` (mengembalikan salinan baru).

    Nilai berprefix '@' diperlakukan sebagai literal string (untuk override
    nilai yang memang berupa dict, mis. '@{"a": 1}').
    """
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, str) and value.startswith("@"):
            # literal: coba parse sbg JSON, kalau gagal pakai teksnya apa adanya
            try:
                value = json.loads(value[1:])
            except (ValueError, TypeError):
                value = value[1:]
            out[key] = value
        elif isinstance(value, str) and "." in key:
            # dotted key dalam override dict -> expand ke nested dict.
            # Heuristik prefix CLI (FR-1): key "scanner.ranking.top_n" bisa
            # berarti (a) cabang scanner.* DI DALAM file ini, atau (b) path
            # relatif terhadap file target (scanner.yaml top-level = ranking).
            # Pilih interpretasi yang cocok dgn struktur file; kalau tidak ada,
            # pakai bentuk nested penuh (buat cabang baru).
            parts = key.split(".")
            if len(parts) > 1 and all(
                    not isinstance(out.get(p), dict) for p in parts[:-1]):
                # tidak ada bagian awal yang berupa dict di file -> perlakukan
                # sebagai sub-path dari kunci pertama (buang prefix)
                parts = parts[1:]
            node = out
            for p in parts[:-1]:
                nxt = node.get(p)
                if not isinstance(nxt, dict):
                    nxt = {}
                    node[p] = nxt
                node = nxt
            node[parts[-1]] = _coerce_scalar(value)
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _coerce_scalar(s: str):
    """Konversi string CLI -> int/float/bool bila mungkin; selain itu tetap str."""
    low = s.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none"):
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def _load_yaml(path: str) -> dict:
    if not os.path.exists(path):
        raise ConfigError(f"config file not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ConfigError(f"config file must be a YAML mapping: {path}")
    return data


@dataclass
class Config:
    """Terbaca-friendly wrapper atas config.yaml (+ override per-run)."""

    raw: dict = field(default_factory=dict)

    # --- accessor dengan dotted path: get("cache.ttl_minutes.1h", 30) ---
    def get(self, dotted_key: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in dotted_key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    # --- typed convenience accessors ---
    @property
    def exchange_id(self) -> str:
        return self.get("exchange.id", "indodax")

    @property
    def quote_currency(self) -> str:
        return self.get("exchange.quote_currency", "IDR")

    @property
    def request_sleep_ms(self) -> int:
        return int(self.get("exchange.request_sleep_ms", 200))

    @property
    def default_timeframes(self) -> list[str]:
        return list(self.get("timeframes.default", ["1h", "4h", "1d"]))

    @property
    def supported_timeframes(self) -> list[str]:
        return list(self.get("timeframes.supported", []))

    @property
    def cache_dir(self) -> str:
        return self.get("cache.dir", "cache")

    def cache_ttl_seconds(self, tf: str) -> int:
        table = self.get("cache.ttl_minutes", {}) or {}
        minutes = table.get(tf, self.get("cache.default_ttl_minutes", 30))
        return int(minutes) * 60

    @property
    def max_candles(self) -> int:
        return int(self.get("cache.max_candles", 300))

    @property
    def circuit_breaker(self) -> dict:
        return {
            "failure_threshold": int(self.get("circuit_breaker.failure_threshold", 3)),
            "failure_window_seconds": int(self.get("circuit_breaker.failure_window_seconds", 60)),
            "cooldown_seconds": int(self.get("circuit_breaker.cooldown_seconds", 300)),
        }

    @property
    def db_path(self) -> str:
        return self.get("state.db_path", "state/market_tools.db")

    @property
    def analysis(self) -> dict:
        defaults = {
            "ema_fast": 20,
            "ema_slow": 50,
            "atr_period": 14,
            "adx_period": 14,
            "rsi_period": 14,
            "macd": {"fast": 12, "slow": 26, "signal": 9},
            "lr_period": 20,
            "swing_lookback": 20,
            "min_atr_distance": 0.5,
            "volume_avg_period": 20,
        }
        return _deep_merge(defaults, self.get("analysis", {}) or {})


@dataclass
class ScannerConfig:
    """Wrapper scanner.yaml (universe filter, pre-filter ADX, bobot skor)."""

    raw: dict = field(default_factory=dict)

    def get(self, dotted_key: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in dotted_key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @property
    def universe(self) -> dict:
        return {
            "quote_currency": self.get("universe.quote_currency", "IDR"),
            "exclude_bases": self.get("universe.exclude_bases", []),
            "min_quote_volume_24h": float(self.get("universe.min_quote_volume_24h", 0)),
            "max_symbols": int(self.get("universe.max_symbols", 200)),
        }

    @property
    def prefilter(self) -> dict:
        return {
            "adx_period": int(self.get("prefilter.adx_period", 14)),
            "min_adx": float(self.get("prefilter.min_adx", 15)),
            "timeframe": self.get("prefilter.timeframe", "4h"),
        }

    @property
    def scoring(self) -> dict:
        return self.get("scoring", {}) or {}

    @property
    def ranking(self) -> dict:
        defaults = {"risk_penalty": 0.5, "top_n": 15, "persist_history": True}
        return _deep_merge(defaults, self.get("ranking", {}) or {})


def load_config(
    path: str = DEFAULT_CONFIG_PATH,
    overrides: dict | None = None,
) -> Config:
    """Load config.yaml lalu apply CLI overrides (FR-1)."""
    data = _load_yaml(path)
    if overrides:
        data = _deep_merge(data, overrides)
    return Config(raw=data)


def load_scanner_config(path: str = DEFAULT_SCANNER_PATH, overrides: dict | None = None) -> ScannerConfig:
    """Load scanner.yaml lalu apply CLI overrides."""
    data = _load_yaml(path)
    if overrides:
        data = _deep_merge(data, overrides)
    return ScannerConfig(raw=data)
