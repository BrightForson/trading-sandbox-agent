# HANDOFF — trading-sandbox-agent

Updated: 2026-09-09 (algorithm upgrade + selective ledger reset)

## Current state

All 5 paper-trading tiers run on GitHub Actions free hosting. 215 tests pass
(`./venv/bin/python -m pytest tests/ -q`). All execution remains paper-only;
every AI decision is journaled for post-hoc calibration.

## What just happened (this session)

Owner direction: improve pre-trade/sell logic, reset all-loss ledgers except
Tier 3, widen the Tier 5 universe beyond the big 3, and let the LLM pick the
single highest-potential trade each cycle.

Implemented:
1. `bot/indicators.py` — RSI-14 (Wilder) + realized volatility, pure functions,
   None on insufficient data.
2. Tier 5 (`bot/futures.py`):
   - best_pick gate: ONE batch LLM call ranks every eligible signal and enters
     the single best (config `futures.best_pick: true`); declined candidates
     logged as shadow proposals; single-candidate symbol-omission leniency.
   - trailing stop: arms +1R, trails 1× entry-ATR, ratchet-only, touch fills
     (`trailing_atr_mult`, `trailing_activate_r`); exit check fires whenever a
     trail is armed (was nested under activation — pullbacks escaped).
   - signal dossier carries `rsi_14` + `trend_distance_pct`.
   - universe: BTC/ETH/SOL/BNB/XRP/DOGE/ADA/AVAX/LINK (`universe` in config).
3. Tier 4 (`bot/memecoin.py`):
   - LLM dossier carries `rsi_14_daily`, `realized_vol_daily_pct`, `recent_news`.
   - wick exits (`wick_exits: true`): sweep reads the CoinGecko 5m window since
     `last_sweep_at` — stops/TPs/trails touched between hourly marks fill at the
     touch.
   - `news_in_gate: true` gates the news fetch (default off in code so tests
     stay hermetic).
4. Tier 3 (`bot/polymarket.py`): mispricing prompt now includes 24h/total
   volume, liquidity, days-to-close; `endDate` normalized to UTC-aware.
5. `bot/research.py`: `news_digest()` — RSS floor + Tavily once per symbol per
   day (journal-meta cache), `journal` param threaded so tests write the right
   DB; `tavily_search` gained the same param; BNB added to `SYMBOL_TO_NAME`.
6. Budget: tavily_monthly_limit corrected to 1500 (free plan; 85 used at
   reset time). conftest strips TAVILY_API_KEY in tests.
7. `tools/reset_all_but_tier3.py`: archived journal to
   `data/archive/trades.db.pre-reset-20260909T225228Z`, reset Tiers 1/2/4/5 to
   day-zero; preserved bets, `wallet_*`, `tavily_count_*`, `active_llm_model`,
   chat state, kill counts. Injectable `db_path`/`archive_dir` (tested).
8. `tools/tier3_review.py`: advisory LLM review of open bets — all 4 HOLD
   (fair-prob 0.45-0.65 vs entry prices 0.15-0.47).
9. `tests/test_improvements.py` — 16 new tests covering all of the above.
10. memecoin workflow now passes `TAVILY_API_KEY`.

## Key decisions & rationale

- LLM gates stay advisory-but-binding-before-entry: deterministic screens
  (trend/momentum/ATR/rug-guard) run FIRST, the LLM only ranks/rejects what
  survived. Exits remain 100% deterministic — never LLM-delegated.
- Best-pick = one LLM call per cycle for ALL candidates (budget + calibration:
  one confidence number per cycle, journaled).
- Tavily caching is per-symbol-per-day because gates run hourly: without the
  cache a 9-symbol universe would burn ~216 credits/day.
- Tier 3 kept: bets are the tier's ongoing measurement; everything else was
  all-loss history with no calibration value.

## Next steps

- Watch the first few post-reset futures/memecoin cycles (hourly Actions runs)
  for gate rejections in the journal (`exec_status='shadow'`) — the batch
  gate's calibration data starts accumulating from zero.
- Monitor `tavily_count_*` meta keys vs the 1500/month cap.
- Re-run `tools/tier3_review.py` after the next scanner settle pass.
- Tier 4 needs a real price window once positions reopen to confirm the
  wick-exit path works against live CoinGecko 5m data.

## Commands

- Tests: `./venv/bin/python -m pytest tests/ -q` (215 pass)
- Tier 4 cycle: `python tools/tier4.py cycle` (or status/buy/sell/reset-kill)
- Tier 5 cycle: `python tools/tier5.py cycle`
- Tier 3 review: `python tools/tier3_review.py`
- Full reset (keep T3): `python tools/reset_all_but_tier3.py [--dry-run]`
