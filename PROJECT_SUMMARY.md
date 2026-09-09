# Trading Sandbox Agent

Five-tier paper-trading system (NO real money anywhere):

1. **Tier 1 — SMA crossover bot (paper only)**: deterministic SMA20/50 golden/death cross on BTC/USD, ETH/USD, SOL/USD, 15-min closed bars, with ATR-based catastrophic exits and account-level paper-risk controls.
2. **Tier 2 — AI agent (SHADOW MODE)**: babysits open positions (proposes early exits when thesis breaks) + scouts for high-conviction entries using news/whale/trend research. It never executes. Scout BUY ideas receive a fixed-horizon, BTC-benchmarked scorecard.
3. **Tier 3 — Polymarket scanner (paper only)**: near-resolution favorites are a watchlist, not automatic bets. LLM candidates must clear an expected-value-after-friction threshold, duplicate and total-exposure checks, then are paper-logged and settled automatically. A virtual betting wallet (real deployment size) mirrors every logged bet at a flat stake, tracks cash/locked equity per epoch, snapshots the trend each cycle, and halts new bets on bust — the tier's go/no-go scoreboard.
4. **Tier 4 — Memecoin canary (automated, virtual $40)**: owner opt-in 2026-09-08 (no real money; automation like the other tiers). Hourly pipeline: deterministic research cards (CoinGecko trending + DexScreener volume spikes) -> per-coin dossier (market data, ATH distance, 30d history, DEX pair profile: liquidity/age) -> fail-closed rug-guard screen (min liquidity $250k, min 24h volume $500k, min pair age 7d, mcap rank <= 300, meme-category screen via `meme_category_keywords`) -> LLM conviction gate (>= 0.75 confidence, memecoin-risk rubric; model failure = no entry) -> conviction-sized entry ($6-$12). Exits, deterministic always: entry-fixed stop-loss (25%), take-profit (50%), partial take-profit (bank half at +80%, runner rides a ratcheting 20% trailing stop armed at +25%), 72h time stop, 7-day per-symbol entry cooldown, and a hard 25% max-drawdown kill that flattens all and auto re-arms after a 24h cooldown. Every auto entry is journaled as a proposal (source=tier4) for the scorecard; virtual fills carry a `[tier4-memecoin]` tag (excluded from Tier 1 P&L) and cards live in their own `tier4_cards` table. `tools/tier4.py` remains the manual override surface (buy/sell/status/reset-kill).
5. **Tier 5 — Leveraged futures canary (automated, virtual $50, owner opt-in 2026-09-09)**: simulated perp futures, 10x leverage, LONG/SHORT on BTC/ETH/SOL/DOGE/XRP. "Mildly extreme risk" by design — 20-50% of the virtual stake may be lost on a bad trade; thorough research + mandatory SL/TP before every entry. Hourly pipeline (15m candles + 1h trend confirmation): deterministic signal (1h EMA50 trend + 15m 3-bar momentum >= 0.5% + volume surge >= 1.5x + ATR band 0.15-3%) -> fail-closed guards (kill/cooldown/position caps/cash floor/SL-before-liquidation) -> LLM conviction gate (>= 0.70) -> conviction-sized margin ($8 base -> $15 max). Exits fully deterministic: liquidation simulation (loss clamped at margin), SL/TP checked against FULL candle ranges since open (a stop touched intra-bar always fills; missed cycles can't skip it), 0.01%/8h funding accrual (longs pay), 12h time stop, and a 25% max-drawdown kill deliberately under the ~29.9% worst-case single-liquidation floor (one bad trade can never kill the ledger; two always can) — second kill requires manual `tools/tier5.py reset-kill`. State isolated in `t5_*` journal meta keys; fills carry a `[tier5-futures]` tag (excluded from Tier 1 P&L); proposals are source=tier5. `tools/tier5.py` is the ops CLI (status/cycle/open/close/reset-kill).

All trade/bet/proposal events, heartbeats (hourly, with equity + SMA gaps), model switches, and a daily 18:00 UTC report go to Discord. Two-way Discord chat (Bright Bot) answers questions with live account data — read-only, can never trigger trades.

## Layout

```
run_bot.py            # Tier 1: trading cycle (--once for serverless)
run_agent.py          # Tier 2: agent cycle (shadow mode)
run_scanner.py        # Tier 3: Polymarket scanner (paper bets)
run_chat.py           # two-way Discord chat cycle
run_report.py         # daily report
backtest.py           # SMA strategy backtest (--days N, --timeframe 1Day)
validation.py         # end-to-end stack sanity check (no orders placed)
tests/                # pytest suite (195 tests)
config.yaml           # symbols, strategy params, risk caps, agent/scanner/memecoin settings
bot/
  config.py           # yaml + env config (lazy credential checks)
  broker.py           # make_broker factory + Alpaca adapter (bars/orders/positions)
  binance_data.py     # keyless Binance public klines (data-api.binance.vision)
  binance_paper.py    # local simulated broker priced by real Binance data
  timeframe.py        # vendor-neutral timeframe objects (15Min / 1Day ...)
  strategy.py         # compute_sma + check_crossover (pure)
  strategies.py       # strategy registry (pluggable, active list in config)
  risk.py             # RiskEngine: notional/exposure caps, daily loss limit, kill switch
  trader.py           # trading loop (strategy-agnostic), agent/scanner entry fns
  agent.py            # TradingAgent: babysitter + scout (shadow mode)
  models.py           # ModelManager: health probe, auto-failover chain, JSON repair
  research.py         # free research tools: RSS, CoinGecko, Tavily (budget-guarded)
  polymarket.py       # Gamma API scanner + paper bet settlement
  wallet.py           # Tier 3 virtual betting wallet (derived from bets, epoch-aware)
  memecoin.py         # Tier 4 canary: research + dossier + rug-guard + LLM gate + auto entries + exit sweep
  futures.py          # Tier 5 futures: signals + LLM gate + SL/TP/liquidation sim + funding + kill
  chat.py             # two-way Discord chat (bot reads channel, agent replies)
  journal.py          # SQLite: trades, proposals, bets, tier4_cards, meta (state)
  report.py           # P&L/win-rate + LLM narrative
  notify.py           # Discord webhook (chunked) with file fallback
  errors.py           # custom exceptions
tools/
  tier4.py            # Tier 4 ops CLI (manual overrides): buy/sell/status/reset-kill/cycle
  tier5.py            # Tier 5 ops CLI (manual overrides): status/cycle/open/close/reset-kill
  reset_day_zero.py   # day-zero reset: archive + clear + new timestamp
  merge_db.py         # lossless SQLite merge for CI push conflicts
data/trades.db        # committed to repo: cross-run state for GitHub Actions
```

## Broker backends (config.yaml `broker:`)

- `binance_paper` (active): local simulated account priced by keyless Binance
  public data (data-api.binance.vision — no API keys, geo-safe). Ledger state
  (cash, positions, fills) persists in journal meta keys `paper_*`, so it
  survives CI runs exactly like every other counter. Fills at the latest
  closed kline ±slippage, minus 0.1% Binance spot taker fee.
- `alpaca` (switchable): the original Alpaca paper account; set
  `broker.name: alpaca` and provide ALPACA keys. All consumers go through
  the same adapter surface, so nothing else changes.
- Live Binance, when you graduate to real money: slots behind the same
  interface, but the runner must move off GitHub Actions (US geo) to your
  machine or a non-US VPS.

## Config (config.yaml)

- `symbols`, `sma_fast/slow`, `notional` — Tier 1
- `broker:` — backend selection (`binance_paper` active, `alpaca` switchable) + paper start cash (100) / taker fee / slippage
- `active_strategies` — which registry entries the loop runs
- `execution:` — conservative taker-fee and slippage assumptions for backtests
- `risk:` — max_notional_per_trade (100), max_open_positions (3), daily-loss flattening, volatility-based sizing, entry-fixed stops (entry − 3×ATR, or 5% fallback; level locked at entry, mirrored in the backtester), allocation cap + kill switch (meta key `kill_switch=on`)
- `agent:` — shadow (true), min_confidence (0.7), max_proposed_notional (50), shadow_start_cash (80), fixed evaluation horizon, babysitter/scout toggles, cycle interval
- `research:` — headlines per symbol, Tavily daily (30) / monthly (1000) caps
- `scanner:` — stake (20), near-resolution watchlist, min_market_volume, mispricing threshold, minimum expected value, total-open-exposure cap, and the betting wallet (`wallet_start_cash: 60`, `wallet_stake: 12`; epoch reset via `bot.wallet.BettingWallet.start_new_epoch()`)
- `memecoin:` — Tier 4 canary: start_cash (40), max_stake (12), entry-fixed stop_loss_pct (25) / take_profit_pct (50) / time_stop_hours (72), trailing stop (20% trail armed at +25%), partial TP (half at +80%), max_drawdown_pct (25) hard kill w/ 24h auto re-arm, DEX-style fee/slippage (1% / 100bps), research-card TTL + spike filters, meme-category screen (`meme_category_keywords`, empty list disables); automation block: auto_entry, min_llm_confidence (0.75), base_stake (6), max_open_positions (3), rug-guard floors (liquidity 250k / volume 500k / age 7d / mcap rank 300), entry_cooldown_hours (168)
- `futures:` — Tier 5 futures canary: start_cash (50), leverage (10), base_margin (8) / max_margin (15), stop_atr_mult (1.5) / tp_atr_mult (2.25), max_hold_hours (12), max_drawdown_pct (25) kill w/ manual-only second re-arm, taker_fee_pct (0.05) / slippage_bps (5) / funding_rate_pct_8h (0.01), universe (BTC/ETH/SOL/DOGE/XRP); signal knobs: trend_ema_period (50), momentum_bars (3), momentum_pct (0.5), volume_surge_mult (1.5), ATR band 0.15-3%; automation: auto_entry, min_llm_confidence (0.70), max_open_positions (2), cooldown_hours (24), min_cash_fraction (0.15)

## Hosting (GitHub Actions, free)

Workflow `.github/workflows/trading-bot.yml`:

- `*/15 * * * *` — trading cycle (Tier 1)
- `5 * * * *` — agent cycle (Tier 2, hourly; whale Tavily searches cached 1/day/symbol)
- `2-59/15 * * * *` — Discord chat reader (offset so it never collides with trading)
- `15 */6 * * *` — Polymarket scanner + bet settlement
- `4 * * * *` — Tier 4 memecoin canary cycle (hourly)
- `23 * * * *` — Tier 5 futures canary cycle (hourly)
- `0 18 * * *` — daily report

All jobs commit `data/trades.db` back to the repo (state persistence). Secrets: ALPACA keys, NVIDIA_API_KEY, DISCORD_WEBHOOK_URL, DISCORD_BOT_TOKEN, TAVILY_API_KEY.

## Model chain (auto-maintained)

`ModelManager` probes the active model with a 1-token call (daily, cached 20h). On failure (404/410 deprecation etc.) it walks a ranked chain — kimi-k3 → nemotron-3-super-120b → deepseek-v4-flash → minimax-m3 → nemotron-3-ultra-550b → … — adopts the first working one, persists it in journal meta, and alerts Discord. JSON calls use native JSON mode with up to 8 draws and TRUE mid-gate rotation to the next chain model (a narrating/degraded model can't block a gate), then system-role plain call, strict repair retry, regex extraction, and fragment reconstruction. All fail-closed.

## Research tools (all free)

- RSS: Cointelegraph + CoinDesk headlines (replaces CryptoPanic)
- CoinGecko keyless: prices, 24h change, trending
- Tavily: general web + whale-activity searches, hard-guarded to 30/day and 1000/month (free tier 1500/mo), counters persisted in journal meta; whale queries cached once/day/symbol
- On budget exhaustion: automatic fallback to RSS/DuckDuckGo — never billed

## Backtest

The backtester generates signals on closed candles and fills them on the next bar,
with configurable adverse slippage and fees. Sizing and stops mirror the live loop:
each BUY is capped at `risk.max_notional_per_trade` and never deploys more than
the configured per-trade notional (no all-in compounding by default), and every
entry records a stop fixed at entry (entry − 3×ATR, 5% fallback) that only ever
exits on the next bar's open. It reports return, drawdown, exposure, turnover,
costs, stop exits, and a buy-and-hold comparison. Historical results
remain hypotheses until evaluated across long, untouched periods and then
confirmed by paper execution.

## Graduation gates (experiment phase → any real money)

1. Tier 1: live paper performance consistent with backtest
2. Tier 2: ≥4 weeks of shadow proposals with positive hypothetical P&L after fees → then semi-auto (high-confidence only, tight caps) → separate gate before wider autonomy
3. Tier 3: multi-week paper-bet record positive after settlement
4. Tier 4/5: canaries survive their 8-week windows with positive net P&L after all costs (gates.py tracks verdicts per tier)
5. Chat is permanently read-only for trading decisions during experiment phase

## Kill/keep criteria (per tier)

Written 2026-09-07 (day zero). Every criterion is measured from the journal
(`data/trades.db`), starting from the day-zero reset — pre-reset history is
archived in `data/archive/` and does not count. "Drawdown" = account equity
peak-to-trough on the relevant ledger. Verdicts are checked when the daily
report runs; any KILL verdict must be acted on manually within a week.

- **Tier 1 (SMA bot, $20 paper ledger)**
  - KILL if net P&L < −20% of starting equity after ≥4 weeks of signals
  - KILL if drawdown > 25% at any point, or > 2 consecutive losing weeks
  - KILL if live paper results diverge from the 30-day backtest by > 2×
    the backtest's own drawdown (strategy not doing what was modeled)
  - KEEP if net P&L ≥ 0 after 4 weeks with all risk gates intact (no
    bypassed blocks, every exit either stop- or signal-driven)
- **Tier 2 (AI shadow agent, $20 virtual ledger)**
  - KILL if < 10 proposals evaluated in 4 weeks (agent not producing) or
    the agent-alpha gate stays red for 4 consecutive weeks
  - KILL if net simulated P&L < −10% after ≥20 evaluated proposals, or
    avg benchmark return ≥ agent return (coin-flipping vs BTC)
  - KEEP if net simulated P&L > 0 after ≥20 evaluated proposals and
    beats the BTC benchmark; then consider the semi-auto gate
- **Tier 3 (Polymarket wallet, $60, epoch-aware)**
  - KILL if wallet busts (equity < one $12 stake → `start_new_epoch()`),
    twice within 8 weeks (a double bust in two epochs is a failed
    edge, not bad luck)
  - KILL if settled-bet win rate < 40% after ≥10 settled bets with
    negative net P&L (fees are supposed to make favorites +EV)
  - KEEP if epoch survives 8 weeks with positive net P&L
- **Tier 4 (memecoin canary, $40, automated since 2026-09-08)**
  - KILL on the hard 25% max-drawdown trigger (auto-flattens; entries
    resume automatically after the 24h kill cooldown) — a second kill
    within 8 weeks of the first is a failed edge, stop the tier
  - KILL if net P&L < −25% of starting equity over ≥4 weeks even without
    a formal drawdown trigger, or if any exit (SL/TP/trailing/time stop)
    is found not to have fired on schedule (discipline failure =
    infrastructure)
  - KEEP if the canary survives 8 weeks with positive net P&L after all
    costs; then consider whether the signal justifies a paper test
- **Tier 5 (futures canary, $50 at 10x, automated since 2026-09-09)**
  - Owner risk contract: "mildly extreme" — 20-50% of the virtual stake
    may be lost on a bad trade; best possible trades because it's paper.
    Mandatory SL/TP research-gated entries, no exceptions
  - KILL on the 25% max-drawdown trigger (deliberately under the ~29.9%
    single-liquidation floor — one bad trade can never kill the ledger);
    first kill auto re-arms after 24h, a second kill requires manual
    `tools/tier5.py reset-kill` (double-kill = failed edge)
  - KILL if any SL/TP/liquidation/funding/time-stop mechanics are found
    not to have fired on schedule, or if a stop was ever skipped by a
    missed cycle (discipline failure = infrastructure)
  - KEEP if the canary survives 8 weeks with positive net P&L after
    fees, funding, and slippage
- **Any tier**: kill immediately on unreconcilable ledger corruption,
  silent risk-gate bypass, or execution without a journal record — those
  are infrastructure failures, not strategy ones, and stop everything
  until fixed (see the 2026-09-06 bug log for why).

## Ops commands

```bash
./venv/bin/python -m pytest tests/ -q      # tests
./venv/bin/python validation.py            # full-stack sanity (no orders)
./venv/bin/python backtest.py --days 30    # Tier 1 backtest
./venv/bin/python run_agent.py --once      # agent cycle
./venv/bin/python run_scanner.py           # scanner cycle
./venv/bin/python tools/tier4.py status    # Tier 4 canary status + cards
./venv/bin/python tools/tier4.py cycle       # one full auto cycle now
./venv/bin/python tools/tier4.py buy DOGE 12   # manual entry override
./venv/bin/python tools/tier4.py reset-kill   # force re-arm after a kill
./venv/bin/python tools/tier5.py status     # Tier 5 futures status
./venv/bin/python tools/tier5.py cycle        # one full futures cycle now
./venv/bin/python tools/tier5.py open BTC LONG 10  # manual entry override ($ margin)
./venv/bin/python tools/tier5.py close BTC          # manual exit override
./venv/bin/python tools/tier5.py reset-kill   # force re-arm after a kill
./venv/bin/python tools/reset_day_zero.py --dry-run  # preview a day-zero reset
```

Kill switch: `sqlite3 data/trades.db "INSERT OR REPLACE INTO meta VALUES ('kill_switch','on');"` (blocks all BUYs; SELLs still allowed; set to 'off' to resume).

Switch broker back to Alpaca paper: set `broker.name: alpaca` in config.yaml and ensure ALPACA keys are in .env. The Binance paper ledger (meta keys `paper_cash`, `paper_positions`, `paper_trades`) is untouched by the Alpaca backend, so you can switch back and forth without losing either account's state.

## Bug log (fixed 2026-09-05)

- Bars fetch without explicit start/end returned only ~37 bars → SMA50 starved → agent price context None (broker.py now sends explicit window)
- `get_position` crashed on "Not Found" (string match too narrow) and ETH/USD vs ETHUSD symbol mismatch (death-cross SELL would never have fired)
- `place_order` passed "BUY" string instead of OrderSide enum — orders always failed
- Sizing by close price could exceed cash (now min(notional, 98% cash))
- LLM JSON outputs: repair-retry + truncation salvage + regex extraction (reasoning models think out loud)

## Bug log (fixed 2026-09-06)

- **Unprotected positions after a re-seed** (the live ETH@$2,458.29 case): `seed_from_alpaca`/`seed_fresh` replaced the position set but left the entry-fixed stop ledger untouched-or-empty, and the fallback ATR check only engaged when the price mark was unusable. Positions carried no real exit until the daily -5% flatten. Fixed three ways: (1) every cycle self-heals any held position without a recorded stop (`ensure_stop`, from current-bars ATR or fallback pct; barless positions get the fallback stop); (2) the barless stop-sweep covers symbols whose feed failed; (3) seeding clears orphaned stops, and `prune_stale_stops` drops ledger entries for symbols no longer held.
- **Silently skippable death-cross exits**: a cross is edge-triggered (visible for exactly one 15-min cycle); if that run was late or failed, the exit was skipped forever. Fixed with persistent per-symbol SMA-relation state (`strat_state_positions` in journal meta): the relation is only advanced after a fully successful cycle, and any transition missed in between is reconciled level-triggered — missed death crosses retry the exit every cycle until it executes; missed golden crosses get exactly one catch-up entry (never retry-spam). A failed strategy SELL also refuses to advance state.
- **Daily report Tier-1 P&L polluted**: `compute_pnl_and_winrate` counted ShadowAccount's virtual `[shadow-account]` trades as strategy fills. Now excluded (shadow P&L stays in its own section).
- **Discord chat could echo itself**: `_is_our_bot` compared the raw base64 token segment to the author id (never equal) — only the `bot` flag saved it from loops with other non-flagged webhook messages. Token segment is now decoded.
- **Polymarket settlement guessed winners**: `0 if yes>=0.999 else 1` scored any ambiguous/not-yet-priced closed market as a NO win. Settlement now requires an unambiguous 1.0/0.0 print; otherwise the bet stays open.
- **Stale whale cache**: data was cached under an undated key while the freshness marker was daily — day 2+ reused day-1 Tavily results forever. Cache data is now per-day and expired.
- Minor: heartbeat labeled local time as UTC and computed SMA gaps from the still-forming bar; `utcfromtimestamp` deprecation; zero-qty BUYs could be submitted to the broker after risk sizing clipped them to nothing; trade alerts showed configured notional instead of the actual order size; dead `SMAStates` class removed.

## Bug log (fixed 2026-09-08, deep-dive review — full detail in DEEP_DIVE_FINDINGS.md)

- **CI journal merge rolled back ledger state on push conflicts**: during `git pull --rebase`, git's `--ours`/`--theirs` are swapped relative to a normal merge, so `safe_commit.sh` merged the fresh journal into the stale remote DB; `merge_db.py`'s `INSERT OR IGNORE` meta union then kept the stale value for every pre-existing key — paper/shadow/t4 ledgers, stops, strategy state and idempotency registry silently reverted while trade rows stayed (double-sell / duplicate-entry risk). Fixed with stage-based resolution (`:1:` base, `:2:` upstream, `:3:` ours), a true 3-way meta merge (both-changed ⇒ resolving run wins, tavily counters MAX), `tier4_cards`/`wallet_snapshots` added to the row union, `fetch-depth: 0` on all committing workflows, and e2e tests reproducing the exact rebase conflict.
- **Push failures exited green**: "journal kept locally" is false on ephemeral CI runners — a failed push lost the whole cycle's writes with a successful checkmark. Now `exit 1` + best-effort Discord alert; merge failures abort the rebase instead of committing an unmerged DB.
- **Discord webhook token leaked into public CI logs**: raw `requests` exception text embeds the webhook URL (the secret) — now masked everywhere (`_mask_secrets`), notify has 429/Retry-After + bounded retries, and the file fallback also appends to `$GITHUB_STEP_SUMMARY` so report tails survive ephemeral runners.
- **Tier 4 kill auto-re-arm was dead code**: `_auto_entries` returned at `kill_active()` before `_auto_rearm_after_cooldown()` could run — the documented 24h auto re-arm never fired. Re-arm now runs before the kill check.
- **Tier 4 rug-guard failed open**: missing pair age skipped the 7-day gate; `bool("false")` accepted a string verdict as a buy. Age unknown now rejects; the LLM gate requires a real JSON boolean `buy` and numeric `confidence`.
- **Tier 4 misc**: workflow timeout 15→30 min (worst-case cycle was ~16+ min; timeout rolled back every write including executed entries); manual `buy()` enforces `max_open_positions`; research cards dedupe within a batch (live responses contained 7 identical pairs); position/cooldown prechecks case-normalized; conviction prompt treats dossier text as data, never directives.
- **Hygiene**: requirements pinned (bounded ranges + pytest); `tests/conftest.py` (the fixture preventing tests from posting to the real webhook) committed; new `tests.yml` CI workflow runs the 150-test suite on push/PR; chat context no longer says "Alpaca" (backend is binance_paper); local `.env` chmod 600.

## Bug log (fixed 2026-09-08/09, deep-dive P1/P2 — full detail in DEEP_DIVE_FINDINGS.md)

- **Missed death-cross exits abandoned on whipsaw** (F12): the catch-up retry was derived from the *current* SMA relation, so a golden→death→golden flip mid-retry silently dropped the exit intent. Exits that fail now persist a `pending_exit_symbols` intent that replays every cycle until the SELL fills, independent of the relation.
- **Constant-reasoning exits suppressed as duplicates** (F13): the daily-loss flatten and catch-up reasons are constants, so a same-qty recurrence on a later day could be eaten by the idempotency key while returning "success" (stuck position). Exit keys now fold in the UTC day + a per-symbol attempt counter; BUY keys unchanged (still one-shot).
- **Journaled fee was fiction** (F16): the trades table recorded 0.25% while the paper broker charges 0.1% (backtest yet another value) — scorecard P&L could never reconcile with ledger equity. The broker now returns the actual fee on the order and the trader journals it; `execution.*` remains backtest-only conservatism.
- **Naive local timestamps** (F15) in the trades journal → now UTC-aware ISO everywhere (report FIFO ordering and merge dedupe were timezone-mixing hazards on any non-UTC host).
- **Risk gate failed OPEN on infra errors** (F18): a BUY proceeding when positions/equity couldn't be enumerated now fails CLOSED.
- **`reset_day_zero` wiped the kill switch** (F14): `kill_switch`/`kill_switch_reason` now preserved.
- **Chat hardening** (F25): answers only `DISCORD_OWNER_IDS` (fail-closed), per-message checkpoint (failed sends retry instead of being marked seen), 5-reply cap per cycle, 429 Retry-After, kill-switch state in context + no-inventing instruction.
- **Tier 2 scout**: BUY with no symbol rejected (F20); duplicate proposals suppressed while held/open (F21 — protects the ≥20-evaluated gate premise); real 24h change in prompt (was 6h, F22); news/whale research mapped for the extra universe (F23).
- **Tier 3 scanner**: `enabled` flag honored; LLM candidates require real liquidity (was volume/10 — thin-market EV was fiction, F24); sibling markets of one event deduped (correlated exposure); bust alert latched once; prompt names the literal outcome instead of "(YES)"; wallet epoch cutoff normalizes timestamp formats.
- **Tier 4**: DexScreener spike cards resolve ticker→CoinGecko id (the leg was dead code — `/coins/WIF` 404s, F8); `/simple/price` paced + 429-aware with a per-cycle cache (F10); unpriceable positions are excluded from valuation + surfaced as `stale_marks` events instead of entry-marked fiction that blinded the drawdown kill (F9); `reset_kill` preserves unsold positions instead of destroying their value; second drawdown kill requires manual `reset-kill` (double-kill = failed-edge criterion now enforced, was infinite −25% staircase).
- **Graduation gates for all tiers** (F26): gates.py now measures Tier 1 (P&L/drawdown/losing-week streak from hourly equity snapshots in `wallet_snapshots` epoch 0), Tier 3 (win rate + double bust), Tier 4 (P&L + kill count), plus a reconciliation gate (broker position without a journal fill = RED). Verdict history persists for multi-week streak conditions.
- **Backtest parity** (F27): the simulator now applies the same ATR-risk sizing (`target_risk_pct_per_trade`) and allocation cap as the live loop; the combined-P&L per-symbol-pool caveat is printed.
- Minor: `1Hour`→`1h` Binance interval fix; kline 429 error message no longer `None`; models.py deprecated `utcnow()` → timezone-aware (reads old naive values); heartbeat baseline from the ledger epoch (`paper_epoch_start_cash`); pre-trade Discord alerts rate-limited; `paper_trades` order log bounded to 500; validation.py risk check runs on an isolated journal (was pinning the production daily-loss baseline); run_report exits non-zero on failed delivery; report pain-meter has `actions: read` (no PAT needed); memecoin timeout 15→30 min (worst-case cycle exceeded it and lost every write).

## Owner actions required (2026-09-09)

1. Set the `DISCORD_OWNER_IDS` Actions **variable** (repo Settings → Secrets and variables → Variables) — until then Bright Bot answers nobody (fail-closed chat gate).
2. Rotate the Discord webhook (secret) — old public CI logs may contain the token from before masking was added.
