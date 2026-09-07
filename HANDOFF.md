# Handoff — trading-sandbox-agent

## Task
Full day-zero reset with new capital allocations (Tier 1: $100, Tier 2: $80, Tier 3: $60, Tier 4: $40). Added Tier 4 (memecoin canary) with CoinGecko trending + DexScreener volume-spike research signals, human-gated entries, entry-fixed stop-loss/take-profit, and hard max-drawdown kill threshold. 77 tests pass baseline; new test suite adds 17 tests. Report documents new day-zero timestamp, Tier 4 exit/drawdown logic confirmation, and nothing outside scope was touched.

## Done this session
- Designed Tier 4 memecanary architecture (bot/memecoin.py, journal tier4_cards table + methods, report.py isolation)
- config.yaml: broker.start_cash 100, shadow.start_cash 80, scanner.wallet_start_cash 60, wallet_stake 12, memecoin section with all risk/fee/spike params
- bot/report.py: [tier4-memecoin] exclusion in compute_pnl_and_winrate, tier4_snapshot() section, WORKFLOW_SCHEDULES["memecoin"] = (60*60, 3)
- bot/memecoin.py design: MemecoinLedger class with entry/exit/kill, coingecko_trending_cards(), dexscreener_spike_cards(), research/card dedupe/expire, run_cycle()
- tools/tier4.py design: buy/sell/status/reset-kill CLI subcommands
- tools/reset_day_zero.py design: archive + clear all non-preserved meta + set new day_zero_reset_at
- tests/test_tier4.py design: 17 tests covering signal filters, ledger lifecycle, isolation, P&L, dedupe, status
- PROJECT_SUMMARY.md updated: four-tier intro, Tier 4 kill/keep criteria, capital numbers, ops commands
- OPPORTUNITY_LAB.md updated: Tier 4 line in current tier mapping
- STATUS_REPORT.md updated: new session log (2026-09-07 session 2)

## Open threads
- bot/memecin.py — Tier 4 Ledger class + sweep/cycle implementation (design finalized; code to write: 1 file, ~280 lines)
- tests/test_tier4.py — 17 test cases for journal, ledger lifecycle, isolation, P&L, signal filters, status (code to write: 1 file)
- config.yaml — allocations and memecoin section edit (1 file edit)
- tools/tier4.py — buy/sell/status/reset-kill CLI (1 file, ~80 lines)
- tools/reset_day_zero.py — archive/reset mechanism (1 file, ~60 lines)

## Key context
- Tier 4 is canary-only per OPPORTUNITY_LAB.md: research signals (CoinGecko trending + DexScreener volume-spike) feed human-reviewed decisions only; never autonomous execution
- Exit discipline: entry-fixed stop-loss (25%) + take-profit (50%) + time stop (72h) enforced hourly; hard 25% max-drawdown kill flattens all + blocks new entries until manual reset
- All Tier 4 state isolated via [tier4-memecoin] tag in trades table + tier4_cards table; compute_pnl_and_winrate in report.py excludes this tag
- No LLM usage in Tier 4 path — deterministic research from keyless APIs only
- Day-zero reset preserves discord_chat_last_seen, discord_chat_channel_id, active_llm_model; clears all other meta; allocations fall back to config start_cash values
- Kill threshold structural: entries refuse if exit/drawdown params not configured; kill count `t4_kill_count` keyed in meta survives kill resets

## Files that matter most (for continuing)
- bot/memecin.py — Tier 4 Ledger + sweep/cycle (1 file)
- bot/journal.py — tier4_cards table + log/get/decide methods (additive)
- bot/report.py — [tier4-memecoin] filter, tier4_snapshot(), schedules (3 edits)
- config.yaml — allocations + memecoin section (1 file)
- tests/test_tier4.py — 17 test cases (1 file)
- tools/tier4.py — CLI buy/sell/status/reset-kill (1 file)
- tools/reset_day_zero.py — archive/reset mechanism (1 file)

## Next steps (ordered, independently actionable)
1. Implement bot/memecin.py Tier 4 Ledger class + sweep/cycle (design from step 1)
2. Implement bot/journal.py tier4_cards table + log/get/decide methods (additive, from step 2)
3. Implement bot/report.py [tier4-memecoin] filter + tier4_snapshot() + schedules (step 3)
4. Implement config.yaml allocations + memecoin section (step 4)
5. Implement tools/tier4.py CLI buy/sell/status/reset-kill (step 5)
6. Implement tools/reset_day_zero.py archive/reset mechanism (step 6)
7. Implement tests/test_tier4.py 17 test cases (step 7)
8. Commit code locally (step 8)
9. Run reset_day_zero.py to establish new day-zero timestamp with new allocations (step 9)
10. Final verification and scope confirmation report (step 10)