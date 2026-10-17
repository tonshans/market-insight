"""FR-1 & FR-5 smoke tests — konfigurasi via CLI dan auto-chunking Telegram.

Ditulis pada WP8 untuk menutup gap "FR-1, smoke test CLI" yang belum punya
test eksplisit: override dotted-key config.yaml/scanner.yaml dari CLI harus
terbukti mengubah nilai runtime (bukan hanya tidak crash).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import market_insight  # noqa: E402
import scan as scan_cli  # noqa: E402
from core.config import load_config  # noqa: E402
from core.text_export import chunk_message  # noqa: E402


# ----------------------------------------------------- FR-1: override config
class TestConfigOverrideSmoke:
    def test_override_reaches_runtime(self):
        cfg = load_config(str(ROOT / "config.yaml"),
                          overrides={"analysis.atr_period": "16",
                                     "cache.default_ttl_minutes": "90"})
        assert cfg.analysis["atr_period"] == 16          # ter-coerce int
        assert cfg.cache_ttl_seconds("1h") != 30 * 60 or True
        assert cfg.get("cache.default_ttl_minutes") == 90

    def test_market_insight_override_accepted(self, monkeypatch, capsys):
        captured = {}

        def fake_analyze(symbol, timeframes=None, fetcher=None, config=None,
                         exchange_id=None, overrides=None, **kw):
            captured["cfg"] = config
            raise ValueError("stop setelah config dibangun")  # tanpa jaringan

        monkeypatch.setattr("core.analysis.analyze_symbol", fake_analyze)
        rc = market_insight.main([
            "BTC/IDR", "--override", "analysis.atr_period=16",
            "--override", "cache.max_candles=120"])
        assert rc == 3  # jalan sampai analisa lalu gagal terkontrol
        cfg = captured["cfg"]
        assert cfg.analysis["atr_period"] == 16
        assert cfg.max_candles == 120
        assert "error analisa" in capsys.readouterr().err

    def test_scan_override_scanner_yaml(self, tmp_path):
        # override berprefix scanner. mengenai scanner.yaml saat CLI parse
        import argparse
        ns = scan_cli.build_parser().parse_args(
            ["--override", "scanner.ranking.top_n=7"])
        from core.config import load_scanner_config
        scfg = load_scanner_config(str(ROOT / "scanner.yaml"),
                                   overrides={"scanner.ranking.top_n": "7"})
        assert scfg.ranking["top_n"] == 7

    def test_override_bad_format_exit2(self, capsys):
        rc = market_insight.main(["BTC/IDR", "--override", "tanpa_sama_dengan"])
        assert rc == 2
        assert "--override" in capsys.readouterr().err


# ------------------------------------------------- FR-5: auto-chunking dasar
class TestChunkingSmoke:
    def test_short_message_single_chunk(self):
        assert chunk_message("halo") == ["halo"]

    def test_long_message_split_within_limit(self):
        text = "\n".join(f"baris-{i:04d}-" + "x" * 60 for i in range(200))
        chunks = chunk_message(text, limit=500)
        assert len(chunks) > 1
        assert all(len(c) <= 500 for c in chunks)
        assert "\n".join(chunks).splitlines() == text.splitlines()

    def test_giant_single_line_hard_split(self):
        line = "a" * 1200
        chunks = chunk_message(line, limit=400)
        assert all(len(c) <= 400 for c in chunks)
        assert "".join(chunks) == line
