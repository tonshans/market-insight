#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bot.py — Telegram Bot Market Tools (F6, FR-5, WP8)

Perintah: /insight, /scan, /watch, /flush_cache, /status (+ /help /start).
Keamanan : whitelist chat_id wajib (bot.yaml), rate limit 10 perintah/menit,
auto-chunking pesan > 4096 karakter, notifikasi API_COOLDOWN anti-spam.

Seluruh logika ada di `botlib.py` (dapat di-test tanpa telegram/jaringan);
file ini hanya adapter async python-telegram-bot v20 + logging ke logs/bot.log.

Konfigurasi (bot.yaml — SECRET, jangan commit; NFR-4):
    telegram_token: "123456:ABC..."
    allowed_chat_ids: [111111111]
    rate_limit_per_minute: 10        # opsional
    notify_cooldown_minutes: 15      # opsional (anti-spam notif cooldown)
    emoji: true                       # opsional

Jalankan:
    python bot.py                     # token dari bot.yaml
    TELEGRAM_BOT_TOKEN=... python bot.py --config bot.yaml
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler

import botlib
from botlib import BotAuthError, BotEngine, build_engine, load_bot_config

logger = logging.getLogger("market_tools.bot")


def setup_logging(level: str = "INFO", log_dir: str = "logs") -> None:
    os.makedirs(log_dir, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = RotatingFileHandler(os.path.join(log_dir, "bot.log"),
                             maxBytes=1_000_000, backupCount=3,
                             encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for h in (fh, sh):
        root.addHandler(h)


def _sync_handler(engine: BotEngine):
    """Factory handler async: logika sinkron botlib jalan di worker thread
    supaya loop asyncio tidak terblokir saat fetch/analisa lama."""

    async def handler(update, context):
        chat_id = update.effective_chat.id
        # PTB memecah args per spasi; rekonstruksi perintah utuh dari pesan asli
        original = ((update.message.text if update and update.message else "")
                    or "").strip()
        first_word = original.split(maxsplit=1)[0] if original else ""
        full_cmd = (f"{first_word} " + " ".join(context.args or [])).strip() \
            if first_word else ""

        # Progress bar utk /scan (PRD F6): kirim 1 pesan, engine meng-edit via
        # update_sync (thread-safe, di-throttle). Non-scan: tanpa progres.
        pm = None
        progress_cb = None
        is_scan = first_word.lower().split("@", 1)[0] == "/scan"
        if is_scan and engine.progress_enabled:
            pm = ProgressMessenger(update.effective_message)
            await pm.start("⏳ Scan dimulai…")
            progress_cb = pm.update_sync

        try:
            messages = await asyncio.to_thread(engine.handle_update,
                                               chat_id, full_cmd,
                                               progress_cb)
        except BotAuthError as e:
            logger.warning("akses ditolak: %s", e)
            if pm is not None:
                await pm.finish()
            return  # chat asing: diam — jangan beri informasi apa pun (NFR-4)
        except Exception as e:  # jangan pernah matikan worker/polling
            logger.exception("handler error: %s", e)
            messages = [f"❌ Error internal: {e}"]
        if pm is not None:
            await pm.finish()
        for i, msg in enumerate(messages):
            await update.effective_message.reply_text(msg)
            if len(messages) > 1 and i < len(messages) - 1:
                await asyncio.sleep(0.15)  # hindari flood-limit utk multi-chunk

    return handler


def register_handlers(app, engine: BotEngine) -> None:
    """Wire command handler PTB -> BotEngine."""
    handler = _sync_handler(engine)
    for name in ("insight", "scan", "watch", "flush_cache", "status"):
        app.add_handler(_cmd(name, handler))
    app.add_handler(_cmd("help", handler))
    app.add_handler(_cmd("start", handler))


def _cmd(name: str, callback):
    from telegram.ext import CommandHandler

    class _NamedCommandHandler(CommandHandler):
        """CommandHandler + atribut `command` eksplisit utk inspeksi/test.

        PTB memakai __slots__ (tanpa __dict__), jadi nama tidak bisa di-set
        sebagai atribt biasa; subclass kecil menghindari ketergantungan pada
        atribut internal (`commands`) yang bisa berubah antar versi PTB.
        """
        __slots__ = ("command",)

        def __init__(self, command_name, cb):
            super().__init__(command_name, cb)
            self.command = command_name

    return _NamedCommandHandler(name, callback)


async def on_error(update, context) -> None:
    logger.exception("exception while handling update", exc_info=context.error)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="bot", description="Telegram bot Market Tools (WP8)")
    p.add_argument("--config", default="bot.yaml", help="Path bot.yaml (secret)")
    p.add_argument("--main-config", default="config.yaml", help="Path config.yaml")
    p.add_argument("--scanner-config", default="scanner.yaml", help="Path scanner.yaml")
    p.add_argument("--token", default=None, help="Override token (default: bot.yaml / env TELEGRAM_BOT_TOKEN)")
    args = p.parse_args(argv)

    try:
        bot_cfg = load_bot_config(args.config)
    except botlib.ConfigError as e:
        print(f"error: {_fmt(e)}", file=sys.stderr)
        print("Buat bot.yaml (lihat docstring bot.py); file tsb di-.gitignore (NFR-4).",
              file=sys.stderr)
        return 2

    token = args.token or os.environ.get("TELEGRAM_BOT_TOKEN") or bot_cfg.get("telegram_token")
    if not token:
        print("error: telegram_token kosong (bot.yaml / env TELEGRAM_BOT_TOKEN / --token)",
              file=sys.stderr)
        return 2
    if not bot_cfg.get("allowed_chat_ids"):
        print("error: allowed_chat_ids kosong di bot.yaml — whitelist WAJIB (FR-5/NFR-4).",
              file=sys.stderr)
        return 2

    try:
        engine = build_engine(bot_cfg, config_path=args.main_config,
                              scanner_config_path=args.scanner_config)
    except botlib.ConfigError as e:
        print(f"error konfigurasi: {_fmt(e)}", file=sys.stderr)
        return 2

    log_level = "INFO"
    try:
        log_level = engine.config.get("logging.level", "INFO")
        setup_logging(log_level, engine.config.get("logging.dir", "logs"))
    except Exception:
        setup_logging(log_level, "logs")

    try:
        from telegram.ext import ApplicationBuilder
    except ImportError:
        print("error: python-telegram-bot belum terpasang "
              "(pip install -r requirements.txt)", file=sys.stderr)
        return 2

    app = ApplicationBuilder().token(token).build()
    register_handlers(app, engine)
    app.add_error_handler(on_error)
    logger.info("Market Tools bot start; whitelist=%d chat, rate=%d/mnt",
                len(engine.allowed_chat_ids), engine.rate_limit_per_minute)
    app.run_polling(drop_pending_updates=True)
    return 0


def _fmt(e: Exception) -> str:
    cause = e.__cause__
    if cause is not None and str(cause):
        return f"{e} [{type(cause).__name__}: {cause}]"
    return str(e)


class ProgressMessenger:
    """Progress bar Telegram utk /scan: kirim 1 pesan, lalu di-edit berulang.

    Thread-safe utk dipanggil dari worker `asyncio.to_thread` (engine sinkron):
    update dikirim ke loop utama via `asyncio.run_coroutine_threadsafe`, dan
    di-throttle ~1 edit/detik (flood-limit Telegram aman). Kegagalan kirim/edit
    (chat diblokir, hiccup network) hanya mematikan progres — scan & hasil
    akhir tetap jalan.
    """

    def __init__(self, message, throttle_seconds: float = 1.0):
        self.message = message          # telegram.Message
        self.throttle_seconds = throttle_seconds
        self.broken = False
        self._last_edit: float | None = None
        self._loop = asyncio.get_running_loop()

    async def start(self, text: str = "⏳ Scan dimulai…") -> None:
        try:
            await self.message.reply_text(text)
        except Exception:
            self.broken = True

    def update_sync(self, text: str) -> None:
        """Dipanggil dari worker thread (progress_cb engine). Non-blocking."""
        if self.broken:
            return
        now = time.monotonic()
        if (self._last_edit is not None
                and now - self._last_edit < self.throttle_seconds):
            return
        self._last_edit = now
        try:
            asyncio.run_coroutine_threadsafe(self._edit(text), self._loop)
        except Exception:
            self.broken = True

    async def _edit(self, text: str) -> None:
        try:
            await self.message.edit_text(text[:4096])
        except Exception as e:
            # "message is not modified" bukan masalah; kegagalan lain: matikan progres
            if "not modified" not in str(e).lower():
                self.broken = True

    async def finish(self, final_text: str | None = None) -> None:
        """Edit ke pesan 'selesai' (default) — jangan hapus agar riwayat terbaca."""
        if self.broken:
            return
        try:
            await self.message.edit_text((final_text or "✅ Scan selesai.")[:4096])
        except Exception:
            pass  # pesan progres nice-to-have; jangan ganggu hasil utama


if __name__ == "__main__":
    sys.exit(main())
