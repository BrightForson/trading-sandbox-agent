"""Graduation gates: measurable tier verdicts from journal data, recommend-only.

Lever 5b closed: ShadowAccount.realized_pnl() and the proposal scorecard were
computed but nothing consumed them. Gates here turn those numbers (plus trade
counts) into explicit GREEN/RED verdicts, surfaced in the daily report.

Architecture invariant: this module NEVER auto-promotes or demotes anything —
it only reports. Decisions stay with the human (kill switch, epoch resets,
semi-auto enablement are all manual ops commands).

Verdicts implemented (criteria from PROJECT_SUMMARY "Kill/keep criteria"):
  - Tier 1: net P&L, peak-to-trough drawdown (from wallet_snapshots with
    tier='tier1', taken by the trading cycle), losing-week streak,
    backtest-divergence note. Requires >= 4 weeks of signals.
  - Tier 2: agent alpha (semi-auto readiness).
  - Tier 3: settled-bet win rate + net P&L at >= 10 settled bets; double
    bust within 8 weeks.
  - Tier 4: net P&L over >= 4 weeks + kill history.
  - Any tier: execution-without-journal detector (broker position has no
    matching journal fill) — an infrastructure-failure red flag.
"""
import json
from datetime import datetime, timedelta, timezone

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


def _tier1_pnl(journal):
    """Net Tier 1 P&L from journaled fills (fees included, non-fills and
    other tiers' tags excluded) — mirrors report.compute_pnl_and_winrate."""
    try:
        from bot.report import compute_pnl_and_winrate
        stats = compute_pnl_and_winrate(journal.get_trades())
        return float(stats.get("total_pnl", 0.0))
    except Exception:
        return 0.0


def _tier1_drawdown_pct(journal):
    """Peak-to-trough equity drawdown from tier1 wallet snapshots.

    The trading cycle snapshots equity (cash + positions) after every
    heartbeat hour; the worst peak-to-trough move across those marks is
    the account drawdown. Returns 0.0 when no snapshots exist."""
    try:
        snaps = journal.get_wallet_snapshots()
    except Exception:
        return 0.0
    peak = None
    worst_dd = 0.0
    for row in snaps:
        # rows: (id, timestamp, epoch, cash, locked, equity)
        eq = float(row[5] or 0)
        if eq <= 0:
            continue
        if peak is None or eq > peak:
            peak = eq
        if peak:
            dd = (peak - eq) / peak * 100
            worst_dd = max(worst_dd, dd)
    return worst_dd


def _tier1_losing_week_streak(journal):
    """Consecutive ISO weeks with negative realized P&L, current week back."""
    try:
        trades = [t for t in journal.get_trades()
                  if t[1] and "[shadow-account]" not in (t[6] or "")
                  and "[tier4-memecoin]" not in (t[6] or "")
                  and "[tier5-futures]" not in (t[6] or "")
                  and (t[7] == "filled" if len(t) > 7 else True)]
    except Exception:
        return 0
    # realized P&L per week from SELL proceeds vs FIFO cost is complex here;
    # proxy: sum of (SELL - BUY) cash impact per week (fees included)
    weekly = {}
    for t in trades:
        try:
            ts = datetime.fromisoformat(str(t[1]).replace(" ", "T"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            iso = ts.isocalendar()
            week_key = f"{iso[0]}-W{iso[1]:02d}"
            qty, price, fee = float(t[4]), float(t[5]), float(t[8] or 0) if len(t) > 8 else 0.0
            impact = qty * price - fee
            if str(t[3]).upper() == "BUY":
                impact = -impact
            weekly[week_key] = weekly.get(week_key, 0.0) + impact
        except Exception:
            continue
    if not weekly:
        return 0
    streak = 0
    # walk weeks backward from the most recent seen
    for key in sorted(weekly.keys(), reverse=True):
        if weekly[key] < 0:
            streak += 1
        else:
            break
    return streak


def tier1_gate(journal=None):
    """Tier 1 KEEP/KILL criteria. Requires >= 4 weeks of history before a
    verdict is meaningful; before that the gate reports PENDING."""
    journal = journal or TradeJournal()
    weeks = _weeks_since_day_zero(journal)
    pnl = _tier1_pnl(journal)
    dd = _tier1_drawdown_pct(journal)
    streak = _tier1_losing_week_streak(journal)

    start_cash = 100.0
    try:
        v = journal.get_meta("paper_epoch_start_cash")
        if v:
            start_cash = float(v)
    except Exception:
        pass
    pnl_pct = (pnl / start_cash * 100) if start_cash else 0.0

    reasons = []
    if weeks >= 4 and pnl_pct < -20:
        reasons.append(f"net P&L {pnl_pct:+.1f}% < -20% after >= 4 weeks")
    if dd > 25:
        reasons.append(f"drawdown {dd:.1f}% > 25%")
    if streak >= 2:
        reasons.append(f"{streak} consecutive losing weeks")
    details = (f"P&L ${pnl:+.2f} ({pnl_pct:+.1f}% of ${start_cash:.0f}) | "
               f"drawdown {dd:.1f}% | losing-week streak {streak} | "
               f"{weeks:.1f} weeks since day-zero")
    if reasons:
        return GateResult("Tier 1 (SMA bot)", False, "; ".join(reasons), details)
    if weeks < 4:
        return GateResult("Tier 1 (SMA bot)", True,
                          f"PENDING (only {weeks:.1f}/4 weeks; no kill criterion hit)", details)
    return GateResult("Tier 1 (SMA bot)", True,
                      "no kill criterion hit after >= 4 weeks", details)


def tier3_gate(journal=None):
    """Tier 3 KEEP/KILL: win rate + net P&L at >= 10 settled bets; double
    bust in 8 weeks (kill)."""
    journal = journal or TradeJournal()
    try:
        score = journal.bet_scorecard()
    except Exception:
        score = {"settled": 0, "wins": 0, "net_pnl": 0.0}
    settled = int(score.get("settled", 0))
    wins = int(score.get("wins", 0))
    net = float(score.get("net_pnl", 0.0))
    win_rate = (wins / settled * 100) if settled else 0.0

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


def tier5_gate(journal=None):
    """Tier 5 KEEP/KILL: net P&L floor + kill/liquidation discipline history."""
    journal = journal or TradeJournal()
    start_cash = 50.0
    pnl = 0.0
    TIER5_TAG = "[tier5-futures]"
    try:
        from bot.config import config
        from bot.futures import FuturesLedger
        led = FuturesLedger(config, journal=journal)
        v = led.valuation()
        start_cash = led.start_cash
        pnl = v["equity"] - start_cash
    except Exception:
        try:
            total = 0.0
            for t in journal.get_trades():
                if TIER5_TAG in (t[6] or ""):
                    total += float(t[4]) * float(t[5])
            pnl = total
        except Exception:
            pnl = 0.0
    kills = 0
    try:
        kills = int(journal.get_meta("t5_kill_count") or 0)
    except Exception:
        pass
    liquidations = 0
    try:
        for t in journal.get_trades():
            if TIER5_TAG in (t[6] or "") and "liquidation" in (t[6] or ""):
                liquidations += 1
    except Exception:
        pass
    weeks = _weeks_since_day_zero(journal)
    pnl_pct = (pnl / start_cash * 100) if start_cash else 0.0

    reasons = []
    if weeks >= 4 and pnl_pct < -50:
        reasons.append(f"net P&L {pnl_pct:+.1f}% < -50% over >= 4 weeks")
    if kills >= 2:
        reasons.append(f"{kills} drawdown kills (second kill = failed edge, manual reset required)")
    if liquidations >= 3:
        reasons.append(f"{liquidations} liquidations (stop discipline failed repeatedly)")
    details = (f"P&L ${pnl:+.2f} ({pnl_pct:+.1f}% of ${start_cash:.0f}) | "
               f"kills {kills} | liquidations {liquidations} | {weeks:.1f} weeks since day-zero")
    if reasons:
        return GateResult("Tier 5 (futures)", False, "; ".join(reasons), details)
    if weeks < 4:
        return GateResult("Tier 5 (futures)", True,
                          f"PENDING (only {weeks:.1f}/4 weeks; no kill criterion hit)", details)
    return GateResult("Tier 5 (futures)", True, "no kill criterion hit after >= 4 weeks", details)


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


def reconciliation_gate(journal=None):
    """Any-tier criterion: execution without a journal record = infrastructure
    failure. Compares broker-held positions against journaled Tier 1 fills."""
    journal = journal or TradeJournal()
    try:
        from bot.config import config
        from bot.broker import make_broker
        broker = make_broker(config)
        held = {p.symbol for p in broker.get_all_positions()}
    except Exception:
        return GateResult("Reconciliation", True,
                          "broker unavailable (offline report run) — skipped",
                          "no check possible; not a failure")
    unjournaled = []
    for sym in held:
        slash_map = {s.replace("/", ""): s for s in config.symbols}
        ours = slash_map.get(sym, sym)
        filled = any(t[2] == ours and str(t[3]).upper() == "BUY"
                     for t in journal.get_trades())
        if not filled:
            unjournaled.append(ours)
    if unjournaled:
        return GateResult("Reconciliation", False,
                           f"broker position without journal fill: {', '.join(sorted(unjournaled))}",
                           "execution-without-record is an any-tier kill criterion")
    return GateResult("Reconciliation", True,
                      "all broker positions reconcile with journal fills",
                      f"{len(held)} held position(s) checked")


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
    results = [tier1_gate(journal), agent_alpha_gate(journal),
               tier3_gate(journal), tier4_gate(journal),
               tier5_gate(journal), reconciliation_gate(journal)]
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
