"""Tier 4 memecoin canary: research signals feed human-reviewed decisions only.

Canary scope (per OPPORTUNITY_LAB.md): memecoins are canary research only.
This module NEVER autonomously executes anything. Its job is:

  1. research: CoinGecko trending + DexScreener volume-spike cards —
     deterministic, keyless APIs, no LLM anywhere in the Tier 4 path
  2. ledger: a virtual $40 canary ledger (memecoin.start_cash); entries
     happen ONLY via the human-gated CLI (tools/tier4.py buy). Every
     entry gets an entry-fixed stop-loss, take-profit and 72h time stop
  3. sweep: an hourly deterministic check enforcing those exits, plus a
     hard max-drawdown kill that flattens everything and blocks new
     entries until a manual reset

All state is isolated: virtual fills are logged to the trades table with
a `[tier4-memecoin]` reasoning tag (excluded from Tier 1 P&L) and research
cards go to the tier4_cards table. Prices come from the same keyless
Binance public data when the symbol exists there, else CoinGecko simple
price; both degrade gracefully.
"""
import json
from datetime import datetime, timedelta, timezone

import requests

from bot.journal import TradeJournal

HEADERS = {"User-Agent": "Mozilla/5.0 (trading-sandbox-agent tier4 canary)"}
COINGECKO_TRENDING_URL = "https://api.coingecko.com/api/v3/search/trending"
COINGECKO_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"
DEXSCREENER_URL = "https://api.dexscreener.com/latest/dex/search"

TIER4_TAG = "[tier4-memecoin]"
KILL_META = "t4_kill"
KILL_COUNT_META = "t4_kill_count"


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(ts):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


class MemecoinLedger:
    """Virtual $40 canary ledger. Human-gated entries, deterministic exits."""

    def __init__(self, cfg, journal=None):
        self.cfg = cfg
        self.journal = journal or TradeJournal()
        m = getattr(cfg, "memecoin", None) or {}
        self.start_cash = float(m.get("start_cash", 40))
        self.max_stake = float(m.get("max_stake", 12))
        self.stop_loss_pct = float(m.get("stop_loss_pct", 25))
        self.take_profit_pct = float(m.get("take_profit_pct", 50))
        self.time_stop_hours = float(m.get("time_stop_hours", 72))
        self.max_drawdown_pct = float(m.get("max_drawdown_pct", 25))
        self.taker_fee_pct = float(m.get("taker_fee_pct", 1.0))
        self.slippage_bps = float(m.get("slippage_bps", 100))
        self.card_ttl_minutes = int(m.get("card_ttl_minutes", 360))
        self.max_trending_cards = int(m.get("max_trending_cards", 7))
        self.spike_volume_multiple = float(m.get("spike_volume_multiple", 3.0))
        self.spike_min_volume_24h = float(m.get("spike_min_volume_24h", 100000))
        self._check_exit_params()

    def _check_exit_params(self):
        """Kill threshold is structural: entries refuse if exit/drawdown
        params are not configured (fail-closed, never trade unprotected)."""
        for v in (self.stop_loss_pct, self.take_profit_pct, self.time_stop_hours,
                  self.max_drawdown_pct):
            if not v > 0:
                raise ValueError("memecoin exit/drawdown params must be > 0")

    # ---------------- state (journal meta) ----------------

    def _cash(self):
        v = self.journal.get_meta("t4_cash")
        return float(v) if v is not None else self.start_cash

    def _positions(self):
        raw = self.journal.get_meta("t4_positions")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save(self, cash, positions):
        self.journal.set_meta("t4_cash", str(round(cash, 8)))
        self.journal.set_meta("t4_positions", json.dumps(positions))

    def _log_trade(self, symbol, action, qty, price, note=""):
        self.journal.log_trade(
            timestamp=_now_iso(),
            symbol=symbol,
            action=action,
            qty=qty,
            price=price,
            reasoning=f"{TIER4_TAG} {note}",
        )

    def _peak_equity_meta(self):
        v = self.journal.get_meta("t4_peak_equity")
        return float(v) if v is not None else None

    # ---------------- prices ----------------

    def price_for(self, symbol):
        """Best-effort mark: Binance public data first, CoinGecko fallback."""
        symbol = str(symbol).replace("/USD", "").replace("-USD", "").upper()
        try:
            from bot.binance_data import BinanceDataClient
            px = BinanceDataClient(self.cfg).last_close(symbol)
            if px and float(px) > 0:
                return float(px)
        except Exception:
            pass
        try:
            resp = requests.get(
                COINGECKO_PRICE_URL,
                params={"ids": symbol.lower(), "vs_currencies": "usd"},
                headers=HEADERS, timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if symbol.lower() in data and "usd" in data[symbol.lower()]:
                px = float(data[symbol.lower()]["usd"])
                if px > 0:
                    return px
        except Exception:
            pass
        return None

    # ---------------- valuation ----------------

    def valuation(self):
        """Replay-derive equity: cash + marked positions."""
        cash = self._cash()
        positions = self._positions()
        total = 0.0
        for symbol, pos in positions.items():
            mark = self.price_for(symbol) or float(pos["entry"])
            total += float(pos["qty"]) * mark
        return {"cash": cash, "positions_value": total, "equity": cash + total}

    def _update_peak(self, equity):
        peak = self._peak_equity_meta()
        if peak is None or equity > peak:
            self.journal.set_meta("t4_peak_equity", str(round(equity, 8)))
            return equity
        return peak

    # ---------------- kill switch ----------------

    def kill_active(self):
        return self.journal.get_meta(KILL_META) == "on"

    def kill_count(self):
        v = self.journal.get_meta(KILL_COUNT_META)
        try:
            return int(v) if v is not None else 0
        except (TypeError, ValueError):
            return 0

    def _drawdown_hit(self, equity):
        peak = self._peak_equity_meta()
        if peak is None or peak <= 0:
            return False
        return (peak - equity) / peak * 100 >= self.max_drawdown_pct

    def _trigger_kill(self, reason):
        """Hard kill: flatten everything, block new entries until manual reset."""
        count = self.kill_count() + 1
        self.journal.set_meta(KILL_META, "on")
        self.journal.set_meta(KILL_COUNT_META, str(count))
        self.journal.set_meta("t4_kill_reason", reason)
        self.journal.set_meta("t4_kill_at", _now_iso())
        for symbol, pos in list(self._positions().items()):
            try:
                self._sell_position(symbol, note="kill flatten")
            except Exception as e:
                print(f"[tier4] kill flatten failed for {symbol}: {e}")

    def reset_kill(self):
        """Manual reset after a kill: re-arms entries with a fresh peak."""
        if not self.kill_active():
            return False, "no kill active"
        self.journal.set_meta(KILL_META, "off")
        self.journal.set_meta("t4_kill_reason", "")
        v = self.valuation()
        self._save(v["cash"], {})  # kill already flattened; safety clear
        self.journal.set_meta("t4_peak_equity", str(round(v["equity"], 8)))
        return True, f"kill reset; canary equity ${v['equity']:.2f}, peak re-armed"

    # ---------------- entries (human-gated only) ----------------

    def buy(self, symbol, stake=None):
        """Human-gated entry via CLI. Refuses under kill, insufficient cash,
        duplicate symbol, or unpriceable symbols. Entry-fixed SL/TP/time stop."""
        symbol = str(symbol).upper()
        if self.kill_active():
            return False, "kill active — entries blocked until manual reset (tools/tier4.py reset-kill)"
        if symbol in self._positions():
            return False, f"already holding {symbol}"
        price = self.price_for(symbol)
        if price is None or price <= 0:
            return False, f"no price available for {symbol}"
        cash = self._cash()
        stake = float(stake) if stake is not None else self.max_stake
        stake = min(stake, self.max_stake, cash)
        if stake <= 0:
            return False, f"insufficient canary cash (${cash:.2f})"
        fee = stake * self.taker_fee_pct / 100.0
        if stake + fee > cash:
            stake = max(0.0, cash / (1 + self.taker_fee_pct / 100.0))
            fee = stake * self.taker_fee_pct / 100.0
            if stake <= 0:
                return False, f"insufficient canary cash (${cash:.2f})"
        entry = price * (1 + self.slippage_bps / 10_000.0)
        qty = (stake - fee) / entry
        positions = self._positions()
        positions[symbol] = {
            "qty": qty,
            "entry": entry,
            "stop": entry * (1 - self.stop_loss_pct / 100.0),
            "take_profit": entry * (1 + self.take_profit_pct / 100.0),
            "opened": _now_iso(),
        }
        self._save(cash - stake, positions)
        self._log_trade(symbol, "BUY", qty, entry, note=f"human-gated canary entry")
        self._update_peak(self.valuation()["equity"])
        return True, (f"canary BUY {symbol}: ${stake:.2f} @ ${entry:.6f} (qty {qty:.6f}) | "
                      f"SL ${positions[symbol]['stop']:.6f} TP ${positions[symbol]['take_profit']:.6f} "
                      f"time stop {self.time_stop_hours:.0f}h")

    def _sell_position(self, symbol, note=""):
        positions = self._positions()
        pos = positions.get(symbol)
        if not pos:
            return None
        mark = self.price_for(symbol)
        if mark is None:
            mark = float(pos["entry"])
        exit_price = mark * (1 - self.slippage_bps / 10_000.0)
        qty = float(pos["qty"])
        proceeds = qty * exit_price
        fee = proceeds * self.taker_fee_pct / 100.0
        pnl = proceeds - fee - qty * float(pos["entry"])
        del positions[symbol]
        self._save(self._cash() + proceeds - fee, positions)
        self._log_trade(symbol, "SELL", qty, exit_price, note=f"{note} (pnl {pnl:+.2f})")
        return {"symbol": symbol, "exit_price": exit_price, "pnl": pnl}

    def sell(self, symbol):
        """Human-gated exit via CLI."""
        symbol = str(symbol).upper()
        if symbol not in self._positions():
            return False, f"no canary position in {symbol}"
        r = self._sell_position(symbol, note="human-gated exit")
        if r is None:
            return False, f"no canary position in {symbol}"
        return True, (f"canary SELL {symbol} @ ${r['exit_price']:.6f}: "
                       f"P&L {r['pnl']:+.2f}")

    # ---------------- hourly sweep (deterministic exits) ----------------

    def sweep(self):
        """Enforce entry-fixed exits + drawdown kill. Returns list of events."""
        events = []
        if self.kill_active():
            return events
        v = self.valuation()
        peak = self._update_peak(v["equity"])
        if self._drawdown_hit(v["equity"]):
            reason = (f"max drawdown {self.max_drawdown_pct:.0f}% hit "
                      f"(equity ${v['equity']:.2f} vs peak ${peak:.2f})")
            self._trigger_kill(reason)
            events.append({"type": "kill", "reason": reason})
            return events
        for symbol, pos in list(self._positions().items()):
            mark = self.price_for(symbol)
            if mark is None:
                continue
            opened = _parse_ts(pos.get("opened"))
            if opened and datetime.now(timezone.utc) - opened >= timedelta(hours=self.time_stop_hours):
                r = self._sell_position(symbol, note="time stop")
                if r:
                    events.append({"type": "time_stop", **r})
                continue
            if mark <= float(pos["stop"]):
                r = self._sell_position(symbol, note="stop loss")
                if r:
                    events.append({"type": "stop_loss", **r})
                continue
            if mark >= float(pos["take_profit"]):
                r = self._sell_position(symbol, note="take profit")
                if r:
                    events.append({"type": "take_profit", **r})
        return events

    # ---------------- research cards (deterministic, keyless) ----------------

    def coingecko_trending_cards(self):
        """Trending memecoin research cards; deduped against recent cards."""
        try:
            resp = requests.get(COINGECKO_TRENDING_URL, headers=HEADERS, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"[tier4] CoinGecko trending failed: {e}")
            return []
        out = []
        for item in data.get("coins", [])[:self.max_trending_cards]:
            it = item.get("item") or {}
            name = (it.get("name") or "").strip()
            symbol_id = (it.get("id") or "").strip()
            if not name or not symbol_id:
                continue
            rank = it.get("market_cap_rank")
            out.append({
                "kind": "coingecko_trending",
                "symbol": symbol_id,
                "name": name,
                "detail": f"CoinGecko trending (mcap rank {rank})",
            })
        return self._dedupe_and_log(out)

    def dexscreener_spike_cards(self):
        """Volume-spike research cards from DexScreener trending pairs."""
        try:
            resp = requests.get(
                DEXSCREENER_URL,
                params={"q": "trending"},
                headers=HEADERS, timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"[tier4] DexScreener spike scan failed: {e}")
            return []
        out = []
        pairs = data.get("pairs") or data.get("data") or []
        if isinstance(pairs, dict):
            pairs = list(pairs.values())
        for p in pairs[:30]:
            try:
                base = (p.get("baseToken") or {}).get("symbol") or ""
                vol24 = float(p.get("volume", {}).get("h24") or 0)
                vol6 = float(p.get("volume", {}).get("h6") or 0)
                liq = float(p.get("liquidity", {}).get("usd") or 0)
                price = float(p.get("priceUsd") or 0)
                if not base or price <= 0:
                    continue
                if vol24 < self.spike_min_volume_24h:
                    continue
                if vol24 > 0 and (vol6 * 4) / vol24 >= self.spike_volume_multiple:
                    out.append({
                        "kind": "dexscreener_spike",
                        "symbol": base,
                        "name": (p.get("baseToken") or {}).get("name") or base,
                        "detail": (f"6h volume ${vol6:,.0f} vs 24h ${vol24:,.0f} "
                                   f"({self.spike_volume_multiple:.0f}x multiple), "
                                   f"liquidity ${liq:,.0f}"),
                    })
            except Exception:
                continue
        return self._dedupe_and_log(out)

    def _dedupe_and_log(self, cards):
        """Drop cards already logged within TTL; expire stale ones; log the rest."""
        now = datetime.now(timezone.utc)
        existing = self.journal.get_tier4_cards()
        seen = {}
        for c in existing:
            key = (c[2], c[3])  # (kind, symbol) -> row
            if key not in seen:
                # rows are newest-first: keep the NEWEST row per key so the
                # TTL window is measured from the most recent card, not the
                # oldest (which would re-admit duplicates early)
                seen[key] = c
        out = []
        for card in cards:
            key = (card["kind"], card["symbol"])
            prev = seen.get(key)
            if prev:
                ts = _parse_ts(prev[1])
                if ts and now - ts < timedelta(minutes=self.card_ttl_minutes):
                    continue
                self.journal.expire_tier4_card(prev[0])
                # the just-expired row was the NEWEST for this key (get_tier4_cards
                # returns newest-first and 'seen' keeps the first sight of each
                # key); nothing fresh remains, so the new card may be logged
            self.journal.log_tier4_card(
                timestamp=_now_iso(),
                kind=card["kind"],
                symbol=card["symbol"],
                name=card["name"],
                detail=card["detail"],
            )
            out.append(card)
        return out

    # ---------------- cycle ----------------

    def run_cycle(self):
        """Research + sweep. Human-gated entries stay outside this path."""
        print(f"[tier4] canary cycle starting ({_now_iso()})")
        events = []
        try:
            events.extend(self.sweep())
        except Exception as e:
            print(f"[tier4] sweep failed: {e}")
        for ev in events:
            try:
                from bot.notify import send_notification
                if ev["type"] == "kill":
                    send_notification(
                        f"⛔ Tier 4 Coins: KILLED — {ev['reason']}. All sold, "
                        f"paused until you run `tier4.py reset-kill`", self.cfg)
                else:
                    pnl = ev.get("pnl", 0)
                    emoji = "📈" if pnl >= 0 else "📉"
                    label = {"stop_loss": "safety exit", "take_profit": "profit exit",
                             "time_stop": "time exit"}.get(ev["type"], ev["type"])
                    send_notification(
                        f"{emoji} Tier 4 Coins {label}: {ev['symbol']} — "
                        f"{'+' if pnl >= 0 else '-'}${abs(pnl):,.2f}", self.cfg)
            except Exception as e:
                print(f"[tier4] event notification failed: {e}")
        trending = []
        spikes = []
        try:
            trending = self.coingecko_trending_cards()
        except Exception as e:
            print(f"[tier4] trending cards failed: {e}")
        try:
            spikes = self.dexscreener_spike_cards()
        except Exception as e:
            print(f"[tier4] spike cards failed: {e}")
        v = self.valuation()
        print(f"[tier4] canary cycle done: {len(trending)} trending, "
              f"{len(spikes)} spike cards, equity ${v['equity']:.2f}")
        return {"events": events, "trending": trending, "spikes": spikes,
                "valuation": v}

    # ---------------- status ----------------

    def status_line(self):
        v = self.valuation()
        positions = self._positions()
        roi = (v["equity"] - self.start_cash) / self.start_cash * 100
        state = "KILLED" if self.kill_active() else "ACTIVE"
        pos_txt = (f"{len(positions)} open: " +
                   ", ".join(f"{s} @ entry ${float(p['entry']):.6f}"
                             for s, p in sorted(positions.items()))
                   ) if positions else "flat"
        return (f"Tier 4 canary: ${v['equity']:.2f} ({roi:+.2f}% of ${self.start_cash:.0f} start) | "
                f"cash ${v['cash']:.2f} | {pos_txt} | kills {self.kill_count()} | {state}")
