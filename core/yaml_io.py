# -*- coding: utf-8 -*-
"""Atomic YAML read/write helper (NFR-2).

Dipakai oleh core/config.py, core/watchlist.py (WP7), dan bagian bot.yaml.
Menulis via file tmp + os.replace() agar tidak ada partial write bila proses
mati mendadak (penting di Raspberry Pi / SD card).
"""
from __future__ import annotations

import os
import tempfile
from typing import Any

import yaml


def load_yaml(path: str, default: Any = None) -> Any:
    """Load YAML mapping; kembalikan `default` bila file belum ada."""
    if not os.path.exists(path):
        if default is None:
            raise FileNotFoundError(f"yaml file not found: {path}")
        return default
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data if data is not None else (default if default is not None else {})


def atomic_write_yaml(path: str, data: Any) -> None:
    """Tulis YAML secara atomik (tmp file di direktori yg sama + os.replace)."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp_", suffix=".yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, sort_keys=False, allow_unicode=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)  # atomik di POSIX
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
