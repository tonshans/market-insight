#!/usr/bin/env python3
"""
scan.py — CLI Scanner Multi-Pair (F3, WP7)

Contoh:
    python scan.py                                  # full universe scan + persist ke SQLite
    python scan.py --watchlist                      # hanya pair di watchlist
    python scan.py --limit 10 --tf 4h               # 10 kandidat teratas, TF analisa 4h
    python scan.py --no-save --json                 # tidak menulis riwayat, output JSON
    python scan.py --top 5 --no-emoji
    python scan.py --override scanner.scoring.weights.trend=0.5   # override config via CLI (FR-1)

Exit code: 0 sukses, 2 argumen/konfigurasi salah, 3 gagal scan total.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from core.config import ConfigError, load_config, load_scanner_config
from core.json_export import _py  # JSON-safe sanitizer (numpy/datetime -> native)
from core.scanner import format_scan_text, run_scan
from core.state import open_state


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scan",
        description="Scanner prospek market: universe -> pre-filter ADX -> scoring -> ranking.",
    )
    p.add_argument("--exchange", default=None, help="ID exchange ccxt (default dari config)")
    p.add_argument("--tf", nargs="+", default=None, metavar="TF",
                   help="Timeframe analisa (default dari config.default_timeframes[0] via scanner)")
    p.add_argument("--limit", type=int, default=None, metavar="N",
                   help="Batas jumlah symbol yang dianalisa setelah pre-filter")
    p.add_argument("--top", dest="top_n", type=int, default=None, metavar="N",
                   help="Jumlah baris peringkat ditampilkan/disimpan (default dari scanner.yaml)")
    p.add_argument("--watchlist", action="store_true",
                   help="Scan hanya isi watchlist (state/watchlist.yaml)")
    p.add_argument("--no-save", action="store_true",
                   help="Jangan persist hasil ke SQLite scan_history")
    p.add_argument("--json", action="store_true", help="Output JSON alih-alih text")
    p.add_argument("--out", default=None, metavar="FILE", help="Tulis hasil ke file")
    p.add_argument("--no-emoji", action="store_true", help="Tanpa emoji di output text")
    p.add_argument("--quiet", action="store_true", help="Sembunyikan progress per-symbol")
    p.add_argument("--config", default="config.yaml", help="Path config.yaml utama")
    p.add_argument("--scanner-config", default="scanner.yaml", help="Path scanner.yaml")
    p.add_argument("--override", action="append", default=[], metavar="KEY=VALUE",
                   help="Override config dotted-key (prefix 'scanner.' mengenai scanner.yaml), "
                        "bisa berulang. Mis: --override scanner.ranking.top_n=8")
    return p


def _fmt_exc(e: Exception) -> str:
    cause = e.__cause__
    if cause is not None and str(cause):
        return f"{e} [{type(cause).__name__}: {cause}]"
    return str(e)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    # --- Config + overrides (FR-1). Override ber-prefix scanner.* diarahkan ke scanner.yaml ---
    try:
        main_ov, scan_ov = {}, {}
        for item in args.override:
            if "=" not in item:
                print(f"error: --override harus KEY=VALUE, dapat: {item!r}", file=sys.stderr)
                return 2
            k, _, v = item.partition("=")
            k, v = k.strip(), v.strip()
            if k.startswith("scanner."):
                scan_ov[k[len("scanner."):]] = v
            else:
                main_ov[k] = v
        cfg = load_config(args.config, overrides=main_ov or None)
        scfg = load_scanner_config(args.scanner_config, overrides=scan_ov or None)
    except ConfigError as e:
        print(f"error konfigurasi: {_fmt_exc(e)}", file=sys.stderr)
        return 2

    if args.tf:
        from core.time_utils import TimeframeError, validate_timeframe
        bad, normed = [], []
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

    from core.fetch import MarketDataFetcher  # lazy import agar mudah di-mock
    fetcher = MarketDataFetcher(cfg)
    if args.exchange:
        fetcher.config.raw["exchange"]["id"] = args.exchange
    state = None
    if not args.no_save:
        try:
            state = open_state(cfg)
        except Exception as e:  # DB korup/readonly -> lanjut tanpa persistensi
            print(f"[warn] state SQLite tidak tersedia ({_fmt_exc(e)}); "
                  f"hasil tidak disimpan.", file=sys.stderr)

    def progress(i: int, n: int, sym: str) -> None:
        if not args.quiet:
            print(f"  [{i}/{n}] {sym}", file=sys.stderr)

    try:
        outcome = run_scan(
            fetcher, cfg, scfg,
            state=state,
            watchlist_only=args.watchlist,
            timeframes=args.tf,
            progress=progress,
            top_n=args.top_n,
            limit=args.limit,
        )
    except Exception as e:
        print(f"error scan: {_fmt_exc(e)}", file=sys.stderr)
        return 3

    if outcome.limit_reached_note:
        print(f"[info] {outcome.limit_reached_note}", file=sys.stderr)

    # --- Render ---
    if args.json:
        payload = {
            "schema_version": "1.0.0",
            "type": "scan",
            "ts_utc": outcome.ts_utc,
            "duration_seconds": round(outcome.duration_seconds, 2),
            "universe_size": outcome.universe_size,
            "prefiltered_size": outcome.prefiltered_size,
            "analysed_size": outcome.analysed_size,
            "cooldown_active": outcome.cooldown_active,
            "rows": [_py(r) for r in outcome.rows],
            "errors": _py(outcome.errors),
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    else:
        text = format_scan_text(outcome, emoji=not args.no_emoji)
        if not args.quiet:
            last = None
            try:
                st = state or open_state(cfg)
                last = st.get_last_scan()
            except Exception:
                pass
            if last:
                print(f"[state] scan tersimpan: {last['ts_utc']} "
                      f"({len(last.get('rows', []))} rows)", file=sys.stderr)

    print(text)
    if args.out:
        try:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(text + "\n")
            print(f"[saved] {args.out}", file=sys.stderr)
        except OSError as e:
            print(f"error menulis {args.out}: {e}", file=sys.stderr)
            return 3

    if outcome.cooldown_active:
        print("[fetch-warning] API_COOLDOWN aktif — sebagian hasil berasal dari cache lama.",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
