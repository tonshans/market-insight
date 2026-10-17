"""WP8 smoke tests — Telegram Bot (botlib.py + adapter bot.py).

Semua test memakai dependency inject (analyze_fn/run_scan_fn/fetcher/state
palsu); TIDAK ada panggilan jaringan dan TIDAK perlu token Telegram.
Adapter `bot.py` diverifikasi lewat import & wiring handler dengan app mock.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import botlib  # noqa: E402
from botlib import (BotAuthError, BotEngine, NotificationGate,  # noqa: E402
                    SlidingWindowRateLimiter, build_engine, format_watchlist,
                    load_bot_config)
from core.config import load_config, load_scanner_config  # noqa: E402
from core.fetch import CircuitBreaker, FetchError, FetchResult  # noqa: E402
from core.scanner import ScanOutcome  # noqa: E402
from core.watchlist import Watchlist  # noqa: E402


# ------------------------------------------------------------------ helpers
def _mk_result(symbol="BTC/IDR"):
    return {
        "summary": {
            "symbol": symbol, "bias": "UPTREND", "agreement": "UNANIMOUS",
            "votes": {"up": 3, "down": 0, "flat": 0},
            "rr": {"mode": "auto", "rr": 2.1, "verdict": "ACCEPTABLE",
                   "entry": 100000.0, "stop": 95000.0, "target": 110500.0},
            "ref_timeframe": "1d", "max_severity": "WARN",
            "warnings": [{"code": "THIN_LIQUIDITY", "severity": "WARN",
                          "message": "volume tipis"}],
            "conflicts": [],
        },
        "timeframes": {
            tf: {"trend": {"label": "STRONG_UPTREND", "score_raw": 0.62,
                           "confidence": "HIGH",
                           "regime": {"name": "BULL_ALIGNED"},
                           "streak": 5, "maturity": "MATURE",
                           "details": {}},
                  "volume": {"avg": 100.0, "last": 120.0, "vsa_score": 0.4,
                             "wick_rejection": 0.2},
                  "sr": {"nearest_support": 90000.0, "nearest_resistance": 110000.0},
                  "risk": {"invalidation": 88000.0, "stop_type": "swing_low",
                           "risk_pct": 1.2, "risk_class": "MED"},
                  "invalidation": {"level": 88000.0}}
            for tf in ("1h", "4h", "1d")
        },
        "meta": {"fetch": {tf: {"source": "cache", "age_seconds": 3.0,
                                "bars": 120} for tf in ("1h", "4h", "1d")}},
    }


class FakeFetcher:
    def __init__(self, breaker=None):
        self.breaker = breaker or CircuitBreaker()
        self.calls = []

    def fetch_ohlcv(self, symbol, tf, limit=200):
        raise AssertionError("FakeFetcher tidak untuk fetch asli")


class FakeState:
    def __init__(self):
        self.kv: dict = {}
        self.saved: list = []

    def get_bot_state(self, key, default=None):
        return self.kv.get(key, default)

    def set_bot_state(self, key, value):
        self.kv[key] = value

    def save_scan_result(self, ts, rows):
        self.saved.append(list(rows))
        return len(rows)

    def get_last_scan(self):
        return None


def _mk_engine(tmp_path, analyze_fn=None, run_scan_fn=None, breaker=None,
               rate=10, notify_s=900.0, chat=111):
    cfg = load_config(str(ROOT / "config.yaml"), overrides={
        f"state.watchlist_path": f"{tmp_path}/watchlist.yaml",
        "cache.dir": f"{tmp_path}/cache",
        "state.db_path": f"{tmp_path}/state.db",
    })
    calls = {}

    def default_analyze(symbol, timeframes=None, fetcher=None, config=None,
                        **kw):
        calls["symbol"] = symbol
        calls["timeframes"] = timeframes
        return _mk_result(symbol)

    engine = BotEngine(
        config=cfg,
        scanner_config=load_scanner_config(str(ROOT / "scanner.yaml")),
        fetcher=FakeFetcher(breaker),
        state=FakeState(),
        allowed_chat_ids={chat},
        rate_limit_per_minute=rate,
        notify_cooldown_seconds=notify_s,
        analyze_fn=analyze_fn or default_analyze,
        run_scan_fn=run_scan_fn,
        format_scan_fn=lambda oc, emoji=True, title="PROSPEK HARI INI":
            "SCAN RESULT\n" + "\n".join(r["symbol"] for r in oc.rows),
    )
    return engine, calls


def _outcome(rows=None, cooldown=False):
    rows = rows if rows is not None else [
        {"rank": 1, "symbol": "UPCOIN/IDR", "adj_score": 71.2,
         "prospect": 80.0, "risk": 17.6, "trend_label": "STRONG_UPTREND",
         "adx": 28.0, "rr": 2.1}]
    return ScanOutcome(ts_utc="2026-10-17T00:00:00Z", rows=rows,
                       universe_size=120, prefiltered_size=40,
                       analysed_size=len(rows), errors={},
                       cooldown_active=cooldown, duration_seconds=1.5)


# ---------------------------------------------------------------- whitelist
class TestWhitelist:
    def test_foreign_chat_rejected(self, tmp_path):
        engine, _ = _mk_engine(tmp_path)
        with pytest.raises(BotAuthError):
            engine.handle_update(999999, "/status")

    def test_whitelisted_chat_ok(self, tmp_path):
        engine, _ = _mk_engine(tmp_path)
        msgs = engine.handle_update(111, "/help")
        assert msgs and "/insight" in msgs[0]

    def test_nonnumeric_chat_id_rejected(self, tmp_path):
        engine, _ = _mk_engine(tmp_path)
        with pytest.raises(BotAuthError):
            engine.assert_whitelisted("abc")

    def test_empty_whitelist_blocks_everyone(self, tmp_path):
        cfg = load_config(str(ROOT / "config.yaml"))
        engine = BotEngine(config=cfg, allowed_chat_ids=set())
        with pytest.raises(BotAuthError):
            engine.handle_update(111, "/help")


# ---------------------------------------------------------------- rate limit
class TestRateLimit:
    def test_ten_allowed_eleventh_blocked(self, tmp_path):
        engine, _ = _mk_engine(tmp_path, rate=10)
        oks = [engine.check_rate(111) for _ in range(10)]
        assert all(oks)
        assert engine.check_rate(111) is False

    def test_window_expiry_restores(self, tmp_path):
        eng, _ = _mk_engine(tmp_path, rate=10)
        rl = SlidingWindowRateLimiter(10, 60.0)
        t0 = 1000.0
        assert all(rl.check(1, now=t0 + i) for i in range(10))
        assert rl.check(1, now=t0 + 10) is False
        # setelah jendela bergeser (semua hit > 60s lalu) -> boleh lagi
        assert rl.check(1, now=t0 + 100) is True

    def test_rate_limit_isolated_per_chat(self):
        rl = SlidingWindowRateLimiter(2, 60.0)
        t0 = 500.0
        assert rl.check("a", now=t0) and rl.check("a", now=t0)
        assert rl.check("a", now=t0) is False
        assert rl.check("b", now=t0) is True

    def test_handle_update_returns_limit_message(self, tmp_path):
        engine, _ = _mk_engine(tmp_path, rate=1)
        first = engine.handle_update(111, "/watch")
        assert first and "Watchlist" in first[0]
        second = engine.handle_update(111, "/watch")
        assert "Rate limit" in second[0]


# ------------------------------------------------------------ auto-chunking
class TestChunking:
    def test_long_insight_split_into_chunks(self, tmp_path):
        def big_analyze(symbol, timeframes=None, **kw):
            r = _mk_result(symbol)
            r["summary"]["warnings"] = [
                {"code": f"W{i}", "severity": "WARN", "message": "m" * 100}
                for i in range(80)]
            return r
        engine, _ = _mk_engine(tmp_path, analyze_fn=big_analyze)
        engine.chunk_limit = 300  # kecilkan agar mudah memicu chunk
        msgs = engine.handle_update(111, "/insight BTC/IDR 2")
        assert len(msgs) > 1
        assert all(len(m) <= 300 for m in msgs)

    def test_short_output_single_chunk(self, tmp_path):
        engine, _ = _mk_engine(tmp_path)
        msgs = engine.handle_update(111, "/insight BTC/IDR")
        assert len(msgs) == 1


# ------------------------------------------------------ cooldown notification
class TestCooldownNotification:
    def test_scan_during_cooldown_notifies_once_then_suppressed(self, tmp_path):
        br = CircuitBreaker(failure_threshold=1, failure_window_seconds=60,
                           cooldown_seconds=300)
        assert br.record_failure() is True   # trigger cooldown
        assert br.in_cooldown
        scan_calls = []

        def fake_run(fetcher, cfg, scfg, state=None, **kw):
            scan_calls.append(kw)
            return _outcome()
        engine, _ = _mk_engine(tmp_path, breaker=br, run_scan_fn=fake_run,
                               notify_s=900)
        msgs = engine.handle_update(111, "/scan")
        assert msgs and "cooldown" in msgs[0].lower()
        assert scan_calls == []  # tidak menjalankan scan saat breaker open
        # perintah lain (mis. /insight sukses) tidak dapat notif ganda
        msgs2 = engine.handle_update(111, "/scan")
        assert msgs2 == []       # digate oleh NotificationGate anti-spam

    def test_gate_allows_after_notify_cooldown(self, tmp_path):
        gate = NotificationGate(FakeState(), cooldown_seconds=60)
        assert gate.allow(1, now=1000.0) is True
        assert gate.allow(1, now=1030.0) is False
        assert gate.allow(1, now=1061.0) is True
        assert gate.allow(2, now=1061.0) is True  # per-chat independen

    def test_gate_persists_in_state(self):
        st = FakeState()
        gate = NotificationGate(st, cooldown_seconds=60)
        gate.allow(42, now=1000.0)
        assert st.kv["notify:42"] == 1000.0
        # engine baru dgn state sama tetap ter-gate (lintas restart)
        gate2 = NotificationGate(st, cooldown_seconds=60)
        assert gate2.allow(42, now=1030.0) is False

    def test_insight_append_cooldown_notice(self, tmp_path):
        br = CircuitBreaker(failure_threshold=1, cooldown_seconds=300)
        br.record_failure()

        def boom(symbol, timeframes=None, **kw):
            raise FetchError("network down", cooldown_active=True)
        engine, _ = _mk_engine(tmp_path, breaker=br, analyze_fn=boom)
        msgs = engine.handle_update(111, "/insight BTC/IDR")
        assert any("API_COOLDOWN" in m for m in msgs)

    def test_scan_cooldown_flag_appended(self, tmp_path):
        engine, _ = _mk_engine(tmp_path, run_scan_fn=lambda *a, **k:
                               _outcome(cooldown=True))
        msgs = engine.handle_update(111, "/scan")
        assert any("API_COOLDOWN" in m for m in msgs)
        # notif kedua kali digate
        msgs2 = engine.handle_update(111, "/scan")
        assert not any("API_COOLDOWN" in m for m in msgs2)


# ------------------------------------------------------------------ commands
class TestCommands:
    def test_insight_parses_symbol_tf_level(self, tmp_path):
        engine, calls = _mk_engine(tmp_path)
        msgs = engine.handle_update(111, "/insight eth/idr 1h 4h 2")
        assert calls["symbol"] == "ETH/IDR"
        assert calls["timeframes"] == ["1h", "4h"]
        assert "ETH/IDR" in msgs[0]

    def test_insight_bad_symbol_and_tf(self, tmp_path):
        engine, _ = _mk_engine(tmp_path)
        assert "tidak valid" in engine.handle_update(111, "/insight ???")[0]
        assert "Timeframe" in engine.handle_update(111, "/insight BTC/IDR 7x")[0]

    def test_insight_no_args_shows_usage(self, tmp_path):
        engine, _ = _mk_engine(tmp_path)
        assert "/insight PAIR" in engine.handle_update(111, "/insight")[0]

    def test_watch_crud(self, tmp_path):
        engine, _ = _mk_engine(tmp_path)
        assert engine.handle_update(111, "/watch add btc/idr breakout hari ini")[0].startswith("✅")
        listing = engine.handle_update(111, "/watch")[0]
        assert "BTC/IDR" in listing and "breakout hari ini" in listing
        assert "Dihapus" in engine.handle_update(111, "/watch remove BTC/IDR")[0]
        assert "kosong" in engine.handle_update(111, "/watch")[0].lower()
        engine.handle_update(111, "/watch add XRP/IDR")
        assert "dikosongkan" in engine.handle_update(111, "/watch clear")[0]

    def test_watch_invalid_input(self, tmp_path):
        engine, _ = _mk_engine(tmp_path)
        assert "❌" in engine.handle_update(111, "/watch add notapair")[0]
        assert "Pakai:" in engine.handle_update(111, "/watch add")[0]
        assert "tidak dikenal" in engine.handle_update(111, "/watch foo")[0]

    def test_scan_flags_forwarded(self, tmp_path):
        seen = {}

        def fake_run(fetcher, cfg, scfg, state=None, **kw):
            seen["kw"] = kw
            seen["state"] = state
            return _outcome()
        engine, _ = _mk_engine(tmp_path, run_scan_fn=fake_run)
        msgs = engine.handle_update(111, "/scan --watchlist --limit 5 --top 3")
        assert "SCAN RESULT" in msgs[0]
        assert seen["kw"]["watchlist_only"] is True
        assert seen["kw"]["limit"] == 5 and seen["kw"]["top_n"] == 3
        assert seen["state"] is engine.state
        # --no-save => state None
        engine.handle_update(111, "/scan --no-save")
        assert seen["state"] is None

    def test_scan_unknown_flag(self, tmp_path):
        engine, _ = _mk_engine(tmp_path)
        assert "tidak dikenal" in engine.handle_update(111, "/scan --bogus")[0]

    def test_scan_error_reported(self, tmp_path):
        def boom(*a, **k):
            raise RuntimeError("exchange exploded")
        engine, _ = _mk_engine(tmp_path, run_scan_fn=boom)
        assert "Scan gagal" in engine.handle_update(111, "/scan")[0]

    def test_flush_cache_removes_files(self, tmp_path):
        cachedir = tmp_path / "cache"
        cachedir.mkdir()
        (cachedir / "indodax__BTC_IDR__1h.parquet").write_text("x")
        (cachedir / "indodax__BTC_IDR__1h.parquet.meta.json").write_text("{}")
        engine, _ = _mk_engine(tmp_path)
        msg = engine.handle_update(111, "/flush_cache")[0]
        assert "2 file" in msg
        assert list(cachedir.iterdir()) == []

    def test_status_reports_breaker_and_scan(self, tmp_path):
        br = CircuitBreaker(failure_threshold=1, cooldown_seconds=300)
        engine, _ = _mk_engine(tmp_path, breaker=br)
        engine.state.set_bot_state("last_scan", {"ts_utc": "T", "n_universe": 100,
                                                 "n_candidates": 20})
        text = engine.handle_update(111, "/status")[0]
        assert "Circuit breaker" in text and "sehat" in text
        assert "Scan terakhir" in text and "T" in text
        assert "Rate limit" in text and "10/menit" in text
        br.record_failure()
        text2 = engine.handle_update(111, "/status")[0]
        assert "COOLDOWN" in text2

    def test_unknown_command(self, tmp_path):
        engine, _ = _mk_engine(tmp_path)
        assert "tidak dikenal" in engine.handle_update(111, "/bogus")[0]

    def test_command_with_bot_mention(self, tmp_path):
        engine, calls = _mk_engine(tmp_path)
        engine.handle_update(111, "/insight@MarketToolsBot BTC/IDR")
        assert calls["symbol"] == "BTC/IDR"


# ------------------------------------------------------------------ factory
class TestBuildEngine:
    def test_build_from_bot_yaml(self, tmp_path):
        p = tmp_path / "bot.yaml"
        p.write_text("telegram_token: 'a:b'\nallowed_chat_ids: [7, 8]\n"
                     "rate_limit_per_minute: 3\nnotify_cooldown_minutes: 2\n")
        cfg = load_bot_config(str(p))
        assert cfg["allowed_chat_ids"] == [7, 8]
        engine = build_engine(cfg, config_path=str(ROOT / "config.yaml"),
                              scanner_config_path=str(ROOT / "scanner.yaml"))
        assert engine.allowed_chat_ids == {7, 8}
        assert engine.rate_limit_per_minute == 3
        assert engine.notify_cooldown_seconds == 120.0
        assert engine.check_rate(7) and engine.check_rate(7) and \
            engine.check_rate(7) and not engine.check_rate(7)

    def test_missing_file_returns_empty_dict(self, tmp_path):
        assert load_bot_config(str(tmp_path / "none.yaml")) == {}

    def test_watchlist_shared_with_core(self, tmp_path):
        engine, _ = _mk_engine(tmp_path)
        engine.handle_update(111, "/watch add SOL/IDR")
        wl_path = engine.config.get("state.watchlist_path")
        assert "SOL/IDR" in Watchlist(wl_path).symbols  # file yang sama dgn CLI


# ------------------------------------------------------------- adapter bot.py
class TestBotAdapter:
    def test_import_and_symbols(self):
        import bot
        for name in ("main", "register_handlers", "_sync_handler",
                     "setup_logging", "on_error"):
            assert hasattr(bot, name), name

    def test_register_handlers_wires_commands(self):
        import bot

        class FakeApp:
            def __init__(self):
                self.handlers = []
                self.error_handlers = []

            def add_handler(self, h):
                self.handlers.append(h)

            def add_error_handler(self, h):
                self.error_handlers.append(h)

        engine, _ = _mk_engine(Path(".").resolve())
        app = FakeApp()
        bot.register_handlers(app, engine)
        names = sorted(h.command for h in app.handlers)
        assert names == sorted(["insight", "scan", "watch", "flush_cache",
                                "status", "help", "start"])

    def test_main_requires_token_and_whitelist(self, tmp_path, capsys, monkeypatch):
        import bot
        # tanpa bot.yaml sama sekali -> exit 2 pesan ramah
        rc = bot.main(["--config", str(tmp_path / "missing.yaml")])
        assert rc == 2
        assert "bot.yaml" in capsys.readouterr().err
        # token ada tapi whitelist kosong -> exit 2 (FR-5 wajib)
        p = tmp_path / "bot.yaml"
        p.write_text("telegram_token: 'a:b'\nallowed_chat_ids: []\n")
        rc = bot.main(["--config", str(p)])
        assert rc == 2
        assert "whitelist" in capsys.readouterr().err.lower() or "allowed_chat_ids" in capsys.readouterr().err

    def test_sync_handler_flow_with_fake_update(self, tmp_path):
        """Simulasi satu update PTB end-to-end di adapter (tanpa telegram)."""
        import asyncio
        import types

        import bot
        engine, calls = _mk_engine(tmp_path)
        handler = bot._sync_handler(engine)
        sent = []

        class Msg:
            text = "/insight BTC/IDR"

            async def reply_text(self, t):
                sent.append(t)

        update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=111),
                                       message=Msg(), effective_message=Msg())
        context = types.SimpleNamespace(args=["BTC/IDR"])
        asyncio.run(handler(update, context))
        assert calls["symbol"] == "BTC/IDR"
        assert sent and "BTC/IDR" in sent[0]

    def test_sync_handler_silences_foreign_chat(self, tmp_path):
        import asyncio
        import types

        import bot
        engine, _ = _mk_engine(tmp_path)
        handler = bot._sync_handler(engine)
        sent = []

        class Msg:
            text = "/status"

            async def reply_text(self, t):
                sent.append(t)

        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=666), message=Msg(),
            effective_message=Msg())
        asyncio.run(handler(update, types.SimpleNamespace(args=[])))
        assert sent == []  # chat asing dibungkam total
