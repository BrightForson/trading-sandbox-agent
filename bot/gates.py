"""Graduation gates: measurable tier verdicts from journal data, recommend-only.

Lever 5b closed: ShadowAccount.realized_pnl() and the proposal scorecard were
computed but nothing consumed them. Gates here turn those numbers (plus trade
counts) into explicit GREEN/RED verdicts, surfaced in the daily report.

Architecture invariant: this module NEVER auto-promotes or demotes anything —
it only reports. Decisions stay with the human (kill switch, epoch resets,
semi-auto enablement are all manual ops commands).
"""
from datetime import datetime, timezone

from bot.journal import TradeJournal

GATE_META_KEY = "agent_gate_history"


class GateResult:
    def __init__(self, tier, passed, summary, details):
        self.tier = tier
        self.passed = passed
        self.summary = summary
        self.details = details


def _day_zero(journal):
    ts = journal.get_meta("day_zero_reset_at")
    if ts:
        try:
            dt = datetime.fromisoformat(ts)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def agent_alpha_gate(journal=None):
    """Tier 2 semi-auto readiness: is the AI agent actually adding alpha?

    GREEN requires ALL of:
      - >= 20 evaluated scout proposals (sample size)
      - net simulated P&L > 0
      - beats the BTC benchmark on average (avg_benchmark_return_pct)
      - shadow account realized P&L > 0 (the executed-counterpart check)
    """
    journal = journal or TradeJournal()
    score = journal.proposal_scorecard()
    evaluated = int(score["evaluated"])
    net_pnl = float(score["net_pnl"])
    benchmark = float(score["avg_benchmark_return_pct"])
    avg_return_pct = score.get("avg_return_pct", 0.0)
    try:
        from bot.config import config
        from bot.broker import make_broker
        from bot.shadow import ShadowAccount
        shadow_pnl = ShadowAccount(config, make_broker(config),
                                   journal=journal).realized_pnl()
    except Exception:
        shadow_pnl = None

    min_evaluated = 20
    reasons = []
    if evaluated < min_evaluated:
        reasons.append(f"only {evaluated}/{min_evaluated} proposals evaluated")
    if net_pnl <= 0:
        reasons.append(f"simulated P&L ${net_pnl:.2f} not positive")
    if evaluated and avg_return_pct <= benchmark:
        reasons.append(f"avg proposal return {avg_return_pct:+.2f}% <= BTC benchmark "
                       f"{benchmark:+.2f}%")
    if shadow_pnl is not None and shadow_pnl <= 0:
        reasons.append(f"shadow realized P&L ${shadow_pnl:.2f} not positive")

    details = (f"evaluated {evaluated} | sim P&L ${net_pnl:.2f} "
               f"| avg return {avg_return_pct:+.2f}% vs BTC benchmark {benchmark:+.2f}% "
               f"| shadow realized ${shadow_pnl if shadow_pnl is not None else float('nan'):.2f}")
    if reasons:
        return GateResult("Tier 2 (agent alpha)", False,
                          "; ".join(reasons), details)
    return GateResult("Tier 2 (agent alpha)", True,
                     "positive edge over benchmark at sufficient sample size",
                     details)


def evaluate_gates(journal=None):
    """All gates, for the daily report. Recommend-only by design."""
    journal = journal or TradeJournal()
    results = [agent_alpha_gate(journal)]
    lines = ["Graduation Gates (recommend-only, never auto-promote):"]
    for r in results:
        verdict = "GREEN" if r.passed else "RED"
        lines.append(f"- {r.tier}: {verdict} — {r.summary} ({r.details})")
    try:
        journal.set_meta(GATE_META_KEY,
                         datetime.now(timezone.utc).isoformat())
    except Exception:
        pass
    return "\n".join(lines)
