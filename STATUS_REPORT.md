# Status Report — 2026-09-07 (end of session)

Everything completed this session. Supersedes the 2026-09-06 report. Tests: 96/96 passing (`./venv/bin/python -m pytest tests/ -q`).

## 1. Commits (all local, not pushed — gh auth keyring broken)

| Commit | Work |
|---|---|
| `31997a3` (prior session, was uncommitted) | 7 bug fixes: stop-ledger self-heal, persistent cross state, report P&L pollution, chat self-echo, polymarket settlement, whale cache, misc batch |
| `ac97fde` | Day-zero reset of trades.db (see §2) |
| `4a527a8` | Actions pain-meter in the daily report (§3) |
| `9b522fc` | Per-tier kill/keep criteria in PROJECT_SUMMARY.md (§4) |
| `14a7400` | Agent-alpha graduation gate, wired into daily report (§5) |
| `ef266e3` | Idempotency keys in the execution path (§6) |
| `77a19d7` | **Tier 4 memecoin canary** (session 2, §11) |
| `9745ba0` | **Day-zero reset with new allocations** (session 2, §12) |

Push blocked by local gh keyring timeout — run `gh auth login` then `git push origin main` to publish.

## 2. Day-zero reset (superseded by session 2 — §12)

- **Timestamp:** `2026-09-07T13:58:46Z` (persisted as meta key `day_zero_reset_at`)
- **Archives:** `data/archive/trades.db.pre-reset-2026-09-06T23-59-00Z` and `data/archive/trades.db.pre-reset-2026-09-07T13-58-46Z`, both committed
- **Cleared:** trades, bets, proposals, wallet_snapshots, paper/shadow/wallet/risk-baseline/whale-cache/strat-state meta
- **Preserved:** `discord_chat_last_seen` (so the chat poller won't re-answer old Discord messages), `discord_chat_channel_id`, `active_llm_model`
- **Day-zero state:** Tier 1 paper $20 flat; Tier 2 shadow falls back to $20 start cash; Tier 3 wallet $10, epoch 1; no open stops; no SMA relation state (absent `prev_relation` disables catch-up logic — no spurious day-zero trades)

## 3. Actions pain-meter — built

`bot/report.py::actions_health()` — new daily-report section. For each workflow (`trade`, `chat`, `agent`, `scanner`, `report`) it checks run freshness via the GitHub Actions API against the cron schedule and flags `STALLED` (past grace), `LAST RUN FAILED`, or API blindness, with a top-level `⚠ INVESTIGATE` marker. Report workflow passes `GITHUB_TOKEN` for API headroom (`.github/workflows/report.yml`).

**First live read caught a real stall:** GitHub's scheduler skipped the 15-min trade/chat crons for ~3h on 2026-09-07 (last trade run 12:18 UTC, nothing queued, quota fine — known high-frequency-cron quirk). If it recurs, consider an external `workflow_dispatch` pinger or hourly consolidation.

## 4. Kill/keep criteria — written (session 1; Tier 4 criteria added session 2)

PROJECT_SUMMARY.md "Kill/keep criteria (per tier)" — measurable verdicts anchored to day zero:
- Tier 1: kill at < −20% P&L after ≥4 weeks, or >25% drawdown, or divergence from 30-day backtest; keep at ≥0% with gates intact
- Tier 2: kill at <10 proposals/4 weeks or red agent-alpha gate 4 straight weeks, or −10% after ≥20 proposals, or failing to beat BTC benchmark; keep if positive after ≥20 and beating benchmark
- Tier 3: kill on double bust within 8 weeks, or <40% win rate after ≥10 settled bets with negative P&L; keep if epoch survives 8 weeks positive
- Any tier: immediate kill on ledger corruption, silent gate bypass, or unjournaled execution

## 5. Agent-alpha graduation gate — wired (Kimi/SSS+ lever 5b closed)

New `bot/gates.py`: `agent_alpha_gate()` returns GREEN/RED from journal data. GREEN requires ALL of: ≥20 evaluated proposals, positive simulated P&L, avg proposal return > BTC benchmark, positive shadow realized P&L. Recommend-only — never auto-promotes. Surfaced in the daily report via `gates_section()`; runs timestamped in meta `agent_gate_history`.

Supporting fix: `journal.proposal_scorecard()` gained `avg_return_pct` (per-proposal return from entry→closed), so the benchmark comparison is return-vs-return instead of the old dollars-vs-percent.

## 6. Idempotency keys — implemented (Kimi/SSS+ pattern #1)

`bot/trader.py`: `_execute_signal` derives a deterministic `client_order_id` from `(symbol, action, reasoning, qty)` and checks a journal-backed registry (`executed_client_order_ids` meta, bounded at 500 newest) before submitting. A resent duplicate of an already-filled intent is suppressed instead of double-filling. Failures are never marked, so genuine retries still go through. Qty binds the key to the exact intended order — the same exit reason on a later, differently-sized position is NOT a duplicate.

## 7. Verified untouched

`git diff 31997a3..HEAD` shows zero changes to `bot/research.py`, `bot/agent.py`, `bot/polymarket.py`, `run_agent.py`, `run_scanner.py` — news/whale research feeds the agent's advisory prompts exactly as before; all session changes were report/execution/journal-side.

## 8. Tier 2 universe & short support (report only, no implementation)

- **Universe**: scout is hard-scoped to `config.symbols` (BTC/ETH/SOL) at three layers — prompt, validation (agent.py:104), shadow account (shadow.py:85). CoinGecko trending data is context only, never actionable. Widening = edit `symbols:` in config.yaml, but it also widens Tier 1's SMA trading and risk-cap consumption (tiers share the list).
- **Shorts**: deliberately unsupported. agent.py:122 rejects scout SELLs ("long-only experiment"); shadow account models long round-trips only; both brokers are spot (no negative positions). Shorting would need margin semantics in the paper broker + a risk-framework review — a tier-level design decision, not a toggle.

## 9. Remaining Kimi/SSS+ patterns (not implemented, per 2026-09-06 agreement status)

- Double-entry ledger, correlation-aware heat, regime labeling, reconciliation diff — all still open ideas, never committed to a plan doc. Correlation heat remains HANDOFF.md lever 4.

## 10. Bottom line (session 1)

- Bug-fix work: committed and verified
- Day zero: reset, archived, timestamped
- Pain-meter, kill/keep criteria, agent-alpha gate, idempotency keys: all built, tested (77/77), committed
- Outstanding: push to origin (blocked on gh auth), monitor the Actions scheduler stall the pain-meter caught

## 11. Tier 4 memecoin canary — built (session 2, commit `77a19d7`)

New `bot/memecoin.py` (`MemecoinLedger`) + `bot/journal.py` tier4_cards table + `tools/tier4.py` CLI + `tests/test_tier4.py` (19 tests) + report/config/docs wiring:

- **Canary-only, per OPPORTUNITY_LAB.md**: deterministic research signals (CoinGecko trending + DexScreener volume-spike, keyless APIs, no LLM anywhere in the Tier 4 path) produce review cards in the `tier4_cards` table. Entries happen ONLY via the human-gated CLI (`tools/tier4.py buy`); nothing auto-executes.
- **Exit discipline (verified by tests)**: every entry records entry-fixed stop-loss (25%), take-profit (50%), and a 72h time-stop anchor at entry; the levels never drift with later volatility. The hourly `sweep()` enforces all three plus the hard 25% max-drawdown kill, which flattens every position and blocks new entries until the manual `reset-kill` re-arms the peak.
- **Structural fail-closed**: `MemecoinLedger.__init__` raises if any exit/drawdown param is missing or ≤ 0 — entries can never run unprotected. Kill history (`t4_kill_count`) survives resets.
- **Isolation (verified by tests)**: virtual fills are tagged `[tier4-memecoin]` in the trades table; `compute_pnl_and_winrate` in report.py excludes the tag, so Tier 1's scorecard is untouched. `tier4_snapshot()` is a new daily-report section; `WORKFLOW_SCHEDULES["memecoin"] = (60*60, 3)` extends the pain-meter.
- **Prices**: keyless Binance public data first (shared `BinanceDataClient`), CoinGecko simple-price fallback; both degrade gracefully.

## 12. Day-zero reset #2 — done (session 2, commit `9745ba0`)

- **Timestamp:** `2026-09-07T22:43:44Z` (meta key `day_zero_reset_at`)
- **Archive:** `data/archive/trades.db.pre-reset-2026-09-07T22-43-44Z`, committed
- **New capital allocations (config.yaml):** Tier 1 paper $100, Tier 2 shadow $80 (max/position $40), Tier 3 wallet $60 with $12 stakes, Tier 4 canary $40 (max stake $12)
- **Cleared:** trades, proposals, bets, wallet_snapshots, tier4_cards, all non-preserved meta
- **Preserved:** `discord_chat_last_seen`, `discord_chat_channel_id`, `active_llm_model`, `t4_kill_count`
- **Day-zero state verified:** meta contains only the 4 keys above + `day_zero_reset_at`; T4 canary $40 flat, ACTIVE, kills 0

## 13. Scope confirmation (session 2)

`git diff fb4c469..HEAD` touches exactly: `bot/memecoin.py` (new), `bot/journal.py` (tier4_cards additive), `bot/report.py` (tag filter + snapshot + schedule), `config.yaml` (allocations + memecoin section), `tests/test_tier4.py` (new), `tools/tier4.py` (new), `tools/reset_day_zero.py` (new), `data/trades.db` (migration + reset), `data/archive/` (new archive), PROJECT_SUMMARY.md / OPPORTUNITY_LAB.md / HANDOFF.md (docs). **Zero changes** to `bot/research.py`, `bot/agent.py`, `bot/polymarket.py`, `bot/trader.py`, `bot/wallet.py`, `bot/shadow.py`, `run_agent.py`, `run_scanner.py`, `backtest.py`, `validation.py` — all tiers 1–3 behavior paths are byte-identical to session 1.

## 14. Bottom line (session 2)

- Tier 4 canary: built, tested (96/96), committed, day-zero verified
- Outstanding at the time: push to origin (blocked on gh auth)

## 15. Follow-ups — done (session 3)

### Push
`gh auth` recovered on its own. All 10 pending commits pushed (`31997a3..a3c2ce7`), then the follow-up commits (`bd1158c` memecoin workflow, docs) — origin/main is current. Verified `gh workflow list` shows the new `memecoin` workflow registered and active.

### Tier 4 Actions workflow (commit `bd1158c`)
`.github/workflows/memecoin.yml` — hourly at `:37` (offset from other crons), 15-min timeout, concurrency-grouped, journal committed via the lossless `safe_commit.sh` merge. **No-execution guarantee, verified by source inspection:** the workflow's only run command is `python tools/tier4.py cycle`, which maps to `MemecoinLedger.run_cycle()` — that method calls only `sweep()` (SL/TP/time-stop/drawdown checks on open positions, which only ever `_sell_position`) plus the two research-card generators (CoinGecko trending, DexScreener spikes). `buy` is a separate CLI subcommand the workflow never invokes. All entries remain manual via `tools/tier4.py buy`, consistent with the canary-only design in OPPORTUNITY_LAB.md. Also added: swept exits and kill events now send Discord notifications from `run_cycle()` (a 3am stop-loss was previously silent). Tests still 96/96.

## 16. Chat-reply delay diagnosis — measured (session 3)

**Complaint:** 10-15 min message-reply delays. Question: Actions slowness vs something else?

**Live pain-meter reading at diagnosis time:** `⚠ INVESTIGATE` — `trade: STALLED`, `chat: STALLED`, agent/scanner/report ok, memecoin awaiting first run.

**Raw measurements (Actions API, complete run history Sep 5 → Sep 7):**

| Metric | chat | trade | report |
|---|---|---|---|
| Queue time (created → run_started) | **0s, every run** | 0s | 0s |
| Run duration | 21-45s (avg 26s) | similar | similar |
| Failures | **0/18** | 0/17 | 0/3 |
| Runs vs cron request | **~5-9/day vs 96 requested** | ~5-8/day vs 96 | 3/3 days (100%) |
| Inter-run gap | avg **183 min** (95-379) | avg 195 min | ~24h, correct |

**Findings:**

1. **Not queue backlog** — created == run_started on 100% of runs. Once GitHub creates a scheduled run, it starts instantly.
2. **Not run duration or failures** — 26s average, zero failures. When a run happens, it's fast and healthy. LLM latency (seconds) is likewise exonerated.
3. **The scheduler simply never creates most runs.** The 15-min crons (`*/15` trade, `9,24,39,54` chat = 96/day each) are being collapsed to ~6-8% of requested runs, continuously since Sep 5.
4. **The Sep 7 "stall" was not a one-off 3-hour blip — it's the chronic condition.** The pain-meter caught one window of it; the full history shows every day looks like that. Max observed gap: 379 min (8× the 45-min STALLED threshold).

**Reply-latency math:** a message arriving at a random minute waits, on median, half the inter-run gap ≈ **~92 min**, not 0-15 min. The observed 10-15 min replies happen only when a message lands just before a scheduled run that actually fires.

**Verdict on migration thresholds: YES — explicitly past them, chronically.** The pain-meter's STALLED threshold is age > interval × (grace+1) = 45 min for chat/trade; both are past it at essentially every measurement, by up to 8×. This is the known high-frequency-cron quirk from §3, now measured as systemic rather than episodic.

**Options (not yet implemented — owner decision required):**
- **(a)** External `workflow_dispatch` pinger (e.g., a free uptime service hitting the dispatch API every 15 min) — keeps Actions hosting, bypasses scheduler laziness.
- **(b)** Consolidate chat to hourly — simple, but accepts up to 60-min reply latency; defeats the purpose of two-way chat.
- **(c) Recommended:** move the chat poller off Actions entirely (local box/VPS running `run_chat.py` on its own 15-min loop, journal still committed via git). Every other workflow tolerates scheduler sloppiness; chat is the only latency-sensitive one.

## 17. Bottom line (session 3)

- Pushed: all commits on origin/main; memecoin workflow registered and active
- Memecoin workflow: research + exit-sweep only; entries provably manual-only; exit/kill notifications added
- Chat delay: root cause measured and confirmed — chronic GitHub scheduler under-delivery on 15-min crons (183-min avg gap, 0s queue, 0% failures). LLM and queue backlog exonerated. Fix options (a)/(b)/(c) above await owner decision.
