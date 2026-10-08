"""Graduation gates: measurable tier verdicts from journal data, recommend-only.

Lever 5b closed: ShadowAccount.realized_pnl() and the proposal scorecard were
computed but nothing consumed them. Gates here turn those numbers (plus trade
counts) into explicit GREEN/RED verdicts, surfaced in the daily report.

Architecture invariant: this module NEVER auto-promotes or demotes anything —
it only reports. Decisions stay with the human (kill switch, epoch resets,
semi-auto enablement are all manual ops commands).

Verdicts implemented (criteria from PROJECT_SUMMARY "Kill/keep criteria"):
  - Tier 2: agent alpha (semi-auto readiness).
  - Tier 3: settled-bet win rate + net P&L at >= 10 settled bets; double
    bust within 8 weeks.
  - Tier 4: net P&L over >= 4 weeks + kill history.
"""
import json
from datetime import datetime, timezone

from bot.journal import TradeJournal

GATE_META_KEY = "agent_gate_history"
GATE_HISTORY_KEY = "gate_verdict_history"


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


def _weeks_since_day_zero(journal):
    dz = _day_zero(journal)
    if dz is None:
        return 0.0
    return (datetime.now(timezone.utc) - dz).total_seconds() / 604800.0


def tier3_gate(journal=None):
    """Tier 3 KEEP/KILL: win rate + net P&L at >= 10 settled bets; double
    bust in 8 weeks (kill)."""
    journal = journal or TradeJournal()
    try:
        score = journal.bet_scorecard()
    except Exception:
        score = {"settled": 0, "win_rate_pct": 0.0, "net_pnl": 0.0}
    settled = int(score.get("settled", 0))
    # bet_scorecard returns win_rate_pct, not a raw win count. Reading a
    # "wins" key that does not exist pinned the win rate to a hard 0%,
    # which fires the KILL below on any negative net at >= 10 settled bets.
    win_rate = float(score.get("win_rate_pct", 0.0) or 0.0)
    net = float(score.get("net_pnl", 0.0))

    reasons = []
    if settled >= 10 and win_rate < 40 and net < 0:
        reasons.append(f"win rate {win_rate:.0f}% < 40% at {settled} settled with net ${net:+.2f}")
    busts = 0
    epoch = 1
    try:
        epoch = int(journal.get_meta("wallet_epoch") or 1)
        busts = max(0, epoch - 1)
    except Exception:
        pass
    if busts >= 2:
        reasons.append(f"{busts} wallet busts (double bust = failed edge)")
    details = (f"{settled} settled bets, {win_rate:.0f}% win rate, net ${net:+.2f}, "
               f"epoch {epoch}, busts {busts}")
    if reasons:
        return GateResult("Tier 3 (Polymarket)", False, "; ".join(reasons), details)
    return GateResult("Tier 3 (Polymarket)", True,
                      ("no kill criterion hit" if settled else "PENDING (no settled bets yet)"),
                      details)


def tier4_gate(journal=None):
    """Tier 4 KEEP/KILL: net P&L floor + kill discipline history."""
    journal = journal or TradeJournal()
    start_cash = 40.0
    pnl = 0.0
    equity = None
    TIER4_TAG = "[tier4-memecoin]"
    try:
        from bot.config import config
        from bot.memecoin import MemecoinLedger
        led = MemecoinLedger(config, journal=journal)
        v = led.valuation()
        equity = v["equity"]
        start_cash = led.start_cash
        pnl = equity - start_cash
    except Exception:
        # offline-safe fallback: replay tier4-tagged fills
        try:
            total = 0.0
            for t in journal.get_trades():
                if TIER4_TAG in (t[6] or ""):
                    total += float(t[4]) * float(t[5])
            pnl = total
        except Exception:
            pnl = 0.0
    kills = 0
    try:
        kills = int(journal.get_meta("t4_kill_count") or 0)
    except Exception:
        pass
    weeks = _weeks_since_day_zero(journal)
    pnl_pct = (pnl / start_cash * 100) if start_cash else 0.0

    reasons = []
    if weeks >= 4 and pnl_pct < -25:
        reasons.append(f"net P&L {pnl_pct:+.1f}% < -25% over >= 4 weeks")
    if kills >= 2:
        reasons.append(f"{kills} drawdown kills (second kill = failed edge, manual reset required)")
    details = (f"P&L ${pnl:+.2f} ({pnl_pct:+.1f}% of ${start_cash:.0f}) | "
               f"kills {kills} | {weeks:.1f} weeks since day-zero")
    if reasons:
        return GateResult("Tier 4 (memecoin)", False, "; ".join(reasons), details)
    if weeks < 4:
        return GateResult("Tier 4 (memecoin)", True,
                          f"PENDING (only {weeks:.1f}/4 weeks; no kill criterion hit)", details)
    return GateResult("Tier 4 (memecoin)", True, "no kill criterion hit after >= 4 weeks", details)


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
        from bot.binance_data import BinanceDataClient
        from bot.shadow import ShadowAccount
        shadow_pnl = ShadowAccount(config, BinanceDataClient(config),
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


def _record_verdict_history(journal, results):
    """Persist a compact verdict series so multi-week conditions ('red 4
    consecutive weeks') are evaluable later."""
    now = datetime.now(timezone.utc).isoformat()
    entry = {"ts": now,
             "verdicts": {r.tier: ("GREEN" if r.passed else "RED") for r in results}}
    try:
        raw = journal.get_meta(GATE_HISTORY_KEY)
        history = json.loads(raw) if raw else []
        if not isinstance(history, list):
            history = []
        history.append(entry)
        journal.set_meta(GATE_HISTORY_KEY, json.dumps(history[-200:]))
    except Exception:
        pass


def evaluate_gates(journal=None):
    """All gates, for the daily report. Recommend-only by design."""
    journal = journal or TradeJournal()
    results = [agent_alpha_gate(journal), tier3_gate(journal),
               tier4_gate(journal)]
    lines = ["Graduation Gates (recommend-only, never auto-promote):"]
    for r in results:
        verdict = "GREEN" if r.passed else "RED"
        lines.append(f"- {r.tier}: {verdict} — {r.summary} ({r.details})")
    try:
        journal.set_meta(GATE_META_KEY,
                         datetime.now(timezone.utc).isoformat())
        _record_verdict_history(journal, results)
    except Exception:
        pass
    return "\n".join(lines)
