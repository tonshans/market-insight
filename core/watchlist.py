# -*- coding: utf-8 -*-
"""CRUD watchlist — YAML + atomic write (F4, NFR-2).

Watchlist disimpan di `state/watchlist.yaml` dan dipakai bersama oleh CLI
dan Telegram bot. Struktur file:

    symbols:
      - symbol: BTC/IDR
        added_utc: "2025-01-15T08:00:00+00:00"
        note: "opsional"

Write selalu atomik (tmp + os.replace via core.yaml_io) supaya file tidak
korup bila proses mati di tengah tulis (penting di RPi / kartu SD).
Simbol dinormalisasi ke format ccxt "BASE/QUOTE" uppercase.
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional

from .time_utils import iso_utc
from .yaml_io import atomic_write_yaml, load_yaml

DEFAULT_WATCHLIST_PATH = "state/watchlist.yaml"

_SYMBOL_RE = re.compile(r"^[A-Z0-9]+$")   # satu leg (base ATAU quote)


def normalize_symbol(raw: str) -> str:
    """'btc/idr', 'BTCIDR ', 'btc-usdt' -> 'BTC/IDR'. Raise ValueError jika invalid."""
    s = (raw or "").strip().upper().replace("-", "/").replace("_", "/")
    if "/" not in s:
        # tebak split 3+3 utk gaya bursa tanpa slash? terlalu ambigu -> tolak
        raise ValueError(f"symbol harus berformat BASE/QUOTE (mis. BTC/IDR): {raw!r}")
    parts = [p for p in s.split("/") if p]
    if len(parts) != 2 or not all(_SYMBOL_RE.match(p) for p in parts):
        raise ValueError(f"symbol tidak valid: {raw!r}")
    return f"{parts[0]}/{parts[1]}"


class Watchlist:
    """Handle satu file watchlist YAML."""

    def __init__(self, path: str = DEFAULT_WATCHLIST_PATH):
        self.path = path
        self._entries: list[dict[str, Any]] = []
        self.load()

    # ------------------------------------------------------------------ io
    def load(self) -> "Watchlist":
        data = load_yaml(self.path, default={}) or {}
        entries = data.get("symbols") or []
        cleaned: list[dict[str, Any]] = []
        for e in entries:
            if isinstance(e, str):  # toleransi bentuk shorthand
                e = {"symbol": e}
            sym = e.get("symbol")
            if not sym:
                continue
            try:
                sym = normalize_symbol(sym)
            except ValueError:
                continue  # lewati entri korup, jangan crash
            item = {"symbol": sym,
                    "added_utc": e.get("added_utc") or iso_utc(),
                    "note": e.get("note")}
            if item["symbol"] not in {c["symbol"] for c in cleaned}:
                cleaned.append(item)
        self._entries = cleaned
        return self

    def save(self) -> None:
        atomic_write_yaml(self.path, {"symbols": self._entries})

    # ------------------------------------------------------------- queries
    @property
    def symbols(self) -> list[str]:
        return [e["symbol"] for e in self._entries]

    def entries(self) -> list[dict[str, Any]]:
        return list(self._entries)

    def __contains__(self, symbol: str) -> bool:
        try:
            return normalize_symbol(symbol) in self.symbols
        except ValueError:
            return False

    def __repr__(self) -> str:  # jangan bocorkan entri saat assert gagal
        return f"<Watchlist {self.path!r} n={len(self._entries)}>"

    def __len__(self) -> int:
        return len(self._entries)

    # ------------------------------------------------------------ mutations
    def add(self, symbol: str, note: Optional[str] = None) -> dict[str, Any]:
        """Tambah simbol (idempotent — update note jika sudah ada)."""
        sym = normalize_symbol(symbol)
        for e in self._entries:
            if e["symbol"] == sym:
                if note is not None:
                    e["note"] = note
                    self.save()
                return e
        entry = {"symbol": sym, "added_utc": iso_utc(), "note": note}
        self._entries.append(entry)
        self.save()
        return entry

    def remove(self, symbol: str) -> bool:
        sym = normalize_symbol(symbol)
        before = len(self._entries)
        self._entries = [e for e in self._entries if e["symbol"] != sym]
        changed = len(self._entries) != before
        if changed:
            self.save()
        return changed

    def clear(self) -> int:
        n = len(self._entries)
        self._entries = []
        self.save()
        return n
