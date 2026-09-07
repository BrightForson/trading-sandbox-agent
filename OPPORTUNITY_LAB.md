# Opportunity Lab

## Objective

Build a continuously running research-and-experiment system that investigates
income opportunities while keeping real-money risk constrained and reviewable
from a phone or laptop.

This is not a promise of profit. The system's job is to find, test, measure,
and discard ideas faster than a person could—while capital controls remain
deterministic and human-approved.

## Operating model

```text
approved sources -> risk screen -> paper experiment -> tiny live canary -> promote or stop
```

An idea is never scaled because an LLM sounds confident. It is scaled only
after measured, net-of-cost evidence.

## Capital buckets

The exact percentages are chosen by the owner. Buckets must remain separate in
the journal and in any broker/exchange configuration.

1. **Reserve** — capital the agent cannot trade, transfer, or rebalance.
2. **Core** — pre-approved lower-risk holdings and rebalance rules. This is
   intended to preserve capital and support steady compounding, not chase fast
   gains.
3. **Experiment** — a capped budget for strategies that passed paper tests but
   are not proven enough for wider use.
4. **Canary** — tiny live tests, such as a $2 trade, used to verify real-world
   execution, fees, liquidity, and operational assumptions.

A small canary limits the size of a loss; it does not make a risky asset or
strategy safe. If fees or the platform minimum make a $2 test uninformative,
the system must reject it rather than force an order.

## Agent responsibilities

Hermes or a similar persistent agent may:

- collect information from explicitly approved sources;
- create a standardized experiment card for each hypothesis;
- run historical, paper, and data-quality tests;
- compare results with an appropriate benchmark;
- monitor open paper experiments and risk limits;
- prepare concise approval tickets for the owner; and
- maintain a history of rejected, failed, active, and promoted ideas.

The agent may not treat web content, social posts, LLM output, or a token's
popularity as authority to trade. These are untrusted research inputs.

## Human approval boundary

### Automatic

- Research, classification, paper testing, reporting, and alerts.
- Cancelling a proposed order before it is submitted.
- Pausing an experiment that breaches its predefined stop condition.

### Owner approval required

- Every new live strategy or asset class.
- Every canary order, unless the owner later creates a narrow, explicit rule.
- Any order above the canary limit.
- Changes to risk limits, position sizes, or promotion criteria.

### Never delegated

- Deposits, withdrawals, or transfers.
- Adding wallet addresses or changing broker permissions.
- Leverage, margin, borrowing, derivatives, or short selling.
- New exchange accounts or credentials.
- Executing instructions embedded in research/news/LLM content.

## Experiment card

Every candidate requires these fields before it can leave research:

- **Hypothesis:** why this should have an edge.
- **Category:** core allocation, systematic market strategy, event/prediction
  market, or speculative canary.
- **Source and evidence:** dated, attributable information—not only an LLM
  conclusion.
- **Entry and exit rule:** deterministic conditions, time limit, and invalidation
  condition.
- **All costs:** fee, spread, slippage, minimum order, network costs, and
  expected liquidity.
- **Risk:** maximum loss, correlation with existing exposure, and failure mode.
- **Benchmark:** what must be beaten after costs.
- **Decision:** reject, paper test, canary, promote, or stop.

## Promotion gates

### Research -> paper

The hypothesis, source, costs, exit rule, and benchmark are complete.

### Paper -> canary

The paper result is positive after conservative costs, has no data-leakage or
execution assumption that cannot be tested, and fits the canary budget.

### Canary -> experiment

Real execution confirms the expected order mechanics, fees, and liquidity.
Performance remains positive versus the stated benchmark, with losses inside
the pre-approved limit.

### Experiment -> wider allocation

Evidence is positive across enough independent observations and relevant market
conditions, stays positive after all costs, and has acceptable drawdown and
portfolio correlation. Owner approval is always required.

### Stop

Stop automatically when a loss limit, drawdown limit, data-quality failure,
liquidity failure, or negative out-of-sample result is hit.

## Current tier mapping

- **Tier 1:** deterministic crypto strategy. It is an Experiment candidate and
  must earn promotion through long historical and paper performance.
- **Tier 2:** AI research and shadow proposals. It is Research only until its
  fixed-horizon scorecard demonstrates useful, benchmark-beating outcomes.
- **Tier 3:** prediction-market research. Near-resolution favorites are a
  watchlist; only friction-adjusted expected-value hypotheses may enter paper
  testing.
- **Tier 4:** memecoin canary research (CoinGecko trending + DexScreener
  volume spikes). Human-gated virtual entries only; never autonomous
  execution.

Memecoins, social-token ideas, or other highly speculative assets are canary
research only. They cannot be promoted from popularity, a social signal, or an
LLM confidence score.

## Mobile approval ticket

Each notification should contain only what is needed to decide:

```text
EXPERIMENT: <name and category>
ACTION: paper / canary / pause
CAPITAL: $<amount> from <bucket>
MAX LOSS: $<amount>
COSTS: $<fees, spread, and minimum-order effect>
EVIDENCE: <one concise reason and source>
BENCHMARK: <comparison>
EXPIRY: <when the decision or experiment ends>
```

Approval must happen through an authenticated interface, not an unverified chat
message. The approval record must be stored with the corresponding experiment.

## First implementation scope

1. Add an experiment registry and status transitions to the journal.
2. Add separate reserve/core/experiment/canary budget configuration.
3. Turn Discord notifications into approval-ready decision cards; no live order
   is submitted from chat.
4. Record costs, benchmark, result, and stop reason for every experiment.
5. Build a weekly scorecard that ranks experiments by net result, drawdown,
   evidence quality, and readiness—not by raw headline return.

No new exchange, wallet, broker credential, deposit, withdrawal, or real-money
execution is included in this phase.
