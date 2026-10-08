import json
import os
import requests
from datetime import datetime, timezone
from bot.journal import TradeJournal


def shadow_snapshot():
    """Shadow account section for the daily report (virtual $80 ledger)."""
    try:
        from bot.config import config
        from bot.binance_data import BinanceDataClient
        from bot.shadow import ShadowAccount
        shadow = ShadowAccount(config, BinanceDataClient(config))
        lines = [f"AI Shadow Account (virtual ${shadow.start_cash:.0f}):",
                 f"- {shadow.status_line()}",
                 f"- Realized P&L from closed AI trades: ${shadow.realized_pnl():.2f}"]
        return "\n".join(lines)
    except Exception as e:
        return f"Shadow account snapshot unavailable: {e}"


def tier3_wallet_snapshot():
    """Tier 3 betting wallet section (virtual $60 Polymarket ledger)."""
    try:
        from bot.config import config
        from bot.wallet import BettingWallet
        wallet = BettingWallet(config)
        v = wallet.valuation()
        net = v["equity"] - wallet.start_cash
        lines = [f"Tier 3 Betting Wallet (virtual ${wallet.start_cash:.0f}, "
                 f"epoch {wallet.epoch}):",
                 f"- {wallet.status_line()}",
                 f"- Net P&L: {'+' if net >= 0 else '-'}${abs(net):.2f} | recent trend: {wallet.trend_line()}"]
        return "\n".join(lines)
    except Exception as e:
        return f"Tier 3 wallet snapshot unavailable: {e}"


def tier4_snapshot():
    """Tier 4 memecoin canary section (virtual ledger, human-gated only)."""
    try:
        from bot.config import config
        from bot.memecoin import MemecoinLedger
        ledger = MemecoinLedger(config)
        v = ledger.valuation()
        net = v["equity"] - ledger.start_cash
        cards = ledger.journal.get_tier4_cards(status="fresh")
        lines = [f"Tier 4 Memecoin Canary (virtual ${ledger.start_cash:.0f}, "
                 f"human-gated entries only):",
                 f"- {ledger.status_line()}",
                 f"- Net P&L: {'+' if net >= 0 else '-'}${abs(net):,.2f}",
                 f"- Fresh research cards: {len(cards)} (CoinGecko trending + "
                 f"DexScreener volume spikes; research only, never auto-executed)"]
        if ledger.kill_active():
            reason = ledger.journal.get_meta("t4_kill_reason") or "unknown"
            lines.append(f"- KILL ACTIVE: {reason} — entries blocked until "
                         f"manual reset (tools/tier4.py reset-kill)")
        return "\n".join(lines)
    except Exception as e:
        return f"Tier 4 canary snapshot unavailable: {e}"


def experiment_scorecards():
    """Deterministic evaluation of AI proposals and paper prediction bets."""
    try:
        journal = TradeJournal()
        proposals = journal.proposal_scorecard()
        bets = journal.bet_scorecard()
        return (
            "Experiment Scorecards:\n"
            f"- AI proposals evaluated: {proposals['evaluated']} | net simulated P&L: ${proposals['net_pnl']:.2f} "
            f"| win rate: {proposals['win_rate_pct']:.1f}% | avg BTC benchmark return: "
            f"{proposals['avg_benchmark_return_pct']:+.2f}%\n"
            f"- Prediction bets settled: {bets['settled']} | net P&L after recorded costs: ${bets['net_pnl']:.2f} "
            f"| win rate: {bets['win_rate_pct']:.1f}% | open exposure: ${bets['open_exposure']:.2f}"
        )
    except Exception as e:
        return f"Experiment scorecards unavailable: {e}"


WORKFLOW_SCHEDULES = {
    "chat": (15 * 60, 2),
    "agent": (60 * 60, 3),
    "scanner": (6 * 60 * 60, 12),
    "memecoin": (60 * 60, 3),
    # grace_runs used to be 30 here, which made the staleness threshold
    # 24h * 31 = 31 days. This is the report's OWN row, and the only thing that
    # ever renders it is this report running, so its age is always ~24h and the
    # row is structurally incapable of ever reporting STALLED. A dead report
    # workflow is exactly the case this meter exists to catch, and 31 days is
    # also longer than the 4-week graduation window. 3 keeps it consistent with
    # the other hourly workflows (4 days of silence).
    "report": (24 * 60 * 60, 3),
}


def actions_health():
    """Pain-meter: GitHub Actions workflow freshness, straight from the API.

    A missed cron shows up here long before it becomes "why didn't the bot
    trade?". Uses GITHUB_API_TOKEN if present (public repo read-only works
    unauthenticated, but the token avoids rate limits on the Actions API).
    """
    repo = os.getenv("GITHUB_REPOSITORY", "BrightForson/trading-sandbox-agent")
    token = os.getenv("GITHUB_API_TOKEN") or os.getenv("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        lines = []
        for wf, (interval, grace_runs) in WORKFLOW_SCHEDULES.items():
            try:
                resp = requests.get(
                    f"https://api.github.com/repos/{repo}/actions/workflows/{wf}.yml/runs",
                    params={"per_page": grace_runs},
                    headers=headers,
                    timeout=15,
                )
                resp.raise_for_status()
                runs = resp.json().get("workflow_runs", [])
            except Exception as e:
                lines.append(f"- {wf}: unavailable ({e})")
                continue
            if not runs:
                # A workflow that has NEVER run is not healthy. This rendered as
                # a plain informational line and matched none of the tokens in
                # the any_pain test below, so a disabled or never-dispatched
                # workflow looked identical to a working one.
                lines.append(f"- {wf}: NO RUNS FOUND ⚠")
                continue
            latest = runs[0]
            last_ts = datetime.fromisoformat(
                latest["created_at"].replace("Z", "+00:00")
            )
            age_h = (datetime.now(timezone.utc) - last_ts).total_seconds() / 3600
            failures = sum(1 for r in runs if r.get("conclusion") == "failure")
            status = latest.get("status") or "?"
            conclusion = latest.get("conclusion") or status
            if conclusion == "failure":
                verdict = "LAST RUN FAILED"
            elif age_h * 3600 > interval * (grace_runs + 1):
                verdict = "STALLED"
            else:
                verdict = "ok"
            age_txt = f"{age_h:.1f}h" if age_h < 48 else f"{age_h/24:.1f}d"
            lines.append(
                f"- {wf}: {verdict} | last run {age_txt} ago ({conclusion}, "
                f"{failures}/{len(runs)} recent failures)"
            )
        header = "Actions Pain-Meter (workflow freshness):"
        any_pain = any(("FAILED" in ln) or ("STALLED" in ln) or ("unavailable" in ln)
                       or ("NO RUNS FOUND" in ln)
                       for ln in lines)
        if any_pain:
            header += " ⚠ INVESTIGATE"
        return header + "\n" + "\n".join(lines)
    except Exception as e:
        return f"Actions pain-meter unavailable: {e}"


def gates_section():
    """Graduation gates in the daily report (recommend-only)."""
    try:
        from bot.gates import evaluate_gates
        return evaluate_gates()
    except Exception as e:
        return f"Graduation gates unavailable: {e}"


def create_daily_report():
    """Assemble the daily report from the per-tier sections."""
    return f"""=== Trading Bot Daily Report ===
{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}

{shadow_snapshot()}

{tier3_wallet_snapshot()}

{tier4_snapshot()}

{experiment_scorecards()}

{actions_health()}

{gates_section()}"""
