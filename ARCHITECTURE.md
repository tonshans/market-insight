# ARCHITECTURE — Market Tools (Analisa Market Multi-Timeframe)

Versi Dokumen : 1.1 (Finalized for Code Generation)
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

### 3.2 core/trend.py & core/volume.py (liquidity proxy)
trend.py menghitung voting 5 indikator dan WAJIB mengembalikan `raw_score`
(float). volume.py menambahkan metrik VSA. price_action.py menambahkan metrik
wick rejection (rasio upper/lower wick terhadap body) sebagai proxy anomali
likuiditas/order book.

### 3.3 core/state.py (SQLite integration)
`sqlite3` built-in. Tabel `scan_history(ts_utc, symbol, rank, prospect, risk,
adj_score, raw_scores_json)` dan `bot_state(key, value)`. WAL mode. Fungsi:
save_scan_result(), get_last_scan(), compare_rank().

### 3.4 core/json_export.py
Schema JSON v1.0.0; memastikan `trend_score_raw`, `vsa_score`,
`wick_rejection_score` diekspor (siap dikonsumsi LLM/ML Fase 2).

## 4. Data Model
### 4.1 Analysis Result (dict)
```
{
  "summary": { "symbol", "bias", "votes", "rr", "warnings" },
  "timeframes": { "<tf>": {
      "trend": { "label", "score_raw", "confidence", "details" },
      "volume": { "avg", "last", "vsa_score", "wick_rejection" },
      "sr", "risk", "invalidation" } }
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
