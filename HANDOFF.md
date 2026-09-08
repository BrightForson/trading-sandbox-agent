# Handoff — trading-sandbox-agent

## Task
Day-zero reset with new allocations + Tier 4 canary: **COMPLETE and live.** Follow-ups (push, Tier 4 workflow, delay diagnosis, pinger): **COMPLETE.** Brief money-first Discord messaging + widened Tier 2 scout universe: **COMPLETE, pushed.**

## Current state (2026-09-08)
- **All work pushed to origin/main** through `2d5a8ac` ("Brief money-first Discord messages + widen Tier 2 scout universe").
- **102/102 tests pass** (`./venv/bin/python -m pytest tests/ -q`).
- **All Discord messages are now brief and money-first.** Format contract in `bot/brief.py`: every tier reports equity vs allocation with profit/loss emoji — e.g. `📈 Tier 3 Bets: $68.00 (was $60 → +$8.00 (+13.3%))`. Quiet tiers get ONE short line (`no open trades / no open bets / nothing held`); no SMA/price noise when flat. Heartbeat (`trader.py::send_heartbeat`) stacks all 4 tiers in one message, once per UTC hour. Event alerts are one-liners: `🟢 Tier 1 BUY: $32.50 of BTC/USD @ $98,000.00`, `🎯 Tier 3 bet: YES on "..." @ 98¢ ($12 in)`, `🏆 Tier 3 bet WON: ... — +$0.24`.
- **Exhausted-cash suppression**: when the $80 Tier 2 ledger can't fund a scout BUY, the idea is STILL journaled + evaluated at its 24h horizon (info-gathering preserved), but Discord gets only `💵 Tier 2 AI: out of money — SYM idea saved, not filled`.
- **Tier 2 scout universe widened**: `agent.scout_extra_universe` in config.yaml (XRP, DOGE, ADA, AVAX, LINK) — scout may propose beyond BTC/ETH/SOL, priced via the same keyless Binance public data; babysitter + Tier 1 execution stay hard-scoped to `symbols:`. `agent.scout_symbols` = Tier 1 + extras; `_validate` checks kind against the right universe.
- **Tier 4 dedupe fix**: card TTL now measured from the NEWEST row per (kind, symbol) — `memecoin.py::_dedupe_and_log`.
- **Pinger LIVE on this machine**: crontab fires `workflow_dispatch` for chat + trade every 15 min (`~/.config/trading-pinger/pinger.sh`). Fixes GitHub's chronic scheduler under-delivery (measured: 183-min avg gaps; queue 0s, failures 0 — runs never created, STATUS_REPORT.md §16).
- Allocations: T1 $100 / T2 $80 / T3 $60 (+$12 stakes) / T4 $40. Day-zero: `2026-09-07T22:43:44Z`, archived under `data/archive/`.

## If continuing
1. Watch the first day of brief messages on Discord: heartbeats should be ~5 short lines; any regression to verbose alerts means an alert path was missed (grep `send_notification` callers against `bot/brief.py` contract).
2. Sizing answers given to user (keep): T1 = ATR-risk sizing, 0.5% equity risk/trade, $100 notional cap, max 3 positions, entry-fixed 3×ATR stop, NO take-profit (trend-following); T2 = $40/position cap, no SL (pure LLM measurement, user confirmed keep); T3 = $12 flat = 20% per bet; T4 = $12/30% per entry.
3. Pinger pings only while this machine is awake. If unreliable, migrate to cron-job.org (cloud, free) — PINGER_SETUP.md.
4. `agent` (hourly) and `scanner` (6h) stay on native schedules — hourly+ cadences haven't shown under-delivery.
5. Real-money day on Binance forces a non-US host anyway (geo) — that migration obsoletes the pinger. See STATUS_REPORT.md §16 venue table.
6. Verify pinger health after a day: `tail ~/.config/trading-pinger/pinger.log` (expect HTTP 204 lines) + pain-meter `ok` for trade/chat.

## Key context
- Tier 4 is canary-only per OPPORTUNITY_LAB.md: deterministic research (CoinGecko trending + DexScreener volume spikes, keyless, no LLM in path) feeds human-reviewed decisions; never autonomous execution.
- Tier 4 exits: entry-fixed SL 25% / TP 50% / time stop 72h, hourly sweep; hard 25% drawdown kill flattens + blocks entries until `tools/tier4.py reset-kill`. Kill count survives resets (structural).
- Tier 4 isolation: `[tier4-memecoin]` tag in trades table (excluded from Tier 1 P&L) + `tier4_cards` table.
- Day-zero reset preserves: `discord_chat_last_seen`, `discord_chat_channel_id`, `active_llm_model`, `t4_kill_count`. Tool: `tools/reset_day_zero.py` (--dry-run supported).
- `MemecoinLedger.__init__` raises if exit/drawdown params ≤ 0 — fail-closed, never trades unprotected.
- Shadow/virtual trades tagged `[shadow-account]` / `[tier4-memecoin]` are excluded from Tier 1 P&L (`report.py::compute_pnl_and_winrate`).
- Model rotation live: probe every 20h via `daily_health_check`, ranked failover chain (`nemotron-3-super-120b` → kimi-k3 → deepseek-v4-flash → …), switch announced to Discord, choice persisted in meta.

## Files that matter
- `bot/brief.py` — money-first message format contract (money_line, emoji_for)
- `bot/trader.py` — heartbeat (all-tier brief block), Tier 1 execution alerts
- `bot/agent.py` — Tier 2 scout/babysitter, exhausted-cash suppression, scout_symbols
- `bot/shadow.py` — $80 virtual ledger (tradable = Tier 1 + scout extras)
- `bot/polymarket.py` — Tier 3 scan/settle messages
- `bot/memecoin.py` — Tier 4 ledger + sweep/cycle, dedupe fix
- `tools/tier4.py` — human-gated CLI (status/buy/sell/reset-kill/cycle)
- `tools/reset_day_zero.py` — archive + reset
- `tests/test_core.py` — messaging/scout-universe/heartbeat tests
- `tests/test_tier4.py` — 20 tests
- `~/.config/trading-pinger/` — pinger scripts + log (NOT in repo)
- `PINGER_SETUP.md` — pinger ops + cron-job.org migration path
- `STATUS_REPORT.md` §11–§17 — session 2/3 record incl. measured delay diagnosis
