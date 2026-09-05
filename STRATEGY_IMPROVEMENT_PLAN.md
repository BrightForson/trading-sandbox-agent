# Trading Sandbox Agent — Improvement Plan

## Purpose and operating rule

The goal is not to maximize the number of trades or add more AI. The goal is to
find a repeatable, cost-adjusted edge in paper trading before any real-money
decision. Each tier must earn its place through independently measured results.

**Status:** keep all execution paper-only. Any AI output remains advisory and
cannot submit an order.

## What to change first: Tier 1 should become the benchmark

Tier 1 is the only deterministic strategy and should be the primary experiment.
The current SMA 20/50 crossover is a reasonable baseline, not yet a proven edge.

### Build an honest backtest

1. Generate a signal only after a candle closes, then execute at the next bar's
   open (or a conservative next-bar price).
2. Model every round-trip cost: trading fee, bid-ask spread, and slippage.
   Start with conservative assumptions and report the assumptions in every run.
3. Track fills, cancellations, partial fills, and actual fees in paper trading;
   do not use the signal price as the execution price.
4. Correct performance measures: net P&L, return on portfolio equity, maximum
   drawdown, exposure, turnover, trade count, and monthly results.
5. Compare each result with buy-and-hold BTC and a no-trade baseline.

### Test for robustness rather than a lucky setting

1. Use at least one to two years of 15-minute bars, covering bull, bear, and
   sideways periods.
2. Select parameters only on an earlier training period, then evaluate the
   chosen rule on later untouched data (walk-forward testing).
3. Test nearby settings such as 15/40, 20/50, and 25/60. Retain the strategy
   only if performance is stable across a range, rather than exceptional at one
   exact pair of parameters.
4. Report portfolio-level results because BTC, ETH, and SOL are correlated.

### Risk rules before any possible graduation

1. Size positions from account equity and volatility, not a fixed dollar amount.
2. Cap the entire crypto allocation as one risk bucket; three crypto symbols are
   not true diversification.
3. Add a catastrophic, volatility-based exit and an account-level loss rule
   that can close exposure after a defined breach. Keep the manual kill switch.
4. Place a hard limit on simultaneous orders and confirm terminal order status
   before recording a trade as filled.

## Tier 2 — AI agent: use it as research, not as a trader

Keep the agent in shadow mode and disable or silence broad "scout" alerts until
its decisions can be evaluated rigorously.

For every proposal, store:

- the information available at decision time;
- proposed entry, stop/invalidating condition, and expiry time;
- actual next-bar entry and eventual outcome, including costs;
- return compared with buy-and-hold over the same holding window; and
- calibration of confidence: for example, did ideas labelled 70% succeed about
  70% of the time?

Use the model to summarize research and identify hypotheses. Do not treat its
confidence score or news interpretation as a trading edge without this evidence.

## Tier 3 — Polymarket scanner: keep it research-only

A 97-cent contract is not automatically attractive; its price already reflects
the market's estimated probability. The relevant calculation is expected value:

`expected value = estimated probability × payout − total cost`

Before paper-logging any contract, the scanner should:

1. Record its independent probability estimate and the evidence supporting it.
2. Include bid/ask spread, applicable taker fees, and liquidity in the cost.
3. Reject duplicate bets and aggregate exposure to correlated events.
4. Separate “near resolution” contracts from genuine independent-information
   hypotheses.
5. Measure performance only after a substantial number of independently
   settled paper bets.

LLM probability estimates are especially likely to mirror public market
information. Until they demonstrate calibration and net positive results, they
are a research signal only—not a reason to bet.

## Hermes Agent — potential role

Hermes Agent is an open-source, persistent agent harness with memory, skills,
scheduling, and an optional runtime that can delegate supported OpenAI/Codex
turns to Codex. It is a workflow/orchestration layer, not a trading strategy or
an independent source of market edge.

If introduced, Hermes should have only these permissions initially:

1. run scheduled data-quality checks and backtests;
2. compile weekly paper-performance reports;
3. open issues or drafts for failed checks; and
4. maintain experiment notes and parameter histories.

It should not receive brokerage credentials, prediction-market credentials, or
permission to place orders. Its usefulness must justify its added token cost and
operational complexity. First use the existing scheduled jobs and logs; add
Hermes only if persistent orchestration and memory solve a concrete operations
problem.

## Recommended 30-day sequence

### Week 1 — measurement correctness

Implement next-bar execution, cost assumptions, confirmed paper-fill logging,
and a single portfolio performance report.

### Week 2 — historical evaluation

Run walk-forward tests on a long history, compare benchmarks, and reject fragile
parameter combinations.

### Weeks 3–4 — paper validation

Run only the surviving Tier 1 configuration in paper trading. Keep Tier 2 and
Tier 3 advisory-only while collecting properly attributed outcomes.

## Graduation criteria

Do not consider real-money trading until the deterministic strategy has:

1. positive out-of-sample, net-of-cost performance;
2. controlled drawdowns relative to the risk budget;
3. stable results across nearby parameters and multiple market regimes;
4. sufficient live-paper observations consistent with the backtest; and
5. an independently reviewed execution and risk-control path.

There is no safe design that guarantees maximum profit. The right target is a
small, verified edge with losses that stay within a predefined and affordable
risk budget.
