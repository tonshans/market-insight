# -*- coding: utf-8 -*-
"""botlib — logika murni Telegram bot Market Tools (F6, FR-5, WP8).

Modul ini SENGAJA tidak meng-import `telegram` supaya bisa di-unit-test tanpa
dependency bot dan tanpa jaringan. `bot.py` adalah adapter tipis di atasnya
(python-telegram-bot v20, async).

Fitur (PRD F6 / FR-5):
    - Whitelist chat_id wajib (NFR-4): hanya user terdaftar yang dilayani.
    - Rate limit 10 command/menit per chat (sliding window, in-memory).
    - Auto-chunking output ke <= 4096 karakter (memakai core.text_export).
    - Notifikasi cooldown circuit-breaker dengan cooldown anti-spam per chat
      (ARCHITECTURE.md §5: "bot mengirim notifikasi jika user memicu /scan
      saat cooldown" — dan tidak boleh spam tiap perintah).
    - Command handler sinkron: /insight /scan /watch /flush_cache /status
      (+ /help /start), mengembalikan daftar pesan siap-kirim.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from core.cache import ParquetCache
from core.config import ConfigError, load_config, load_scanner_config
from core.fetch import FetchError
from core.state import open_state
from core.text_export import chunk_message, render
from core.watchlist import Watchlist, normalize_symbol

DEFAULT_BOT_CONFIG_PATH = "bot.yaml"
TELEGRAM_LIMIT = 4096


class BotAuthError(Exception):
    """Chat id tidak ada dalam whitelist (NFR-4)."""


def _fmt_exc(e: Exception) -> str:
    cause = e.__cause__
    if cause is not None and str(cause):
        return f"{e} [{type(cause).__name__}: {cause}]"
    return str(e)


# ------------------------------------------------------------------ config
def load_bot_config(path: str = DEFAULT_BOT_CONFIG_PATH) -> dict[str, Any]:
    """Load bot.yaml (SECRET — di .gitignore, NFR-4).

    Struktur minimal::

        telegram_token: "123:ABC"
        allowed_chat_ids: [111111, 222222]
        rate_limit_per_minute: 10          # opsional (default 10, FR-5)
        notify_cooldown_minutes: 15        # opsional: anti-spam notif cooldown

    Return {} bila file tidak ada — caller (`main` di bot.py) memberi pesan
    ramah; fungsi lain tetap memakai nilai bawaan agar mudah di-test.
    """
    from core.yaml_io import load_yaml
    data = load_yaml(path, default={}) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"bot config harus YAML mapping: {path}")
    return data


# ------------------------------------------------------------- rate limiter
class SlidingWindowRateLimiter:
    """Batas N perintah per jendela waktu (detik), per chat_id (FR-5)."""

    def __init__(self, max_commands: int = 10, window_seconds: float = 60.0):
        self.max_commands = int(max_commands)
        self.window_seconds = float(window_seconds)
        self._hits: dict[Any, list[float]] = {}

    def check(self, key: Any, now: Optional[float] = None) -> bool:
        """Catat satu perintah; True bila masih di bawah batas."""
        t = time.monotonic() if now is None else float(now)
        hits = [x for x in self._hits.get(key, [])
                if t - x < self.window_seconds]
        if len(hits) >= self.max_commands:
            self._hits[key] = hits          # simpan window tersisa
            return False
        hits.append(t)
        self._hits[key] = hits
        return True

    def remaining(self, key: Any, now: Optional[float] = None) -> int:
        t = time.monotonic() if now is None else float(now)
        hits = [x for x in self._hits.get(key, [])
                if t - x < self.window_seconds]
        return max(0, self.max_commands - len(hits))


# ------------------------------------------------------------ notification
class NotificationGate:
    """Cooldown anti-spam utk notifikasi system (mis. API_COOLDOWN).

    `allow(key)` sekali per `cooldown_seconds` per key (chat_id). Nilai
    last-notified disimpan di SQLite `bot_state` key ``notify:<key>`` supaya
    bertahan lintas restart proses.
    """

    def __init__(self, state=None, cooldown_seconds: float = 900.0):
        self.state = state
        self.cooldown_seconds = float(cooldown_seconds)
        self._fallback: dict[str, float] = {}   # dipakai bila state=None

    def allow(self, key: Any, now: Optional[float] = None) -> bool:
        t = time.time() if now is None else float(now)
        skey = f"notify:{key}"
        last: Optional[float] = None
        if self.state is not None:
            try:
                raw = self.state.get_bot_state(skey)
                last = float(raw) if raw is not None else None
            except (TypeError, ValueError):
                last = None
        else:
            last = self._fallback.get(skey)
        if last is not None and t - last < self.cooldown_seconds:
            return False
        if self.state is not None:
            try:
                self.state.set_bot_state(skey, t)
            except Exception:
                pass  # state write gagal bukan alasan perintah gagal
        else:
            self._fallback[skey] = t
        return True


# ---------------------------------------------------------------- helpers
def format_watchlist(wl: Watchlist) -> str:
    entries = wl.entries()
    if not entries:
        return "📝 Watchlist kosong.\nTambah: /watch add BTC/IDR"
    lines = [f"📝 Watchlist ({len(entries)}):"]
    for e in entries:
        note = f"  — {e['note']}" if e.get("note") else ""
        lines.append(f"  • {e['symbol']}{note}")
    lines.append("")
    lines.append("Perintah: /watch add PAIR [catatan] | /watch remove PAIR | /watch clear")
    return "\n".join(lines)


HELP_TEXT = """🤖 Market Tools — Fase 1 (Observasi)

/insight PAIR [tf...] [level] — analisa single pair multi-TF
    contoh: /insight BTC/IDR | /insight eth/idr 1h 4h 1d 2
/scan [--watchlist|--limit N|--top N|--no-save] — scanner prospek market
/watch — lihat watchlist
/watch add PAIR [catatan] — tambah ke watchlist
/watch remove PAIR — hapus dari watchlist
/watch clear — kosongkan watchlist
/flush_cache [exchange] — bersihkan cache Parquet
/status — health bot, breaker, cache & riwayat scan
/help — pesan ini

Hanya chat_id terdaftar (whitelist) yang dilayani; maks 10 perintah/menit."""


# ------------------------------------------------------------ progress bar
def render_progress(done: int, total: int, label: str = "", width: int = 18,
                    eta_seconds: Optional[float] = None) -> str:
    """Render satu baris progress text (dipakai CLI & Telegram).

    Contoh: ``⏳ [██████░░░░░░░░░░░░] 6/18 (33%)  XRP/IDR  ETA 02:15``
    """
    total = max(1, int(total))
    done = min(max(0, int(done)), total)
    filled = round(width * done / total)
    bar = "█" * filled + "░" * (width - filled)
    pct = 100 * done // total
    line = f"⏳ [{bar}] {done}/{total} ({pct}%)"
    if label:
        line += f"  {label[:40]}"
    if eta_seconds is not None and eta_seconds >= 0:
        m, s = divmod(int(eta_seconds), 60)
        line += f"  ETA {m:02d}:{s:02d}"
    return line


class ProgressThrottle:
    """Rate-limiter update pesan progress (Telegram flood-limit ~1 edit/detik/chat).

    `throttle` detik minimum antar callback; callback dipanggil dengan
    ``(done, total, label)`` — signature sama dengan `progress` di
    core.scanner.run_scan. Bila `max_len` diberikan, label panjang (daftar
    simbol yang sudah diproses) dipotong dari depan agar muat 1 baris chat.
    """

    def __init__(self, callback: Callable[[int, int, str], None],
                 throttle_seconds: float = 1.0, clock: Callable[[], float] = time.monotonic,
                 max_len: int = 0):
        self.callback = callback
        self.throttle_seconds = float(throttle_seconds)
        self.clock = clock
        self.max_len = int(max_len)
        self._last_emit: Optional[float] = None

    def _emit(self, done: int, total: int, label: str) -> None:
        if self.max_len and len(label) > self.max_len:
            label = "…" + label[-(self.max_len - 1):]
        self.callback(done, total, label)

    def __call__(self, done: int, total: int, label: str = "") -> bool:
        """Return True bila callback benar-benar dipanggil (untuk test/logika)."""
        t = self.clock()
        if (self._last_emit is not None
                and t - self._last_emit < self.throttle_seconds
                and done != total):          # progres penuh: selalu emit
            return False
        self._last_emit = t
        self._emit(done, total, label)
        return True

    def finish(self, done: int, total: int, label: str = "") -> None:
        """Emit final tanpa memedulikan throttle (mis. 'selesai')."""
        self._last_emit = self.clock()
        self._emit(done, total, label)


# ---------------------------------------------------------------- engine
@dataclass
class BotEngine:
    """Orkestrasi perintah bot; semua dependency bisa di-inject utk test."""

    config: Any = None                 # core.config.Config
    scanner_config: Any = None         # core.config.ScannerConfig
    fetcher: Any = None                # core.fetch.MarketDataFetcher (lazy)
    state: Any = None                  # core.state.StateStore (lazy)
    allowed_chat_ids: set = field(default_factory=set)
    rate_limit_per_minute: int = 10
    notify_cooldown_seconds: float = 900.0
    analyze_fn: Optional[Callable[..., dict]] = None       # core.analysis.analyze_symbol
    run_scan_fn: Optional[Callable[..., Any]] = None       # core.scanner.run_scan
    format_scan_fn: Optional[Callable[..., str]] = None    # core.scanner.format_scan_text
    emoji: bool = True
    chunk_limit: int = TELEGRAM_LIMIT
    progress_enabled: bool = True        # bot.yaml: progress: false utk mematikan

    def __post_init__(self):
        if self.config is None:
            self.config = load_config("config.yaml")
        if self.allowed_chat_ids:
            self.allowed_chat_ids = {int(x) for x in self.allowed_chat_ids}
        self.rate_limiter = SlidingWindowRateLimiter(self.rate_limit_per_minute)
        self._analyze_fn = self.analyze_fn
        self._run_scan_fn = self.run_scan_fn
        self._format_scan_fn = self.format_scan_fn

    # ---------------------------------------------------------- dependencies
    @property
    def analyze_symbol(self):
        if self._analyze_fn is None:
            from core.analysis import analyze_symbol
            self._analyze_fn = analyze_symbol
        return self._analyze_fn

    @property
    def scanner_run(self):
        if self._run_scan_fn is None:
            from core.scanner import run_scan
            self._run_scan_fn = run_scan
        return self._run_scan_fn

    @property
    def scan_formatter(self):
        if self._format_scan_fn is None:
            from core.scanner import format_scan_text
            self._format_scan_fn = format_scan_text
        return self._format_scan_fn

    def get_fetcher(self):
        if self.fetcher is None:
            from core.fetch import MarketDataFetcher
            self.fetcher = MarketDataFetcher(self.config)
        return self.fetcher

    def get_state(self):
        if self.state is None:
            self.state = open_state(self.config)
        return self.state

    def get_watchlist(self) -> Watchlist:
        path = self.config.get("state.watchlist_path", "state/watchlist.yaml")
        return Watchlist(path)

    def _cache(self) -> ParquetCache:
        return ParquetCache(self.config.cache_dir)

    # ------------------------------------------------------------- guards
    def assert_whitelisted(self, chat_id: Any) -> None:
        """Whitelist wajib (FR-5/NFR-4). Raise BotAuthError bila asing."""
        try:
            cid = int(chat_id)
        except (TypeError, ValueError):
            raise BotAuthError(f"chat_id tidak valid: {chat_id!r}")
        if cid not in self.allowed_chat_ids:
            raise BotAuthError(f"chat_id {cid} tidak diizinkan (whitelist)")

    def check_rate(self, chat_id: Any) -> bool:
        return self.rate_limiter.check(chat_id)

    def _notify_gate(self) -> NotificationGate:
        return NotificationGate(self.get_state(), self.notify_cooldown_seconds)

    def _cooldown_notice(self, chat_id: Any, remaining_s: float) -> Optional[str]:
        """Pesan API_COOLDOWN, dibatasi anti-spam per chat (NotificationGate)."""
        if self._notify_gate().allow(chat_id):
            return ("⚠️ API_COOLDOWN: exchange sedang pause "
                    f"({remaining_s:.0f}s tersisa) — hasil berasal dari cache lama.")
        return None

    # ------------------------------------------------------------- commands
    def cmd_insight(self, chat_id: Any, args: list[str]) -> list[str]:
        if not args:
            return ["Pakai: /insight PAIR [tf tf ...] [level 1|2]\n"
                    "contoh: /insight BTC/IDR 1h 4h 1d 2"]
        symbol_raw = args[0]
        level = 1
        rest = args[1:]
        # Level (1|2) dibaca dari token TERAKHIR — "/insight BTC/IDR 2" dan
        # "/insight eth/idr 1h 4h 2" sama-sama valid; token sisanya TF.
        if rest and rest[-1] in ("1", "2"):
            level = int(rest[-1])
            rest = rest[:-1]
        tfs: list[str] = list(rest)
        try:
            symbol = normalize_symbol(symbol_raw)
        except ValueError as e:
            return [f"❌ Symbol tidak valid: {e}"]
        if tfs:
            from core.time_utils import TimeframeError, validate_timeframe
            try:
                tfs = [validate_timeframe(t) for t in tfs]
            except TimeframeError as e:
                return [f"❌ Timeframe tidak valid: {e}"]

        try:
            result = self.analyze_symbol(
                symbol, timeframes=tfs or self.config.default_timeframes,
                fetcher=self.get_fetcher(), config=self.config)
        except (ValueError, FetchError) as e:
            msgs = [f"❌ Analisa {symbol} gagal: {_fmt_exc(e)}"]
            cb = getattr(self.fetcher, "breaker", None)
            if cb is not None and cb.in_cooldown:
                n = self._cooldown_notice(chat_id, cb.remaining_cooldown())
                if n:
                    msgs.append(n)
            return msgs

        text = render(result, level=level, ansi=False, emoji=self.emoji)
        msgs = chunk_message(text, self.chunk_limit)
        cb = getattr(self.fetcher, "breaker", None)
        if cb is not None and cb.in_cooldown:
            n = self._cooldown_notice(chat_id, cb.remaining_cooldown())
            if n:
                msgs.append(n)
        return msgs

    def cmd_scan(self, chat_id: Any, args: list[str],
                 progress_cb: Optional[Callable[[str], None]] = None) -> list[str]:
        """Jalankan scanner. `progress_cb` (opsional) dipakai adapter bot.py utk
        meng-edit pesan progres di Telegram; engine tetap murni/tanpa I/O."""
        # Parser satu-pass menjaga urutan & pasangan flag-nilai:
        # "/scan --watchlist --limit 5 --top 3" tidak boleh menyisakan
        # angka posisi yang salah dianggap argumen tak dikenal.
        known_flags = {"--watchlist", "-w", "--no-save", "--json"}
        value_flags = ("--limit", "--top")
        flags: set[str] = set()
        opts: dict[str, Any] = {}
        i = 0
        while i < len(args):
            a = args[i].lower()
            if a in known_flags:
                flags.add(a)
            elif a in value_flags:
                try:
                    opts[a[2:]] = int(args[i + 1])
                except (IndexError, ValueError):
                    return [f"❌ {a} butuh angka. contoh: /scan {a} 10"]
                i += 1
            else:
                return [f"❌ Argumen tidak dikenal: {args[i]}\n"
                        "Pakai: /scan [--watchlist] [--limit N] [--top N] [--no-save]"]
            i += 1

        breaker = getattr(self.get_fetcher(), "breaker", None)
        if breaker is not None and breaker.in_cooldown:
            msg = ("⏳ Exchange dalam cooldown circuit-breaker "
                   f"({breaker.remaining_cooldown():.0f}s tersisa); /scan akan "
                   "hanya membaca cache lama. Tunggu cooldown selesai "
                   "atau jalankan lagi setelahnya.")
            return [msg] if self._notify_gate().allow(chat_id) else []

        kwargs: dict[str, Any] = {
            "watchlist_only": bool(flags & {"--watchlist", "-w"}),
        }
        if "limit" in opts:
            kwargs["limit"] = opts["limit"]
        if "top" in opts:
            kwargs["top_n"] = opts["top"]
        state = None if "--no-save" in flags else self.get_state()

        # progress bar (PRD F6) — engine murni: hanya panggil progress_cb bila
        # adapter menyuplainya; throttle + ETA dihitung di sini, I/O di bot.py.
        scan_progress = None
        if progress_cb is not None and self.progress_enabled:
            import time as _t
            seen_syms: list[str] = []
            t0 = _t.monotonic()

            def on_scan_progress(done: int, total: int, label: str) -> None:
                phase = ("Pre-filter ADX" if "(ADX)" in label
                         else "Scoring")
                base = label.split(" (")[0] if "(ADX)" in label else \
                    label.split(" (")[0].strip()
                if base and (not seen_syms or seen_syms[-1] != base):
                    seen_syms.append(base)
                eta = None
                if done > 0:
                    elapsed = _t.monotonic() - t0
                    eta = max(0.0, (elapsed / done) * (total - done))
                tail = ", ".join(seen_syms[-4:]) if seen_syms else ""
                line = render_progress(done, total, f"{phase}: {tail}",
                                       eta_seconds=eta)
                if len(line) > self.chunk_limit:
                    line = line[:self.chunk_limit]
                progress_cb(line)

            scan_progress = ProgressThrottle(on_scan_progress,
                                             throttle_seconds=1.0)

        try:
            outcome = self.scanner_run(self.get_fetcher(), self.config,
                                       self.scanner_config, state=state,
                                       progress=scan_progress, **kwargs)
        except Exception as e:
            return [f"❌ Scan gagal: {_fmt_exc(e)}"]

        if scan_progress is not None:
            scan_progress.finish(outcome.analysed_size, outcome.analysed_size,
                                 "selesai — menyusun hasil")

        text = self.scan_formatter(outcome, emoji=self.emoji)
        msgs = chunk_message(text, self.chunk_limit)
        if outcome.cooldown_active:
            rem = breaker.remaining_cooldown() if breaker else 0.0
            extra = ("⚠️ Sebagian data scan berasal dari cache lama "
                     "(API_COOLDOWN aktif selama scan)."
                     if not (breaker and breaker.in_cooldown) else
                     f"⚠️ API_COOLDOWN masih aktif ({rem:.0f}s tersisa).")
            if self._notify_gate().allow(chat_id):
                msgs.append(extra)
        return msgs

    def cmd_watch(self, chat_id: Any, args: list[str]) -> list[str]:
        wl = self.get_watchlist()
        sub = args[0].lower() if args else ""
        if not sub or sub == "list":
            return [format_watchlist(wl)]
        if sub == "add":
            if len(args) < 2:
                return ["Pakai: /watch add PAIR [catatan]  mis. /watch add BTC/IDR breakout"]
            try:
                sym = normalize_symbol(args[1])
            except ValueError as e:
                return [f"❌ {e}"]
            note = " ".join(args[2:]) or None
            wl.add(sym, note=note)
            return [f"✅ Ditambahkan ke watchlist: {sym}"
                    + (f" ({note})" if note else "")]
        if sub in ("remove", "rm", "del"):
            if len(args) < 2:
                return ["Pakai: /watch remove PAIR"]
            try:
                sym = normalize_symbol(args[1])
            except ValueError as e:
                return [f"❌ {e}"]
            return [f"🗑 Dihapus: {sym}" if wl.remove(sym)
                    else f"ℹ️ {sym} memang tidak ada di watchlist."]
        if sub == "clear":
            n = wl.clear()
            return [f"🧹 Watchlist dikosongkan ({n} entri dihapus)."]
        return [f"❌ Subcommand /watch tidak dikenal: {sub}\n"
                "Pakai: /watch [list|add PAIR [catatan]|remove PAIR|clear]"]

    def cmd_flush_cache(self, chat_id: Any, args: list[str]) -> list[str]:
        exchange = args[0] if args else None
        removed = self._cache().flush(exchange)
        scope = exchange or "semua exchange"
        return [f"🧹 Cache dibersihkan ({scope}): {removed} file parquet dihapus."]

    def cmd_status(self, chat_id: Any, args: list[str]) -> list[str]:
        lines = ["🩺 Status Market Tools", ""]
        fetcher = self.fetcher
        if fetcher is not None and getattr(fetcher, "breaker", None) is not None:
            st = fetcher.breaker.status()
            cd = st.get("remaining_seconds", 0) or 0
            state_txt = f"COOLDOWN {cd:.0f}s tersisa" if st.get("in_cooldown") \
                else "closed (sehat)"
            lines.append(f"Circuit breaker : {state_txt}")
        else:
            lines.append("Circuit breaker : belum pernah dipakai (belum ada fetch)")
        last = None
        try:
            last = self.get_state().get_bot_state("last_scan")
        except Exception as e:
            lines.append(f"State SQLite    : tidak tersedia ({_fmt_exc(e)})")
        if isinstance(last, dict):
            lines.append(f"Scan terakhir   : {last.get('ts_utc', '?')} "
                         f"(universe {last.get('n_universe', '?')}, "
                         f"kandidat {last.get('n_candidates', '?')})")
        elif last is not None:
            lines.append(f"Scan terakhir   : {last}")
        else:
            lines.append("Scan terakhir   : belum ada (jalankan /scan atau scan.py)")
        cstats = self._cache().stats()
        lines.append(f"Cache           : {cstats['files']} file di {cstats['dir']}")
        try:
            wl = self.get_watchlist()
            lines.append(f"Watchlist       : {len(wl)} pair")
        except Exception as e:
            lines.append(f"Watchlist       : error ({_fmt_exc(e)})")
        lines.append(f"Rate limit      : {self.rate_limit_per_minute}/menit, "
                     f"sisa {self.rate_limiter.remaining(chat_id)} perintah")
        return chunk_message("\n".join(lines), self.chunk_limit)

    # -------------------------------------------------------------- router
    def handle_update(self, chat_id: Any, text: str,
                      progress_cb: Optional[Callable[[str], None]] = None) -> list[str]:
        """Satu update -> daftar pesan balas (sudah di-chunk).

        Guard berurutan: whitelist (wajib) → parse command → rate limit →
        dispatch. Tidak pernah raise untuk error input user; hanya
        BotAuthError yang dilempar utk chat asing (caller hrs memutuskan
        apakah diam atau logging). `progress_cb` diteruskan ke /scan (progress
        bar Telegram; PRD F6).
        """
        self.assert_whitelisted(chat_id)
        parts = (text or "").strip().split()
        if not parts:
            return [HELP_TEXT]
        cmd = parts[0].lower().split("@", 1)[0]  # dukung /scan@MarketToolsBot
        args = parts[1:]
        if cmd in ("/start", "/help", "help"):
            return [chunk_message(HELP_TEXT, self.chunk_limit)[0]]
        if not self.check_rate(chat_id):
            return [f"🛑 Rate limit tercapai "
                    f"({self.rate_limit_per_minute} perintah/menit). "
                    "Tunggu sebentar lalu coba lagi."]
        dispatch = {
            "/insight": self.cmd_insight,
            "/scan": self.cmd_scan,
            "/watch": self.cmd_watch,
            "/flush_cache": self.cmd_flush_cache,
            "/status": self.cmd_status,
        }
        fn = dispatch.get(cmd)
        if fn is None:
            return [f"❓ Perintah tidak dikenal: {cmd}\nKetik /help utk bantuan."]
        if cmd == "/scan":
            return fn(chat_id, args, progress_cb=progress_cb)
        return fn(chat_id, args)


def build_engine(bot_cfg: dict[str, Any], config_path: str = "config.yaml",
                 scanner_config_path: str = "scanner.yaml",
                 overrides: Optional[dict] = None) -> BotEngine:
    """Factory: gabungkan bot.yaml (whitelist dsb) + config.yaml + scanner.yaml."""
    cfg = load_config(config_path, overrides=overrides)
    scfg = load_scanner_config(scanner_config_path)
    notify_min = float(bot_cfg.get("notify_cooldown_minutes", 15))
    return BotEngine(
        config=cfg,
        scanner_config=scfg,
        allowed_chat_ids=set(bot_cfg.get("allowed_chat_ids") or []),
        rate_limit_per_minute=int(bot_cfg.get("rate_limit_per_minute", 10)),
        notify_cooldown_seconds=notify_min * 60.0,
        emoji=bool(bot_cfg.get("emoji", True)),
        progress_enabled=bool(bot_cfg.get("progress", True)),
    )
