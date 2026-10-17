# PRD — Market Tools (Analisa Market Multi-Timeframe untuk Trader Retail)

Versi Dokumen : 1.1 (Finalized for Code Generation)
Versi Produk  : 1.0.0
Status        : Fase 1 (Observasi)

## 1. Latar Belakang & Tujuan
Trader retail yang memantau banyak pair di exchange (khususnya Indodax) sering
kesulitan menjawab pertanyaan cepat: pair mana yang trending, berapa lama trend
berjalan, di mana S/R relevan, berapa risiko entry sekarang, pair mana paling
prospek hari ini.

Tujuan Fase 1: alat bantu observasi yang memberi insight cepat (< 10 detik),
melatih feeling trader, menyediakan data konsisten antar sesi, dan menjadi
basis data (SQLite) untuk fase 2 (prediksi ML/LLM). BUKAN tujuan: memprediksi
arah harga atau memberi sinyal mutlak.

Success metrics: analisa single pair ~5 menit → < 10 detik; scan seluruh market
~30 menit → < 5 menit (first run); konsistensi output 100%; tahan error API
(Circuit Breaker aktif).

## 2. Target Pengguna
Primary: trader retail individu (single user), familiar dengan istilah teknikal,
akses via CLI & Telegram. Non-goals: multi-user/SaaS, auto-trading, real-time
streaming (WebSocket).

## 3. Scope Fase 1
- F1 Analisa Single Pair Multi-Timeframe: fetch OHLCV via ccxt; deteksi trend
  (voting 5 indikator + raw score); regime EMA20/50; streak & trend maturity;
  S/R zones adaptif; risk per TF; invalidation level; volume context & price
  action (Wick Rejection/VSA sebagai proxy likuiditas); warnings dengan
  severity; R:R calculator.
- F2 Multi-Timeframe Confluence: 3 TF independen, bias aggregation & deteksi
  konflik.
- F3 Scanner: universe filter, pre-filter ADX, prospect score, risk score,
  risk-adjusted ranking, rank change vs scan sebelumnya (SQLite).
- F4 Watchlist & State: CRUD watchlist (YAML); dynamic state (last_scan,
  history) di SQLite; shared antara CLI & bot.
- F5 Caching & Resilience: Parquet cache per (exchange, symbol, TF) dengan TTL;
  Circuit Breaker cooldown otomatis saat API error berturut-turut.
- F6 Output & Telegram Bot: text Level 1/2, JSON (untuk LLM), ANSI/Emoji;
  commands /insight, /scan, /watch, /flush_cache, /status; whitelist chat_id;
  rate limit; progress indicator.

## 4. Functional Requirements
- FR-1 Konfigurasi via config.yaml & scanner.yaml; CLI dapat override.
- FR-2 Data fetching & resilience: ccxt adapter; cache Parquet + TTL; CIRCUIT
  BREAKER — jika 3 fetch gagal (network/5xx), pause semua fetch selama 5 menit
  dan kirim warning ke user.
- FR-3 Analisa: trend label (STRONG_UPTREND s/d SIDEWAYS); WAJIB menyertakan
  `trend_score_raw` (float -1.0..1.0) di output JSON/DB; invalidation = swing
  low/high terdekat dengan min distance 0.5×ATR.
- FR-4 Scanner: skor prospect, risk, adj_score; simpan riwayat scan ke SQLite.
- FR-5 Telegram bot: whitelist wajib, rate limit 10 cmd/menit, auto-chunking.

## 5. Non-Functional Requirements
- NFR-1 Performa: single pair < 1 detik (cache hit); scanner full < 10 menit
  (cache miss).
- NFR-2 Reliability: atomic write untuk YAML; SQLite WAL mode untuk state.
- NFR-3 Portability: Python 3.10+, jalan di Raspberry Pi 4 (4GB RAM).
- NFR-4 Security: bot.yaml di .gitignore; whitelist chat_id.

## 6. Keputusan Desain (Rationale)
- D-1 State: SQLite untuk dynamic state (riwayat scan, bot state); YAML untuk
  config/watchlist. SQLite lebih robust dari JSON untuk RPi.
- D-2 Liquidity proxy: Wick Rejection & VSA dari OHLCV alih-alih order book,
  untuk menghindari rate-limit dan latency.
- D-3 Raw score logging: simpan skor mentah indikator utk persiapan feature
  engineering Fase 2 (ML).
- D-4 Circuit breaker: mencegah resource RPi terbuang saat exchange down.

## 7. Risiko & Mitigasi
- R-1 Indodax rate limit/down → cache agresif, sleep 200ms, circuit breaker
  (pause 5 menit jika 3x gagal).
- R-2 RPi SD card wear → SQLite WAL mode, cache di RAM/tmpfs (opsional), log
  rotation.
