# Handoff — trading-sandbox-agent

## Task
Implement 5 profit-maximization levers identified by a read-only audit of the 3-tier paper-trading system. User approved all 5 ("implement them and do your best") and required preserving existing journal data (3 trades in data/trades.db must survive). Status: not started (research/audit complete, zero files modified — git status clean, verified).

## Done this session
- Delegated read-only profit-lever audit (explore subagent) of bot/* — findings below.
- Read in full: strategy.py, strategies.py, trader.py, risk.py, agent.py, polymarket.py, shadow.py, journal.py, broker.py, report.py, config.py, config.yaml, backtest.py, tests/test_core.py.
- Confirmed repo clean: `git status --short` empty, no stashes. HEAD = 7a37f29.
- No files created/modified. No tests run yet.

## Open threads
- Lever 1: backtest.py has NO fees/slippage — fills at exact close (backtest.py:83-97). 0.5%/round-trip (Alpaca ~0.25%/side) × ~33 trips/30d ≈ 16.5%/mo unmodeled drag. +16.7% BTC backtest may be negative net of costs. Highest priority.
- Lever 2: Tier 1 exit is death-cross ONLY (strategies.py:43) — lags peak ~SMA50 lookback (~12h on 15m bars); no stop-loss/trailing/take-profit anywhere.
- Lever 3: no anti-whipsaw filter — raw cross → immediate market order (trader.py:105); no trend-strength/ATR/volume/confirmation gate. 29-36% win rate = many noise round trips.
- Lever 4: no correlation control (risk.py:56-79) — BTC+ETH+SOL (~0.8 correlated) can hold 3 concurrent longs = 3x crypto beta; daily 5% loss limit is the only aggregate brake.
- Lever 5a: Tier 3 flat $20 stake regardless of edge (polymarket.py:180, config.yaml:59); no Kelly/EV sizing.
- Lever 5b: ShadowAccount computes realized_pnl() (shadow.py:142-170) but NOTHING consumes it — no graduation gate exists (grep for gate/promote: zero hits).
- Dead param: `scanner.friction_pct: 1.0` (config.yaml:65) never referenced in code — wire it into EV or remove.
- Tier 3 LLM "true probability" prompt has no market data (polymarket.py:134-142), no calibration tracking.
- Journal state: trades(3 rows), meta(16), proposals(2), bets(0) in data/trades.db (committed to repo for CI state persistence — DO NOT wipe).

## Key context
- Architecture invariant: Tier 1 = deterministic loop; LLM confined to advisory/shadow roles (propose-only, chat read-only for trading). Graduation gates per PROJECT_SUMMARY.md:78-83. Keep this separation in all 5 levers.
- Backtest↔live parity is critical: any exit logic added to strategies.py sma_cross MUST be mirrored in backtest.py simulate() so backtests validate live behavior.
- Style: no comments in code, no emojis in files (Discord messages already use emojis — existing convention). Config-driven via config.yaml + Config.__getattr__ (bot/config.py:27-31); tests use _FakeCfg classes (tests/test_core.py:50-53).
- Commands: `./venv/bin/python -m pytest tests/ -q` (14 tests), `./venv/bin/python validation.py`, `./venv/bin/python backtest.py --days 30`.
- Sizing today: `min(notional, cash*0.98)` at trader.py:135; caps in config.yaml:32-35.
- Files that matter (most important first): bot/strategies.py, backtest.py, bot/risk.py, bot/polymarket.py, config.yaml (+ tests/test_core.py, bot/shadow.py, bot/agent.py).

## Next steps
1. Lever 1 — backtest.py: add --fee-pct (default 0.25) and --slippage-pct args; apply per side in simulate() buys/sells; report gross vs net P&L; re-run `./venv/bin/python backtest.py --days 30` to validate SMA viability under costs.
2. Lever 2 — add trailing-stop/ATR exit to sma_cross (strategies.py) + mirror in simulate() (needs high/low or close-based trail since live df has OHLC; keep death cross as fallback); config keys e.g. trailing_stop_pct/atr_period under strategy params; default ON but tunable.
3. Lever 3 — confirmation/trend-strength gate on golden crosses (e.g. require N confirming bars or SMA slope > 0); config-gated; mirror in backtest; re-run backtest to measure noise-trade reduction vs levers 1-2.
4. Lever 4 — risk.py: add `max_portfolio_notional` (e.g. 200) check summing open position notionals before BUY approval; config under risk:; new test in tests/test_core.py.
5. Lever 5 — polymarket.py: EV/Kelly-fraction stake sizing (edge-proportional, capped at config stake); wire or remove friction_pct; new bot/gates.py: graduation gate evaluation (min proposals N, shadow realized_pnl > 0, min window) surfaced in daily report (report.py) — recommend, never auto-promote.
6. Run full pytest + validation.py + fee-aware backtest; update PROJECT_SUMMARY.md levers/config docs; commit (user has approved implementation work; commit only if asked again — repo owner commits journal via CI, so keep data/trades.db untouched).
