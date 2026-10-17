# Market Tools — Analisa Market Multi-Timeframe untuk Trader Retail

**Versi Produk:** 1.0.0 · **Status:** Fase 1 (Observasi) — WP1..WP9 selesai
**Dokumen terkait:** `PRD.md` (kebutuhan & scope) · `ARCHITECTURE.md` (desain teknis)

Aplikasi Python single-process dengan tiga entry point yang berbagi library `core/`:

| Entry point | Fungsi | PRD Feature |
|---|---|---|
| `market_insight.py` | CLI analisa single pair multi-timeframe (trend, S/R, risk, R:R, warnings) | F1 + F2 |
| `scan.py` | CLI scanner prospek seluruh universe (ranking risk-adjusted, riwayat SQLite) | F3 + F4 |
| `bot.py` | Telegram bot (whitelist chat_id, rate limit, auto-chunking) | F6 / FR-5 |

Prinsip desain: core logic murni, config-first, cache agresif (Parquet + TTL),
circuit breaker, dan JSON-ready dengan raw scores (`trend_score_raw`,
`vsa_score`, `wick_rejection_score`) sebagai bekal feature-engineering Fase 2.

> ⚠️ **Disclaimer:** alat bantu observasi — BUKAN prediksi arah harga,
> BUKAN sinyal beli/jual mutlak, BUKAN auto-trading.

---

## 1. Fitur Utama

- **Analisa single pair multi-TF (F1/F2):** voting 5 indikator (EMA, ADX, MACD,
  RSI, linear-regression slope) → label trend `STRONG_UPTREND` … `SIDEWAYS`
  dengan `score_raw` float −1.0..+1.0; regime EMA20/50; streak & trend maturity;
  zona S/R adaptif; risk per TF; invalidation = swing low/high terdekat dengan
  min distance 0.5×ATR (FR-3); volume context + VSA & wick rejection sebagai
  proxy likuiditas (D-2); warnings ber-severity; kalkulator R:R.
- **Confluence:** 3 TF independen (default 1h/4h/1d), agregasi bias + deteksi konflik.
- **Scanner (F3):** universe filter (quote IDR, min volume 24h, exclude stablecoin)
  → pre-filter ADX ≥ 15 → prospect score (bobot di `scanner.yaml`) → risk score
  → risk-adjusted ranking → rank change vs scan sebelumnya (SQLite).
- **Watchlist & State (F4):** CRUD watchlist YAML (atomic write); riwayat scan &
  bot state di SQLite WAL mode — dipakai bersama oleh CLI & bot.
- **Caching & Resilience (F5):** cache Parquet per (exchange, symbol, TF) + TTL;
  resolusi 4-level Cache → Network → Fallback → Stale; circuit breaker: 3 fetch
  gagal berturut-turut (network/5xx dalam window) → cooldown 5 menit, selama
  cooldown return cache lama + flag `API_COOLDOWN` tanpa crash (FR-2).
- **Output (F6):** text Level 1 (ringkas) / Level 2 (lengkap) dengan ANSI & emoji,
  JSON schema v1.0.0 siap konsumsi LLM, compact mode hemat token.

---

## 2. Struktur Folder

```
market-tools/
├── core/                  # Library bersama (murni, dapat di-unit-test)
│   ├── time_utils.py      # WITA/UTC, validasi TF
│   ├── indicators.py      # EMA, ADX, MACD, ATR, RSI, LR slope
│   ├── cache.py           # Parquet cache + TTL
│   ├── fetch.py           # ccxt wrapper + Circuit Breaker
│   ├── trend.py           # Trend label, raw score, regime, maturity
│   ├── sr.py              # Support/Resistance detection
│   ├── risk.py            # Risk per TF + invalidation
│   ├── rr.py              # R:R calculator
│   ├── volume.py          # Volume context & VSA (liquidity proxy)
│   ├── price_action.py    # Range position, wick rejection
│   ├── warnings.py        # Warning + severity
│   ├── state.py           # SQLite wrapper (WAL) utk dynamic state
│   ├── watchlist.py       # CRUD watchlist (YAML atomic write)
│   ├── analysis.py        # Orkestrasi single pair
│   ├── scanner.py         # Universe, scoring, rank
│   ├── json_export.py     # Build JSON utk LLM (include raw scores)
│   ├── text_export.py     # Build text (CLI & Bot)
│   ├── config.py          # Loader config.yaml/scanner.yaml + override
│   └── yaml_io.py         # Utilitas YAML atomic
├── prompts/               # Template LLM (Fase 2 — placeholder)
├── deploy/market-bot.service  # systemd user unit (ProtectSystem=full)
├── tests/                 # pytest WP1..WP8 (offline, tanpa jaringan)
├── market_insight.py      # CLI single pair
├── scan.py                # CLI scanner
├── bot.py / botlib.py     # Telegram bot (adapter async / engine testable)
├── config.yaml scanner.yaml bot.yaml(SECRET) bot.yaml.example
├── requirements.txt pytest.ini .gitignore
└── cache/ state/ logs/    # dibuat otomatis saat runtime
```

---

## 3. Instalasi

Prasyarat: **Python 3.10+** (development target Raspberry Pi 4 / 4GB RAM, NFR-3).

```bash
git clone https://github.com/tonshans/market-insight && cd market-insight
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Konfigurasi cukup file `config.yaml` (default) dan `scanner.yaml`; keduanya
sudah terisi nilai wajar untuk Indodax. Tidak ada langkah setup database —
`state/market_tools.db` (SQLite, WAL) dan `cache/` dibuat otomatis.

---

## 4. Cara Penggunaan

### 4.1 CLI Analisa Single Pair (`market_insight.py`)

```bash
python market_insight.py BTC/IDR                       # output text Level 1
python market_insight.py ETH/IDR --tf 1h 4h 1d --level 2
python market_insight.py SOL/IDR --json                # JSON schema v1.0.0 ke stdout
python market_insight.py BTC/IDR --compact --no-emoji  # payload hemat token utk LLM
python market_insight.py BTC/IDR --out hasil.txt       # tulis ke file
python market_insight.py "btc/idr" --exchange indodax \
    --override analysis.atr_period=16 \
    --override cache.ttl_by_tf.1h=60                   # override config via CLI (FR-1)
```

Opsi penting: `--level {1,2}`, `--json|--compact` (mutually exclusive),
`--no-color/--color/--no-emoji`, `--config PATH`, `--override KEY=VALUE` (berulang).
Simbol dinormalisasi otomatis (`btc/idr` → `BTC/IDR`).

**Exit code:** `0` sukses · `2` argumen/konfigurasi salah · `3` gagal fetch/analisa.

### 4.2 CLI Scanner (`scan.py`)

```bash
python scan.py                            # full universe + persist riwayat ke SQLite
python scan.py --watchlist                # hanya pair di state/watchlist.yaml
python scan.py --limit 10 --tf 4h         # batasi simbol yang dianalisa
python scan.py --top 5 --no-emoji         # tampilkan 5 peringkat teratas
python scan.py --no-save --json           # tanpa menulis riwayat, output JSON
python scan.py --quiet                    # sembunyikan progress per-symbol
python scan.py --override scanner.ranking.top_n=8   # override scanner.yaml (FR-1)
```

Peringkat ditampilkan beserta **rank change vs scan sebelumnya** (dari tabel
`scan_history`). **Exit code** sama dengan CLI single pair.

### 4.3 Telegram Bot (`bot.py`)

```bash
cp bot.yaml.example bot.yaml      # lalu isi telegram_token & allowed_chat_ids
chmod 600 bot.yaml                # JANGAN pernah commit bot.yaml (NFR-4)
python bot.py                     # atau: TELEGRAM_BOT_TOKEN=... python bot.py --config bot.yaml
```

Perintah yang dilayani (hanya dari chat_id dalam whitelist; lainnya dibungkam):

```
/insight PAIR [tf tf ...] [level]   cth: /insight BTC/IDR 1h 4h 1d 2
/scan [--watchlist|--limit N|--top N|--no-save]
/watch                              lihat watchlist
/watch add PAIR [catatan]           tambah (CRUD F4)
/watch remove PAIR                  hapus
/watch clear                        kosongkan
/flush_cache [exchange]             bersihkan cache Parquet
/status                             health bot, breaker, cache & riwayat scan
/help /start                        daftar perintah
```

Keamanan sesuai FR-5: **whitelist chat_id wajib**, **rate limit 10 cmd/menit/chat**,
**auto-chunking** pesan > 4096 karakter, notifikasi `API_COOLDOWN` anti-spam
(maksimal 1x per `notify_cooldown_minutes`). Log rotasi ke `logs/bot.log`.

**Progress bar `/scan` (PRD F6):** saat `/scan` dijalankan, bot mengirim satu
pesan progres yang di-edit otomatis ±1x/detik (aman flood-limit Telegram):

```
⏳ [██████░░░░░░░░░░░░] 24/68 (35%)  Pre-filter ADX: DOGE/IDR, XRP/IDR  ETA 03:12
```

Setelah selesai pesan berubah menjadi "selesai" lalu hasil scan menyusul.
Matikan lewat `bot.yaml`: `progress: false`. Perintah lain (`/insight` dll.)
tidak memakai progress karena durasinya pendek.

### 4.4 Deployment systemd (Raspberry Pi)

```bash
mkdir -p ~/.config/systemd/user
cp deploy/market-bot.service ~/.config/systemd/user/   # sesuaikan path /home/pi/market-tools
systemctl --user daemon-reload
systemctl --user enable --now market-bot.service
journalctl --user -u market-bot -f
```

Unit sudah mencakup hardening `ProtectSystem=full`, `NoNewPrivileges`,
`PrivateTmp`, `Restart=on-failure`, dan `ReadWritePaths` utk cache/state/logs.

---

## 5. Konfigurasi

| File | Isi | Catatan |
|---|---|---|
| `config.yaml` | exchange (indodax, sleep 200ms), timeframes default/supported, parameter analisis (EMA20/50, ATR14, ADX14, RSI14, MACD 12/26/9, LR20, swing 20, min_atr_distance 0.5), cache dir & TTL per TF, state path | Nilai default; CLI bisa override per-run |
| `scanner.yaml` | universe filter, pre-filter ADX, bobot prospect score (trend .35 / confluence .25 / momentum .15 / volume .15 / rr .10), bobot risk score, ranking top_n | Override via `--override scanner.…` |
| `bot.yaml` | token, `allowed_chat_ids`, rate limit, anti-spam notif, emoji | **SECRET** — di `.gitignore`; contoh di `bot.yaml.example` |

Skema state SQLite (ARCHITECTURE §4.2):

```sql
CREATE TABLE scan_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL, symbol TEXT NOT NULL, rank INTEGER,
    prospect REAL, risk REAL, adj_score REAL, raw_scores_json TEXT
);
CREATE INDEX idx_symbol_ts ON scan_history(symbol, ts_utc);
-- plus: bot_state(key, value)
```

---

## 6. Testing & Verifikasi

```bash
pip install pytest
pytest -q          # 161 tes lulus; seluruhnya offline (tanpa jaringan/telegram)
```

Cakupan: smoke import & CLI (WP1/WP2), data layer cache+fetch+circuit breaker
(WP3), mesin analisa & raw scores (WP4), orkestrasi & export JSON/text (WP5),
state SQLite + watchlist atomic + scanner (WP6), kedua CLI entrypoint (WP7),
bot engine: whitelist/rate-limit/chunking/cooldown-notice (WP8).

Sanity check manual: pastikan `trend_score_raw`, `vsa_score`, dan
`wick_rejection_score` hadir di output `--json`. Resilience: putuskan internet →
breaker memicu cooldown 5 menit dan CLI/bot tetap mengembalikan stale cache +
flag `API_COOLDOWN` tanpa crash. Performa (NFR-1): run kedua satu pair dengan
cache hit < 1 detik; full scan cache-miss < 10 menit.

---

## 7. Batasan & Catatan Deviasi

Tercatat dari audit kesesuaian kode ↔ PRD/ARCHITECTURE (per Okt 2026).
ARCHITECTURE.md v1.2 sudah disinkronkan dengan implementasi untuk poin 1, 2,
dan 4 — yang tersisa hanyalah poin 3:

1. ~~Posisi `wick_rejection_score`~~ **SINKRON** — ARCHITECTURE §4.1 kini
   mengikuti kode: `wick_rejection_score` berada di blok
   `timeframes.<tf>.price_action.*`, terpisah dari blok `volume`.
2. ~~Rasio wick~~ **SINKRON** — ARCHITECTURE §3.2 kini mendeskripsikan
   denominator total range bar (high−low) beserta ambang rejection
   (wick ≥ 60% range, body ≤ 25%, + partisipasi volume).
3. ~~**Progress indicator** belum ada di Telegram bot~~ **SINKRON (v1.3)** —
   `/scan` kini menampilkan progress bar yang di-edit otomatis (±1x/detik,
   dengan ETA), dapat dimatikan via `progress: false` di `bot.yaml`;
   lihat ARCHITECTURE §8. Deviasi fungsional terhadap PRD/ARCHITECTURE:
   **tidak ada lagi**.
4. ~~Kolom `raw_scores_json`~~ **SINKRON** — ARCHITECTURE §3.3 mencatat bahwa
   kolom ini menyimpan dump seluruh field tambahan baris scan, termasuk
   komponen skor (0..1) pembentuk prospect/risk score dari `core/scanner.py`,
   siap dipakai sebagai feature source Fase 2 (D-3).

Roadmap Fase 2: konsumsi JSON oleh LLM/ML (template di `prompts/`), feature
engineering dari raw scores, dan backtesting.

## Lisensi / Kontak

Penggunaan pribadi (single-user). Repo: <https://github.com/tonshans/market-insight>
