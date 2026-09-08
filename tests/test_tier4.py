import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.journal import TradeJournal
from bot.memecoin import MemecoinLedger, TIER4_TAG


# ---------------- fixtures ----------------

class _T4Cfg:
    memecoin = {
        "start_cash": 40, "max_stake": 12, "stop_loss_pct": 25,
        "take_profit_pct": 50, "time_stop_hours": 72, "max_drawdown_pct": 25,
        "taker_fee_pct": 1.0, "slippage_bps": 100, "card_ttl_minutes": 360,
        "max_trending_cards": 7, "spike_volume_multiple": 3.0,
        "spike_min_volume_24h": 100000,
    }


def _ledger(tmp_path, prices=None):
    j = TradeJournal(db_path=str(tmp_path / "t4.db"))
    led = MemecoinLedger(_T4Cfg, journal=j)
    if prices:
        led.price_for = lambda s: prices.get(str(s).upper())
    return led, j


def _mk_pair(base="DOGE", price=0.1, vol24=400000, vol6=500000, liq=50000):
    return {
        "baseToken": {"symbol": base, "name": base + " coin"},
        "volume": {"h24": vol24, "h6": vol6},
        "liquidity": {"usd": liq},
        "priceUsd": price,
    }


# ---------------- journal tier4_cards ----------------

def test_journal_tier4_card_lifecycle(tmp_path):
    _, j = _ledger(tmp_path)
    j.log_tier4_card("2026-09-07T10:00:00", "coingecko_trending", "dogwifhat",
                     "dogwifhat", "trending #3")
    cards = j.get_tier4_cards()
    assert len(cards) == 1 and cards[0][2] == "coingecko_trending"
    assert j.has_tier4_card("coingecko_trending", "dogwifhat")
    assert not j.has_tier4_card("dexscreener_spike", "dogwifhat")
    j.expire_tier4_card(cards[0][0])
    assert j.get_tier4_cards() == []
    # expired rows are preserved as history
    assert len(j.get_tier4_cards(status="expired")) == 1
    assert len(j.get_tier4_cards(status=None)) == 1


# ---------------- entry-fixed exit params ----------------

def test_ledger_refuses_unconfigured_exit_params(tmp_path):
    class _BrokenCfg:
        memecoin = {"start_cash": 40, "max_stake": 12, "stop_loss_pct": 0,
                    "take_profit_pct": 50, "time_stop_hours": 72,
                    "max_drawdown_pct": 25}
    j = TradeJournal(db_path=str(tmp_path / "t4.db"))
    with pytest.raises(ValueError):
        MemecoinLedger(_BrokenCfg, journal=j)


def test_entry_records_fixed_sl_tp_and_time_stop(tmp_path):
    led, _ = _ledger(tmp_path, prices={"DOGE": 0.10})
    ok, note = led.buy("DOGE")
    assert ok
    pos = led._positions()["DOGE"]
    assert pos["stop"] == pytest.approx(0.10 * (1 + 0.01) * 0.75, rel=1e-3)  # entry - 25%
    assert pos["take_profit"] == pytest.approx(0.10 * (1 + 0.01) * 1.5, rel=1e-3)  # entry + 50%
    assert pos["opened"]  # time-stop anchor
    # SL/TP are fixed at entry: a later price move never drifts them
    led.price_for = lambda s: 0.5
    pos2 = led._positions()["DOGE"]
    assert pos2["stop"] == pos["stop"] and pos2["take_profit"] == pos["take_profit"]


def test_buy_blocked_when_kill_active(tmp_path):
    led, j = _ledger(tmp_path, prices={"DOGE": 0.10})
    j.set_meta("t4_kill", "on")
    ok, note = led.buy("DOGE")
    assert not ok and "kill" in note.lower()
    assert led._positions() == {}


def test_buy_rejects_duplicate_symbol_and_no_price(tmp_path):
    led, _ = _ledger(tmp_path, prices={"DOGE": 0.10})
    ok, _ = led.buy("DOGE")
    assert ok
    ok, note = led.buy("DOGE")
    assert not ok and "already holding" in note
    ok, note = led.buy("PEPE")
    assert not ok and "no price" in note


def test_buy_stake_capped_and_cash_bound(tmp_path):
    led, _ = _ledger(tmp_path, prices={"DOGE": 0.10, "SHIB": 0.00001})
    ok, note = led.buy("DOGE", stake=100)  # request $100 -> capped at $12
    assert ok
    cash = led._cash()
    assert cash == pytest.approx(40 - 12)
    # a second entry can only deploy remaining cash
    ok2, note2 = led.buy("SHIB", stake=50)
    assert ok2
    assert led._cash() == pytest.approx(28 - 12)  # capped at max_stake again


# ---------------- exit sweep ----------------

def test_sweep_enforces_stop_loss(tmp_path):
    led, _ = _ledger(tmp_path, prices={"DOGE": 0.10})
    led.buy("DOGE")
    led.price_for = lambda s: 0.05  # 50% below entry -> breaches 25% SL
    events = led.sweep()
    assert [e["type"] for e in events] == ["stop_loss"]
    assert led._positions() == {}
    assert len([t for t in led.journal.get_trades() if t[3] == "SELL"]) == 1


def test_sweep_enforces_take_profit(tmp_path):
    led, _ = _ledger(tmp_path, prices={"DOGE": 0.10})
    led.buy("DOGE")
    led.price_for = lambda s: 0.20  # +100% -> breaches 50% TP
    events = led.sweep()
    assert [e["type"] for e in events] == ["take_profit"]
    assert led._positions() == {}


def test_sweep_enforces_time_stop(tmp_path):
    led, _ = _ledger(tmp_path, prices={"DOGE": 0.10})
    led.buy("DOGE")
    # age the position past the 72h time stop
    positions = led._positions()
    positions["DOGE"]["opened"] = (datetime.now(timezone.utc)
                                   - timedelta(hours=73)).isoformat()
    led.journal.set_meta("t4_positions", __import__("json").dumps(positions))
    events = led.sweep()
    assert [e["type"] for e in events] == ["time_stop"]
    assert led._positions() == {}


def test_sweep_hold_inside_exit_band(tmp_path):
    led, _ = _ledger(tmp_path, prices={"DOGE": 0.10})
    led.buy("DOGE")
    led.price_for = lambda s: 0.12  # +20%: inside the 25% SL / 50% TP band
    events = led.sweep()
    assert events == []
    assert "DOGE" in led._positions()


# ---------------- kill threshold ----------------

def test_drawdown_kill_flattens_and_blocks(tmp_path):
    led, _ = _ledger(tmp_path, prices={"DOGE": 0.10})
    led.buy("DOGE")
    led.price_for = lambda s: 0.08  # mark down: cash 28 + position 9.4 < 25% peak? no
    # force the peak high so the mark-down breaches 25% drawdown
    led.journal.set_meta("t4_peak_equity", "40")
    # equity = 28 + 12*0.75 = 37 -> 7.5% drawdown, not enough; mark further
    led.price_for = lambda s: 0.02  # equity ~ 28 + 2.97 = ~30.97... still not 25%
    # direct structural test: peak 40, equity must fall below 30
    led.price_for = lambda s: 0.01  # position ~1.48 -> equity ~29.5 < 30
    events = led.sweep()
    assert [e["type"] for e in events] == ["kill"]
    assert led._positions() == {}  # flattened
    assert led.kill_active()
    assert led.kill_count() == 1
    # entries blocked after kill
    ok, note = led.buy("SHIB", stake=5)
    assert not ok and "kill" in note.lower()
    # kill count survives a manual reset
    led.reset_kill()
    assert not led.kill_active()
    assert led.kill_count() == 1  # history preserved


def test_reset_kill_rearms_peak(tmp_path):
    led, _ = _ledger(tmp_path, prices={"DOGE": 0.10})
    led.journal.set_meta("t4_kill", "on")
    v_before = led.valuation()
    ok, note = led.reset_kill()
    assert ok
    assert float(led.journal.get_meta("t4_peak_equity")) == pytest.approx(v_before["equity"])
    # with no kill active, reset is a no-op error
    ok2, note2 = led.reset_kill()
    assert not ok2 and "no kill" in note2


# ---------------- isolation ----------------

def test_tier4_trades_excluded_from_tier1_pnl(tmp_path):
    from bot.report import compute_pnl_and_winrate
    led, _ = _ledger(tmp_path, prices={"DOGE": 0.10})
    led.buy("DOGE")
    led.price_for = lambda s: 0.20
    led.sweep()
    trades = [
        (1, "2026-09-07T10:00", "BTC/USD", "BUY", 0.01, 100.0, "r", 0.0, "o1", "filled"),
        (2, "2026-09-07T10:01", "DOGE", "BUY", 100, 0.101, TIER4_TAG + " entry", 0.0, None, "filled"),
        (3, "2026-09-07T11:00", "DOGE", "SELL", 100, 0.198, TIER4_TAG + " take profit (pnl +9.6)", 0.0, None, "filled"),
        (4, "2026-09-07T12:00", "BTC/USD", "SELL", 0.01, 110.0, "r", 0.0, "o2", "filled"),
    ]
    stats = compute_pnl_and_winrate(trades)
    assert stats["round_trips"] == 1
    assert stats["total_pnl"] == pytest.approx(0.10)


def test_tier4_trades_tagged_in_journal(tmp_path):
    led, j = _ledger(tmp_path, prices={"DOGE": 0.10})
    led.buy("DOGE")
    led.price_for = lambda s: 0.05
    led.sweep()
    t4 = [t for t in j.get_trades() if TIER4_TAG in (t[6] or "")]
    assert len(t4) == 2  # BUY + SELL both tagged
    assert all(t[2] == "DOGE" for t in t4)


# ---------------- signal filters ----------------

def test_dexscreener_spike_filter(tmp_path, monkeypatch):
    led, _ = _ledger(tmp_path)
    pairs = [
        _mk_pair("MOON", vol24=500000, vol6=800000),   # 6.4x of 24h -> spike
        _mk_pair("LOWV", vol24=1000, vol6=900),        # below min volume -> out
        _mk_pair("FLAT", vol24=500000, vol6=50000),    # 0.4x multiple -> out
    ]

    class _Resp:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return {"pairs": pairs}

    monkeypatch.setattr("bot.memecoin.requests.get", lambda *a, **k: _Resp())
    cards = led.dexscreener_spike_cards()
    assert [c["symbol"] for c in cards] == ["MOON"]
    assert "multiple" in cards[0]["detail"]


def test_trending_cards_logged_and_deduped(tmp_path, monkeypatch):
    led, j = _ledger(tmp_path)

    class _Resp:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return {"coins": [
                {"item": {"name": "dogwifhat", "id": "dogwifhat", "market_cap_rank": 42}},
                {"item": {"name": "Pepe", "id": "pepe", "market_cap_rank": 51}},
            ]}

    monkeypatch.setattr("bot.memecoin.requests.get", lambda *a, **k: _Resp())
    cards = led.coingecko_trending_cards()
    assert len(cards) == 2
    # immediate re-scan within TTL: fully deduped (no new rows), originals stay fresh
    cards2 = led.coingecko_trending_cards()
    assert cards2 == []
    fresh = j.get_tier4_cards(status="fresh")
    assert len(fresh) == 2
    # after TTL the card is eligible again (fresh row re-logged)
    assert led.card_ttl_minutes > 0


def test_card_dedupe_expires_after_ttl(tmp_path, monkeypatch):
    led, j = _ledger(tmp_path)

    class _Resp:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return {"coins": [{"item": {"name": "Pepe", "id": "pepe",
                                        "market_cap_rank": 51}}]}

    monkeypatch.setattr("bot.memecoin.requests.get", lambda *a, **k: _Resp())
    assert len(led.coingecko_trending_cards()) == 1
    # age the logged card beyond the TTL
    stale = (datetime.now(timezone.utc) - timedelta(minutes=led.card_ttl_minutes + 1)).isoformat()
    j.set_meta("noop", "0")  # touch meta to prove journal writes work
    conn_cards = j.get_tier4_cards(status=None)
    import sqlite3
    with sqlite3.connect(j.db_path) as conn:
        conn.execute("UPDATE tier4_cards SET timestamp=? WHERE symbol='pepe'", (stale,))
        conn.commit()
    # now the same card is re-logged as a fresh row
    assert len(led.coingecko_trending_cards()) == 1
    assert len(j.get_tier4_cards(status="fresh")) == 1


def test_card_dedupe_uses_newest_row(tmp_path, monkeypatch):
    """TTL must be measured from the NEWEST card per (kind, symbol).

    Regression: rows come newest-first; the old code overwrote 'seen' so the
    OLDEST row won, letting a symbol re-log duplicates once its oldest card
    aged out even though a newer card was still fresh."""
    led, j = _ledger(tmp_path)

    class _Resp:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return {"coins": [{"item": {"name": "Pepe", "id": "pepe",
                                        "market_cap_rank": 51}}]}

    monkeypatch.setattr("bot.memecoin.requests.get", lambda *a, **k: _Resp())
    assert len(led.coingecko_trending_cards()) == 1
    # age the FIRST logged card past TTL while keeping it fresh-eligible,
    # then simulate a second log: the newest row's timestamp is what matters
    import sqlite3
    ttl = led.card_ttl_minutes
    stale = (datetime.now(timezone.utc) - timedelta(minutes=ttl + 1)).isoformat()
    recent = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(j.db_path) as conn:
        # two rows for pepe: an old one (beyond TTL) and a recent one (fresh)
        conn.execute(
            "INSERT INTO tier4_cards (timestamp, kind, symbol, name, detail, status) "
            "VALUES (?, 'coingecko_trending', 'pepe', 'Pepe', 'd', 'fresh')",
            (stale,))
        conn.execute(
            "INSERT INTO tier4_cards (timestamp, kind, symbol, name, detail, status) "
            "VALUES (?, 'coingecko_trending', 'pepe', 'Pepe', 'd', 'fresh')",
            (recent,))
        conn.commit()
    # the recent row is inside the TTL window -> NO new card may be logged
    assert led.coingecko_trending_cards() == []
    assert len(j.get_tier4_cards(status="fresh")) == 3  # original + 2 seeded


# ---------------- status ----------------

def test_status_line_shape(tmp_path):
    led, _ = _ledger(tmp_path, prices={"DOGE": 0.10})
    line = led.status_line()
    assert "Tier 4 canary: $40.00 (+0.00% of $40 start)" in line
    assert "flat" in line and "ACTIVE" in line and "kills 0" in line
    led.journal.set_meta("t4_kill", "on")
    assert "KILLED" in led.status_line()


def test_sell_realizes_pnl(tmp_path):
    led, _ = _ledger(tmp_path, prices={"DOGE": 0.10})
    led.buy("DOGE")
    led.price_for = lambda s: 0.15
    ok, note = led.sell("DOGE")
    assert ok
    assert led._positions() == {}
    # entry ~0.101 (slippage), exit ~0.1485 (slippage), minus fees both sides
    cash = led._cash()
    assert cash > 40 + 12 * 0.4  # well above break-even on a +50% move
