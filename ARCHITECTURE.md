# ARCHITECTURE — Market Tools (Analisa Market Multi-Timeframe)

Versi Dokumen : 1.3 (Sinkron dengan implementasi v1.0.x — incl. progress bar Telegram)
Versi Produk  : 1.0.0

## 1. Overview
Aplikasi Python single-process dengan tiga entry point (CLI single pair, CLI
scanner, Telegram bot) yang berbagi library `core/`. State dinamis di SQLite,
config di YAML, cache OHLCV di Parquet. Prinsip: core logic murni,
config-first, cache agresif, circuit breaker, JSON-ready (dengan raw scores).

## 2. Struktur Folder
```
market-tools/
├── core/                  # Library bersama
│   ├── time_utils.py      # WITA/UTC, TF validation
│   ├── indicators.py      # EMA, ADX, MACD, ATR, LR
│   ├── cache.py           # Parquet cache + TTL
│   ├── fetch.py           # ccxt wrapper + Circuit Breaker
│   ├── trend.py           # Trend label, raw score, regime, maturity
│   ├── sr.py              # Support/Resistance detection
│   ├── risk.py            # Risk per TF + invalidation
│   ├── rr.py              # R:R calculator
│   ├── volume.py          # Volume context & VSA (liquidity proxy)
│   ├── price_action.py    # Range position, wick rejection
│   ├── warnings.py        # Warning + severity
│   ├── state.py           # SQLite wrapper untuk dynamic state
│   ├── watchlist.py       # CRUD watchlist (YAML)
│   ├── analysis.py        # Orkestrasi single pair
│   ├── scanner.py         # Universe, scoring, rank
│   ├── json_export.py     # Build JSON untuk LLM (include raw scores)
│   └── text_export.py     # Build text (CLI & Bot)
├── prompts/               # Template LLM (fase 2)
├── deploy/                # systemd unit
├── market_insight.py      # CLI single pair
├── scan.py                # CLI scanner
├── bot.py                 # Telegram bot
├── config.yaml, scanner.yaml, bot.yaml (SECRET)
├── requirements.txt
├── README.md, PRD.md, ARCHITECTURE.md, .gitignore
├── cache/                 # Parquet cache
├── state/                 # watchlist.yaml, market_tools.db (SQLite)
└── logs/                  # bot.log
```

## 3. Data Flow & Modul Utama
### 3.1 core/fetch.py (dengan Circuit Breaker)
Wrapper ccxt. 4-level resolution: Cache → Network → Fallback → Stale.
Circuit breaker in-memory (`last_error_time`): jika 3 request berturut-turut
gagal (timeout/5xx), cooldown 5 menit. Selama cooldown langsung return cache
lama + flag `API_COOLDOWN`.

### 3.2 core/trend.py, core/volume.py & core/price_action.py (liquidity proxy)
trend.py menghitung voting 5 indikator dan WAJIB mengembalikan `raw_score`
(float). volume.py menambahkan metrik VSA. price_action.py menambahkan metrik
wick rejection: rasio upper/lower wick dihitung terhadap **total range bar
(high−low)** — bukan body — agar stabil saat body ≈ 0; candle dianggap
rejection bila wick ≥ 60% range, body ≤ 25%, disertai partisipasi volume.
`wick_rejection_score` [-1..+1] adalah agregat lookback pendek, dilengkapi
`range_position` (posisi harga dalam high-low range N bar). Semuanya menjadi
proxy anomali likuiditas/order book (D-2).

### 3.3 core/state.py (SQLite integration)
`sqlite3` built-in. Tabel `scan_history(ts_utc, symbol, rank, prospect, risk,
adj_score, raw_scores_json)` dan `bot_state(key, value)`. WAL mode. Fungsi:
save_scan_result(), get_last_scan(), compare_rank().
Catatan implementasi: kolom `raw_scores_json` menyimpan dump seluruh field
tambahan baris hasil scan — termasuk komponen skor (0..1) pembentuk
prospect/risk score dari `core/scanner.py` — sehingga siap dipakai sebagai
feature source Fase 2 (D-3).

### 3.4 core/json_export.py
Schema JSON v1.0.0; memastikan `trend_score_raw`, `vsa_score`,
`wick_rejection_score` diekspor (siap dikonsumsi LLM/ML Fase 2). Kontrak rentang
nilai dieksplisitkan di `summary.raw_score_contract` ([-1.0, 1.0]).

## 4. Data Model
### 4.1 Analysis Result (dict)
Posisi field mengikuti implementasi (`core/json_export.py`): blok `volume` dan
blok `price_action` terpisah per timeframe; `wick_rejection_score` berada di
dalam `price_action`, bukan di `volume`.
```
{
  "schema_version", "generated_at_utc",
  "summary": { "symbol", "bias", "votes", "rr", "warnings",
               "raw_score_contract" },
  "timeframes": { "<tf>": {
      "trend":        { "label", "score_raw", "confidence", "details" },
      "sr", "risk", "invalidation",
      "volume":       { "avg", "last", "ratio", "state", "vsa_score",
                        "pattern_counts", "recent_bars" },
      "price_action": { "range_position", "last_bar", "rejection_bars",
                        "wick_rejection_score", "interpretation" } } }
}
```
### 4.2 SQLite Schema
```sql
CREATE TABLE scan_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    symbol TEXT NOT NULL,
    rank INTEGER,
    prospect REAL,
    risk REAL,
    adj_score REAL,
    raw_scores_json TEXT
);
CREATE INDEX idx_symbol_ts ON scan_history(symbol, ts_utc);
```

## 5. Error Handling & Resilience
- Circuit breaker: ConnectionError/HTTPError>=500 terjadi 3x dalam 1 menit →
  cooldown; log "Exchange API unresponsive. Cooldown 5 minutes."; bot mengirim
  notifikasi jika user memicu /scan saat cooldown.
- State concurrency: SQLite menangani locking internal; YAML watchlist atomic
  write (tmp + os.replace); single user → race condition CLI/Bot diabaikan.

## 6. Deployment & Testing
- Raspberry Pi 4, Python 3.10+, SQLite built-in; systemd unit dengan
  `ProtectSystem=full`.
- Smoke test: import core, jalankan CLI & bot. Sanity check: pastikan
  `trend_score_raw` ada di JSON output. Resilience test: matikan internet /
  block IP Indodax → circuit breaker memicu cooldown tanpa crash.

## 7. Catatan Sinkronisasi Dokumen (v1.2, Okt 2026)

Dokumen ini disesuaikan dengan kode aktual (kode = sumber kebenaran):

- **§3.2** — rasio wick memakai total range bar (high−low) sebagai denominator,
  bukan body; ambang rejection: wick ≥ 60% range, body ≤ 25%, + volume.
- **§4.1** — `wick_rejection_score` berada di blok `price_action` per timeframe
  (bukan di dalam `volume`), sesuai `core/json_export.py`; ditambahkan field
  `schema_version`, `generated_at_utc`, dan `summary.raw_score_contract`.
- **§3.3** — kolom `raw_scores_json` menyimpan dump seluruh field tambahan baris
  scan termasuk komponen skor (0..1) dari `core/scanner.py` (D-3).

Deviasi fungsional: **tidak ada** — progress indicator scanner kini tersedia
di Telegram bot (v1.3) sekaligus CLI (`--quiet` utk mematikan).

## 8. Progress Bar Telegram (v1.3, Okt 2026)

Menutup deviasi PRD F6 terakhir. Arsitektur mengikuti pola engine/adapter:

- `core/scanner.py` — callback `progress(done, total, label)` kini juga
  dipanggil pada fase scoring per-kandidat (sebelumnya hanya pre-filter ADX);
  label format `"SYM (scoring i/n)"`. Kontrak opsional → tidak breaking.
- `botlib.py` (murni, ter-test offline):
  - `render_progress(done, total, label, eta_seconds)` → satu baris text:
    `⏳ [██████░░░░░░░░░░░░] 6/18 (33%)  Pre-filter ADX: BTC/IDR, …  ETA 02:15`.
  - `ProgressThrottle(callback, throttle_seconds=1.0, clock=…)` — membatasi
    frekuensi update (Telegram flood-limit; ~1 edit/detik aman), selalu emit
    pada `done==total`, plus `finish()` untuk emit final.
  - `BotEngine.cmd_scan(..., progress_cb=None)` & `handle_update(..., progress_cb=None)`
    — engine menghitung fase (Pre-filter ADX / Scoring), daftar simbol terbaru,
    dan ETA (linear extrapolation), lalu memanggil `progress_cb(str)`.
    Tanpa cb → perilaku lama persis. Flag `progress_enabled` (bot.yaml:
    `progress: false`) mematikan sepenuhnya.
- `bot.py` (adapter async, satu-satunya yang menyentuh telegram):
  `ProgressMessenger(message)` — `/scan` mengirim 1 pesan "⏳ Scan dimulai…",
  worker thread engine memanggil `update_sync(text)` yang meneruskan ke loop
  asyncio via `run_coroutine_threadsafe` → `edit_text`. Semua kegagalan
  kirim/edit hanya mematikan progres (`broken=True`); hasil scan tetap terkirim.
- Tests: kelas `TestProgressBar` di `tests/test_wp8_telegram_bot.py` (format,
  throttle dgn clock injeksi, feed cb, regresi tanpa cb, flag disable, routing).
