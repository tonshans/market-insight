# -*- coding: utf-8 -*-
"""Build JSON schema v1.0.0 untuk konsumsi LLM / Fase 2 (ARCH §3.4, D-3).

Prinsip:
- Self-describing: selalu ada `schema_version` & `generated_at_utc`.
- WAJIB menyertakan raw scores yang dijanjikan PRD/ARCH:
    * summary.trend_scores_raw.{tf}      (alias dari trend.score_raw)
    * timeframes.{tf}.trend.score_raw
    * timeframes.{tf}.volume.vsa_score
    * timeframes.{tf}.price_action.wick_rejection_score
- Output harus json.dump-able (datetime/np types dikonversi aman).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pandas as pd

SCHEMA_VERSION = "1.0.0"


def _py(obj: Any) -> Any:
    """Konversi rekursif tipe numpy/pandas/datetime agar JSON-safe."""
    if isinstance(obj, dict):
        return {str(k): _py(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_py(v) for v in obj]
    if isinstance(obj, (pd.Timestamp, datetime)):
        return obj.isoformat()
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, (int, float)):
        # buang NaN/Inf yang tidak valid di JSON strict
        if obj != obj or obj in (float("inf"), float("-inf")):
            return None
        return obj
    try:
        import numpy as _np
        if isinstance(obj, _np.integer):
            return int(obj)
        if isinstance(obj, _np.floating):
            f = float(obj)
            return None if f != f else f
        if isinstance(obj, _np.bool_):
            return bool(obj)
    except ImportError:  # pragma: no cover
        pass
    if obj is None or isinstance(obj, str):
        return obj
    return str(obj)


def build_json(result: dict[str, Any], include_recent_bars: bool = True,
               extra_meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Bangun payload JSON final dari hasil core.analysis.analyze_symbol."""
    summary = dict(result.get("summary", {}))
    tfs = result.get("timeframes", {})

    tf_out: dict[str, Any] = {}
    for tf, r in tfs.items():
        trend = dict(r.get("trend", {}))
        volume = dict(r.get("volume", {}))
        pa = dict(r.get("price_action", {}))
        if not include_recent_bars:
            volume.pop("recent_bars", None)
            pa.pop("rejection_bars", None)
        tf_out[tf] = {
            "last_close": r.get("last_close"),
            "last_ts": r.get("last_ts"),
            "trend": {
                "label": trend.get("label"),
                "score_raw": trend.get("score_raw"),      # FR-3: WAJIB
                "confidence": trend.get("confidence"),
                "votes": trend.get("votes"),
                "regime": trend.get("regime"),
                "streak": trend.get("streak"),
                "maturity": trend.get("maturity"),
                "details": trend.get("details"),
            },
            "sr": r.get("sr", {}),
            "risk": r.get("risk", {}),
            "invalidation": {
                "level": (r.get("risk") or {}).get("invalidation"),
                "type": (r.get("risk") or {}).get("stop_type"),
                "min_atr_ok": (r.get("risk") or {}).get("stop_type")
                              != "NEAREST_SWING_BELOW_MIN_ATR",
            },
            "volume": {
                "avg": volume.get("avg"),
                "last": volume.get("last"),
                "ratio": volume.get("ratio"),
                "state": volume.get("state"),
                "vsa_score": volume.get("vsa_score"),     # ARCH §3.4: WAJIB
                "pattern_counts": volume.get("pattern_counts"),
                "recent_bars": volume.get("recent_bars"),
            },
            "price_action": {
                "range_position": pa.get("range_position"),
                "last_bar": pa.get("last_bar"),
                "rejection_bars": pa.get("rejection_bars"),
                "wick_rejection_score": pa.get("wick_rejection_score"),  # WAJIB
                "interpretation": pa.get("interpretation"),
            },
        }

    payload = {
        "schema_version": result.get("schema_version", SCHEMA_VERSION),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "summary": {
            **{k: v for k, v in summary.items() if k != "warnings"},
            "raw_score_contract": {
                "trend_score_raw_range": [-1.0, 1.0],
                "vsa_score_range": [-1.0, 1.0],
                "wick_rejection_score_range": [-1.0, 1.0],
            },
        },
        "warnings": summary.get("warnings", []),
        "timeframes": tf_out,
        "meta": {
            "fetch": result.get("meta", {}).get("fetch", {}),
            "params": result.get("meta", {}).get("params", {}),
            **(extra_meta or {}),
        },
    }
    return _py(payload)


def dumps(result: dict[str, Any], include_recent_bars: bool = True,
          indent: int | None = 2, extra_meta: dict | None = None) -> str:
    """Helper satu-baris: build + serialisasi string JSON."""
    return json.dumps(build_json(result, include_recent_bars=include_recent_bars,
                                 extra_meta=extra_meta),
                      indent=indent, ensure_ascii=False, allow_nan=False)
