# Handoff — trading-sandbox-agent

## Task
Full deep dive → fix everything → 4-week test phase readiness: **COMPLETE.**
All work pushed to origin/main through `999bfb2` (P1/P2 commit `1f6abd5` rebased on top).
Full findings catalog with per-item status: `DEEP_DIVE_FINDINGS.md` (F1–F32).

## Current state (2026-09-09 early UTC)
- **170/170 tests pass** (`./venv/bin/python -m pytest tests/ -q`). `validation.py` ALL CHECKS PASSED (risk check now isolated — no production meta writes). Backtest smoke-tested with the new parity sizing.
- **P0 (committed earlier, `ec8cfac`)**: 3-way stage-based journal merge (F1/F2), Tier 4 re-arm + rug-guard fail-close + strict LLM schema (F4/F5), memecoin timeout 30m (F6), webhook masking + 429 Retry-After + GITHUB_STEP_SUMMARY fallback (F3/F7), pinned reqs + committed conftest + tests.yml CI (F28), chmod 600 .env (F32).
- **P1a (this session)**: pending-exit intent flag `pending_exit_symbols` — a failed SELL now retries every cycle until filled regardless of SMA whipsaw (F12); exit idempotency keys fold UTC-day + per-symbol attempt counter so constant-reasoning exits can't be suppressed as duplicates (F13); `kill_switch` preserved by day-zero reset (F14); all journal writes UTC-aware ISO (F15); risk-gate BUY checks fail CLOSED when positions/equity can't be enumerated (F18); heartbeat baseline from `paper_epoch_start_cash` (written at seed; F19a); pre-trade alert rate-limited to 1/30min per intent (F19h); `1Hour`→`1h` interval fix + 429 last_err message (F19b/c).
- **P1b**: chat hardening (F25) — owner allowlist via `DISCORD_OWNER_IDS` env (empty = answers NOBODY, fail-closed), per-message checkpoint (cursor advances only after a successful send; failed sends retry next cycle), reply cap 5/cycle (rest deferred, not dropped), 429 Retry-After on replies, kill-switch state in system context + "answer only from context numbers, never invent" instruction, `_send_message` retry. **chat.yml passes `vars.DISCORD_OWNER_IDS`.**
- **P2**: F16 broker returns actual fee (`_Order.fee`), trader journals it (scorecard reconciles with ledger — the $0.098 drift decomposes exactly); F8 spike cards resolve ticker→CG id via cached `/coins/list` map (the leg was dead code), `price_for` uses map + 60s per-process cache + pacing; F9 stale-mark positions EXCLUDED from valuation (never entry-marked fictionally), stale_marks sweep event + Discord alert; F10 CoinGecko `/simple/price` paced + 429 retry; F11 reset_kill preserves unsold positions; kill escalation: 2nd kill requires manual `tools/tier4.py reset-kill` (t4_manual_reset_required); F20 BUY without symbol rejected; F21 scout suppressed while symbol held or open proposal exists (protects the ≥20-sample gate premise); F22 real 24h change (96 bars) + 6h change labeled separately; F23 research name map for XRP/DOGE/ADA/AVAX/LINK; F24 scanner.enabled honored, LLM candidates need liquidity ≥ min_market_liquidity (10k) AND full volume (no more /10), event-family dedupe via `event=<slug>` in bet notes, bust alert latched (cleared by start_new_epoch), prompt names the literal first outcome (no "(YES)"), wallet epoch cutoff normalizes timestamp formats; F26 gates.py implements Tier 1/3/4 verdicts + reconciliation gate (broker position w/o journal fill = RED) + `gate_verdict_history` meta for streaks; Tier 1 equity snapshots to wallet_snapshots (epoch=0) from the hourly heartbeat; F27 backtest `target_risk_pct_per_trade` ATR-risk sizing + `equity_cap_pct` allocation cap parity, combined-P&L caveat printed; F29 report.yml `actions: read` (pain-meter 403 fix); F31 conviction prompt frames dossier as untrusted data; F30 partial (bare-path journal guard); models.py `utcnow()`→aware (back-compat read of old naive values).

## ⚠ Owner actions needed (the ONLY things requiring you)
1. **Set `DISCORD_OWNER_IDS`** — GitHub repo → Settings → Secrets and variables → Actions → **Variables** tab (not Secrets) → new variable `DISCORD_OWNER_IDS` = your Discord user ID (comma-separate for multiple). Until set, **Bright Bot will answer nobody in chat** (fail-closed by design). Find your ID: Discord settings → Advanced → enable Developer Mode → right-click your name → Copy User ID.
2. **Rotate the Discord webhook** (Settings → Integrations → Webhooks → copy new URL → update `DISCORD_WEBHOOK_URL` secret). Cheap insurance: pre-fix public CI logs may contain the token (F3 existed for a while; masking is in place now but old logs persist).
3. Optionally verify `GITHUB_API_TOKEN` isn't needed anymore — report.yml now uses `GITHUB_TOKEN` with `actions: read`, so the pain-meter should work without any PAT.
4. (Optional, later) F17 fill-semantics decision: live paper fills at signal-bar close vs backtest next-bar open — left as-is; if you want exact parity, ask the next session to price paper fills off the forming candle.

## If continuing (new window)
1. **Nothing in-flight.** All P0/P1/P2 items above are implemented, tested (170/170), committed, and pushed (`999bfb2`). The 4-week test phase can start now.
2. Deferred-by-design items (documented in DEEP_DIVE_FINDINGS.md "P2-remaining"): F17 fill semantics, journal connection lifecycle (per-call connections still; benign for CI's short-lived processes), meme-category filter for Tier 4 trending (Polkadot/Zcash can still get researched; rug-guard + LLM gate still apply).
3. **Watch first days**: tests.yml green on push; heartbeats brief; `resolved trades.db conflict` lines in Actions logs = the new merge machinery working (previously silent); Tier 1 snapshots appearing in wallet_snapshots (epoch=0); daily report gates section now shows all 4 tiers + Reconciliation.
4. Pre-reset archives in `data/archive/` are corrupted (documented ETH ledger mismatch) and excluded from scorecards by design — never merge them into `data/trades.db`.
5. Audit tool available: `./venv/bin/python tools/audit_ledgers.py` — reconstructs each tier's cash/positions from journaled fills vs meta ledgers, detects duplicate fills/phantom sells. Run anytime; expect the fee line to reconcile now (F16).
6. Pinger untouched (`*/15` crontab → workflow_dispatch trade+chat). Agent hourly + scanner 6h remain on native schedules. All six workflows have `fetch-depth: 0` now.

## Key context for the 4-week phase
- Kill/keep criteria are now MEASURED: daily report "Graduation Gates" covers Tier 1 (P&L <-20% @4wk, DD >25%, 2 losing weeks), Tier 2 (agent alpha), Tier 3 (win rate <40% @≥10 settled + double bust), Tier 4 (P&L <-25% @4wk + kill count), Reconciliation (unjournaled position = RED). Verdict history accumulates in `gate_verdict_history` meta.
- Tier 1 fees in the journal are now the broker's ACTUAL charged fee (0.1%); `execution.taker_fee_pct` (0.25%) is backtest-only conservatism. Scorecard P&L now reconciles with ledger equity.
- Tier 4: after a SECOND drawdown kill, auto re-arm is disabled — manual `reset-kill` required (matches the "double kill = failed edge" criterion).
- Chat: messages from non-owner users are silently ignored (never answered, never disclosed to); empty DISCORD_OWNER_IDS = bot silent. Set the variable (owner action #1).
- New meta keys this session: `pending_exit_symbols`, `exit_attempt_counters`, `paper_epoch_start_cash`, `wallet_bust_alerted`, `gate_verdict_history`, `t4_manual_reset_required`, `pretrade_alert_<id>` (bounded by usage).

## Files that matter
- `DEEP_DIVE_FINDINGS.md` — F1–F32 catalog, each with file:line, severity, FIXED/DEFERRED status
- `bot/trader.py` — pending-exit replay, exit nonce, actual-fee journaling, fail-closed BUY checks, Tier 1 snapshots
- `bot/chat.py` — owner gate, per-message checkpoints, reply cap, ops grounding
- `bot/gates.py` — all-tier verdicts + reconciliation gate (rewritten)
- `bot/memecoin.py` — ticker map, price cache/pacing, stale marks, kill escalation
- `bot/polymarket.py` / `bot/wallet.py` — liquidity floor, event dedupe, bust latch, ts normalization
- `bot/binance_paper.py` — `_Order.fee` (actual fee), bounded paper_trades log, epoch start cash
- `bot/agent.py` / `bot/research.py` — scout validation/cooldown, 24h fix, extras research
- `backtest.py` — risk-sizing + allocation-cap parity
- `tests/test_deep_dive_fixes.py` — 20 new tests covering the P1/P2 fixes
- `tools/audit_ledgers.py` — ledger reconciliation audit
