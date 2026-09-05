# Trading Sandbox Agent

Three-tier paper-trading system (NO real money anywhere):

1. **Tier 1 — SMA crossover bot (low risk)**: deterministic SMA20/50 golden/death cross on BTC/USD, ETH/USD, SOL/USD, 15-min closed bars, Alpaca paper.
2. **Tier 2 — AI agent (medium risk, SHADOW MODE)**: babysits open positions (proposes early exits when thesis breaks) + scouts for high-conviction entries using news/whale/trend research. Proposes only — never executes. Every proposal logged with rationale + confidence and pushed to Discord.
3. **Tier 3 — Polymarket scanner (high risk, paper bets)**: scans public Gamma API for near-resolution favorites (≥97¢, ending ≤3 days, EV net of price) and LLM-flagged mispricings (model's true-probability estimate vs market price, gap ≥8%). Bets are paper-logged and settled automatically when markets resolve.

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
tests/                # pytest suite (14 tests)
config.yaml           # symbols, strategy params, risk caps, agent/scanner settings
bot/
  config.py           # yaml + env config (lazy credential checks)
  broker.py           # Alpaca paper broker (bars/orders/positions)
  strategy.py         # compute_sma + check_crossover (pure)
  strategies.py       # strategy registry (pluggable, active list in config)
  risk.py             # RiskEngine: notional/exposure caps, daily loss limit, kill switch
  trader.py           # trading loop (strategy-agnostic), agent/scanner entry fns
  agent.py            # TradingAgent: babysitter + scout (shadow mode)
  models.py           # ModelManager: health probe, auto-failover chain, JSON repair
  research.py         # free research tools: RSS, CoinGecko, Tavily (budget-guarded)
  polymarket.py       # Gamma API scanner + paper bet settlement
  chat.py             # two-way Discord chat (bot reads channel, agent replies)
  journal.py          # SQLite: trades, proposals, bets, meta (state)
  report.py           # P&L/win-rate + LLM narrative
  notify.py           # Discord webhook (chunked) with file fallback
  errors.py           # custom exceptions
data/trades.db        # committed to repo: cross-run state for GitHub Actions
```

## Config (config.yaml)

- `symbols`, `sma_fast/slow`, `notional` — Tier 1
- `active_strategies` — which registry entries the loop runs
- `risk:` — max_notional_per_trade (100), max_open_positions (3), daily_loss_limit_pct (5) + kill switch (meta key `kill_switch=on`)
- `agent:` — shadow (true), min_confidence (0.7), max_proposed_notional (50), babysitter/scout toggles, cycle interval
- `research:` — headlines per symbol, Tavily daily (30) / monthly (1000) caps
- `scanner:` — stake (20), near_resolution_days (3), near_resolution_min_price (0.97), min_market_volume, mispricing_threshold (0.08)

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

## Backtest (Tier 1 baseline, 2026-09-05, 30d of 15m bars)

BTC +16.7% (33 trips, 36% win), ETH +12.0% (36 trips, 31% win), SOL +26.8% (31 trips, 29% win) on $100 notional each — combined +$55. Low win rate + positive P&L = classic trend-following (many small losses, few big wins). Baseline saved in journal meta `tier1_backtest_30d`.

## Graduation gates (experiment phase → any real money)

1. Tier 1: live paper performance consistent with backtest
2. Tier 2: ≥4 weeks of shadow proposals with positive hypothetical P&L after fees → then semi-auto (high-confidence only, tight caps) → separate gate before wider autonomy
3. Tier 3: multi-week paper-bet record positive after settlement
4. Chat is permanently read-only for trading decisions during experiment phase

## Ops commands

```bash
./venv/bin/python -m pytest tests/ -q      # tests
./venv/bin/python validation.py            # full-stack sanity (no orders)
./venv/bin/python backtest.py --days 30    # Tier 1 backtest
./venv/bin/python run_agent.py --once      # agent cycle
./venv/bin/python run_scanner.py           # scanner cycle
```

Kill switch: `sqlite3 data/trades.db "INSERT OR REPLACE INTO meta VALUES ('kill_switch','on');"` (blocks all BUYs; SELLs still allowed; set to 'off' to resume).

## Bug log (fixed 2026-09-05)

- Bars fetch without explicit start/end returned only ~37 bars → SMA50 starved → agent price context None (broker.py now sends explicit window)
- `get_position` crashed on "Not Found" (string match too narrow) and ETH/USD vs ETHUSD symbol mismatch (death-cross SELL would never have fired)
- `place_order` passed "BUY" string instead of OrderSide enum — orders always failed
- Sizing by close price could exceed cash (now min(notional, 98% cash))
- LLM JSON outputs: repair-retry + truncation salvage + regex extraction (reasoning models think out loud)
