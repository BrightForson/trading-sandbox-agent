# Handoff — trading-sandbox-agent

## Task
Day-zero reset with new allocations + Tier 4 memecoin canary: **COMPLETE and live.** Follow-ups (push, Tier 4 workflow, delay diagnosis, pinger): **COMPLETE.**

## Current state (2026-09-08)
- **All work pushed to origin/main** through `e616e03`. gh auth recovered; no push debt.
- **96/96 tests pass** (`./venv/bin/python -m pytest tests/ -q`).
- **Tier 4 canary live**: virtual $40, human-gated entries only via `tools/tier4.py buy`; hourly Actions workflow (`memecoin.yml`, `:37`) does research cards + exit sweep only — never entries (verified by source inspection + tests).
- **Pinger LIVE on this machine**: crontab fires `workflow_dispatch` for chat + trade every 15 min (`~/.config/trading-pinger/pinger.sh`, token refreshed Sundays 04:00). Fixes GitHub's chronic scheduler under-delivery (measured: 183-min avg gaps, ~6-8% of requested runs; queue 0s, failures 0 — runs never created, STATUS_REPORT.md §16). First dispatches verified end-to-end (204 → completed/success).
- Allocations: T1 $100 / T2 $80 / T3 $60 (+$12 stakes) / T4 $40. Day-zero: `2026-09-07T22:43:44Z`, archived under `data/archive/`.

## If continuing
1. Verify pinger health after a day: `tail ~/.config/trading-pinger/pinger.log` (expect HTTP 204 lines) + check the daily report's pain-meter reads `ok` for trade/chat. Run counts should approach 96/day each.
2. Pinger pings only while this machine is awake. If it becomes unreliable, migrate to cron-job.org (cloud, free) — full instructions in PINGER_SETUP.md.
3. `agent` (hourly) and `scanner` (6h) stay on native schedules — hourly+ cadences haven't shown under-delivery.
4. Real-money day on Binance forces a non-US host anyway (geo) — that migration obsoletes the pinger. See STATUS_REPORT.md §16 venue table.

## Key context
- Tier 4 is canary-only per OPPORTUNITY_LAB.md: deterministic research (CoinGecko trending + DexScreener volume spikes, keyless, no LLM in path) feeds human-reviewed decisions; never autonomous execution.
- Tier 4 exits: entry-fixed SL 25% / TP 50% / time stop 72h, hourly sweep; hard 25% drawdown kill flattens + blocks entries until `tools/tier4.py reset-kill`. Kill count survives resets (structural).
- Tier 4 isolation: `[tier4-memecoin]` tag in trades table (excluded from Tier 1 P&L) + `tier4_cards` table.
- Day-zero reset preserves: `discord_chat_last_seen`, `discord_chat_channel_id`, `active_llm_model`, `t4_kill_count`. Tool: `tools/reset_day_zero.py` (--dry-run supported).
- `MemecoinLedger.__init__` raises if exit/drawdown params ≤ 0 — fail-closed, never trades unprotected.

## Files that matter
- `bot/memecoin.py` — Tier 4 ledger + sweep/cycle
- `tools/tier4.py` — human-gated CLI (status/buy/sell/reset-kill/cycle)
- `tools/reset_day_zero.py` — archive + reset
- `tests/test_tier4.py` — 19 tests
- `~/.config/trading-pinger/` — pinger scripts + log (NOT in repo)
- `PINGER_SETUP.md` — pinger ops + cron-job.org migration path
- `STATUS_REPORT.md` §11–§17 — session 2/3 record incl. measured delay diagnosis
