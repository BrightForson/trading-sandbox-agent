# Handoff — trading-sandbox-agent

Updated: 2026-09-28 (audit remediation, commits 1-5 landed)

Supersedes the 2026-09-09 handoff (algorithm upgrade + selective ledger reset).
That session's work is committed (`993bf39` and later) and all landed.

## Task

Fix a full-codebase audit of the sandbox: ~60 correctness findings across all
five tiers, worked severity-tiered with a regression test per fix. **Status:
in progress — commits 1-5 landed (5/8).**

Owner decisions already made (do not re-litigate):
- Scope: everything, tiered in order, **one commit per phase** (8 total).
- Tests: a regression test per fix, each proven to fail before it passes.
- Ledger: **no day-zero reset.** Keep all history; repair the stuck rows.
- Tier 3 redesign: quality over frequency — never force a bet to satisfy an
  invariant; report near-misses instead so thresholds can be tuned on evidence.
- Max **3** concurrent subagents (rate-limit limit).

## Resume here

**Commits 1-5 of 8 are landed and green. Next up is commit 6/8, the Tier 3
rebuild** — the design is fully researched and agreed in "Key context"
below; nothing needs new reconnaissance, it needs building. After it come the
data repair (7) and pinger hardening (8).

Before starting commit 6:
1. Read the **Tier 3 redesign** section of "Key context" in full. It carries
   owner decisions already taken — do not re-litigate them. Two re-measurements
   are recorded there and one of them **reversed an earlier number**: do not
   relax `min_market_volume`.
2. Re-run the suite to confirm the baseline you are building on:
   `./venv/bin/python -m pytest tests/ -q -p no:cacheprovider` → expect
   **338 passed**, budget **>= 800s**.
3. Keep the discipline: one regression test per finding, proven to fail
   before it passes. The cheap way to prove it is `git stash push -- bot/
   validation.py` (production code only — the tests must stay), run the audit
   file, confirm the new tests fail *for the right reason* (not an incidental
   `TypeError` from a missing kwarg), then `git stash pop`.
4. Do **not** stage `data/trades.db` (see Open threads).
5. Note that **`get_strategies` now raises** on an unknown strategy name, and
   **`TradeJournal.get_meta_float` now raises** on unparseable or non-finite
   meta. Both are intentional. Anything that reads a cash or peak balance must
   go through the latter.

## Done this session

Five commits landed:

**`8dd6025` — audit 1/8: graduation gates.** Four wrong verdicts, all now
covered by tests:
- `_tier1_drawdown_pct` walked the peak across Tier 1 (epoch 0) *and* Tier 3's
  separate $60 wallet (epoch >= 1) → reported **40% drawdown on an account at
  -3.6%** (a daily false KILL). Filtered to `epoch=TIER1_EPOCH`. Live: 40.0% → 9.4%.
- `_tier1_losing_week_streak` filtered on `t[7] == "filled"`, but `t[7]` is the
  **fee** column and status is `t[9]`, so every trade was dropped and the
  streak was permanently 0 — a documented KILL criterion that could never fire.
  It also read `t[8]` (order_id, TEXT) as a float, raising into a bare `except`.
- Its P&L proxy summed weekly BUY-vs-SELL *cash impact*, measuring capital
  deployed rather than money lost (W38 read -$66.25 vs -$0.31 actual). Replaced
  with `_tier1_weekly_realized_pnl()`: FIFO matching, each closed round trip
  credited to the week it **closed**.
- `tier3_gate` read `score["wins"]`, a key `bet_scorecard` never returns, and
  the fallback dict seeded `"wins": 0` so it looked intentional. Win rate was
  hardcoded **0%**. Now reads `win_rate_pct`. Live: 0% → 67%.
- `compute_pnl_and_winrate` scored a split round trip twice (both FIFO branches
  incremented the counters). Now scores once when the buy is fully consumed;
  a flat round trip is breakeven, not a loss.
- `journal.log_bet`/`log_trade` now return `lastrowid` (matching `log_proposal`).

**`fb72f00` — audit 2/8: criticals.** Five defects, all reproduced first:
- Tier 5 trailing stop tested bars from the **position's open** instead of the
  current trail level, so a trail armed on a healthy uptrend closed it on the
  same sweep, booking profit the market never gave and raising
  `t5_peak_equity` (disarming the 25% kill). Now stamps
  `trailing_stop_armed_at` and restarts the window on every ratchet; a trail
  with no arm time is judged on the current mark only. **Filling at the stop
  price is unchanged — that is correct stop semantics; only the trigger was wrong.**
- The kill switch disabled all exit enforcement (`sweep()` returned early on
  `kill_active()`, but `_trigger_kill` swallows flatten failures so survivors
  are real). Both tiers now retry the flatten then continue normal enforcement.
- A dead price feed fabricated a drawdown: `valuation()` dropped unpriceable
  positions, so one outage on a $12 coin of a $40 ledger read as 30% and tripped
  the kill; the flatten then filled at **entry**, fictitiously restoring equity
  and re-arming the peak at an invented value. Both tiers fall back to the last
  observed mark (`_mark_for`), still reporting `stale_symbols`. With no mark at
  all, Tier 4 refuses the close rather than booking a fiction.
- `realized_volatility_pct` had no `sqrt(periods_per_year)` factor —
  **1.39% reported for a 30% annualized series** (~19x understatement). Its only
  caller is the LLM conviction gate gating automated Tier 4 entries.
  `periods_per_year` is now **required**; no safe default exists.
- `rsi_30d` was `None` for every coin forever: history kept `closes[-10:]` while
  the caller used `period=14` (needs 15). All closes retained now.

**`88c92ea` — audit 3/8: silent fallbacks.** 8 files, +563/-62 (the
predicted stat, exactly — the full suite was run before committing):

- `_interval_for` is now **idempotent** and **raises** on unrecognised specs.
  It was neither: `"1Hour"` → `"1h"` → `"15m"`, and `binance_paper` applies it
  twice, so any non-15m config silently fetched 15m bars and the strategy never
  fired. The Alpaca branch was dead code (imported `TimeFrameUnit` from a module
  that doesn't exist; `value_count` doesn't exist in alpaca-py 0.44). Now handles
  `value_count`, Alpaca `amount`/`unit`, and every Binance interval.
- `interval_minutes` knows all 13 intervals and raises on unknown (answered 15 for
  everything, silently computing the wrong history window).
- `make_timeframe` no longer defaults to 15 minutes on a typo.
- `trader.py`: a non-positive mark is "no usable price", not a 100% loss — it no
  longer market-sells on a data outage, and the previously-unreachable ATR
  fallback is reachable.
- `risk.stop_triggered`: `current_price <= 0` → "no usable mark"; the 50x
  hardcode is now `implausible_mark_multiple` config; message uses `.6g` so
  sub-dollar assets are readable. The guard stays deliberately **one-sided** —
  see Key context.
- `report.generate_report` catches TypeError/ValueError/AttributeError and treats
  a `None` narrative as failure; `run_report.py:17` calls `create_daily_report()`
  outside its try, so an escaping TypeError lost the whole daily report.
- `models.py`: 5 sites subscripted/concatenated a possibly-`None` model response
  (`raw[:300]`, `raw + "\n" + raw2`). All fixed; `_parse_json_arr` now raises
  `ModelError` (not `AttributeError`) on `None`. Duplicate repair suffix
  `('"}', '"}', '"} }')` → three distinct closers.
- `agent._validate` rejects a **NaN notional** — `nan > cap` and `nan <= 0` are
  both False, so it passed, opened a real shadow position, and sqlite stored NULL.
- `polymarket.scan_llm_mispricing` matches by **index**, not a byte-for-byte
  question echo, plus `_norm_q()` fuzzy fallback and an explicit
  `matched/dropped` counter with a WARNING on total failure.

**`80cdd82` — audit 4/8: medium (17), 18 files, +1354/-169.** 28 new
regression tests, **each confirmed to FAIL on the pre-fix code first** (verified
by stashing only the production files and re-running: 28 failed / 64 passed).
Full suite **317 passed** (was 288).

**`43f4f8e` — audit 5/8: low.** 21 new tests, **18 of them proven to FAIL on
the pre-fix code** (verified by `git stash push -- bot/ validation.py` and
re-running — the tests must stay unstashed). Suite **338 passed** (was 317):
- `shadow._last_close` read **`iloc[-1]`** — the still-forming candle. Both data
  backends append it, so virtual entries, exits *and* the reported shadow
  equity all carried a price that could still move. Every other reader in the
  repo already drops it (`agent.py:58`, `trader.py:369`, `futures.py:248`,
  and `binance_data.last_close` itself). Now `iloc[-2]`, and a 1-row frame
  yields no price.
- Four byte-identical `_cash()` copies did `float(get_meta(...))`. That
  **raises** `ValueError` on unparseable text (far from the broken key) and
  **accepts** `"nan"` — and NaN passes every sizing guard, since `nan > cap`
  and `nan <= 0` are both False, so a corrupt ledger read as *unlimited buying
  power*. New `journal.get_meta_float` names the key and refuses
  unparseable/non-finite. `futures._peak()` had the same hole with worse
  consequences: a NaN peak **silently disarmed the 25% drawdown kill**,
  because `nan >= pct` is also False.
- The **source** of that NaN was in the paper broker: `float(qty)` with no
  finiteness check, so a NaN quantity passed both `qty <= 0` and
  `cost + fee > cash`, and `_save` persisted `str(round(nan, 8)) == "nan"` as
  the cash balance. Now refused.
- `sma_cross` **fell through to SELL** for any cross it did not recognise, so
  a renamed signal sold the position. Signals now go through `_signal()`, and
  `get_strategies` **raises** on an unknown name — it used to print and skip,
  leaving the trader an empty list and a **green, trade-free cycle**, so a
  dead config looked exactly like a quiet market. **Two existing tests
  asserted that old behaviour** (`test_registry_resolves_and_skips_unknown`,
  `test_heartbeat_tier1_label_and_tier_one_liners`) and were updated, not
  deleted.
- **43 naive `datetime.now()` sites** stamped local time into CI logs, the
  daily-report header and the report fallback filename. Nothing ever
  disagreed because `gates.py` re-tags a naive stored value as UTC. All UTC
  now, pinned by an **AST invariant test** (`test_no_module_calls_a_naive_clock`)
  — the repo has no linter, and the daily report's own header contradicted its
  body on any non-UTC host. Verified on a UTC host, so the value test injects
  a fake clock 3 hours behind; a plain comparison would have passed pre-fix.
- Tier 5's trend guard used `[s for s in signals if s not in eligible]` — a
  nested comprehension doing `len(signals) x len(eligible)` **dict**
  comparisons to answer a question about *symbols*, and by **value**, so two
  equal signals would both read eligible. Now `_partition_eligible`, one pass,
  keyed on symbol.
- Stale docs: `chat.py` claimed a 5-min cron (it is **15-min**, and the pinger
  dispatches it); `agent.py:36`'s hard-scope comment named 3 coins over a
  **10**-symbol universe, and `trader.py`'s heartbeat still printed
  `Tier 1 BTC/ETH/SOL` to **Discord** — now derived from `config.symbols`;
  `shadow.py`'s docstring named a `shadow_trades` table that exists nowhere
  (state is the `trades` table, found back by the `[shadow-account]` reasoning
  prefix, plus two meta keys).
- Deleted a dead `iv` local in `binance_data.fetch_history` holding a
  millisecond interval width — `iv` means implied volatility, so the name
  invited a future "fix" handing `900000` to the interval parameter.

**TWO more findings did not survive verification (5 retracted in total) — do
not reintroduce:**
- **Paper SELL clipping was already correct.** `min(qty, held["qty"])` has
  been there since the broker was written (`acbc40a`); a not-held SELL is
  rejected. The audit note was wrong. A test pinning the over-sell clip was
  *added* rather than a "fix" made.
- **The paper fill log is already bounded** to 500 entries (`999bfb2`), as are
  the other two JSON-list accumulators in the repo (`gates.py:447`,
  `trader.py:291`). **No unbounded log accumulation exists in `bot/`.**

**Left open deliberately in commit 5** (real, but not on the commit-5 list —
do not assume they were forgotten):
- `shadow.realized_pnl` and 5 other callers (`gates.py:66,168,280,325,338,428`,
  `report.py:363`) call **`get_trades()` with no limit** — a full scan of a
  grow-only table, then a Python-side filter on `t[6]`. This is the deferred
  **F30** item from the 2026-09-08 audit, still open.
- `shadow.mark_to_market` still marks an unpriceable position **at entry**,
  which is the same fabricated-equity pattern commits 2 and 4 removed from
  Tiers 4/5. The right fix is a last-known-good mark stored on the position,
  not entry.
- `binance_paper._mark` does **one network fetch per position**, and
  `trader.py:637` calls `get_all_positions()` once per symbol per cycle →
  `symbols x positions` round trips (30 at current config) with no caching,
  unlike Tier 4 which caches `last_close` per symbol per day.
- `bot/risk.py:101-120` and `bot/binance_paper.py:254-266` **both** own the
  `open_stops` meta key. A paper re-seed wipes Tier 1's entry-fixed stop
  ledger, and `risk.ensure_stop` then re-derives it from the *current* ATR
  ("Healed missing stop"). Two owners, one key.

**THREE findings did not survive verification — do not reintroduce them as bugs:**
- **There is no per-tier JSON ledger.** All four `_save` methods write into the
  `meta` table of the single `data/trades.db` via `set_meta`. "Non-atomic
  ledger `_save`" was really about *transaction scope*: `set_meta` committed per
  call, so a 2-key save was 2 transactions. Fixed with `set_meta_many`, not with
  temp-file+rename. There is no file to make atomic.
- **`min_cash_fraction` WAS enforced** (`futures.py`, since `aa1d6221`/2026-09-09).
  The real defect was the entry fee not being reserved — Tier 5 alone clipped the
  margin to free cash and then charged the fee on top, so cash went negative by
  the fee. Tiers 1 and 4 both size against cost+fee.
- **Tier 4 P&L did not double-charge the entry fee — it FORGAVE it.** Cost basis
  is `qty*entry` = stake MINUS the fee (the fee is paid in fewer tokens, not an
  extra cash debit), so omitting it *understates cost by one entry_fee*. Same
  arithmetic error, opposite sign. The **ledger cash was always correct**, which
  is why no gate ever saw it — only `trades.reasoning` and the exit alert.

**WAL is deliberately OFF** (do not "fix" this). `tools/safe_commit.sh:20` stages
`data/trades.db` alone and resolves binary conflicts from the `:1:`/`:2:`/`:3:`
stages of that one file. In WAL mode the newest rows sit in a `-wal` sidecar
until checkpoint, so the commit would stage a **stale** journal and defeat the
union merge the script exists to perform. `busy_timeout` is now explicit (30s)
and `_init_db` is one explicit transaction, which covers the lock-contention and
torn-state concerns. `.gitignore` now excludes `*-wal`/`*-shm` as a backstop.
Parallel workflows each run on their own runner, so there is no same-file
concurrency for WAL to buy.

**New risk bugs found during recon (severity understated as "medium"):**
- `futures._atr` could return **NaN**, and both guards (`if not atr or atr <= 0`)
  let it through, because `not nan` and `nan <= 0` are both False. It reached the
  position dict as a bare `NaN` from `json.dumps`, where a NaN stop compares
  False against every bar low — **the position could never be stopped out or take
  profit**, and `if stop <= liq` (the SL-before-liquidation check) was skipped
  too. `open()` literally reported `SL $nan TP $nan`. Now returns `None`.
- Tier 4 `_sell_position` had **no fill-price parameter**, so every Tier 4 exit
  filled at the hourly mark, never at the stop/trail/TP level. A 50% intrabar
  crash stopped out at the pre-crash price, booking a small loss *and* leaving
  `t4_peak_equity` high (disarming the 25% kill). Tier 5 always filled at the
  touch. The partial TP deliberately still fills at the mark — it triggers on a
  gain threshold, not a level touch.
- Tier 4's trailing stop could **fire on the same bar that armed it** (level
  derived from a 5m window's high, tested against that window's low) and
  **pre-empted the partial TP**. The partial now runs first and a trail records
  `trailing_stop_armed_at` (the fix Tier 5 got in `fb72f00`).

**Two pre-existing test-isolation defects fixed here** (both made the suite's
green/red meaningless — worth remembering when trusting a past "288 passed"):
- `bot.memecoin._TICKER_MAP_CACHE` is a **module-level global with a 24h TTL**.
  Any earlier test that made a real `/coins/list` call poisons it, and
  `test_dexscreener_spike_filter` then resolved against LIVE data
  (`black-unicorn-corp` instead of `mooncoin`). Confirmed pre-existing: it fails
  the same way with the commit-3 test file.
- `test_pain_meter_all_healthy` patched only **5 of the 7** workflows the meter
  knows about — and passed only because the 2 unpatched ones rendered as a benign
  "no runs found". The "everything is healthy" test had two unmonitored
  workflows in it. The fixture now derives its list from `WORKFLOW_SCHEDULES`.

**Test-harness traps found in commit 4 are listed in "Gotchas that cost time"
at the bottom** — read them before writing any Tier 4/5/notify test. The two
isolation bugs are the important ones:

- `bot.memecoin._TICKER_MAP_CACHE` is a **module-level global with a 24h TTL**.
  Any earlier test that made a real `/coins/list` call poisons it, and
  `test_dexscreener_spike_filter` then resolved against LIVE data
  (`black-unicorn-corp` instead of `mooncoin`). Confirmed pre-existing: it fails
  the same way with the commit-3 test file.
- `test_pain_meter_all_healthy` patched only **5 of the 7** workflows the meter
  knows about — and passed only because the 2 unpatched ones rendered as a benign
  "no runs found". The "everything is healthy" test had two unmonitored
  workflows in it. The fixture now derives its list from `WORKFLOW_SCHEDULES`.

## Open threads

- **Commits 3, 4 and 5 are done and verified.** `88c92ea` (288 passed),
  `80cdd82` (317) and `43f4f8e` (338). For commits 4 and 5 every new test that
  asserts a fix was proven to fail against the pre-fix production code first
  (28 and 18 respectively). The 3 that pass both ways are deliberate: one
  guards a fallback that must *not* change, one pins an argument a deleted
  dead local could have corrupted, one covers already-correct clipping.
- **Working tree:** `HANDOFF.md` and `data/trades.db` are modified; nothing is
  staged. The journal is left unstaged on purpose — `tools/safe_commit.sh`
  owns it. Prior audit commits did not include `HANDOFF.md` either, so it is
  normal for it to be dirty at a context boundary.
- **The pending `tests/test_audit_fixes.py` docstring change is now folded into
  commit 5** (it also gained a commit-5 paragraph). Nothing outstanding there.
- **Commits 6-8 not started.** Full list at the bottom. Commit 6 is the Tier 3
  rebuild and is the last one needing real design work.
- `data/archive/*.db.pre-reset-*` (4 files) are now **gitignored** — done in
  commit 4, they used to be listed as commit-5 work.

## Key context

**The 4-week gate deadline is 2026-10-07 — 10 days out.** `day_zero_reset_at` is
2026-09-09, so 2.53/4 weeks are already elapsed and Tier 1's P&L kill criterion
arms in 10 days. That is why gate correctness went first. Expect the daily report
to change after commit 1: Tier 1 stays RED but now for a **genuine** 2-week
losing streak (W37 -$5.67, W38 -$0.31 realized) instead of a fabricated 40%
drawdown, and Tier 3's win rate becomes 67%.

**Commit 4 changed what the Tier 4/5 ledgers will report from now on**, so expect
the numbers to move again: Tier 5 exit fees are now charged on the exit notional
(they were under-charged on winners, over-charged on losers), Tier 5 entries now
reserve the entry fee so cash can no longer go negative, funding accrues on the
mark notional and its cursor survives a zero-delta cycle, and Tier 4 booked
per-trade P&L is one entry_fee lower per exit than it used to report. **None of
this changes gate math** — the gates consume equity, which was always computed
from cash deltas and was always correct. Historical `trades.reasoning` strings
written before commit 4 remain inflated by one entry_fee per Tier 4 exit and are
not retroactively corrected.

**Binance is firewalled on the dev box** (`data-api.binance.vision` hangs even on
`/ping`) but fine on GitHub runners — proof: the futures/memecoin workflows
succeed on schedule and the 94 journaled fills came from them. Tests mock the
network. So: **nothing needs the owner's machine to be on.** Any step needing
live prices must be dispatched to a runner via `gh workflow_dispatch` (`gh` is
authenticated with `repo` + `workflow`). Gamma/coingecko are reachable.

**Tier 3 redesign (commit 6), researched and agreed, not yet built.** Measured
against the live API:
- `gamma-api.polymarket.com/markets?end_date_min=&end_date_max=` filters by
  resolution date server-side; `order=liquidityNum` sorts by depth.
- 2-14d window = 100 markets, $25.9M liquidity, median $72k, all above
  `min_market_liquidity: 10000` — but only **7 of 100** clear
  `min_market_volume: 50000`, so that config value **must** be relaxed or the
  tier still starves.
  **→ RE-MEASURED 2026-09-27, and the original number is STALE. Do NOT relax
  `min_market_volume`.** Against today's live Gamma API the same 2-14d window
  returns 100 markets / **$37.0M** liquidity / median **$169k** / min **$92,965**,
  and **88 of 100** clear `min_market_volume: 50000`. The "7 of 100" figure was
  measured on a different window or date. The real binding constraint is the
  **100-market page cap**, not the volume floors — which commit 4 fixed by
  paginating (`offset`, 3 pages) and by sorting on `liquidityNum` instead of
  `volume24hr`.
- `endDate` is the *event* end, not market resolution: 31 of 100 "7-14 day"
  markets were tennis matches resolving in hours. Concentration is heavy
  (21 Brazil presidential, 10 Rio governor) and `_family_has_open_bet` already
  blocks a second leg in a held family.
- Free + unauthenticated research endpoints, all verified working:
  `clob.polymarket.com/book?token_id=` (real bid/ask spread),
  `clob.polymarket.com/prices-history?market=&interval=1w&fidelity=60` (169 pts),
  `data-api.polymarket.com/trades?market=<conditionId>` (price/size/side).
- Markets now charge real taker fees (`feeType: politics_fees`, `rate: 0.04`,
  `takerOnly`), so flat `friction_pct: 1.0` is a guess. **Do not ship the fee
  formula into the EV math until it reconciles against a known settled market.**
- Agreed shape: hard quality floor (never waived) + edge bar (never waived);
  *widen the search* when the window is dry rather than lowering the bar. Fair
  probability stays a market-weighted blend — only bet an **identifiable**
  inconsistency (family sum ≠ 1.00 primary), never a model merely disagreeing.
  Record a dossier per bet in new `research_json` / `edge_source` columns
  (`_ensure_columns` pattern). Scanner 6-hourly → hourly. LLM narrows to
  screening resolution-criteria ambiguity.
- Root cause of 18 days with no bets: the echo-matching bug above. Bet 4 (CS2
  GamerLegion) landed 14h after bet 3, so the "stuck" bet never blocked
  anything ($80 headroom remained).

**Two earlier findings retracted after verification — do not reintroduce:**
- Bet 3 (Brazil 2026 election, `outcome='open'`) is **not stuck**. Gamma reports
  `closed: False, outcomePrices: ["0.425","0.575"]` — the election genuinely has
  not resolved, and `polymarket.py:328-345` deliberately only settles on an
  unambiguous final print. **No repair. The exact-string winner compare at
  `polymarket.py:350` is a low-severity robustness item, not high.**
- **The machine-off watchdog already works.** `report.yml` runs on GitHub and
  `bot/report.py:actions_health` caught a real ~20h blackout at
  `2026-09-26T20:42:19Z` ("trade: STALLED | last run 1.2h ago") and pushed it to
  Discord. So commit 8 must **not** build new alerting. Its real scope is
  self-healing auth + memecoin coverage + staleness-gating (see below).

**Machine dependency (pre-existing, not introduced by this work).**
`~/.config/trading-pinger/pinger.sh` runs from the owner's crontab every 15 min to
dispatch `trade`/`chat`, because GitHub's native scheduler delivers only ~6-8% of
15-min runs. Measured 24h delivery: trade ~35%, chat ~36%, agent ~58%,
futures ~54%, **memecoin ~37% with zero pinger coverage**. `PINGER_SETUP.md`
already states the host must stay on. The cached `gh_token` is dated **2026-09-07**
(`refresh_token.sh` runs Sundays 04:00 and hasn't succeeded since). cron-job.org
was tried as a cloud alternative and **failed silently** (zero dispatches over
7h, rolled back 2026-09-10) — do not re-attempt without vetting the silent-failure
mode. Commit 8 scope: prefer `gh auth token` over the cached file, add memecoin
to the dispatch net, convert chat/trade from unconditional to staleness-gated,
make 401/403 and 000 loud instead of routine log lines.

**Proposal 386 is genuinely stuck** (the one real data repair, still open and
verified again 2026-09-27). `NEAR/USD` BUY, `exec_status='open'`, expired
2026-09-21, now 6 days overdue, all fields populated (entry 4.134, btc_entry
80769.55, `exec_price` NULL). It is the **only** `open` ai_agent proposal in the
table, and both the capped and the symbol-scoped query still return it, so
commit 4 did not change its status.

Cause: `bot/agent.py:326-327`'s `continue` when `_price_context` returns None —
no retry cap, no dead-letter. That path is gated on `entry_price` and
`expiry_timestamp` being populated; this row *has* both, but the `continue`
still drops it every cycle, so `evaluate_due_proposals` can never advance it, and
`bot/agent.py:433`'s suppression check then blocks **all** future NEAR scout
proposals forever. (Both line numbers moved in commit 4; they were 264 and 387.)

Note the interaction with commit 4: `evaluate_due_proposals` iterates
`get_open_proposals()`, which is now **capped at 500** and ordered `timestamp
ASC`. With one stuck row that is harmless — the cap keeps the most overdue rows
first — and the symbol-scoped check at :433 is correct at any depth by
construction. A bounded query alone does **not** release the row; commit 7 still
needs an explicit expiry/stale reaper.

If Binance is still blocked, **do not fabricate a P&L** — mark `rejected` with
`expired_unevaluable` and flag for re-evaluation.

**Style conventions.** No comments unless they carry non-obvious reasoning (this
commit series adds comments explaining *why* a bug existed, matching the repo's
existing style). No emojis in code. Tests mirror existing conventions
(`test_deep_dive_fixes.py` / `test_tier4_fixes.py`); monkeypatching
`led.price_for = lambda s: ...` produces harmless LSP warnings (same pattern
already appears 6x in `tests/test_tier5.py`), as do unresolved `pandas`/
`openai`/`pytest` venv imports. **No lint or typecheck exists in this repo** —
that absence is why a 224-green suite shipped a 19x volatility error; the owner
chose regression-tests-only, so this is flagged in commit messages, not fixed.

**Gotchas that cost time:**
- `TradingAgent(cfg, broker, journal=..., model=None)` constructs a real
  `ModelManager`, which probes the network on init and **hangs the test run**.
  Always pass a stub model (`_NoModel` in `test_audit_fixes.py`). The same trap
  exists at `bot/polymarket.py:164-166` — `scan_llm_mispricing` builds a real
  `ModelManager` unless you pass `model=`.
- `tests/conftest.py` has an **autouse** fixture that replaces
  `bot.notify.send_notification` with a no-op. To test the real function,
  capture it at *module import* time (collection precedes fixtures):
  `from bot.notify import send_notification as _real_send_notification`.
- `_validate(proposal, kind)` returns a **2-tuple** `(None, errors)` on failure,
  `(dict, [])` on success — not 3 elements, and `kind` is positional.
- `memecoin.buy(symbol, stake=None, reason=...)` — no `price`/`note` kwargs; it
  uses `price_for`.
- Futures `sweep()` fires liquidation **before** stop-loss, and the stop is purely
  bar-range based (`bars["low"].min() <= stop`), so a test mark below the stop
  still needs bars whose lows dip through it.
- Tier 5's `_atr` now returns **`None`** (not NaN) for an unusable window, and
  its two guards test `is None` explicitly. Any new caller must do the same —
  `not atr` / `atr <= 0` will **not** catch a NaN, which is the entire bug.
- `_price_context` drops the last bar as still-forming (`df.iloc[:-1]`), so
  test expectations must be computed from `closes[:-1]`.
- `pd.concat([...]).max(axis=1)` **skips** a single NaN; poisoning a rolling
  mean needs a fully-null row, and it only matters if it lands inside the window
  `.iloc[-1]` reads.
- A Tier 4 sweep fires the FULL take-profit at `entry*1.5` by default, which
  masks a trailing-stop test — set `pos["take_profit"]` high to isolate one rule.
- Tier 4 `sweep()` only consults `_wick_extremes` when `wick_exits` is True; the
  `_T4Cfg` fixture in `test_audit_fixes.py` has it **False** while `config.yaml`
  has it True. Set it explicitly or a wick-driven test passes vacuously.
- `futures._accrue_funding` event amounts are `round(..., 8)`, so assertions
  cannot be tighter than `abs=1e-8`.
- `bot.memecoin._TICKER_MAP_CACHE` is a module-level global with a 24h TTL —
  reset it in any test that mocks CoinGecko, or it resolves against live data.
- Always run pytest with `-p no:cacheprovider`; the default cache provider
  interacts badly with the long runs. Never run the output inline — redirect to
  a file and `tail` it, or a 300-line report floods the context.
- **Two APIs now raise where they used to return quietly** (commit 5). Any new
  caller must handle it: `TradeJournal.get_meta_float` raises `JournalError` on
  an unparseable or non-finite meta value (use it for every cash/peak read —
  do not re-inline `float(get_meta(...))`), and `strategies.get_strategies`
  raises `ValueError` on a name not in `REGISTRY`. Both are deliberate.
- `binance_paper._Order` **stringifies** `filled_qty` / `filled_avg_price` (it
  mirrors Alpaca's shape). Wrap in `float()` before comparing to a number.
- To test a **local-time vs UTC** claim on a UTC dev box, inject a fake clock
  (a class with `now(tz)` returning a fixed naive local time). A plain
  before/after comparison passes on a UTC host and proves nothing — this is why
  `test_daily_report_header_stamp_is_utc` monkeypatches `report.datetime`.
- `create_daily_report()` opens the **real** `data/trades.db` and calls
  `actions_health()` (GitHub API). To test it, monkeypatch `report.TradeJournal`
  plus the snapshot helpers and take the no-trades early-return path, which
  only renders the header.

## Remaining commits

4. ~~**Medium (17)**~~ — **DONE, `80cdd82`.** All 17 addressed; see "Done this
   session" for the three whose premise did not survive verification and what was
   actually wrong instead.

5. ~~**Low (18)**~~ — **DONE, `43f4f8e`.** All addressed except the two that
   were **already correct** (paper SELL clipping, `await_terminal_order` log
   bound) and the one **already done** (`data/archive/` gitignore, commit 4) —
   5 retracted findings in total across the series. Two adjacent defects of
   the same class were picked up and fixed with them: the paper broker's NaN
   quantity (the *source* of a poisoned cash ledger) and `futures._peak()`
   (a NaN peak disarmed the drawdown kill). Four real defects were found but
   deliberately left open and are listed under "Done this session".

6. **Tier 3 rebuild** — see Key context above. **Next up.** The agreed shape:
   hard quality floor + edge bar, never waived; *widen the search* when the
   window is dry rather than lowering the bar. Bet only an **identifiable**
   inconsistency (family sum ≠ 1.00 primary). Dossier per bet in new
   `research_json` / `edge_source` columns. Scanner 6-hourly → hourly.
7. **Data repair** — `tools/repair_stuck.py` + a `workflow_dispatch` workflow
   (model on `tools/tier4.py` + `memecoin.yml`); plus a `fix_boundary` meta
   marker and a one-line report note labelling pre-fix numbers unreliable
   (~10 lines, no data discarded).
8. **Pinger hardening** — see Key context above.

## Commands

- Tests: `./venv/bin/python -m pytest tests/ -q -p no:cacheprovider`
  (baseline **224** pre-audit; **251** after commit 2; **288** after
  commit 3; **317** after commit 4; **338** after commit 5). It takes
  **~6 min** — run it with output redirected to a file and `tail` the
  summary, never inline, or it floods the context. `tests/test_tier5.py` alone
  can exceed a 3-min timeout because `test_funding_event_notifies_without_symbol`
  spends ~104s in network retries against the firewalled Binance host; that is
  pre-existing and not a hang. Budget >= 800s for the full run.
- Audit tests only (fast): `./venv/bin/python -m pytest tests/test_audit_fixes.py -q -p no:cacheprovider`
- Full gate report: `python run_report.py`; gates alone via `bot.gates.evaluate_gates`
- Tier cycles: `python tools/tier4.py cycle`, `python tools/tier5.py cycle`
- Journal: `python tools/audit_ledgers.py`; reset helpers in `tools/reset_day_zero.py`
