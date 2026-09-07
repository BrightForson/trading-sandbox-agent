# Handoff — trading-sandbox-agent

## Task
Full day-zero reset with new capital allocations (Tier 1: $100, Tier 2: $80, Tier 3: $60, Tier 4: $40). Added Tier 4 (memecoin canary) with CoinGecko trending + DexScreener volume-spike research signals, human-gated entries, entry-fixed stop-loss/take-profit, and hard max-drawdown kill threshold. **COMPLETE — 96/96 tests pass.**

## Done (session 3, 2026-09-07)
- Implemented `bot/memecoin.py` — `MemecoinLedger` class: entry/exit/kill, `coingecko_trending_cards()`, `dexscreener_spike_cards()`, research-card dedupe/expire, `sweep()`, `run_cycle()`, `status_line()`
- Implemented `bot/journal.py` tier4_cards table + `log_tier4_card` / `get_tier4_cards` / `has_tier4_card` / `expire_tier4_card` methods (additive)
- Implemented `bot/report.py` `[tier4-memecoin]` exclusion in `compute_pnl_and_winrate`, `tier4_snapshot()` section, `WORKFLOW_SCHEDULES["memecoin"] = (60*60, 3)`
- Implemented `config.yaml`: broker.start_cash 100, agent.shadow_start_cash 80 (max/position 40), scanner.wallet_start_cash 60 + wallet_stake 12, full memecoin section (start_cash 40, max_stake 12, SL 25%, TP 50%, time stop 72h, max drawdown 25%, fees 1%, slippage 100bps)
- Implemented `tools/tier4.py` — human-gated CLI (status/buy/sell/reset-kill/cycle)
- Implemented `tools/reset_day_zero.py` — archive + clear + new day_zero_reset_at + preserve 4 meta keys
- Implemented `tests/test_tier4.py` — 19 tests (2 more than planned: structural fail-closed + TTL expiry)
- Ran day-zero reset: `2026-09-07T22:43:44Z`, archived to `data/archive/trades.db.pre-reset-2026-09-07T22-43-44Z`
- Updated PROJECT_SUMMARY.md (four-tier intro, Tier 4 kill/keep criteria, ops commands), OPPORTUNITY_LAB.md (Tier 4 in current tier mapping), STATUS_REPORT.md (session 2 sections §11–§14)
- All committed locally: `77a19d7` (Tier 4 canary), `9745ba0` (day-zero reset), `e4294c9` (STATUS_REPORT)

## Open threads
- Push to origin blocked on gh auth keyring — run `gh auth login` then `git push origin main`
- Optional: add a GitHub Actions workflow for the hourly memecoin cycle (schedule entry exists in pain-meter; no workflow file created — needs owner decision on hosting cadence)

## Key context
- Tier 4 is canary-only per OPPORTUNITY_LAB.md: research signals feed human-reviewed decisions only; never autonomous execution
- Exit discipline: entry-fixed SL (25%) + TP (50%) + time stop (72h) enforced hourly; hard 25% max-drawdown kill flattens all + blocks new entries until `tools/tier4.py reset-kill`
- All Tier 4 state isolated via `[tier4-memecoin]` tag in trades table + tier4_cards table; `compute_pnl_and_winrate` excludes this tag
- No LLM in Tier 4 path — deterministic research from keyless APIs only
- Day-zero reset preserves discord_chat_last_seen, discord_chat_channel_id, active_llm_model, t4_kill_count; allocations fall back to config start_cash values
- Kill threshold structural: `MemecoinLedger.__init__` raises if exit/drawdown params ≤ 0 (fail-closed, never trades unprotected)

## Files that matter most (for continuing)
- `bot/memecoin.py` — Tier 4 Ledger + sweep/cycle
- `bot/journal.py` — tier4_cards table + methods
- `bot/report.py` — tag filter + tier4_snapshot() + schedules
- `config.yaml` — allocations + memecoin section
- `tests/test_tier4.py` — 19 test cases
- `tools/tier4.py` — CLI buy/sell/status/reset-kill
- `tools/reset_day_zero.py` — archive/reset mechanism

## Next steps (if continuing)
1. `gh auth login` + `git push origin main` (3 commits pending)
2. Decide on memecoin cycle hosting (Actions workflow `memecoin.yml` hourly, or manual `tools/tier4.py cycle`)
3. Monitor first live `memecoin` pain-meter entry in the daily report
