"""Tests for the 2026-09-08 P0b Tier 4 hotfixes (deep-dive findings F4/F5/F11).

Covers: kill auto-re-arm reachable through _auto_entries (was dead code),
rug-guard fail-closed on unknown pair age, strict LLM gate schema (string
"false"/"true" and bool-typed confidence rejected), manual buy position cap,
and batch card dedupe.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.journal import TradeJournal
from bot.memecoin import MemecoinLedger
from tests.test_tier4 import _T4Cfg, _ledger, _mk_pair


# ---------------- F4: kill auto-re-arm is reachable ----------------

def test_rearm_runs_before_kill_check_in_auto_entries(tmp_path):
    """A kill with an elapsed cooldown must re-arm (then allow entries) when
    the pipeline runs — previously _auto_entries returned at kill_active()
    before the re-arm call could ever execute."""
    led, j = _ledger(tmp_path / "a", prices={"DOGE": 0.1})
    led.buy("DOGE", 6)
    # simulate a kill that flattened everything, cooldown long elapsed
    j.set_meta("t4_kill", "on")
    j.set_meta("t4_kill_at", (datetime.now(timezone.utc) - timedelta(hours=48)
                              ).isoformat())
    led._save(40, {})
    events = led._auto_entries([])  # must run the re-arm, not return early
    assert j.get_meta("t4_kill") == "off"


def test_rearm_not_triggered_when_cooldown_active(tmp_path):
    led, j = _ledger(tmp_path)
    j.set_meta("t4_kill", "on")
    j.set_meta("t4_kill_at", (datetime.now(timezone.utc) - timedelta(hours=2)
                              ).isoformat())
    led._save(40, {})
    assert led._auto_rearm_after_cooldown() is False
    assert led.kill_active() is True


# ---------------- F5: rug-guard fail-closed ----------------

def _dossier(**over):
    base = {
        "coin_id": "doge", "mcap_rank": 50, "liquidity_usd": 500000,
        "volume_24h_usd": 600000, "pair_age_days": 30, "ath_distance_pct": -50,
        "categories": ["Meme"],
    }
    base.update(over)
    return base


def test_rug_guard_rejects_unknown_pair_age(tmp_path):
    led, _ = _ledger(tmp_path)
    ok, reasons = led._rug_guard(_dossier(pair_age_days=None))
    assert not ok
    assert any("age unknown" in r for r in reasons)


def test_rug_guard_accepts_valid_dossier(tmp_path):
    led, _ = _ledger(tmp_path)
    ok, reasons = led._rug_guard(_dossier())
    assert ok, reasons


# ---------------- F5: strict LLM gate schema ----------------

class _FakeModel:
    def __init__(self, payload):
        self.payload = payload

    def generate_json(self, prompt, max_tokens=300, temperature=0.2, system=None):
        return self.payload


def test_llm_gate_rejects_string_false(tmp_path):
    led, _ = _ledger(tmp_path)
    led.model = _FakeModel({"buy": "false", "confidence": 0.9, "reason": "x"})
    buy, conf, reason = led._llm_conviction(_dossier(), [], None)
    assert buy is False
    assert "not a boolean" in reason


def test_llm_gate_rejects_string_true(tmp_path):
    led, _ = _ledger(tmp_path)
    led.model = _FakeModel({"buy": "true", "confidence": 0.9, "reason": "x"})
    buy, conf, reason = led._llm_conviction(_dossier(), [], None)
    assert buy is False
    assert "not a boolean" in reason


def test_llm_gate_rejects_bool_confidence(tmp_path):
    led, _ = _ledger(tmp_path)
    led.model = _FakeModel({"buy": True, "confidence": True, "reason": "x"})
    buy, conf, reason = led._llm_conviction(_dossier(), [], None)
    assert buy is False
    assert "not numeric" in reason


def test_llm_gate_accepts_valid_booleans(tmp_path):
    led, _ = _ledger(tmp_path)
    led.model = _FakeModel({"buy": True, "confidence": 0.8, "reason": "ok"})
    buy, conf, reason = led._llm_conviction(_dossier(), [], None)
    assert buy is True and conf == 0.8


# ---------------- F11: manual buy cap + batch dedupe ----------------

def test_manual_buy_respects_max_open_positions(tmp_path):
    led, _ = _ledger(tmp_path, prices={"A": 0.1, "B": 0.1, "C": 0.1, "D": 0.1})
    assert led.buy("A", 6)[0]
    assert led.buy("B", 6)[0]
    assert led.buy("C", 6)[0]
    ok, msg = led.buy("D", 6)
    assert not ok and "max open positions" in msg


def test_batch_card_dedupe(tmp_path):
    led, j = _ledger(tmp_path)
    card = {"kind": "coingecko_trending", "symbol": "dogwifhat",
            "name": "dogwifhat", "detail": "trending #3"}
    fresh = led._dedupe_and_log([card, dict(card), dict(card)])
    assert len(fresh) == 1
    assert len(j.get_tier4_cards()) == 1


# ---------------- fee journaling (ledger/audit reconciliation) ----------------

def test_buy_journals_actual_fee(tmp_path):
    led, j = _ledger(tmp_path, prices={"A": 0.1})
    ok, msg = led.buy("A", 9)
    assert ok
    trades = j.get_trades()
    assert len(trades) == 1
    # get_trades returns DESC (newest first): row = (id, ts, sym, action,
    # qty, price, reasoning, fee, ...)
    assert trades[0][7] == pytest.approx(0.09, abs=1e-6)
    # fee model matches: cash left = 40 - 9 (fee embedded in qty, see _buy)
    assert led._cash() == pytest.approx(31.0, abs=1e-9)


def test_sell_journals_actual_fee(tmp_path):
    led, j = _ledger(tmp_path, prices={"A": 0.1})
    assert led.buy("A", 9)[0]
    r = led._sell_position("A", note="test exit")
    assert r is not None
    rows = j.get_trades()
    sell_row = next(t for t in rows if t[3] == "SELL")
    # SELL fee = proceeds * 1% journaled on the row
    sell_fee = sell_row[7]
    assert sell_fee > 0
    assert sell_fee == pytest.approx(sell_row[4] * sell_row[5] * 0.01, rel=1e-6)
