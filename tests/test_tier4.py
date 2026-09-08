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
        # automation settings
        "auto_entry": True, "auto_cooldown_hours": 24, "min_llm_confidence": 0.75,
        "base_stake": 6, "max_open_positions": 3, "min_liquidity_usd": 250000,
        "min_volume_24h_usd": 500000, "min_age_hours": 168, "max_mcap_rank": 300,
        "trailing_stop_pct": 20, "trailing_activate_pct": 25,
        "partial_tp_pct": 80, "partial_tp_sell_frac": 0.5,
        "entry_cooldown_hours": 168,
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
    # +60%: above the +50% entry-fixed TP, below the +80% partial trigger
    led.price_for = lambda s: 0.165
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


# ---------------- automation: rug-guard screen ----------------

def _dossier(**over):
    base = {
        "coin_id": "dogwifhat", "symbol": "WIF", "mcap_rank": 42,
        "liquidity_usd": 1_000_000, "volume_24h_usd": 2_500_000,
        "pair_age_days": 400, "price_usd": 3.0,
    }
    base.update(over)
    return base


def test_rug_guard_passes_healthy_coin(tmp_path):
    led, _ = _ledger(tmp_path)
    ok, reasons = led._rug_guard(_dossier())
    assert ok and reasons == []


def test_rug_guard_blocks_rug_signals(tmp_path):
    led, _ = _ledger(tmp_path)
    # each classic rug signal independently blocks
    for over, expect in [
        ({"mcap_rank": 999}, "rank"),
        ({"liquidity_usd": 1_000}, "liquidity"),
        ({"volume_24h_usd": 1_000}, "volume"),
        ({"pair_age_days": 0.5}, "age"),
        ({"coin_id": None}, "id"),
    ]:
        ok, reasons = led._rug_guard(_dossier(**over))
        assert not ok, f"rug guard should block: {over}"
        assert all(expect not in r.lower() for r in reasons) is False  # reason mentions the failing check


def test_rug_guard_reasons_are_specific(tmp_path):
    led, _ = _ledger(tmp_path)
    ok, reasons = led._rug_guard(_dossier(liquidity_usd=10))
    assert not ok
    assert any("liquidity" in r.lower() for r in reasons)


# ---------------- automation: LLM conviction gate ----------------

class _FakeModel:
    def __init__(self, decision):
        self.decision = decision

    def generate_json(self, prompt, max_tokens=800, temperature=0.2):
        return self.decision


def test_llm_gate_buy_at_high_confidence(tmp_path):
    led, _ = _ledger(tmp_path)
    led.model = _FakeModel({"buy": True, "confidence": 0.9, "reason": "strong momentum, deep liquidity"})
    buy, conf, reason = led._llm_conviction(_dossier(), None, None)
    assert buy and conf == 0.9


def test_llm_gate_rejects_low_confidence(tmp_path):
    led, _ = _ledger(tmp_path)
    led.model = _FakeModel({"buy": True, "confidence": 0.6, "reason": "ok but unsure"})
    buy, conf, reason = led._llm_conviction(_dossier(), None, None)
    assert not buy and "threshold" in reason


def test_llm_gate_rejects_llm_no_buy(tmp_path):
    led, _ = _ledger(tmp_path)
    led.model = _FakeModel({"buy": False, "confidence": 0.9, "reason": "exhausted move"})
    buy, conf, reason = led._llm_conviction(_dossier(), None, None)
    assert not buy and "declined" in reason


def test_llm_gate_fails_closed_without_model(tmp_path, monkeypatch):
    led, _ = _ledger(tmp_path)
    def boom():
        raise RuntimeError("no key")
    monkeypatch.setattr("bot.models.ModelManager", lambda **kw: boom())
    buy, conf, reason = led._llm_conviction(_dossier(), None, None)
    assert not buy and "unavailable" in reason


# ---------------- automation: conviction sizing ----------------

def test_stake_scales_with_conviction(tmp_path):
    led, _ = _ledger(tmp_path)
    at_threshold = led._stake_for(led.min_llm_confidence)
    at_max = led._stake_for(1.0)
    assert at_threshold == pytest.approx(led.base_stake)
    assert at_max == pytest.approx(led.max_stake)
    mid = led._stake_for((led.min_llm_confidence + 0.95) / 2)
    assert led.base_stake < mid < led.max_stake
    # below threshold -> still base (gate would have rejected already)
    assert led._stake_for(0.2) == pytest.approx(led.base_stake)


# ---------------- automation: entry cooldown ----------------

def test_exit_puts_symbol_in_cooldown(tmp_path):
    led, _ = _ledger(tmp_path, prices={"DOGE": 0.10})
    led.buy("DOGE")
    led.price_for = lambda s: 0.05
    led.sweep()  # stop loss exit
    assert led._in_cooldown("DOGE")
    # a card for DOGE within cooldown never reaches the LLM
    events = led._auto_entries([{"kind": "coingecko_trending",
                                 "symbol": "DOGE", "name": "Dogecoin",
                                 "detail": "trending"}])
    assert events == []
    proposals = led.journal.get_proposals(kind="auto_entry")
    assert proposals == []  # filtered before even logging a proposal


def test_cooldown_expires_after_window(tmp_path):
    led, _ = _ledger(tmp_path)
    import json as _json
    stale = (datetime.now(timezone.utc) - timedelta(hours=led.entry_cooldown_hours + 1)).isoformat()
    led.journal.set_meta("t4_entry_cooldowns",
                         _json.dumps({"DOGE": [stale, led.entry_cooldown_hours]}))
    assert not led._in_cooldown("DOGE")


# ---------------- automation: auto entry pipeline ----------------

def _patch_pipeline(monkeypatch, dossier=None, history=None, pair=None):
    d = dossier if dossier is not None else _dossier()
    monkeypatch.setattr(MemecoinLedger, "_coingecko_dossier",
                        lambda self, cid: d)
    monkeypatch.setattr(MemecoinLedger, "_coingecko_history",
                        lambda self, cid, days=30: history)
    monkeypatch.setattr(MemecoinLedger, "_dexscreener_pair",
                        lambda self, sym: pair)


def test_auto_entry_executes_when_all_gates_pass(tmp_path, monkeypatch):
    led, j = _ledger(tmp_path, prices={"DOGWIFHAT": 3.0, "WIF": 3.0})
    led.model = _FakeModel({"buy": True, "confidence": 0.85, "reason": "momentum + liquidity"})
    pair = {"liquidity_usd": 2_000_000, "pair_age_days": 400, "volume_24h_usd": 5_000_000,
            "dex": "raydium"}
    _patch_pipeline(monkeypatch, pair=pair)
    cards = [{"kind": "coingecko_trending", "symbol": "dogwifhat",
              "name": "dogwifhat", "detail": "trending #3"}]
    events = led._auto_entries(cards)
    assert [e["type"] for e in events] == ["auto_buy"]
    pos = led._positions()
    # entry keyed by the card's coin_id (uppercased by buy())
    assert "DOGWIFHAT" in pos
    # stake is conviction-sized: conf 0.85 -> between base and max
    assert led.base_stake < 40 - led._cash() <= led.max_stake
    # proposal journaled as executed
    props = j.get_proposals(kind="auto_entry")
    assert len(props) == 1 and props[0][9] == "executed"
    # the position carries the full exit kit
    p = pos["DOGWIFHAT"]
    assert p["stop"] and p["take_profit"] and p["opened"]
    assert p.get("trailing_stop") is None  # armed only by sweep


def test_auto_entry_skipped_when_kill_active(tmp_path, monkeypatch):
    led, j = _ledger(tmp_path)
    led.model = _FakeModel({"buy": True, "confidence": 0.9, "reason": "x"})
    _patch_pipeline(monkeypatch)
    led.journal.set_meta("t4_kill", "on")
    events = led._auto_entries([{"kind": "coingecko_trending", "symbol": "dogwifhat",
                                 "name": "dogwifhat", "detail": "t"}])
    assert events == []
    assert led._positions() == {}


def test_auto_entry_rug_guard_rejection_is_journaled(tmp_path, monkeypatch):
    led, j = _ledger(tmp_path)
    led.model = _FakeModel({"buy": True, "confidence": 0.9, "reason": "x"})
    _patch_pipeline(monkeypatch, dossier=_dossier(liquidity_usd=5_000,
                                                  volume_24h_usd=10_000),
                    pair=None)
    events = led._auto_entries([{"kind": "coingecko_trending", "symbol": "dogwifhat",
                                 "name": "dogwifhat", "detail": "t"}])
    assert events == []
    props = j.get_proposals(kind="auto_entry")
    assert len(props) == 1 and props[0][9] == "rejected"
    reasons = props[0][8].lower()
    assert "liquidity" in reasons and "volume" in reasons


def test_auto_entry_llm_rejection_is_journaled_as_shadow(tmp_path, monkeypatch):
    led, j = _ledger(tmp_path)
    led.model = _FakeModel({"buy": True, "confidence": 0.5, "reason": "meh"})
    _patch_pipeline(monkeypatch)
    events = led._auto_entries([{"kind": "coingecko_trending", "symbol": "dogwifhat",
                                 "name": "dogwifhat", "detail": "t"}])
    assert events == []
    props = j.get_proposals(kind="auto_entry")
    assert len(props) == 1 and props[0][9] == "shadow"
    assert led._positions() == {}


def test_auto_entry_respects_max_open_positions(tmp_path, monkeypatch):
    led, j = _ledger(tmp_path,
                     prices={"A": 0.10, "B": 0.10, "C": 0.10, "WIF": 3.0})
    led.model = _FakeModel({"buy": True, "confidence": 0.9, "reason": "x"})
    _patch_pipeline(monkeypatch)
    for sym in ("A", "B", "C"):
        ok, _ = led.buy(sym, stake=6)
        assert ok
    assert len(led._positions()) == 3
    events = led._auto_entries([{"kind": "coingecko_trending", "symbol": "dogwifhat",
                                 "name": "dogwifhat", "detail": "t"}])
    assert events == []


def test_auto_entry_off_switch(tmp_path, monkeypatch):
    class _OffCfg:
        memecoin = {**_T4Cfg.memecoin, "auto_entry": False}
    j = TradeJournal(db_path=str(tmp_path / "off.db"))
    led = MemecoinLedger(_OffCfg, journal=j)
    led.model = _FakeModel({"buy": True, "confidence": 0.9, "reason": "x"})
    _patch_pipeline(monkeypatch)
    events = led._auto_entries([{"kind": "coingecko_trending", "symbol": "dogwifhat",
                                 "name": "dogwifhat", "detail": "t"}])
    assert events == []
    assert led._positions() == {}


def test_one_entry_per_cycle(tmp_path, monkeypatch):
    led, j = _ledger(tmp_path, prices={"WIF": 3.0, "PEPE": 0.00001})
    led.model = _FakeModel({"buy": True, "confidence": 0.85, "reason": "x"})
    _patch_pipeline(monkeypatch)
    cards = [
        {"kind": "coingecko_trending", "symbol": "dogwifhat", "name": "wif", "detail": "t"},
        {"kind": "coingecko_trending", "symbol": "pepe", "name": "pepe", "detail": "t"},
    ]
    events = led._auto_entries(cards)
    assert len(events) == 1
    assert len(led._positions()) == 1


# ---------------- automation: kill auto-rearm ----------------

def test_kill_auto_rearms_after_cooldown(tmp_path):
    led, j = _ledger(tmp_path, prices={"DOGE": 0.10})
    led.buy("DOGE")
    led.journal.set_meta("t4_peak_equity", "40")
    led.price_for = lambda s: 0.01
    events = led.sweep()
    assert events[0]["type"] == "kill" and led.kill_active()
    # fresh kill: cooldown not elapsed -> no re-arm
    assert not led._auto_rearm_after_cooldown()
    # age the kill past the auto cooldown -> re-arms
    stale = (datetime.now(timezone.utc)
             - timedelta(hours=led.auto_cooldown_hours + 1)).isoformat()
    j.set_meta("t4_kill_at", stale)
    assert led._auto_rearm_after_cooldown()
    assert not led.kill_active()
    # peak re-armed to current equity
    assert float(j.get_meta("t4_peak_equity")) == pytest.approx(led.valuation()["equity"])


def test_auto_rearm_lets_entries_resume(tmp_path):
    led, j = _ledger(tmp_path, prices={"DOGE": 0.10})
    led.journal.set_meta("t4_kill", "on")
    stale = (datetime.now(timezone.utc)
             - timedelta(hours=led.auto_cooldown_hours + 1)).isoformat()
    j.set_meta("t4_kill_at", stale)
    ok, note = led.buy("DOGE")
    # direct buy() checks kill only; _auto_entries path handles re-arm —
    # simulate the cycle: re-arm then buy works
    led._auto_rearm_after_cooldown()
    ok, note = led.buy("DOGE")
    assert ok


# ---------------- automation: trailing stop ----------------

def test_trailing_stop_arms_and_exits(tmp_path):
    led, _ = _ledger(tmp_path, prices={"DOGE": 0.10})
    led.buy("DOGE")
    # +30% -> arms the trail at 0.13 * 0.8 = 0.104
    led.price_for = lambda s: 0.13
    events = led.sweep()
    assert events == []  # armed but not breached
    pos = led._positions()["DOGE"]
    assert pos["trailing_stop"] == pytest.approx(0.13 * 0.8, rel=1e-6)
    # pull back to below the trail -> exits
    led.price_for = lambda s: 0.103
    events = led.sweep()
    assert [e["type"] for e in events] == ["trailing_stop"]
    assert led._positions() == {}


def test_trailing_stop_ratchets_up_only(tmp_path):
    led, _ = _ledger(tmp_path, prices={"DOGE": 0.10})
    led.buy("DOGE")
    # runner mode: bank the partial at +88% first (entry-fixed TP is then
    # disabled for this position; the trail alone governs the runner)
    led.price_for = lambda s: 0.20
    led.sweep()
    assert led._positions()["DOGE"].get("partial_taken")
    led.price_for = lambda s: 0.25  # trail armed at 0.20
    led.sweep()
    pos = led._positions()["DOGE"]
    first_ts = pos["trailing_stop"]
    # price dips but stays above the trail: trail must NOT move down
    led.price_for = lambda s: 0.24
    led.sweep()
    pos = led._positions()["DOGE"]
    assert pos["trailing_stop"] == first_ts
    # price rips higher: trail ratchets up
    led.price_for = lambda s: 0.30
    led.sweep()
    pos = led._positions()["DOGE"]
    assert pos["trailing_stop"] > first_ts


def test_trailing_stop_not_armed_below_activation(tmp_path):
    led, _ = _ledger(tmp_path, prices={"DOGE": 0.10})
    led.buy("DOGE")
    led.price_for = lambda s: 0.12  # +~19% < +25% activation
    led.sweep()
    pos = led._positions()["DOGE"]
    assert pos["trailing_stop"] is None
    # a dip to entry does NOT exit (no trail armed, SL not breached)
    led.price_for = lambda s: 0.101
    assert led.sweep() == []


# ---------------- automation: partial take-profit ----------------

def test_partial_tp_sells_half_at_spike(tmp_path):
    led, _ = _ledger(tmp_path, prices={"DOGE": 0.10})
    led.buy("DOGE")
    qty_before = float(led._positions()["DOGE"]["qty"])
    led.price_for = lambda s: 0.19  # +~88% >= +80% partial trigger
    events = led.sweep()
    assert [e["type"] for e in events] == ["partial_tp"]
    pos = led._positions()["DOGE"]
    assert pos["partial_taken"] is True
    assert pos["qty"] == pytest.approx(qty_before * 0.5, rel=1e-3)
    # the runner half keeps riding (position alive)
    assert "DOGE" in led._positions()


def test_partial_tp_fires_once_only(tmp_path):
    led, _ = _ledger(tmp_path, prices={"DOGE": 0.10})
    led.buy("DOGE")
    led.price_for = lambda s: 0.19
    led.sweep()  # partial fires
    events = led.sweep()  # still +88%: no second partial
    assert events == []
    assert led._positions()["DOGE"]["partial_taken"] is True


def test_full_tp_exits_remaining_runner(tmp_path):
    led, _ = _ledger(tmp_path, prices={"DOGE": 0.10})
    led.buy("DOGE")
    led.price_for = lambda s: 0.19
    led.sweep()  # partial: half out at +88%
    # the runner is NOT exited by the entry-fixed TP anymore (trail governs)
    led.price_for = lambda s: 0.16  # +~58% > +50% TP
    assert led.sweep() == []
    assert "DOGE" in led._positions()
    # a pullback below the ratcheted trail exits the runner
    led.price_for = lambda s: 0.10
    events = led.sweep()
    assert [e["type"] for e in events] == ["trailing_stop"]
    assert led._positions() == {}
