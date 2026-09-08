# Handoff — trading-sandbox-agent

## Task
Deep dive of the whole project (find bugs + pitch improvements), then fix P0: **COMPLETE.**
Full findings catalog: `DEEP_DIVE_FINDINGS.md` (32 findings, severity + status per item).

## Current state (2026-09-08, post-P0)
- **150/150 tests pass** (was 127; +23 new: merge_db 3-way unit+e2e rebase-conflict, Tier 4 re-arm/rug-guard/LLM-schema/buy-cap/batch-dedupe, notify masking/429/summary-fallback). `./venv/bin/python -m pytest tests/ -q`
- **`validation.py` ALL CHECKS PASSED** after the changes.
- **P0a — CI state integrity FIXED**: `tools/safe_commit.sh` now resolves binary trades.db conflicts via index stages (`:1:` base / `:2:` upstream / `:3:` ours — ours is the merge destination); `tools/merge_db.py` does a true 3-way meta merge (both-changed ⇒ resolving run wins; tavily counters MAX; `tier4_cards` + `wallet_snapshots` added to the row union — they were lost wholesale before); merge failure aborts rebase (fail-closed); final push failure = `exit 1` + best-effort Discord alert (no more silent green data loss); `fetch-depth: 0` on all 6 workflows. E2e test reproduces the exact rebase conflict and proves both sides' rows AND concurrent meta updates survive.
- **P0b — Tier 4 hotfixes**: kill auto-re-arm now runs BEFORE the kill check (was unreachable dead code); rug-guard rejects unknown pair age (fail-closed); LLM gate requires `isinstance(bool)` buy + numeric confidence (string "false"/"true" rejected); manual `buy()` enforces max_open_positions; batch card dedupe; case-normalized prechecks; prompt treats dossier text as data; `memecoin.yml` timeout 15→30 min.
- **P0c — Security/CI hygiene**: `_mask_secrets` in `bot/notify.py` + `bot/chat.py` (webhook URL never printed); 429 Retry-After + bounded retries in `_post_chunk`; file fallback appends to `$GITHUB_STEP_SUMMARY` on CI; requirements.txt pinned (bounded) + pytest declared; `tests/conftest.py` COMMITTED (was untracked — fresh clones would have run tests against the real webhook); new `.github/workflows/tests.yml` runs the suite on push/PR; chat says "Binance public data (simulated)" not "Alpaca"; `.env` chmod 600.

## If continuing
1. **PUSH the P0 fixes** (`git add` everything incl. tests/conftest.py, tests/test_merge_db.py, tests/test_tier4_fixes.py, tests/test_notify_fixes.py, DEEP_DIVE_FINDINGS.md, .github/workflows/tests.yml) — the five journal workflows now rebase with the fixed script on their next run.
2. **Watch the first day**: look for `resolved trades.db conflict` lines in Actions logs (now visible, previously silent), and the tests workflow's first green run.
3. **Remaining findings (P1/P2) are cataloged in DEEP_DIVE_FINDINGS.md** with file:line evidence. Recommended next order:
   - P1a: Tier 1 exit hardening — pending-exit intent flag (F12, whipsaw abandons missed death-cross exits), idempotency nonce for exits (F13, constant-reasoning suppression), `kill_switch` in reset_day_zero PRESERVED_META (F14), UTC timestamps (F15), risk-gate fail-closed (F18).
   - P1b: chat hardening — owner allowlist, per-message checkpoint, ops grounding (F25).
   - P2: Tier 4 ticker→coin-id resolution (F8, spike leg is dead code), stale-mark exclusion + price pacing (F9/F10), fee single-source-of-truth (F16), Tier 2 scout fixes (F20-F23), Tier 3 dead knobs/liquidity floor/event dedupe (F24), gates.py Tier 1/3/4 verdicts (F26), backtest parity (F27), report PAT (F29), journal infra (F30).
4. Pinger unaffected (still fires workflow_dispatch for trade+chat; runs will now rebase safely).
5. Rotate the Discord webhook if pre-fix CI logs may have captured the token (public logs, URL-embedded secret — cheap insurance).

## Key context
- Merge destination rule: the RESOLVING run's journal always wins shared-key conflicts (it is the newest commit); row tables union by logical key; tavily counters take MAX.
- `memecoin.yml` timeout is 30 min; worst-case cycle ~16 min (paced CG dossiers).
- conftest.py double-guards tests (delenv + no-op patches every send_notification holder) — keep it committed forever.
- Requirements are bounded pins (e.g. `openai>=3.7,<4`); venv has openai 3.7.0, pandas 3.0.5 — consistent.

## Files that matter
- `DEEP_DIVE_FINDINGS.md` — the full review: 32 findings, each with file:line, severity, status
- `tools/safe_commit.sh`, `tools/merge_db.py` — state integrity (heavily rewritten)
- `bot/memecoin.py` — P0b fixes at lines ~316 (buy cap), ~494 (age fail-close), ~537 (LLM schema), ~553 (re-arm order), ~870 (batch dedupe)
- `bot/notify.py` — masking + 429 + GITHUB_STEP_SUMMARY
- `.github/workflows/tests.yml` — new CI test workflow
- `tests/test_merge_db.py`, `tests/test_tier4_fixes.py`, `tests/test_notify_fixes.py` — new test files
- `PROJECT_SUMMARY.md` — bug log (fixed 2026-09-08) section appended
