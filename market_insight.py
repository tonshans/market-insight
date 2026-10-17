#!/usr/bin/env python3
"""
market_insight.py — CLI Analisa Single Pair Multi-Timeframe (F1+F2, WP7)

Contoh:
    python market_insight.py BTC/IDR
    python market_insight.py ETH/IDR --tf 1h 4h 1d --level 2
    python market_insight.py SOL/IDR --json
    python market_insight.py BTC/IDR --compact --no-emoji        # payload LLM / bot
    python market_insight.py "btc/idr" --exchange indodax --config config.yaml \
        --override analysis.atr_period=16                        # override config via CLI (FR-1)

Exit code: 0 sukses, 2 argumen/konfigurasi salah, 3 gagal fetch/analisa.
"""
from __future__ import annotations

import argparse
import json
import sys

from core.config import ConfigError, load_config
from core.fetch import FetchError
from core.json_export import build_json
from core.text_export import render


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="market_insight",
        description="Analisa market multi-timeframe untuk trader retail (Fase 1: observasi).",
    )
    p.add_argument("symbol", help="Simbol pair, mis. BTC/IDR (dinormalisasi otomatis)")
    p.add_argument("--tf", nargs="+", default=None, metavar="TF",
                   help="Timeframe utk confluence, mis. --tf 1h 4h 1d (default dari config)")
    p.add_argument("--exchange", default=None, help="ID exchange ccxt (default dari config)")
    p.add_argument("--level", type=int, choices=[1, 2], default=1,
                   help="Detail output text: 1 ringkas (<10 dtk), 2 lengkap (default 1)")
    fmt = p.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true", help="Output JSON schema v1.0.0 (stdout)")
    fmt.add_argument("--compact", action="store_true",
                     help="JSON compact tanpa recent bars (hemat token utk LLM)")
    p.add_argument("--out", default=None, metavar="FILE",
                   help="Tulis hasil (json/text) ke file")
    p.add_argument("--no-color", action="store_true", help="Matikan ANSI color")
    p.add_argument("--color", action="store_true", help="Paksa ANSI color")
    p.add_argument("--no-emoji", action="store_true", help="Tanpa emoji (mis. terminal minimalis)")
    p.add_argument("--config", default="config.yaml", help="Path config.yaml utama")
    p.add_argument("--override", action="append", default=[], metavar="KEY=VALUE",
                   help="Override config dotted-key, bisa berulang. Mis: "
                        "--override cache.ttl_by_tf.1h=60 --override analysis.atr_period=16")
    return p


def _fmt_exc(e: Exception) -> str:
    cause = e.__cause__
    if cause is not None and str(cause):
        return f"{e} [{type(cause).__name__}: {cause}]"
    return str(e)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    # --- Load config + CLI overrides (FR-1) ---
    try:
        overrides = {}
        for item in args.override:
            if "=" not in item:
                print(f"error: --override harus KEY=VALUE, dapat: {item!r}", file=sys.stderr)
                return 2
            k, _, v = item.partition("=")
            overrides[k.strip()] = v.strip()
        cfg = load_config(args.config, overrides=overrides or None)
    except ConfigError as e:
        print(f"error konfigurasi: {_fmt_exc(e)}", file=sys.stderr)
        return 2

    # --- Validasi TF dini (pesan ramah sebelum menyentuh jaringan) ---
    if args.tf:
        from core.time_utils import TimeframeError, validate_timeframe
        bad = []
        normed = []
        for t in args.tf:
            try:
                normed.append(validate_timeframe(t))
            except TimeframeError:
                bad.append(t)
        if bad:
            print(f"error: timeframe tidak valid: {', '.join(bad)} "
                  f"(contoh valid: 15m 1h 4h 1d)", file=sys.stderr)
            return 2
        args.tf = normed

    # --- Analisa (fetch + engine + confluence) ---
    try:
        from core.analysis import analyze_symbol
        result = analyze_symbol(
            args.symbol,
            timeframes=args.tf or cfg.default_timeframes,
            config=cfg,
            exchange_id=args.exchange,
        )
    except (ValueError, FetchError) as e:
        print(f"error analisa {args.symbol}: {_fmt_exc(e)}", file=sys.stderr)
        return 3

    # --- Render output ---
    if args.json or args.compact:
        payload = build_json(result, include_recent_bars=not args.compact)
        text = json.dumps(payload, ensure_ascii=False,
                          indent=None if args.compact else 2)
    else:
        use_color = None
        if args.no_color:
            use_color = False
        elif args.color:
            use_color = True
        text = render(result, level=args.level,
                      ansi=use_color, emoji=not args.no_emoji)

    print(text)
    if args.out:
        try:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(text + "\n")
            print(f"[saved] {args.out}", file=sys.stderr)
        except OSError as e:
            print(f"error menulis {args.out}: {e}", file=sys.stderr)
            return 3

    # Warning fetch-level (mis. API_COOLDOWN / STALE_FALLBACK) tetap tampil di stderr
    for w in (result.get("meta", {}).get("fetch", {}) or {}).get("warnings", []):
        print(f"[fetch-warning] {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
