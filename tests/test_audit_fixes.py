"""Regression tests for the 2026-09-27 sandbox audit.

Each test here failed on the code as it stood before the corresponding fix.
Grouped by commit so the provenance stays traceable.

Commit 1 (gate correctness): Tier 1 drawdown must not mix ledgers (epoch
filter), the losing-week streak must use the right column indices, the
Tier 3 gate must read a win rate that bet_scorecard actually returns, and
a split round trip must be scored once rather than per partial sell.

Commit 2 (criticals): a trailing stop must be judged only on bars since its
level was armed; a live kill must not suspend exit enforcement; an unpriceable
position must not vanish from equity; volatility must be annualized; RSI must
actually be computable.

Commit 3 (silent fallbacks): interval mapping must be idempotent and total, a
non-positive mark is no usable price rather than a total loss, a None model
response must not be subscripted, a NaN notional must be rejected, and the LLM
must not be asked to echo a question string byte-for-byte.

Commit 4 (medium): the journal must close its connections and save a tier's
cash+positions in one transaction; _ensure_columns must refuse unknown
identifiers; get_open_proposals must be bounded but the per-symbol suppression
check must not be; a NaN ATR must not become a NaN stop; Tier 5 must reserve
the entry fee and charge the exit fee on the exit notional; the funding cursor
must survive a zero-delta cycle; Tier 4 must charge the entry fee in its booked
P&L, fill at the level that triggered the exit, and let the partial take-profit
precede the trailing stop; agent context must use interval-aware bar counts and
refuse a non-finite value; notify must not silently drop a message; the pain
meter must flag a dead workflow; the scanner must paginate; research must use
the caller's journal and request a recency window.

Commit 5 (low): the shadow account must mark and fill on the last CLOSED bar,
not the still-forming one; a corrupt cash or equity-peak meta key must be
refused rather than read as unlimited buying power or as "no drawdown"; the
paper broker must not write a non-finite balance; the strategy contract must
be validated where signals are produced and an unknown strategy name must
raise; no module may stamp a naive local clock.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.journal import TradeJournal

# conftest's autouse fixture replaces bot.notify.send_notification with a no-op
# so no test can fire a live webhook. Capture the real function at import time
# (collection precedes fixtures) for the tests that must exercise it.
from bot.notify import send_notification as _real_send_notification


# ---------------- commit 1: Tier 3 gate win rate ----------------

def test_tier3_gate_reports_real_win_rate(tmp_path):
    """2 wins and 1 loss is a 66.7% win rate, not 0%.

    The gate read score["wins"], a key bet_scorecard never returns, and the
    fallback dict seeded "wins": 0 so the bug looked intentional. That made
    the win rate a hard 0% forever, which drives the >=10-settled-bet KILL
    criterion to fire regardless of the real record.
    """
    from bot.gates import tier3_gate
    j = TradeJournal(db_path=str(tmp_path / "b.db"))
    base = datetime.now(timezone.utc) - timedelta(days=3)
    outcomes = [("won", 60.0), ("won", 50.0), ("lost", 0.0)]
    for i, (outcome, payout) in enumerate(outcomes):
        bid = j.log_bet(timestamp=(base + timedelta(hours=i)).isoformat(),
                        market=f"mkt-{i}", question=f"q{i}", side="Yes",
                        price=0.5, stake=20.0, outcome="open", fee=0.2)
        j.update_bet(bid, outcome, payout)

    score = j.bet_scorecard()
    assert score["settled"] == 3
    assert abs(score["win_rate_pct"] - 66.7) < 0.1

    gate = tier3_gate(journal=j)
    # the gate detail line must carry the true win rate
    assert "67%" in gate.details or "66." in gate.details


# ================= commit 2: critical =================

import pandas as pd


# ---------------- volatility must be annualized ----------------

def test_realized_vol_is_annualized_for_daily_closes():
    """A 30%-annualized series must read ~30%, not ~1.6%.

    The function returned the per-bar standard deviation of log returns with
    no sqrt(periods_per_year) factor. Its only caller feeds the Tier 4 LLM
    conviction gate, which gates automated entries -- so a risk gate was
    reporting volatility ~19x too small for daily data.
    """
    import math
    import random
    from bot.indicators import PERIODS_PER_YEAR, realized_volatility_pct
    random.seed(7)
    sigma_daily = 0.30 / math.sqrt(365)
    px, closes = 100.0, [100.0]
    for _ in range(80):
        px *= math.exp(random.gauss(0, sigma_daily))
        closes.append(px)

    got = realized_volatility_pct(closes, periods_per_year=PERIODS_PER_YEAR["1d"])
    assert 22.0 < got < 40.0, f"expected roughly 30% annualized, got {got:.2f}"


def test_realized_vol_scales_with_sampling_rate():
    """The same volatility must annualize consistently at 15m and 1d."""
    import math
    import random
    from bot.indicators import PERIODS_PER_YEAR, realized_volatility_pct
    random.seed(11)
    target = 0.50
    for label, ppy, n in (("1d", 365, 90), ("15m", 35040, 800)):
        random.seed(11)
        sigma = target / math.sqrt(ppy)
        px, closes = 100.0, [100.0]
        for _ in range(n):
            px *= math.exp(random.gauss(0, sigma))
            closes.append(px)
        got = realized_volatility_pct(closes, periods_per_year=ppy)
        assert abs(got - target * 100) < 12.0, f"{label}: {got:.1f}%"


def test_realized_vol_requires_a_sampling_rate():
    """No safe default exists, so an unscaled call must not return a number."""
    from bot.indicators import realized_volatility_pct
    assert realized_volatility_pct([1.0, 1.1, 1.2, 1.15]) is None


def test_indicators_return_none_on_malformed_input():
    """The module contract promises None for unusable data, not an exception."""
    from bot.indicators import realized_volatility_pct, rsi
    assert rsi([1.0, None, 3.0, 4.0, 5.0, 6.0]) is None
    assert rsi([1.0, 2.0, "x", 4.0, 5.0]) is None
    assert rsi([1.0, float("nan")] * 20) is None
    assert realized_volatility_pct([1.0, None, 1.2], periods_per_year=365) is None


def test_rsi_receives_enough_closes_from_the_tier4_history():
    """The Tier 4 history must retain enough closes for a 14-period RSI.

    _coingecko_history kept only the last 10 of 30 daily closes while the
    caller used the default period=14 (which needs 15), so rsi_30d was None
    for every coin on every cycle -- a named risk lens silently missing from
    an automated entry gate.
    """
    from bot.indicators import rsi
    thirty = [100.0 + (i % 7) for i in range(30)]
    assert rsi(thirty[-10:]) is None, "10 closes cannot feed a 14-period RSI"
    assert rsi(thirty) is not None, "30 closes must feed a 14-period RSI"


# ---------------- stale marks must not fabricate a drawdown ----------------

class _T4Cfg:
    memecoin = {
        "start_cash": 40, "max_stake": 12, "stop_loss_pct": 25,
        "take_profit_pct": 50, "time_stop_hours": 72, "max_drawdown_pct": 25,
        "taker_fee_pct": 1.0, "slippage_bps": 100, "card_ttl_minutes": 360,
        "max_trending_cards": 7, "spike_volume_multiple": 3.0,
        "spike_min_volume_24h": 100000, "auto_entry": True,
        "auto_cooldown_hours": 24, "min_llm_confidence": 0.75,
        "base_stake": 6, "max_open_positions": 3, "min_liquidity_usd": 250000,
        "min_volume_24h_usd": 500000, "min_age_hours": 168, "max_mcap_rank": 300,
        "trailing_stop_pct": 20, "trailing_activate_pct": 25,
        "partial_tp_pct": 80, "partial_tp_sell_frac": 0.5,
        "entry_cooldown_hours": 168,
    }


def _t4_ledger(tmp_path, prices=None):
    from bot.memecoin import MemecoinLedger
    j = TradeJournal(db_path=str(tmp_path / "t4.db"))
    led = MemecoinLedger(_T4Cfg, journal=j)
    if prices is not None:
        led.price_for = lambda s: prices.get(str(s).upper())
    return led, j


def test_stale_mark_does_not_collapse_equity(tmp_path):
    """One dead price feed must not read as a 30% drawdown on the ledger.

    valuation() dropped unpriceable positions from equity entirely, so a
    single outage on one $12 coin of a $40 ledger understated equity by 30%
    and tripped the account kill.
    """
    led, j = _t4_ledger(tmp_path, prices={"AAA": 1.0, "BBB": 1.0, "CCC": 1.0})
    for sym in ("AAA", "BBB", "CCC"):
        led.buy(sym, stake=12)
    healthy = led.valuation()

    # CCC's feed dies entirely
    led.price_for = lambda s: None if str(s).upper() == "CCC" else 1.0
    degraded = led.valuation()

    assert degraded["equity"] == healthy["equity"], (
        "a stale mark must not change equity when the last known price still "
        "stands; equity collapsed, which is what fired the kill")
    assert degraded["stale_symbols"] == ["CCC"]


def test_stale_mark_still_reported_so_the_blind_spot_is_visible(tmp_path):
    led, j = _t4_ledger(tmp_path, prices={"AAA": 1.0, "CCC": 1.0})
    for sym in ("AAA", "CCC"):
        led.buy(sym, stake=12)
    led.valuation()  # records last_mark while the feed is healthy
    led.price_for = lambda s: None
    v = led.valuation()
    assert sorted(v["stale_symbols"]) == ["AAA", "CCC"]


def test_unpriceable_position_is_never_closed_at_entry(tmp_path):
    """With no mark and no last_mark, refuse the close instead of faking it.

    Falling back to the entry price fictitiously restored equity during a
    forced flatten, and reset_kill then re-armed the peak at that invented
    value -- disarming the drawdown kill permanently.
    """
    led, j = _t4_ledger(tmp_path, prices={"AAA": 1.0})
    led.buy("AAA", stake=12)
    cash_before = led._cash()

    led.price_for = lambda s: None
    pos = led._positions()["AAA"]
    pos.pop("last_mark", None)
    import json as _json
    j.set_meta("t4_positions", _json.dumps({"AAA": pos}))

    assert led._sell_position("AAA", note="kill flatten") is None
    assert "AAA" in led._positions(), "position must survive an unpriceable close"
    assert led._cash() == cash_before, "no proceeds may be credited"


def test_kill_switch_does_not_suspend_tier4_exits(tmp_path):
    """An open Tier 4 position keeps its stop while the kill switch is on."""
    led, j = _t4_ledger(tmp_path, prices={"AAA": 1.0})
    led.buy("AAA", stake=12)
    j.set_meta("t4_kill", "on")

    # price collapses 40%: well past the 25% stop, and above entry
    led.price_for = lambda s: 0.60
    events = led.sweep()

    assert "AAA" not in led._positions(), (
        "a breached stop must be enforced with the kill switch on")


# ================= commit 3: high =================

# ---------------- timeframe mapping ----------------

@pytest.mark.parametrize("spec,expected", [
    ("15Min", "15m"), ("1Hour", "1h"), ("1h", "1h"), ("4h", "4h"),
    ("1Day", "1d"), ("1d", "1d"), ("2Day", "1d"), ("30Min", "30m"),
    ("1Week", "1w"), ("d", "1d"), ("h", "1h"), ("m", "1m"),
    ("60m", "1h"), ("15min", "15m"), ("5Min", "5m"),
])
def test_interval_for_is_idempotent(spec, expected):
    """Mapping must be a fixed point.

    binance_paper applied _interval_for twice, and the old mapper collapsed
    anything it did not explicitly know back to "15m". So "1Hour" became
    "1h" and then "15m": the strategy silently fetched 15m bars whatever the
    config said, and with sma_slow=50 check_crossover returned [] so the bot
    never traded -- with no error anywhere.
    """
    from bot.binance_data import _interval_for
    once = _interval_for(spec)
    assert once == expected
    assert _interval_for(once) == once
    assert _interval_for(_interval_for(once)) == once


def test_amount_unit_timeframe_object_maps_to_its_real_interval():
    """amount+unit timeframe objects previously always resolved to 15m.

    The old branch read a value_count attribute alpaca-py 0.44 no longer
    has and imported TimeFrameUnit from a module that does not exist, so the
    whole block was dead and it fell through to `return "15m"`.
    """
    from types import SimpleNamespace
    from bot.binance_data import _interval_for

    def tf(amount, unit):
        return SimpleNamespace(amount=amount, unit=SimpleNamespace(name=unit))

    assert _interval_for(tf(1, "Hour")) == "1h"
    assert _interval_for(tf(1, "Day")) == "1d"
    assert _interval_for(tf(15, "Minute")) == "15m"


def test_bot_timeframe_objects_still_map():
    """bot.timeframe objects expose value_count in minutes; keep supporting."""
    from bot.binance_data import _interval_for
    from bot.timeframe import Hours, Minutes, make_timeframe
    assert _interval_for(Minutes(15)) == "15m"
    assert _interval_for(Hours(1)) == "1h"
    assert _interval_for(make_timeframe("1Day")) == "1d"


@pytest.mark.parametrize("bad", ["banana", "", "hourly", "-1Min", "0Min"])
def test_unknown_timeframe_raises_instead_of_defaulting(bad):
    """A silent wrong-timeframe fallback produces plausible output for the
    wrong data, which is worse than a loud failure."""
    from bot.binance_data import _interval_for
    from bot.timeframe import make_timeframe
    with pytest.raises(ValueError):
        _interval_for(bad)
    with pytest.raises(ValueError):
        make_timeframe(bad)


def test_interval_minutes_knows_every_emitted_interval():
    """The lookup answered 15 for anything it did not know, which silently
    computed the wrong history window."""
    from bot.binance_data import _INTERVAL_MINUTES, interval_minutes
    for iv in ("1m", "5m", "15m", "30m", "1h", "4h", "1d"):
        assert interval_minutes(iv) == _INTERVAL_MINUTES[iv]
    assert interval_minutes("1h") == 60
    with pytest.raises(ValueError):
        interval_minutes("7q")


def test_make_timeframe_accepts_lowercase_specs():
    from bot.timeframe import Days, Hours, Minutes, make_timeframe
    assert isinstance(make_timeframe("15min"), Minutes)
    assert isinstance(make_timeframe("1h"), Hours)
    assert isinstance(make_timeframe("1d"), Days)


# ---------------- zero / implausible marks ----------------

class _J:
    def __init__(self):
        self.m = {}

    def get_meta(self, k):
        return self.m.get(k)

    def set_meta(self, k, v):
        self.m[k] = v


# ---------------- report must survive a model that returns nothing ----------------

def test_model_parsers_reject_none_with_modelerror():
    """None content from a reasoning model must be a ModelError, not an
    AttributeError/TypeError that escapes every caller."""
    from bot.errors import ModelError
    from bot.models import ModelClient
    with pytest.raises(ModelError):
        ModelClient._parse_json_arr(None)
    with pytest.raises(ModelError):
        ModelClient._parse_json(None)


# ---------------- NaN notional ----------------

class _AgentCfg:
    """Minimal stand-in for bot.config.config as the agent reads it."""

    def __init__(self):
        self.symbols = ["BTC/USD", "ETH/USD", "SOL/USD"]
        self.agent = {"enabled": True, "shadow": True, "min_confidence": 0.5,
                      "max_proposed_notional": 50}
        self.research = {}


class _NoModel:
    """Never touches the network: a real ModelManager probes on construction."""

    def generate_json(self, *a, **k):
        return {}

    def generate_json_arr(self, *a, **k):
        return []

    def generate_text(self, *a, **k):
        return ""

    def daily_health_check(self):
        return None


def _agent(tmp_path):
    from bot.agent import TradingAgent
    j = TradeJournal(db_path=str(tmp_path / "a.db"))
    return TradingAgent(_AgentCfg(), market_data=None, journal=j, model=_NoModel())


def test_nan_notional_is_rejected(tmp_path):
    """Both numeric guards are False for NaN, so it passed validation, opened a
    real shadow position, and sqlite stored NULL -- scoring the proposal as a
    flat non-win forever."""
    ag = _agent(tmp_path)
    ok, errors = ag._validate({
        "action": "BUY", "symbol": "BTC/USD",
        "notional": float("nan"), "confidence": 0.8, "rationale": "r",
    }, "scout")
    assert ok is None
    assert any("NaN" in e for e in errors), errors


def test_infinite_notional_is_rejected_by_the_cap(tmp_path):
    ag = _agent(tmp_path)
    ok, errors = ag._validate({
        "action": "BUY", "symbol": "BTC/USD",
        "notional": float("inf"), "confidence": 0.8, "rationale": "r",
    }, "scout")
    assert ok is None
    assert any("above agent cap" in e for e in errors), errors


def test_finite_notional_still_validates(tmp_path):
    """The NaN guard must not reject ordinary proposals."""
    ag = _agent(tmp_path)
    out, errors = ag._validate({
        "action": "BUY", "symbol": "BTC/USD",
        "notional": 10.0, "confidence": 0.8, "rationale": "r",
    }, "scout")
    assert out is not None and not errors, errors
    assert out["notional"] == 10.0


# ---------------- LLM response matching ----------------

def test_norm_q_absorbs_cosmetic_drift():
    from bot.polymarket import _norm_q
    base = "Will Eduardo Leite win the 2026 Brazilian presidential election?"
    for variant in (
        base,
        "  will  eduardo leite win the 2026 brazilian presidential election…  ",
        "Will Eduardo Leite win the 2026 Brazilian presidential election",
        'Will "Eduardo Leite" win the 2026 Brazilian Presidential Election.',
    ):
        assert _norm_q(base) == _norm_q(variant), variant


_GAMMA_MARKET = {
    "id": "m1", "slug": "m1",
    "question": "Will X happen by December?",
    "outcomes": '["Yes", "No"]', "outcomePrices": '["0.30", "0.70"]',
    "volume24hr": 50000.0, "volume": 500000.0, "liquidityNum": 50000.0,
    "endDate": "2026-12-31T00:00:00Z", "active": True, "closed": False,
    "events": [{"id": "ev1", "slug": "ev1"}],
}


class _ScanCfg:
    scanner = {"stake": 20, "mispricing_threshold": 0.08,
               "friction_pct": 1.0, "min_expected_value_pct": 2.0,
               "min_market_liquidity": 1000, "min_market_volume": 1000,
               "max_llm_markets": 5}


class _IdxModel:
    """Returns the market index only, never the question text."""

    def generate_json_arr(self, prompt, max_tokens=0):
        assert "[0]" in prompt, "prompt must label markets with an index"
        return [{"index": 0, "true_prob": 0.62}]


class _EchoModel:
    """A verbose model that echoes a lowercased, de-punctuated question."""

    def generate_json_arr(self, prompt, max_tokens=0):
        return [{"question": "will x happen by december", "true_prob": 0.62}]


def test_llm_mispricing_matches_by_index_not_by_echo():
    """The only path that can place a Tier 3 bet required the model to echo a
    200-char truncated question byte-for-byte, which models essentially never
    do -- so every candidate was discarded and the tier placed no bet for 18
    days while logging '0 EV-qualified finds'."""
    from bot.polymarket import scan_llm_mispricing
    finds = scan_llm_mispricing(_ScanCfg(), markets=[dict(_GAMMA_MARKET)],
                                model=_IdxModel())
    assert len(finds) == 1, "an index-keyed response must produce a find"
    assert finds[0]["llm_prob"] == 0.62
    assert finds[0]["side"] == "Yes"
    assert finds[0]["paper_bet_allowed"] is True


def test_llm_mispricing_still_accepts_a_question_echo():
    """A model that echoes the question instead of the index must still work."""
    from bot.polymarket import scan_llm_mispricing
    finds = scan_llm_mispricing(_ScanCfg(), markets=[dict(_GAMMA_MARKET)],
                                model=_EchoModel())
    assert len(finds) == 1


def test_llm_mispricing_surfaces_a_total_match_failure(capsys):
    """A silent total drop-off is indistinguishable from 'no opportunities
    existed', which is exactly how 18 days of no bets went unnoticed."""
    from bot.polymarket import scan_llm_mispricing

    class _BadModel:
        def generate_json_arr(self, prompt, max_tokens=0):
            return [{"index": 99, "question": "something else entirely",
                     "true_prob": 0.9}]

    finds = scan_llm_mispricing(_ScanCfg(), markets=[dict(_GAMMA_MARKET)],
                                model=_BadModel())
    assert finds == []
    out = capsys.readouterr().out
    assert "WARNING" in out, out
    assert "0/1" in out, out


# ================= commit 4: journal durability =================

def test_journal_closes_its_connections(tmp_path):
    """`with sqlite3.connect(...)` ends a transaction but never closes the
    handle, so all 23 journal call sites leaked a descriptor until the GC ran.
    The helper has to close deterministically."""
    import sqlite3 as _s
    j = TradeJournal(db_path=str(tmp_path / "j.db"))
    opened = []
    real_connect = _s.connect

    def _spy(*a, **k):
        conn = real_connect(*a, **k)
        opened.append(conn)
        return conn

    _s.connect = _spy
    try:
        j.log_trade("2026-01-01T00:00:00Z", "BTC/USD", "BUY", 1.0, 100.0, "x")
        j.get_meta("nothing")
        j.get_trades()
    finally:
        _s.connect = real_connect
    assert opened, "expected the journal to open connections"
    assert all(c.__class__ is not None for c in opened)
    # sqlite3.Connection has no public "closed" flag, so prove it by using it:
    # a closed connection raises ProgrammingError on any operation.
    for conn in opened:
        with pytest.raises(_s.ProgrammingError):
            conn.execute("SELECT 1")


def test_journal_sets_a_busy_timeout_and_stays_off_wal(tmp_path):
    """Lock waits are made explicit rather than relying on the stdlib's
    undocumented 5s default, and the file stays in DELETE journal mode.

    WAL is deliberately NOT enabled: tools/safe_commit.sh stages the journal as
    one self-contained binary and resolves conflicts from the :1:/:2:/:3: stages
    of that single file, so committed rows living in a -wal sidecar would mean
    staging a stale journal and defeating the union merge.
    """
    import sqlite3 as _s
    j = TradeJournal(db_path=str(tmp_path / "j.db"))
    with j._conn() as conn:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"


def test_set_meta_many_is_one_transaction(tmp_path):
    """A tier's cash and positions keys must never be observable disagreeing.

    set_meta committed per call, so a two-key save was two transactions and a
    crash between them left cash debited with positions unchanged -- which
    reads as free money rather than as corruption.
    """
    j = TradeJournal(db_path=str(tmp_path / "j.db"))
    calls = []
    real = j._conn

    class _Spy:
        def __init__(self, inner):
            self._inner = inner

        def __enter__(self):
            self._cm = real()
            conn = self._cm.__enter__()
            calls.append(conn)
            return conn

        def __exit__(self, *a):
            return self._cm.__exit__(*a)

    j._conn = lambda: _Spy(real)
    j.set_meta_many([("t4_cash", "1"), ("t4_positions", "{}"), ("t5_cash", "2")])
    assert len(calls) == 1, f"expected one connection, got {len(calls)}"
    assert j.get_meta("t4_cash") == "1"
    assert j.get_meta("t5_cash") == "2"


def test_ensure_columns_rejects_unknown_identifiers(tmp_path):
    """_ensure_columns interpolates a table, a column name AND a free-form type
    definition into DDL. All current callers pass literals, so this is a trap
    for the next caller rather than a live injection -- but the definition half
    can carry a "); DROP TABLE ..." payload."""
    from bot.errors import JournalError
    j = TradeJournal(db_path=str(tmp_path / "j.db"))
    import sqlite3 as _s
    conn = _s.connect(j.db_path)
    try:
        cur = conn.cursor()
        with pytest.raises(JournalError):
            j._ensure_columns(cur, "sqlite_master", {"x": "TEXT"})
        with pytest.raises(JournalError):
            j._ensure_columns(cur, "trades", {"evil": "TEXT"})
        with pytest.raises(JournalError):
            j._ensure_columns(cur, "trades", {"fee": "TEXT); DROP TABLE trades; --"})
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='trades'").fetchone()
    finally:
        conn.close()


def test_open_proposals_query_is_bounded_and_indexed(tmp_path):
    """The only unbounded reader in the journal, run twice per agent cycle.

    The cap is safe for the evaluation path (ASC ordering keeps the most
    overdue rows) but NOT for the per-symbol suppression check, which is why
    that caller moved to a symbol-scoped query.
    """
    import sqlite3 as _s
    j = TradeJournal(db_path=str(tmp_path / "j.db"))
    for i in range(5):
        j.log_proposal(timestamp=f"2026-01-0{i+1}T00:00:00Z", source="ai_agent",
                       kind="scout", symbol="BTC/USD", action="BUY",
                       notional=10, confidence=0.9, rationale="r",
                       exec_status="open")
    assert len(j.get_open_proposals(limit=2)) == 2
    # symbol-scoped lookup is unaffected by the global cap
    assert len(j.get_open_proposal_for_symbol("BTC/USD")) == 5
    assert j.get_open_proposal_for_symbol("ETH/USD") == []
    conn = _s.connect(j.db_path)
    try:
        plan = " ".join(str(r) for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM proposals WHERE source=? AND "
            "exec_status=? ORDER BY timestamp ASC LIMIT 500",
            ("ai_agent", "open")).fetchall())
        assert "idx_proposals_open" in plan, plan
        assert "TEMP B-TREE" not in plan, plan
    finally:
        conn.close()


# ---------------- commit 4: Tier 4 P&L, exit ordering, fill price ----------------

def test_tier4_booked_pnl_charges_the_entry_fee(tmp_path):
    """The cost basis is qty*entry, which is stake MINUS the entry fee (the fee
    is paid in tokens, not by an extra cash debit). So comparing proceeds
    against it without charging the entry fee again FORGIVES that fee: the
    reported pnl came out exactly one entry_fee too high.

    The ledger cash was always right, which is why no gate ever saw it -- only
    the per-trade pnl in trades.reasoning and the exit alert were inflated.
    """
    led, j = _t4_ledger(tmp_path, prices={"AAA": 0.10})
    cash_before_buy = led._cash()
    assert led.buy("AAA", stake=12)[0]
    pos = led._positions()["AAA"]
    entry_fee = 12 * 1.0 / 100
    assert pos["entry_fee"] == pytest.approx(entry_fee, abs=1e-12)
    r = led._sell_position("AAA", note="test")
    true_pnl = led._cash() - cash_before_buy
    assert r["pnl"] == pytest.approx(true_pnl, abs=1e-9), (
        f"booked pnl {r['pnl']} != actual cash delta {true_pnl}")


def test_tier4_exit_fills_at_the_triggering_level_not_the_mark(tmp_path):
    """_sell_position had no fill-price parameter, so it re-fetched the hourly
    mark and every Tier 4 exit filled at the mark -- never at the stop, trail or
    TP level that triggered it. A 50% intrabar crash stopped out at the
    pre-crash hourly price, booking a small loss instead of the real one, which
    also left t4_peak_equity high and disarmed the 25% drawdown kill."""
    led, j = _t4_ledger(tmp_path, prices={"AAA": 0.10})
    assert led.buy("AAA", stake=12)[0]
    pos = led._positions()["AAA"]
    stop = float(pos["stop"])
    # the mark is far ABOVE the stop, so a mark-fill would look profitable
    r = led._sell_position("AAA", note="stop loss", fill=stop)
    assert r["exit_price"] < 0.10, "filled at the mark instead of the stop"
    assert r["exit_price"] == pytest.approx(
        stop * (1 - led.slippage_bps / 10_000.0), rel=1e-9)
    assert r["pnl"] < 0


def test_tier4_partial_tp_is_not_pre_empted_by_the_trailing_stop(tmp_path):
    """The trailing stop is a rule for managing a runner, but it was evaluated
    BEFORE the partial take-profit. Because the trail level is derived from the
    same 5m window's high and then tested against that window's low, one bar
    spanning 0.19 -> 0.14 both armed 0.152 and breached it in a single pass --
    so a position that should have banked 50% and kept a runner was closed in
    full with no partial banked at all."""
    import pandas as pd
    px = {"AAA": 0.10}
    led, j = _t4_ledger(tmp_path, prices=px)
    led.wick_exits = True          # the real config has this on
    assert led.buy("AAA", stake=12)[0]
    px["AAA"] = 0.188          # +85% -> past the +80% partial threshold
    # a single 5m bar spanning 0.19 -> 0.14 both creates the 0.152 trail level
    # and breaches it, which is what let the trail claim the whole position
    led._wick_extremes = lambda s, since: (0.19, 0.14)
    led.partial_tp_pct = 80
    led.trailing_activate_pct = 25
    events = led.sweep()
    kinds = [e["type"] for e in events]
    assert "partial_tp" in kinds, (
        f"partial was pre-empted, got {kinds} (the 0.19->0.14 wick armed and "
        f"breached the trail in the same bar)")
    assert "trailing_stop" not in kinds
    # and the runner survives with partial_taken set
    assert led._positions()["AAA"]["partial_taken"] is True


def test_tier4_trail_cannot_fire_on_the_bar_that_armed_it(tmp_path):
    """A trail level that did not exist at any observable instant cannot have
    been breached. Tier 5 got this in fb72f00; Tier 4 never had it.

    The wick mock only reports a breaching low for windows that start AFTER the
    arm time, which is the whole point: the pre-fix code measured from the
    position's open and so saw lows from before the level existed.
    """
    px = {"AAA": 0.10}
    led, j = _t4_ledger(tmp_path, prices=px)
    led.wick_exits = True
    assert led.buy("AAA", stake=12)[0]
    px["AAA"] = 0.188
    led.partial_tp_pct = 999     # isolate the trail
    led.trailing_activate_pct = 25
    opened_ts = datetime.now(timezone.utc) - timedelta(hours=2)
    import json as _json
    pos = led._positions()["AAA"]
    pos["opened"] = opened_ts.isoformat()
    pos["take_profit"] = 99.0   # and isolate it from the full TP
    j.set_meta("t4_positions", _json.dumps({"AAA": pos}))

    def _wicks(sym, since):
        # the breaching low belongs to the window that includes PRE-arm history;
        # a window starting at the arm time has no bars yet
        if since is not None and since > datetime.now(timezone.utc) - timedelta(seconds=30):
            return (None, None)
        return (0.19, 0.14)

    led._wick_extremes = _wicks
    events = led.sweep()
    assert all(e["type"] != "trailing_stop" for e in events), (
        f"trail fired on the bar that armed it: {events}")
    # the level is still recorded, so a LATER sweep can honour it
    assert led._positions()["AAA"]["trailing_stop"] is not None
    assert "trailing_stop_armed_at" in led._positions()["AAA"]


# ---------------- commit 4: agent context, alerting, notify, report, research ----

def test_change_24h_spans_the_right_number_of_intervals(tmp_path):
    """A window of N bars spans N-1 intervals, so the bar one full period back
    is iloc[-(N+1)]. iloc[-96] spans 95 intervals -- 23h45m at 15m -- and was
    reported as 24h. The availability gate was wrong by the same one, computing
    a 23.75h change out of 96 bars."""
    import pandas as pd
    from bot.agent import TradingAgent
    closes = [100.0 + i for i in range(200)]

    class _Cfg:
        timeframe = "15Min"
        lookback_bars = 200
        sma_fast = 10
        sma_slow = 50

    class _Broker:
        def get_crypto_bars(self, symbol, tf, n):
            idx = pd.date_range(end=pd.Timestamp.now(tz="UTC"), periods=len(closes),
                                freq="15min", tz="UTC")
            return pd.DataFrame({"close": closes}, index=idx)

    a = TradingAgent.__new__(TradingAgent)
    a.cfg = _Cfg()
    a.market_data = _Broker()
    ctx = a._price_context("BTC/USD")
    # the last bar is dropped as still-forming, so measure on the same series
    closed = closes[:-1]
    # 24h before the last CLOSED close is 96 intervals back
    expected = (closed[-1] / closed[-(96 + 1)] - 1) * 100
    assert ctx["change_24h_pct"] == pytest.approx(expected, rel=1e-9)
    assert ctx["change_24h_pct"] != pytest.approx(
        (closed[-1] / closed[-96] - 1) * 100, rel=1e-9)


def test_bar_counts_follow_the_configured_interval(tmp_path):
    """96/24 are fifteen-minute constants. At 1Hour the '24h' change was
    computed over 4 days and the '6h' change over 23 hours."""
    import pandas as pd
    from bot.agent import TradingAgent
    closes = [100.0 + i for i in range(200)]

    class _Cfg:
        timeframe = "1Hour"
        lookback_bars = 200
        sma_fast = 10
        sma_slow = 50

    class _Broker:
        def get_crypto_bars(self, symbol, tf, n):
            idx = pd.date_range(end=pd.Timestamp.now(tz="UTC"), periods=len(closes),
                                freq="1h", tz="UTC")
            return pd.DataFrame({"close": closes}, index=idx)

    a = TradingAgent.__new__(TradingAgent)
    a.cfg = _Cfg()
    a.market_data = _Broker()
    ctx = a._price_context("BTC/USD")
    assert len(ctx["recent_24_bars"]) == 6, "6h at 1h bars is 6 bars, not 24"
    # 24h at 1h = 24 intervals -> 25 bars inclusive
    closed = closes[:-1]
    expected = (closed[-1] / closed[-(24 + 1)] - 1) * 100
    assert ctx["change_24h_pct"] == pytest.approx(expected, rel=1e-9)


def test_sma_gap_is_none_when_the_window_is_degenerate(tmp_path):
    """slow is a mean of closes, i.e. a price, and these are numpy floats, so a
    zero window yields inf/nan with a RuntimeWarning rather than
    ZeroDivisionError -- and the nan then rides into the journal and the LLM
    prompt. The two sibling fields already guarded their divisor."""
    import pandas as pd
    from bot.agent import TradingAgent
    closes = [0.0] * 200

    class _Cfg:
        timeframe = "15Min"
        lookback_bars = 200
        sma_fast = 10
        sma_slow = 50

    class _Broker:
        def get_crypto_bars(self, symbol, tf, n):
            idx = pd.date_range(end=pd.Timestamp.now(tz="UTC"), periods=len(closes),
                                freq="15min", tz="UTC")
            return pd.DataFrame({"close": closes}, index=idx)

    a = TradingAgent.__new__(TradingAgent)
    a.cfg = _Cfg()
    a.market_data = _Broker()
    ctx = a._price_context("BTC/USD")
    assert ctx["sma_gap_pct"] is None
    # and the context must still be JSON-serialisable under allow_nan=False
    import json
    json.dumps(ctx, allow_nan=False)


def test_journal_write_refuses_a_non_finite_context(tmp_path):
    """allow_nan=False has to be caught HERE: this call sits before
    log_proposal, so an escaping ValueError loses the row entirely, and
    run_cycle's handler only prints to the CI log -- the proposal would vanish
    with a green check."""
    j = TradeJournal(db_path=str(tmp_path / "a.db"))
    before = len(j.get_proposals(limit=1000))
    a = _agent(tmp_path)
    a._price_context = lambda s: {
        "symbol": s, "last_close": float("nan"), "sma_fast": 1.0,
        "sma_slow": 1.0, "sma_gap_pct": None, "recent_24_bars": [1.0],
        "change_6h_pct": 0.0, "change_24h_pct": None,
    }
    a._log_and_alert({"kind": "scout", "action": "BUY", "symbol": "BTC/USD",
                      "notional": 10, "confidence": 0.9, "rationale": "r"})
    assert len(j.get_proposals(limit=1000)) == before, "a non-finite context was journaled"


def test_log_and_alert_surfaces_the_downgrade_reason(tmp_path, monkeypatch):
    """`extra` was accepted and never used, so a downgraded low-confidence exit
    reached Discord as a plain HOLD idea -- the operator could not tell the
    model had wanted out and been overruled."""
    sent = []
    import bot.agent
    monkeypatch.setattr(bot.agent, "send_notification",
                        lambda m, c=None: sent.append(m))
    a = _agent(tmp_path)
    a._price_context = lambda s: None
    a._log_and_alert({"kind": "exit", "action": "HOLD", "symbol": "BTC/USD",
                      "notional": 0, "confidence": 0.4, "rationale": "r"},
                     extra="(low-confidence exit suggestion — logged as HOLD)")
    assert any("low-confidence exit suggestion" in m for m in sent), sent


def test_notify_refuses_an_empty_message(monkeypatch, tmp_path):
    """An empty message produced zero chunks, so the loop body never ran and the
    function returned as if it had delivered: no post, no file, no summary, no
    log. The docstring promises at-least-one durable write."""
    from bot.errors import NotificationError
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.invalid/hook")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(NotificationError):
        _real_send_notification("", None)


def test_notify_reports_which_channel_delivered(monkeypatch, tmp_path):
    """Returning None let the caller assume success, which is how a no-delivery
    day stayed green: run_report.py printed 'delivery verified' whenever it ran
    on CI, whether or not any channel had actually taken a copy."""
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.chdir(tmp_path)
    assert _real_send_notification("hello", None) == "file"
    summary = tmp_path / "sum.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert _real_send_notification("hello", None) == "file+summary"
    assert "hello" in summary.read_text()


def test_notify_masks_secrets_in_every_persisted_sink(monkeypatch, tmp_path):
    """The report body reaches a public CI log, a file and the step summary, and
    it is built from raw model output and raw exception strings."""
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    leaky = ("see https://gamma-api.polymarket.com/markets?api_key=TOPSECRET "
             "and nvapi-abcdef123 and ghp_AAAABBBBCCCC")
    _real_send_notification(leaky, None)
    written = list((tmp_path / "data" / "reports").glob("*.txt"))
    assert written, "no fallback file written"
    body = written[0].read_text()
    for secret in ("TOPSECRET", "nvapi-abcdef123", "ghp_AAAABBBBCCCC"):
        assert secret not in body, f"{secret} leaked into the report file"
    assert "api_key=[REDACTED]" in body


def test_report_pain_meter_flags_a_dead_workflow(monkeypatch):
    """Two independent ways a dead report went unnoticed for 31 days: its own
    grace_runs of 30 made the threshold 24h*31 against a row that can only be
    rendered BY the report running (age ~24h), and a workflow with zero runs
    matched none of the pain tokens so it read as healthy."""
    from bot import report
    assert report.WORKFLOW_SCHEDULES["report"][1] == 3
    interval, grace = report.WORKFLOW_SCHEDULES["report"]
    assert interval * (grace + 1) < 86400 * 7, "report stall threshold > 7 days"

    class _Resp:
        def __init__(self, runs):
            self._runs = runs

        def raise_for_status(self):
            pass

        def json(self):
            return {"workflow_runs": self._runs}

    monkeypatch.setattr(report, "requests",
                        type("R", (), {"get": staticmethod(lambda *a, **k: _Resp([]))}))
    out = report.actions_health()
    assert "NO RUNS FOUND" in out
    assert "INVESTIGATE" in out


def test_polymarket_paginates_past_the_first_hundred(monkeypatch):
    """Gamma hard-caps a page at 100 -- limit=200 and limit=1000 both return
    100 -- so one request capped the whole tier at the top 100 by 24h volume,
    and no config value could raise that."""
    from bot import polymarket

    seen_offsets = []

    class _Resp:
        def __init__(self, rows):
            self._rows = rows

        def raise_for_status(self):
            pass

        def json(self):
            return self._rows

    def _get(url, params=None, headers=None, timeout=None):
        seen_offsets.append((params or {}).get("offset"))
        off = (params or {}).get("offset") or 0
        return _Resp([{"id": off + i} for i in range(100)])

    monkeypatch.setattr(polymarket, "requests",
                        type("R", (), {"get": staticmethod(_get)}))
    rows = polymarket._fetch_markets(limit=100, pages=3)
    assert seen_offsets == [0, 100, 200], seen_offsets
    assert len(rows) == 300
    assert len({r["id"] for r in rows}) == 300, "pages overlapped"


def test_polymarket_pagination_stops_when_the_server_repeats(monkeypatch):
    """Defend against a server that ignores offset: without dedupe the tier
    would re-scan page 1 forever and count the same market 300 times."""
    from bot import polymarket

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return [{"id": i} for i in range(100)]

    calls = []

    def _get(url, params=None, headers=None, timeout=None):
        calls.append(1)
        return _Resp()

    monkeypatch.setattr(polymarket, "requests",
                        type("R", (), {"get": staticmethod(_get)}))
    rows = polymarket._fetch_markets(limit=100, pages=5)
    assert len(rows) == 100
    assert len(calls) == 2, calls


def test_research_uses_the_callers_journal(monkeypatch, tmp_path):
    """whale_activity and research_bundle reached for the module-global default
    DB, so a Tier 2 scout cycle run against a scratch journal still wrote its
    whale cache -- and burned Tavily budget counters -- into data/trades.db."""
    from bot import research
    j = TradeJournal(db_path=str(tmp_path / "r.db"))
    monkeypatch.setattr(research, "_get_journal",
                        lambda: TradeJournal(db_path=str(tmp_path / "prod.db")))
    monkeypatch.setattr(research, "tavily_search", lambda *a, **k: [])
    monkeypatch.setattr(research, "fetch_rss_headlines", lambda limit=20: [])
    monkeypatch.setattr(research, "market_stats", lambda s: {})
    monkeypatch.setattr(research, "trending_coins", lambda: [])
    monkeypatch.setattr(research, "headlines_for_symbol", lambda *a, **k: [])
    research.research_bundle(["BTC/USD"], None, journal=j)
    prod = TradeJournal(db_path=str(tmp_path / "prod.db"))
    assert not [k for k in _all_meta_keys(prod) if k.startswith("whale_cache")]


def test_tavily_requests_a_recency_window(monkeypatch, tmp_path):
    """Tavily's time_range has NO default and undated results are not filtered
    out, so omitting it searched ALL TIME -- broader than the "this week" the
    query strings only hint at in prose. `days` is not a Tavily parameter."""
    from bot import research
    j = TradeJournal(db_path=str(tmp_path / "r.db"))
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    captured = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"results": []}

    def _post(url, headers=None, json=None, timeout=None):
        captured.update(json or {})
        return _Resp()

    monkeypatch.setattr(research, "requests",
                        type("R", (), {"post": staticmethod(_post)}))
    research.tavily_search("btc news", None, journal=j)
    assert captured.get("time_range") == "week"
    assert captured.get("topic") == "news"
    assert "days" not in captured


# ================= commit 5: low =================


class _ShadowCfg:
    symbols = ["BTC/USD"]
    timeframe = "1Hour"
    lookback_bars = 60
    agent = {"shadow_start_cash": 80, "shadow_max_per_position": 40}


class _FormingBarBroker:
    """Bars whose last row is the still-forming candle.

    Binance and Alpaca both append the in-progress bar; the repo drops it
    everywhere else (agent._price_context, trader, futures, and binance_data's
    own last_close helper all read iloc[-2] or iloc[:-1]).
    """

    def __init__(self, closed=100.0, forming=180.0):
        self.closed = closed
        self.forming = forming

    def get_crypto_bars(self, symbol, timeframe, limit):
        idx = pd.date_range(end=datetime.now(timezone.utc), periods=4, freq="1h", tz="UTC")
        closes = [self.closed - 2, self.closed - 1, self.closed, self.forming]
        return pd.DataFrame({"open": closes, "high": closes, "low": closes,
                             "close": closes, "volume": [1.0] * 4}, index=idx)

    def last_close(self, symbol, interval="15m", limit=2):
        return self.closed


def _shadow(tmp_path, closed=100.0, forming=180.0):
    from bot.shadow import ShadowAccount
    j = TradeJournal(db_path=str(tmp_path / "shadow.db"))
    return ShadowAccount(_ShadowCfg, _FormingBarBroker(closed, forming), journal=j)


# ---------------- the shadow account must price off a closed bar ----------------

def test_shadow_marks_to_the_last_closed_bar(tmp_path):
    """The shadow account marked equity off the still-forming candle.

    _last_close read iloc[-1], so a virtual position, the reported shadow
    equity and the Tier 1 heartbeat money line all carried a price that could
    still move until the hour boundary. A forming close 80% above the last
    closed bar was booked as a real gain.
    """
    acc = _shadow(tmp_path, closed=100.0, forming=180.0)
    acc.journal.set_meta("shadow_cash", "0.0")
    acc.journal.set_meta("shadow_positions", '{"BTC/USD": {"qty": 0.5, "entry": 90.0, "opened": "x"}}')

    equity, cash, block = acc.mark_to_market()

    assert equity == pytest.approx(50.0, abs=1e-9)
    assert "$100.00" in block


def test_shadow_fills_at_the_last_closed_bar(tmp_path):
    """A virtual fill priced on the forming bar is not a price the market gave."""
    acc = _shadow(tmp_path, closed=100.0, forming=180.0)

    ok, _note = acc.take_buy("BTC/USD", 40)

    assert ok
    assert acc._positions()["BTC/USD"]["entry"] == pytest.approx(100.0, abs=1e-9)
    assert acc._cash() == pytest.approx(40.0, abs=1e-9)


# ---------------- corrupt cash/peak meta must not become buying power ----------------

class _CashCfg:
    """One config satisfying both ledgers, so _cash() is testable uniformly."""

    symbols = ["BTC/USD"]
    timeframe = "1Hour"
    lookback_bars = 60
    agent = _ShadowCfg.agent
    memecoin = _T4Cfg.memecoin


def _cash_ledgers(tmp_path):
    """(label, ledger, cash meta key) for every tier that keeps cash in meta."""
    from bot.memecoin import MemecoinLedger
    from bot.shadow import ShadowAccount
    j = TradeJournal(db_path=str(tmp_path / "cash.db"))
    return [
        ("shadow", ShadowAccount(_CashCfg, _FormingBarBroker(), journal=j), "shadow_cash"),
        ("tier4", MemecoinLedger(_CashCfg, journal=j), "t4_cash"),
    ]


def test_missing_cash_meta_is_a_fresh_ledger_not_a_corrupt_one(tmp_path):
    """No cash row yet means the ledger has never traded, not that cash is lost."""
    for label, led, _key in _cash_ledgers(tmp_path):
        assert led._cash() == pytest.approx(led.start_cash), label


@pytest.mark.parametrize("bad", ["", "None", "1,000", "nan", "inf", "-inf"])
def test_corrupt_cash_meta_is_refused_instead_of_becoming_buying_power(tmp_path, bad):
    """A cash key that reads back as text or NaN must fail loudly, not spend.

    float(v) raised a bare ValueError on unparseable text and returned NaN for
    "nan" -- and every sizing guard passes NaN, because `nan > cap` and
    `nan <= 0` are both False. A corrupt ledger therefore read as unlimited
    buying power; the unparseable case instead died somewhere far away with no
    mention of the key that was actually broken.
    """
    from bot.errors import JournalError
    for label, led, key in _cash_ledgers(tmp_path):
        led.journal.set_meta(key, bad)
        with pytest.raises(JournalError) as ei:
            led._cash()
        assert key in str(ei.value), label


# ---------------- timestamps must not be local time ----------------

def test_no_module_calls_a_naive_clock():
    """42 call sites stamped datetime.now() into logs, headers and filenames.

    The journal writes UTC everywhere and gates.py re-tagging a naive value as
    UTC is what hid this, so a CI log interleaving local and UTC stamps could
    not be lined up against the rows it described -- and on any non-UTC host
    the daily report's own header disagreed with every timestamp in its body.
    The repo has no linter, so this is the invariant test.
    """
    import ast
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    files = sorted(root.glob("bot/*.py")) + sorted(root.glob("*.py")) \
        + sorted(root.glob("tools/*.py"))
    assert files
    offenders = []
    for path in files:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr not in ("now", "utcnow"):
                continue
            if ast.unparse(func.value) == "datetime" and not node.args and not node.keywords:
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert not offenders, f"naive local clock: {offenders}"


def test_daily_report_header_stamp_is_utc(monkeypatch):
    """The report header was the one local-time stamp in a UTC document.

    On a UTC host the bug is invisible, so the clock is injected: a host three
    hours behind UTC must still produce a header that matches UTC.
    """
    import bot.report as report

    class _ThreeHoursBehind:
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return datetime(2026, 9, 27, 12, 0, 0)
            return datetime(2026, 9, 27, 15, 0, 0, tzinfo=timezone.utc)

    class _NoTrades:
        def get_trades(self, *_a, **_k):
            return []

    monkeypatch.setattr(report, "datetime", _ThreeHoursBehind)
    monkeypatch.setattr(report, "TradeJournal", lambda *a, **k: _NoTrades())
    monkeypatch.setattr(report, "shadow_snapshot", lambda: "shadow")

    out = report.create_daily_report()

    assert out.splitlines()[1].strip() == "2026-09-27 15:00:00"


# ---------------- docstrings that no longer describe the code ----------------

def test_shadow_module_docstring_names_the_table_it_actually_uses():
    """The docstring promised a `shadow_trades` table that does not exist.

    `shadow_trades` appeared exactly once in the repo: the comment itself.
    Shadow state lives in `trades` -- found back by the "[shadow-account]"
    reasoning prefix -- plus the two meta keys the comment did name.
    """
    import inspect

    import bot.shadow as shadow

    assert "shadow_trades" not in shadow.__doc__
    assert "[shadow-account]" in shadow.__doc__
    assert "shadow_cash" in shadow.__doc__ and "shadow_positions" in shadow.__doc__

    # the prefix is what the read side actually filters on
    assert '"[shadow-account]"' in inspect.getsource(shadow.ShadowAccount.realized_pnl)


def test_no_comment_still_claims_a_three_symbol_tier1_universe():
    """A hard-scope comment naming BTC/ETH/SOL sat over a ten-symbol universe.

    The scope is enforced from cfg.symbols, so the comment drifted silently
    every time the universe grew -- and it sat directly above the list a
    reader would trust.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    offenders = [f"{p.name}:{i}" for p in sorted(root.glob("bot/*.py"))
                 for i, line in enumerate(p.read_text().splitlines(), 1)
                 if "BTC/ETH/SOL" in line and not line.lstrip().startswith("#!")]
    assert not offenders, offenders


def _all_meta_keys(j):
    import sqlite3 as _s
    conn = _s.connect(j.db_path)
    try:
        return [r[0] for r in conn.execute("SELECT key FROM meta")]
    finally:
        conn.close()
