# Trading Sandbox Agent

Three-tier paper-trading system (NO real money anywhere):

1. **Tier 1 — SMA crossover bot (paper only)**: deterministic SMA20/50 golden/death cross on BTC/USD, ETH/USD, SOL/USD, 15-min closed bars, with ATR-based catastrophic exits and account-level paper-risk controls.
2. **Tier 2 — AI agent (SHADOW MODE)**: babysits open positions (proposes early exits when thesis breaks) + scouts for high-conviction entries using news/whale/trend research. It never executes. Scout BUY ideas receive a fixed-horizon, BTC-benchmarked scorecard.
3. **Tier 3 — Polymarket scanner (paper only)**: near-resolution favorites are a watchlist, not automatic bets. LLM candidates must clear an expected-value-after-friction threshold, duplicate and total-exposure checks, then are paper-logged and settled automatically. A virtual $10 betting wallet (real deployment size) mirrors every logged bet at a flat $2 stake, tracks cash/locked equity per epoch, snapshots the trend each cycle, and halts new bets on bust — the tier's go/no-go scoreboard.

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
tests/                # pytest suite (54 tests)
config.yaml           # symbols, strategy params, risk caps, agent/scanner settings
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
  wallet.py           # Tier 3 virtual $10 betting wallet (derived from bets, epoch-aware)
  chat.py             # two-way Discord chat (bot reads channel, agent replies)
  journal.py          # SQLite: trades, proposals, bets, meta (state)
  report.py           # P&L/win-rate + LLM narrative
  notify.py           # Discord webhook (chunked) with file fallback
  errors.py           # custom exceptions
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
- `broker:` — backend selection (`binance_paper` active, `alpaca` switchable) + paper start cash / taker fee / slippage
- `active_strategies` — which registry entries the loop runs
- `execution:` — conservative taker-fee and slippage assumptions for backtests
- `risk:` — max_notional_per_trade (100), max_open_positions (3), daily-loss flattening, volatility-based sizing, entry-fixed stops (entry − 3×ATR, or 5% fallback; level locked at entry, mirrored in the backtester), allocation cap + kill switch (meta key `kill_switch=on`)
- `agent:` — shadow (true), min_confidence (0.7), max_proposed_notional (50), fixed evaluation horizon, babysitter/scout toggles, cycle interval
- `research:` — headlines per symbol, Tavily daily (30) / monthly (1000) caps
- `scanner:` — stake (20), near-resolution watchlist, min_market_volume, mispricing threshold, minimum expected value, total-open-exposure cap, and the betting wallet (`wallet_start_cash: 10`, `wallet_stake: 2`; epoch reset via `bot.wallet.BettingWallet.start_new_epoch()`)

## Hosting (GitHub Actions, free)

Workflow `.github/workflows/trading-bot.yml`:

- `*/15 * * * *` — trading cycle (Tier 1)
- `5 * * * *` — agent cycle (Tier 2, hourly; whale Tavily searches cached 1/day/symbol)
- `2-59/15 * * * *` — Discord chat reader (offset so it never collides with trading)
- `15 */6 * * *` — Polymarket scanner + bet settlement
- `0 18 * * *` — daily report

All jobs commit `data/trades.db` back to the repo (state persistence). Secrets: ALPACA keys, NVIDIA_API_KEY, DISCORD_WEBHOOK_URL, DISCORD_BOT_TOKEN, TAVILY_API_KEY.

## Model chain (auto-maintained)

`ModelManager` probes the active model with a 1-token call (daily, cached 20h). On failure (404/410 deprecation etc.) it walks a ranked chain — nemotron-3-super-120b → kimi-k3 → deepseek-v4-flash → minimax-m3 → nemotron-3-ultra-550b → … — adopts the first working one, persists it in journal meta, and alerts Discord. Chatty models are handled by a JSON repair-retry + truncation-salvaging parser + regex last resort.

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
4. Chat is permanently read-only for trading decisions during experiment phase

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
- **Tier 3 (Polymarket wallet, $10, epoch-aware)**
  - KILL if wallet busts (equity < one $2 stake → `start_new_epoch()`),
    twice within 8 weeks (a double bust in two epochs is a failed
    edge, not bad luck)
  - KILL if settled-bet win rate < 40% after ≥10 settled bets with
    negative net P&L (fees are supposed to make favorites +EV)
  - KEEP if epoch survives 8 weeks with positive net P&L
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
