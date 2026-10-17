# -*- coding: utf-8 -*-
"""Build output text Level 1 & Level 2 — dipakai CLI maupun Telegram bot (F6).

Level 1 (ringkas, target baca < 10 detik — PRD §1.2):
    header bias + agreement, tabel mini per TF, R:R, warning teratas.
Level 2 (detail): semua itu + S/R zone, invalidation, volume/VSA, wick
    rejection, streak/maturity per TF.

ANSI aktif hanya pada TTY; bot dapat memanggil dengan ansi=False dan
emoji=True. Auto-chunking tersedia via chunk_message() untuk batas 4096
karakter Telegram (FR-5).
"""
from __future__ import annotations

import os
from typing import Any

TELEGRAM_LIMIT = 4096

BIAS_EMOJI = {
    "UPTREND": "📈", "DOWNTREND": "📉", "SIDEWAYS": "➖",
    "CONFLICTED": "⚠️",
}
SEV_EMOJI = {"HIGH": "🔴", "WARN": "🟡", "INFO": "🔵", "NONE": "✅"}
SEV_COLOR = {"HIGH": "\033[31m", "WARN": "\033[33m", "INFO": "\033[36m",
             "NONE": "\033[32m"}
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"


def _money(v: Any) -> str:
    if v is None:
        return "-"
    try:
        return f"{float(v):,.0f}".replace(",", ".")
    except (TypeError, ValueError):
        return str(v)


class _Style:
    def __init__(self, ansi: bool):
        self.ansi = ansi
        self.bold = BOLD if ansi else ""
        self.dim = DIM if ansi else ""
        self.reset = RESET if ansi else ""

    def sev(self, severity: str) -> str:
        return SEV_COLOR.get(severity, "") if self.ansi else ""


def render(result: dict[str, Any], level: int = 1, ansi: bool | None = None,
           emoji: bool = True) -> str:
    """Render hasil core.analysis.analyze_symbol menjadi teks."""
    if ansi is None:
        ansi = bool(os.isatty(1)) and os.environ.get("TERM", "") not in ("", "dumb")

    s = _Style(ansi)
    summary = result.get("summary", {})
    tfs = result.get("timeframes", {})
    meta = result.get("meta", {}).get("fetch", {})
    em = (lambda k: k) if emoji else (lambda k: "")

    bias = summary.get("bias", "SIDEWAYS")
    agree = summary.get("agreement", "NONE")
    sym = summary.get("symbol", "?")
    exch = summary.get("exchange", "")
    lines: list[str] = []

    head = f"{s.bold}{sym}{s.reset} ({exch}) | Bias: {em(BIAS_EMOJI.get(bias, '❔'))} " \
           f"{s.bold}{bias}{s.reset} [{agree}]"
    lines.append(head)

    # sumber data + umur (transparansi cache/cooldown)
    src_parts = []
    for tf, m in meta.items():
        tag = {"cache": "cache", "network": "live",
               "fallback_stale": "STALE!", "cooldown_stale": "COOLDOWN!"}.get(
            m.get("source"), m.get("source"))
        age = m.get("age_seconds") or 0
        src_parts.append(f"{tf}:{tag}" + (f"({age/60:.0f}m)" if age else ""))
    if src_parts:
        lines.append(f"{s.dim}data: {' '.join(src_parts)}{s.reset}")

    # ---- tabel mini per TF ---------------------------------------------------
    lines.append("")
    for tf, r in tfs.items():
        t = r["trend"]
        score = t.get("score_raw", 0.0)
        vol = r.get("volume", {})
        pa = r.get("price_action", {})
        row = (f"  {tf:<4} {t['label']:<17} raw={score:+.2f} "
               f"conf={t.get('confidence','?'):<6} "
               f"{t['regime'].get('name','?'):<20} "
               f"streak={t.get('streak',0)} ({t.get('maturity','-')})")
        if level >= 2:
            row += (f"\n       votes={t.get('votes')} "
                    f"vsa={vol.get('vsa_score',0):+.2f} "
                    f"wick={pa.get('wick_rejection_score',0):+.2f} "
                    f"vol_ratio={vol.get('ratio')}")
        lines.append(row)

    # ---- konflik confluence (F2) ---------------------------------------------
    conflicts = summary.get("conflicts") or []
    if conflicts:
        lines.append("")
        lines.append(f"{s.sev('HIGH')}Konflik TF:{s.reset}")
        for c in conflicts:
            a, b = c["timeframes"]
            lines.append(f"  - {a} {c['directions'][a]} vs {b} {c['directions'][b]}")

    # ---- R:R ------------------------------------------------------------------
    rr = summary.get("rr") or {}
    if rr.get("rr") is not None or rr.get("verdict") != "NOT_APPLICABLE":
        lines.append("")
        ref = summary.get("ref_timeframe", "?")
        verdict = rr.get("verdict", "-")
        vcol = s.sev({"ACCEPTABLE": "NONE", "MARGINAL": "WARN"}.get(verdict, "HIGH"))
        lines.append(f"R:R @{ref}: entry {_money(rr.get('entry'))} | "
                     f"stop {_money(rr.get('stop'))} | "
                     f"target {_money(rr.get('target'))} | "
                     f"{vcol}{s.bold}RR {rr.get('rr')}{s.reset} [{verdict}]"
                     + (f" (target: ATR projection)"
                        if rr.get("target_source") == "ATR_PROJECTION" else ""))
        if level >= 2 and rr.get("scenarios"):
            for sc in rr["scenarios"]:
                lines.append(f"       scenario target {_money(sc['target'])} -> RR {sc['rr']}")

    # ---- detail per TF (level 2) ----------------------------------------------
    if level >= 2:
        lines.append("")
        for tf, r in tfs.items():
            sr = r.get("sr", {})
            risk = r.get("risk", {})
            sup = _money(sr.get("nearest_support"))
            res = _money(sr.get("nearest_resistance"))
            inv = risk.get("invalidation")
            pos = sr.get("position_in_range")
            pos_s = f"{pos*100:.0f}%" if pos is not None else "-"
            lines.append(f"  {tf}: S={sup} R={res} pos-range={pos_s} | "
                         f"invalidation={_money(inv)} "
                         f"[{risk.get('stop_type','-')}] "
                         f"risk={risk.get('risk_pct','-')}% ({risk.get('risk_class','-')})")

    # ---- warnings ---------------------------------------------------------------
    warns = summary.get("warnings") or []
    lines.append("")
    if not warns:
        lines.append(f"{s.sev('NONE')}Warnings: {em(SEV_EMOJI['NONE'])} bersih{s.reset}")
    else:
        lines.append(f"Warnings ({len(warns)}) max={summary.get('max_severity')}:")
        show = warns if level >= 2 else warns[:3]
        for w in show:
            lines.append(f"  {em(SEV_EMOJI.get(w['severity'],''))} "
                         f"{s.sev(w['severity'])}[{w['severity']}]{s.reset} "
                         f"{w['code']}: {w['message']}")
        if len(warns) > len(show):
            lines.append(f"  {s.dim}+{len(warns)-len(show)} lainnya (gunakan --level 2){s.reset}")

    return "\n".join(lines)


def chunk_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Pecah pesan agar aman utk Telegram (FR-5 auto-chunking).

    Prioritas pecah di batas baris; baris yang lebih panjang dari limit
    dipaksa cut per `limit` karakter.
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    cur_len = 0
    for line in text.split("\n"):
        if len(line) > limit:  # baris raksasa: hard split
            if current:
                chunks.append("\n".join(current))
                current, cur_len = [], 0
            for i in range(0, len(line), limit):
                chunks.append(line[i:i + limit])
            continue
        add = len(line) + (1 if current else 0)
        if cur_len + add > limit:
            chunks.append("\n".join(current))
            current, cur_len = [line], len(line)
        else:
            current.append(line)
            cur_len += add
    if current:
        chunks.append("\n".join(current))
    return chunks
