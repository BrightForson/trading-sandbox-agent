#!/usr/bin/env python3
"""Tier 3 advisory: LLM review of open paper bets (read-only).

The owner asked for an LLM sanity pass over the open Polymarket bets
after the reset decision: for each open bet, the model sees the market
question, side, entry price, and stake, and returns an advisory verdict
with a fair probability estimate. ADVISORY ONLY — nothing is closed,
modified, or journaled as an outcome; settlement stays deterministic
(settle_open_bets is the single source of truth).

Usage: python tools/tier3_review.py
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.config import config
from bot.journal import TradeJournal
from bot.models import ModelManager
from bot.notify import send_notification


def review():
    journal = TradeJournal()
    open_bets = journal.get_open_bets()
    if not open_bets:
        print("no open paper bets — nothing to review")
        return 0
    try:
        model = ModelManager(journal=journal)
    except Exception as e:
        print(f"model unavailable: {e}")
        return 1
    # rows: id, timestamp, market, question, side, price, stake, outcome,
    #       payout, notes, fee, estimated_probability, expected_value
    lines = []
    for b in open_bets:
        lines.append(
            f"- bet #{b[0]}: \"{b[3] or b[2]}\" | our side: {b[4]} @ {b[5]:.2f} "
            f"(${b[6]:.0f} stake) | our entry est_prob: {b[11]}")
    prompt = f"""You are a prediction-market risk reviewer. For each OPEN paper
bet, estimate the CURRENT fair probability (0.00-1.00) that OUR side wins,
and give a verdict: hold (edge intact), watch (uncertain, no action), or
worry (thesis broken — flag it for the human). Bets cannot be closed from
here; this is advisory only.

Bets:
{chr(10).join(lines)}

Respond with ONLY a JSON array:
[{{"bet_id": int, "fair_prob": 0.0-1.0, "verdict": "hold"|"watch"|"worry", "note": "<= 20 words"}}, ...]"""
    try:
        verdicts = model.generate_json_arr(prompt, max_tokens=1200)
    except Exception as e:
        print(f"LLM review failed: {e}")
        return 1
    if not isinstance(verdicts, list):
        print("LLM returned no verdict list")
        return 1
    by_id = {}
    for v in verdicts:
        if isinstance(v, dict) and "bet_id" in v:
            by_id[int(v["bet_id"])] = v
    out_lines = []
    worry = 0
    for b in open_bets:
        v = by_id.get(b[0])
        if not v:
            out_lines.append(f"bet #{b[0]} [{b[4]} @ {b[5]:.2f}] — no LLM verdict returned")
            continue
        verdict = str(v.get("verdict", "watch")).lower()
        if verdict not in ("hold", "watch", "worry"):
            verdict = "watch"
        note = str(v.get("note", ""))[:120]
        try:
            fp = float(v.get("fair_prob"))
            fp_txt = f"{fp:.2f}"
        except (TypeError, ValueError):
            fp_txt = "?"
        if verdict == "worry":
            worry += 1
        out_lines.append(
            f"bet #{b[0]} [{b[4]} @ {b[5]:.2f} on \"{(b[3] or b[2])[:60]}\"] — "
            f"{verdict.upper()} | fair prob {fp_txt} | {note}")
    text = "\n".join(out_lines)
    print(text)
    try:
        send_notification(
            f"🔍 Tier 3 LLM bet review: {len(open_bets)} open bets, "
            f"{worry} flagged worry — advisory only\n{text}", config)
    except Exception as e:
        print(f"review notification failed: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(review())
