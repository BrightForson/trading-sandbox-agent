# Status Report — 2026-09-07 (end of session)

Everything completed this session. Supersedes the 2026-09-06 report. Tests: 77/77 passing (`./venv/bin/python -m pytest tests/ -q`).

## 1. Commits (all local, not pushed — gh auth keyring broken)

| Commit | Work |
|---|---|
| `31997a3` (prior session, was uncommitted) | 7 bug fixes: stop-ledger self-heal, persistent cross state, report P&L pollution, chat self-echo, polymarket settlement, whale cache, misc batch |
| `ac97fde` | Day-zero reset of trades.db (see §2) |
| `4a527a8` | Actions pain-meter in the daily report (§3) |
| `9b522fc` | Per-tier kill/keep criteria in PROJECT_SUMMARY.md (§4) |
| `14a7400` | Agent-alpha graduation gate, wired into daily report (§5) |
| `ef266e3` | Idempotency keys in the execution path (§6) |

Push blocked by local gh keyring timeout — run `gh auth login` then `git push origin main` to publish.

## 2. Day-zero reset — done

- **Timestamp:** `2026-09-07T13:58:46Z` (persisted as meta key `day_zero_reset_at`)
- **Archives:** `data/archive/trades.db.pre-reset-2026-09-06T23-59-00Z` and `data/archive/trades.db.pre-reset-2026-09-07T13-58-46Z`, both committed
- **Cleared:** trades, bets, proposals, wallet_snapshots, paper/shadow/wallet/risk-baseline/whale-cache/strat-state meta
- **Preserved:** `discord_chat_last_seen` (so the chat poller won't re-answer old Discord messages), `discord_chat_channel_id`, `active_llm_model`
- **Day-zero state:** Tier 1 paper $20 flat; Tier 2 shadow falls back to $20 start cash; Tier 3 wallet $10, epoch 1; no open stops; no SMA relation state (absent `prev_relation` disables catch-up logic — no spurious day-zero trades)

## 3. Actions pain-meter — built

`bot/report.py::actions_health()` — new daily-report section. For each workflow (`trade`, `chat`, `agent`, `scanner`, `report`) it checks run freshness via the GitHub Actions API against the cron schedule and flags `STALLED` (past grace), `LAST RUN FAILED`, or API blindness, with a top-level `⚠ INVESTIGATE` marker. Report workflow passes `GITHUB_TOKEN` for API headroom (`.github/workflows/report.yml`).

**First live read caught a real stall:** GitHub's scheduler skipped the 15-min trade/chat crons for ~3h on 2026-09-07 (last trade run 12:18 UTC, nothing queued, quota fine — known high-frequency-cron quirk). If it recurs, consider an external `workflow_dispatch` pinger or hourly consolidation.

## 4. Kill/keep criteria — written

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

## 10. Bottom line

- Bug-fix work: committed and verified
- Day zero: reset, archived, timestamped
- Pain-meter, kill/keep criteria, agent-alpha gate, idempotency keys: all built, tested (77/77), committed
- Outstanding: push to origin (blocked on gh auth), monitor the Actions scheduler stall the pain-meter caught
