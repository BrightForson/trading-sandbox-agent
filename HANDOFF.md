# Handoff — trading-sandbox-agent (session 2026-09-09 ~16:00 UTC, ALL WORK COMMITTED + PUSHED)

## READ THIS FIRST
Everything from the previous session is DONE, verified, committed, and pushed.
Five-tier paper system is fully live on GitHub Actions. Next phase: the 4-week
no-touch test — monitoring, not building.

## Session state (what just happened)
1. **Full suite green after models.py overhaul**: 199/199 passed. Two test-fake
   signatures needed the new `system=` kwarg (tests/test_tier4.py:450,
   tests/test_tier4_fixes.py:79).
2. **LLM gate verified live**: `FuturesLedger._llm_conviction` 5 trials across
   two runs — clean verdicts with data-citing reasons (take=True conf
   0.78-0.90). One trial exposed a REAL bug:
   - **`select_model` was chain-head-always, so mid-gate "rotation" was a
     no-op** — a narrating model blocked the gate 8 draws in a row. Fixed:
     new `rotate_model()` (next-in-chain, true rotation) wired into
     `generate_json` draws 3/6 (bot/models.py). After fix, kimi probe failed
     under load, rotation walked to nemotron-nano-omni and STILL returned
     clean JSON — failover working as designed.
3. **Live cycles verified**:
   - `tools/tier5.py cycle`: opened BTC SHORT margin $10.24, conf 0.78.
   - `tools/tier4.py cycle`: meme filter rejected 4 non-meme coins (AI,
     launchpad categories), LLM gate passed venice-token conf 0.85 →
     $9 stake. Tier 4's first-ever entries now flowing on merit.
4. **All pushed in 5 commits** (heartbeat fallback, Tier 5, Tier 4 meme
   filter, models.py overhaul + rotation, journal). Rebase conflict on
   data/trades.db resolved via the 3-way merge_db.py recipe (as documented
   in safe_commit.sh).
5. **GitHub verified**: tests.yml green on push; `futures` workflow
   registered; first dispatch run completed SUCCESS (3 signals, equity
   $49.91, safe commit back). Pinger staleness net now also covers futures.
6. **Docs updated**: PROJECT_SUMMARY.md (Tier 5 in tier list/layout/config/
   crons/model chain/kill-keep/ops, five-tier, 199 tests), DEEP_DIVE_FINDINGS
   (F11 meme filter closed), PINGER_SETUP.md (staleness net + VPS note).
   validation.py ALL CHECKS PASSED (model roundtrip via rotation-active chain).

## The 4-week phase (from here)
- Start date: 2026-09-09. Kill/keep criteria per tier: PROJECT_SUMMARY.md
  "Kill/keep criteria" — gates.py measures them in the daily report.
- Do NOT touch strategies/params mid-test unless a KILL verdict fires or an
  infrastructure bug appears (discipline failures = infra, fix immediately).
- Watch: daily 18:00 UTC report in Discord; pain-meter for workflow health.

## Remaining owner actions (tell the owner when back)
1. **Rotate the Discord webhook secret** — pre-fix public CI logs may contain
   the old token (masking is in place now but old logs persist). Discord →
   channel settings → integrations → webhook → edit URL, then
   `gh secret set DISCORD_WEBHOOK_URL`.
2. Optionally eyeball the daily report pain-meter (report.yml uses
   GITHUB_TOKEN with actions:read — should work without GITHUB_API_TOKEN).
3. ~~VPS~~ → **solved via cron-job.org 2026-09-09**: two always-on cloud jobs
   dispatch trade+chat every 15 min (PAT: fine-grained, repo-only,
   Actions:rw, 90d — **rotate by 2026-12-07 or the jobs 401**). Local
   crontab pinger decommissioned (files kept at ~/.config/trading-pinger/
   for rollback; native schedules remain as reduced-rate fallback).
   Oracle card issue was the prepaid-card decline (OR_CCR_104) — needs a
   real credit/debit card, region irrelevant. A VPS only matters again
   on real-money day (Binance geo).

## Key context (settled decisions — do not re-derive)
- Tier 5: $50 virtual, 10x, 15m candles + 1h trend confirm; 25% kill under
  the ~29.9% single-liquidation floor (one bad trade can't kill; two can);
  second kill manual-only (reset-kill). SL/TP checked vs FULL candle ranges
  since open — missed cycles can't skip a touched stop.
- Tier 4 meme filter: CoinGecko categories must match meme_category_keywords
  (empty list disables). Non-meme coins never reach the LLM.
- LLM JSON recipe (do not regress): kimi-k3 first + JSON mode + system-role
  dossier + terse user prompt + redraw/TRUE-rotation. Discarded: JSON-mode
  alone, "Decide now"/long user prompts (trigger narration), fragment-regex
  alone. nemotron-3-super JSON compliance degrades under load (503s).
- Heartbeat: trade cycle posts hourly (dedupe by hour); agent cycle is the
  fallback when trade cycles missed. 15-min trade/chat delivery is now
  guaranteed by the cron-job.org cloud pinger (laptop can sleep); the local
  pinger's agent/futures staleness net was NOT replicated (blind POSTs) —
  acceptable: GitHub delivers hourly schedules reliably.
- Journal DB data/trades.db committed by safe_commit.sh; NEVER merge
  data/archive into it (pre-reset archives corrupted by design).
- Audit: `./venv/bin/python tools/audit_ledgers.py`; tests:
  `./venv/bin/python -m pytest tests/ -q` (199); sanity:
  `./venv/bin/python validation.py`.

## Post-session fix (2026-09-09 ~18:00 UTC, fee-journaling reconciliation)
- Audit was flagging PROBLEMS; root causes found and fixed:
  1. `config.yaml execution.taker_fee_pct` was 0.25 vs broker's actual 0.1
     — historical tier1 rows journaled inflated fees (orders 1-3 pre-fix).
     Repaired in DB (recomputed at 0.1%); config now 0.1 to match broker.
  2. Tier 4/5 ledgers journaled fee=0 rows (cash correct, audit blind) —
     `_log_trade` now journals actual fee (and funding on futures CLOSE).
  3. Audit excluded tier5 rows from tier1 filter → 2 phantom-sell false
     positives; tier5 gets an informational section (margins/kill state).
- Audit now CLEAN: tier1/tier2/tier4 reconcile to the cent.
- 4 regression tests added (fee journaling); suite 199/199 green.

## Current live state (at session end)
- Tier 1 paper: ~$99.77 (SMA bot, flat, whipsaw losses all-time -$0.31).
- Tier 3 wallet: 4 open bets, epoch alive.
- Tier 4: $39.82, 1 open (VENICE-TOKEN $9 @ conf 0.85).
- Tier 5: $49.90, 1 open (BTC/USD SHORT $10.24 margin @ conf 0.78), kills 0.
- Active LLM model: rotates under load (kimi-k3 primary, chain walks down
  when it 503s — rotation now true next-in-chain).
- Workflows live: trade/chat (15-min, cron-job.org cloud-pinged), agent/memecoin/futures
  (hourly, native cron), scanner (6h), report (daily 18:00 UTC), tests (on push).
