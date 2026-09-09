import time
import schedule
import pandas as pd
from datetime import datetime, timezone
from bot.config import config
from bot.broker import make_broker
from bot.strategies import get_strategies
from bot.risk import RiskEngine
from bot.journal import TradeJournal
from bot.errors import BrokerError
from bot.notify import send_notification
from bot.timeframe import make_timeframe
import time as _time

HEARTBEAT_INTERVAL_SECONDS = 3600


def _tier_one_liners(journal, broker):
    """Money lines + activity one-liners for tiers 2/3/4 (best-effort each)."""
    from bot.brief import money_line, emoji_for
    lines = []
    # Tier 2 — AI shadow account
    try:
        from bot.shadow import ShadowAccount
        sh = ShadowAccount(config, broker, journal=journal)
        v = sh.mark_to_market()
        delta = v[0] - sh.start_cash
        lines.append(f"{emoji_for(delta)} {money_line('Tier 2 AI', v[0], sh.start_cash)}"
                     + (f" | {len(sh._positions())} open AI trade(s)" if sh._positions() else " | no open trades"))
    except Exception:
        pass
    # Tier 3 — Polymarket wallet
    try:
        from bot.wallet import BettingWallet
        w = BettingWallet(config, journal=journal)
        v = w.valuation()
        delta = v["equity"] - w.start_cash
        lines.append(f"{emoji_for(delta)} {money_line('Tier 3 Bets', v['equity'], w.start_cash)}"
                     + (f" | {v['open_bets']} open bet(s)" if v["open_bets"] else " | no open bets"))
    except Exception:
        pass
    # Tier 4 — memecoin canary
    try:
        from bot.memecoin import MemecoinLedger
        led = MemecoinLedger(config, journal=journal)
        v = led.valuation()
        delta = v["equity"] - led.start_cash
        pos = led._positions()
        lines.append(f"{emoji_for(delta)} {money_line('Tier 4 Coins', v['equity'], led.start_cash)}"
                     + (f" | holding {', '.join(sorted(pos))}" if pos else " | nothing held"))
    except Exception:
        pass
    # Tier 5 — futures canary
    try:
        from bot.futures import FuturesLedger
        fl = FuturesLedger(config, journal=journal)
        v = fl.valuation()
        delta = v["equity"] - fl.start_cash
        pos = fl._positions()
        lines.append(f"{emoji_for(delta)} {money_line('Tier 5 Futures', v['equity'], fl.start_cash)}"
                     + (f" | {len(pos)} open leveraged position(s)" if pos else " | flat"))
    except Exception:
        pass
    return "\n".join(lines)


def send_heartbeat(broker, journal):
    """One brief hourly glance: money per tier, activity only if it exists."""
    now = _time.time()
    current_hour = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H")
    last = journal.get_meta("last_heartbeat_hour")
    if last is not None and last == current_hour:
        return
    try:
        from bot.brief import money_line, emoji_for
        acct = broker.get_account()
        positions = list(broker.get_all_positions())

        # Tier 1 money line: equity vs the epoch baseline (seed/reset marker,
        # falling back to configured start cash)
        start_cash = None
        seeded_at = journal.get_meta("paper_seeded_at")
        if seeded_at:
            try:
                # the ledger epoch baseline: cash recorded at the last seed
                start_cash = float(journal.get_meta("paper_epoch_start_cash") or 0)
            except Exception:
                start_cash = None
        if not start_cash:
            start_cash = float((getattr(config, "broker", None) or {})
                               .get("paper", {}).get("start_cash", 100) or 100)
        equity = float(acct.equity)
        delta = equity - start_cash

        lines = [f"🫀 {emoji_for(delta)} "
                 f"{money_line('Tier 1 BTC/ETH/SOL', equity, start_cash)}"]
        if positions:
            # activity: open positions with unrealized P&L each
            for p in positions:
                pl = float(p.unrealized_pl)
                lines.append(f"   {emoji_for(pl)} {p.symbol}: {float(p.qty):.4f} @ ${float(p.avg_entry_price):,.2f} "
                             f"({('+' if pl >= 0 else '-')}${abs(pl):,.2f} now)")
        else:
            lines.append("   ➖ no open trades")

        lines.append(_tier_one_liners(journal, broker))

        msg = "\n".join(lines)
        send_notification(msg, config)
        journal.set_meta("last_heartbeat_hour", current_hour)
        # Tier 1 equity snapshot (epoch 0 namespace in wallet_snapshots):
        # feeds the kill/keep drawdown + losing-week criteria in gates.py
        try:
            equity_now = float(acct.equity)
            cash_now = float(acct.cash)
            positions_value = equity_now - cash_now
            journal.log_wallet_snapshot(
                timestamp=datetime.now(timezone.utc).isoformat(),
                epoch=0, cash=cash_now, locked=positions_value, equity=equity_now)
        except Exception as snap_err:
            print(f"[{datetime.now()}] Tier 1 equity snapshot failed: {snap_err}")
        print(f"[{datetime.now()}] Heartbeat sent (hour {current_hour})")
    except Exception as e:
        print(f"[{datetime.now()}] Heartbeat failed (will retry next cycle): {e}")


# ---------------- persistent strategy state ----------------
# Edge-triggered signals (a cross on ONE bar) die with the cycle that saw
# them: if that run is late or fails, the exit/entry is skipped forever. This
# state, persisted in journal meta, survives missed cycles: the transition
# is derived from the CURRENT fast-vs-slow relation vs the LAST SUCCESSFULLY
# PROCESSED one, so a missed bar catch-up happens however late it arrives.

import json as _json
import hashlib as _hashlib

POSITION_STATE_KEY = "strat_state_positions"
IDEMPOTENCY_KEY = "executed_client_order_ids"


def _load_position_states(journal):
    raw = journal.get_meta(POSITION_STATE_KEY)
    if not raw:
        return {}
    try:
        data = _json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_position_states(journal, states):
    journal.set_meta(POSITION_STATE_KEY, _json.dumps(states))


PENDING_EXITS_KEY = "pending_exit_symbols"


def _pending_exits(journal):
    raw = journal.get_meta(PENDING_EXITS_KEY)
    if not raw:
        return set()
    try:
        data = _json.loads(raw)
        return set(data) if isinstance(data, list) else set()
    except Exception:
        return set()


def _add_pending_exit(journal, symbol):
    """Record an intended-but-unexecuted exit. The intent persists until the
    SELL actually fills, independent of SMA relation whipsaws (a golden→death
    →golden flip mid-retry can no longer abandon the exit)."""
    cur = _pending_exits(journal)
    cur.add(symbol)
    journal.set_meta(PENDING_EXITS_KEY, _json.dumps(sorted(cur)))


def _clear_pending_exit(journal, symbol):
    cur = _pending_exits(journal)
    if symbol in cur:
        cur.discard(symbol)
        journal.set_meta(PENDING_EXITS_KEY, _json.dumps(sorted(cur)))


def _client_order_id(symbol, action, reasoning, qty=0.0):
    """Deterministic idempotency key for one intended order.

    Same (symbol, action, reasoning, qty) -> same id, so a retried/resent
    submission of an already-executed intent is recognized as a duplicate
    instead of filling twice. The reasoning carries the signal context
    (cross type, SMA levels, stop prices) and qty binds the key to the
    exact intended order, making keys stable across retries but distinct
    between genuinely different intents (e.g. the same exit reason on a
    later, differently-sized position is NOT a duplicate).

    Exits additionally fold in the UTC day + attempt counter, so a
    constant-reasoning exit (daily-loss flatten, catch-up reasons) can
    legitimately repeat on a later day or after a re-entry without being
    suppressed as a duplicate of the first execution.
    """
    nonce = ""
    if action.upper() == "SELL":
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        attempts = _exit_attempts(journal_ref.get(), symbol)
        nonce = f"|{day}|a{attempts}"
    raw = f"{symbol}|{action}|{reasoning}|{qty:.8f}{nonce}"
    return _hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


import threading
_journal_attempts_local = threading.local()


class journal_ref:
    """Late-bound journal handle for the exit-attempt counter (set per cycle)."""
    _inst = None

    @classmethod
    def get(cls):
        return cls._inst

    @classmethod
    def set(cls, j):
        cls._inst = j


def _exit_attempts(journal, symbol):
    """Monotonic per-symbol attempt counter so retried exits get distinct
    idempotency keys (a suppressed retry must not suppress the next one too)."""
    if journal is None:
        return 0
    try:
        raw = journal.get_meta("exit_attempt_counters")
        data = _json.loads(raw) if raw else {}
        return int(data.get(symbol, 0))
    except Exception:
        return 0


def _bump_exit_attempt(journal, symbol):
    if journal is None:
        return
    try:
        raw = journal.get_meta("exit_attempt_counters")
        data = _json.loads(raw) if raw else {}
        if not isinstance(data, dict):
            data = {}
        data[symbol] = int(data.get(symbol, 0)) + 1
        # bounded: keep newest 100 symbols
        items = sorted(data.items(), key=lambda kv: kv[1], reverse=True)
        journal.set_meta("exit_attempt_counters", _json.dumps(dict(items[:100])))
    except Exception:
        pass


def _iso_age_minutes(iso_ts):
    """Minutes elapsed since an ISO timestamp (aware or naive; naive = UTC)."""
    try:
        ts = datetime.fromisoformat(iso_ts)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() / 60.0
    except Exception:
        return 1e9  # unparseable -> treat as very old


def _already_executed(journal, client_order_id):
    raw = journal.get_meta(IDEMPOTENCY_KEY)
    if not raw:
        return False
    try:
        done = _json.loads(raw)
        return isinstance(done, dict) and client_order_id in done
    except Exception:
        return False


def _mark_executed(journal, client_order_id, order_ref):
    raw = journal.get_meta(IDEMPOTENCY_KEY)
    try:
        done = _json.loads(raw) if raw else {}
        if not isinstance(done, dict):
            done = {}
    except Exception:
        done = {}
    done[client_order_id] = order_ref
    # bounded: keep the newest 500 entries (idempotency is for near-term
    # retries, not an unbounded growth key)
    if len(done) > 500:
        done = dict(list(done.items())[-500:])
    journal.set_meta(IDEMPOTENCY_KEY, _json.dumps(done))


def _sma_relation(df, cfg):
    """'above' | 'below' | None (insufficient data) using closed bars."""
    fast, slow = cfg.sma_fast, cfg.sma_slow
    if df is None or len(df) < slow:
        return None
    close = df["close"]
    f = close.rolling(fast).mean().iloc[-1]
    s = close.rolling(slow).mean().iloc[-1]
    if f != f or s != s:  # NaN guard
        return None
    return "above" if f > s else "below"


def _cross_on_current_edge(df, cfg):
    """True if the fast/slow cross happened on the LAST closed bar (the
    edge-triggered strategy will fire for it this cycle) vs earlier (the
    transition was missed by a late/failed cycle and needs catch-up)."""
    from bot.strategy import check_crossover
    try:
        signal, *_ = check_crossover(df, cfg.sma_fast, cfg.sma_slow)
    except Exception:
        return False
    return signal is not None


def _catchup_signal(holding, relation, prev_relation, fresh_cross):
    """Reconciliation for a cross missed by late/failed cycles.

    Derived from the CURRENT fast-vs-slow relation vs the last SUCCESSFULLY
    PROCESSED one — level-triggered, so a cross visible for a single 15-min
    cycle can no longer be silently skipped forever.
    :return: 'SELL' (missed death cross while holding — retries until the
             exit executes), 'BUY' (missed golden cross while flat — exactly
             one catch-up shot), or None
    """
    if fresh_cross or relation is None or prev_relation is None or relation == prev_relation:
        return None
    if relation == "below" and prev_relation == "above" and holding:
        return "SELL"
    if relation == "above" and prev_relation == "below" and not holding:
        return "BUY"
    return None


def run_trading_cycle():
    """Run one trading cycle for all symbols across all registered strategies."""
    print(f"[{datetime.now()}] Starting trading cycle...")

    # Initialize components
    try:
        broker = make_broker(config)
        journal = TradeJournal()
    except Exception as e:
        print(f"[{datetime.now()}] Failed to initialize components: {e}")
        return

    strategies = get_strategies(getattr(config, "active_strategies", ["sma_cross"]))
    if not strategies:
        print(f"[{datetime.now()}] No strategies registered — nothing to do")
        return

    risk_engine = RiskEngine(config, broker, journal)

    # Set up timeframe for data fetching
    timeframe = make_timeframe(config.timeframe)

    # Fetch bars once per symbol; strategies share them
    bars_by_symbol = {}
    for symbol in config.symbols:
        try:
            df = broker.get_crypto_bars(symbol, timeframe, config.lookback_bars)
            if df is None or df.empty:
                print(f"[{datetime.now()}] No data for {symbol}")
                continue
            # Drop the still-forming (unclosed) bar: signals must use closed bars only
            df = df.iloc[:-1]
            bars_by_symbol[symbol] = df
        except BrokerError as e:
            print(f"[{datetime.now()}] Broker error fetching {symbol}: {e}")
        except Exception as e:
            print(f"[{datetime.now()}] Unexpected fetch error for {symbol}: {e}")

    if risk_engine.daily_loss_hit():
        print(f"[{datetime.now()}] Daily loss limit hit")
        if (getattr(config, "risk", None) or {}).get("flatten_on_daily_loss", False):
            try:
                held_symbols = {p.symbol for p in broker.get_all_positions()}
            except Exception as e:
                print(f"[{datetime.now()}] Could not enumerate positions for flatten: {e}")
                held_symbols = set(bars_by_symbol)
            # map broker symbols (ETHUSD) back to our format (ETH/USD)
            slash_map = {s.replace("/", ""): s for s in config.symbols}
            for sym in held_symbols:
                symbol = slash_map.get(sym, sym)
                df = bars_by_symbol.get(symbol)
                if df is None:
                    df = pd.DataFrame({"close": [0.0]})
                _execute_signal(broker, journal, risk_engine, symbol, df, {
                    "action": "SELL",
                    "reasoning": "account-level daily loss limit: flattening paper exposure",
                })
        send_heartbeat(broker, journal)
        return

    position_states = _load_position_states(journal)
    pending_exits = _pending_exits(journal)
    journal_ref.set(journal)

    for symbol, df in bars_by_symbol.items():
        symbol_cycle_ok = True
        relation = None
        try:
            print(f"[{datetime.now()}] Processing {symbol}...")

            # ---- pending exit replay: an exit that previously failed must
            # retry until executed, regardless of SMA-relation whipsaws.
            # (relation-derived catch-up only reconciles the LAST transition;
            # a persisted intent closes that hole.)
            if symbol in pending_exits:
                position = broker.get_position(symbol)
                if position and float(getattr(position, "qty", 0) or 0) > 0:
                    mark = float(df["close"].iloc[-1]) if df is not None and len(df) else 0.0
                    executed = _execute_signal(broker, journal, risk_engine, symbol, df, {
                        "action": "SELL",
                        "reasoning": "pending exit replay (previously failed exit intent)",
                    })
                    if not executed:
                        symbol_cycle_ok = False
                    else:
                        _clear_pending_exit(journal, symbol)
                        pending_exits.discard(symbol)
                else:
                    # position gone (sold elsewhere/manual): intent satisfied
                    _clear_pending_exit(journal, symbol)
                    pending_exits.discard(symbol)


            # ---- persistent SMA relation: catch crosses missed by late/failed
            # cycles. A death cross visible for exactly one 15-min bar is no
            # longer a single-cycle event: if the previous PROCESSED state was
            # 'above' and we are now 'below', that transition happened while
            # we were blind, and the exit must fire now. Exits retry every
            # cycle until executed; entries get exactly one catch-up shot.
            relation = _sma_relation(df, config)
            prev_relation = position_states.get(symbol)
            fresh_cross = _cross_on_current_edge(df, config)

            position = broker.get_position(symbol)
            if position:
                # self-heal: a position without a recorded stop (e.g. seeded
                # after a re-seed wiped the ledger) gets one NOW, from the
                # current bars' ATR or the fallback pct
                healed, stop_price = risk_engine.ensure_stop(
                    symbol, getattr(position, "avg_entry_price", 0),
                    _atr(df, int((getattr(config, "risk", None) or {}).get("atr_period", 14))))
                if healed:
                    print(f"[{datetime.now()}] Healed missing stop for {symbol}: "
                          f"${stop_price:.2f} (entry ${float(position.avg_entry_price):.2f})")
                    try:
                        send_notification(
                            f"🩹 **Stop healed — {symbol}**\n"
                            f"Position had no recorded stop (ledger lost in a re-seed). "
                            f"Recorded stop ${stop_price:.2f} vs entry "
                            f"${float(position.avg_entry_price):,.2f}.",
                            config)
                    except Exception:
                        pass
                # check the entry-fixed stop against the latest closed bar
                # (live broker mark as fallback if bars are stale/unusable)
                mark = float(df["close"].iloc[-1]) if df is not None and len(df) else 0.0
                if mark <= 0:
                    mark = float(getattr(position, "current_price", 0) or 0)
                triggered, reason = risk_engine.stop_triggered(symbol, mark)
                if not triggered and mark <= 0:
                    # recorded stop exists but no usable mark: fall back to the
                    # legacy current-ATR check rather than skipping entirely
                    atr = _atr(df, int((getattr(config, "risk", None) or {}).get("atr_period", 14)))
                    triggered, reason = risk_engine.atr_stop_triggered(
                        float(position.avg_entry_price), mark, atr
                    )
                if triggered:
                    _execute_signal(broker, journal, risk_engine, symbol, df, {
                        "action": "SELL", "reasoning": reason,
                    })
                    symbol_cycle_ok = False  # state not advanced; re-check next cycle
                    continue
                holding = True
                catchup = _catchup_signal(holding, relation, prev_relation, fresh_cross)
                if catchup == "SELL":
                    # missed death cross: exit retries until it executes
                    executed = _execute_signal(broker, journal, risk_engine, symbol, df, {
                        "action": "SELL",
                        "reasoning": (f"[sma_cross] missed death cross catch-up: SMA relation was "
                                      f"'{prev_relation}' at last processed cycle, now '{relation}'"),
                    })
                    if not executed:
                        symbol_cycle_ok = False
            else:
                holding = False
                catchup = _catchup_signal(holding, relation, prev_relation, fresh_cross)
                if catchup == "BUY":
                    # missed golden cross while flat: one catch-up entry shot
                    # (the transition is consumed either way — entries never
                    # retry-spam; a fresh cross is left to the strategy itself)
                    _execute_signal(broker, journal, risk_engine, symbol, df, {
                        "action": "BUY",
                        "reasoning": (f"[sma_cross] missed golden cross catch-up: SMA relation was "
                                      f"'{prev_relation}' at last processed cycle, now '{relation}'"),
                    })

            for strat_name, strat_fn in strategies:
                try:
                    signals = strat_fn(symbol, df, config)
                except Exception as e:
                    print(f"[{datetime.now()}] Strategy {strat_name} error on {symbol}: {e}")
                    symbol_cycle_ok = False
                    continue
                for sig in signals:
                    executed = _execute_signal(broker, journal, risk_engine, symbol, df, sig)
                    if not executed and sig.get("action") == "SELL":
                        # exit did not execute while a position is held:
                        # do NOT advance state — the transition must retry
                        symbol_cycle_ok = False
                    # a failed/blocked BUY consumes the transition by design:
                    # entries are one-shot and never retry-spam
        except BrokerError as e:
            print(f"[{datetime.now()}] Broker error for {symbol}: {e}")
            symbol_cycle_ok = False
        except Exception as e:
            print(f"[{datetime.now()}] Unexpected error for {symbol}: {e}")
            symbol_cycle_ok = False
        finally:
            # state advances ONLY on a fully successful cycle for this symbol:
            # a late/failed cycle leaves the old state so its transitions are
            # caught on the next successful one
            if symbol_cycle_ok:
                if relation is not None:
                    position_states[symbol] = relation
                _save_position_states(journal, position_states)

    # stops protect positions even when their bar feed failed this cycle
    try:
        slash_map = {s.replace("/", ""): s for s in config.symbols}
        held_symbols = {slash_map.get(p.symbol, p.symbol)
                        for p in broker.get_all_positions()}
        for symbol in held_symbols - set(bars_by_symbol):
            position = broker.get_position(symbol)
            if not position:
                continue
            mark = float(getattr(position, "current_price", 0) or 0)
            # barless positions can't compute ATR here; the fallback pct stop
            # guarantees they're not stop-less until bars return
            healed, stop_price = risk_engine.ensure_stop(
                symbol, getattr(position, "avg_entry_price", 0), None)
            if healed:
                print(f"[{datetime.now()}] Healed missing stop for {symbol} (no bars): "
                      f"${stop_price:.2f}")
                try:
                    send_notification(
                        f"🩹 **Stop healed — {symbol}**\n"
                        f"Recorded fallback stop ${stop_price:.2f} vs entry "
                        f"${float(position.avg_entry_price):,.2f} (bar feed unavailable).",
                        config)
                except Exception:
                    pass
            if mark <= 0:
                continue
            triggered, reason = risk_engine.stop_triggered(symbol, mark)
            if triggered:
                _execute_signal(broker, journal, risk_engine, symbol, None, {
                    "action": "SELL", "reasoning": reason,
                })
        # ledger stops for symbols no longer held are dead weight (e.g. a
        # re-seed wiped positions): drop them so they can never mis-fire
        stale = risk_engine.prune_stale_stops(held_symbols)
        if stale:
            print(f"[{datetime.now()}] Pruned stale stops: {stale}")
    except Exception as e:
        print(f"[{datetime.now()}] Stop sweep for barless positions failed: {e}")

    # Hourly status heartbeat (no-op if less than an hour since last one)
    send_heartbeat(broker, journal)

    print(f"[{datetime.now()}] Trading cycle completed.")


def _execute_signal(broker, journal, risk_engine, symbol, df, sig):
    """Validate a signal through risk and execute it with full Discord trail.

    :return: True if a fill was confirmed (or nothing needed doing),
             False when the signal did NOT execute (blocked, failed, zero qty)
    """
    action = sig["action"]
    try:
        price = float(df['close'].iloc[-1])
    except Exception:
        price = 0.0

    position = broker.get_position(symbol)
    current_qty = float(position.qty) if position else 0.0
    if position and price <= 0:
        # no usable bar data (e.g. flatten without bars): fall back to broker marks
        try:
            price = float(getattr(position, "current_price", 0) or 0)
        except Exception:
            price = 0.0

    qty = 0.0
    if action == "BUY" and current_qty == 0:
        try:
            acct = broker.get_account()
            available = float(acct.cash)
        except Exception:
            available = config.notional
        atr = _atr(df, int((getattr(config, "risk", None) or {}).get("atr_period", 14)))
        qty = risk_engine.size_for_atr(price, atr, available, config.notional)
        if qty <= 0 or price <= 0:
            # risk sizing clipped the order to nothing (or no usable price):
            # never submit an empty order to the broker
            print(f"[{datetime.now()}] BUY {symbol} sized to zero (price={price}, "
                  f"cash={available}); skipping")
            return False
    elif action == "SELL" and current_qty > 0:
        qty = current_qty
    else:
        print(f"[{datetime.now()}] {action} signal for {symbol} but no action needed (qty={current_qty})")
        return True  # desired end state already holds; nothing failed

    if qty > 0 and action == "BUY":
        # fail-closed position/equity checks: an infra error reading the
        # account must not degrade max_open_positions/max_notional caps
        try:
            positions = list(broker.get_all_positions())
            open_count = len(positions)
            crypto_notional = sum(abs(float(getattr(p, "market_value", 0) or 0)) for p in positions)
        except Exception:
            print(f"[{datetime.now()}] BUY {symbol}: cannot enumerate positions — failing closed")
            return False
        try:
            account_equity = float(broker.get_account().equity)
        except Exception:
            account_equity = None
        allowed, reason = risk_engine.check(symbol, action, qty, price, open_count,
                                            current_crypto_notional=crypto_notional,
                                            account_equity=account_equity)
        if not allowed:
            print(f"[{datetime.now()}] RISK BLOCKED {action} {symbol}: {reason}")
            try:
                send_notification(
                    f"🛑 Tier 1: {action} {symbol} blocked — {reason}",
                    config
                )
            except Exception:
                pass
            return False

    print(f"[{datetime.now()}] Placing {action} order for {symbol}: qty={qty:.6f}, price={price:.2f}")
    reasoning = sig.get("reasoning", action)
    client_order_id = _client_order_id(symbol, action, reasoning, qty)
    if _already_executed(journal, client_order_id):
        print(f"[{datetime.now()}] Duplicate order suppressed for {symbol} {action} "
              f"(client_order_id {client_order_id} already executed)")
        return True
    try:
        # rate-limit identical pre-trade alerts: a persistently failing SELL
        # retry would otherwise spam every 15 minutes. Same intent within
        # 30 minutes = one alert.
        alert_key = f"pretrade_alert_{client_order_id}"
        last_alert = journal.get_meta(alert_key)
        now_iso = datetime.now(timezone.utc).isoformat()
        if last_alert is None or _iso_age_minutes(last_alert) > 30:
            send_notification(
                f"🟡 Tier 1: {action} {symbol} @ ${price:,.2f} — placing order…",
                config
            )
            journal.set_meta(alert_key, now_iso)
    except Exception as notify_err:
        print(f"[{datetime.now()}] Pre-trade Discord alert failed (continuing trade): {notify_err}")
    try:
        order = broker.place_order(symbol, qty, action.upper())
        confirmed = broker.await_terminal_order(order.id)
    except BrokerError as e:
        print(f"[{datetime.now()}] Order submission/confirmation failed for {symbol}: {e}")
        if action == "SELL":
            # exit intent did NOT execute: persist it so it retries every
            # cycle until filled, regardless of SMA-relation whipsaws
            _add_pending_exit(journal, symbol)
            _bump_exit_attempt(journal, symbol)
        return False
    raw_status = getattr(confirmed, "status", "")
    status = str(getattr(raw_status, "value", raw_status)).lower()
    if status == "filled":
        fill_qty = float(getattr(confirmed, "filled_qty", None) or qty)
        fill_price = float(getattr(confirmed, "filled_avg_price", None) or price)
        # journal the fee the broker ACTUALLY charged (paper broker returns
        # it on the order). Estimated/config fees are backtest-only; the
        # scorecard must reconcile with the ledger. Fall back to the
        # configured estimate only for brokers that don't report fees.
        actual_fee = getattr(confirmed, "fee", None)
        if actual_fee is None or float(actual_fee) < 0:
            fee_pct = float((getattr(config, "execution", None) or {}).get("taker_fee_pct", 0)) / 100.0
            actual_fee = fill_qty * fill_price * fee_pct
        journal.log_trade(
            timestamp=datetime.now(timezone.utc).isoformat(), symbol=symbol, action=action,
            qty=fill_qty, price=fill_price, reasoning=reasoning, fee=float(actual_fee),
            order_id=str(getattr(order, "id", "")), status="filled"
        )
        _mark_executed(journal, client_order_id,
                       str(getattr(order, "id", "")))
        if action == "BUY":
            # fix the stop at entry: level decided now, never moved afterwards
            entry_atr = _atr(df, int((getattr(config, "risk", None) or {}).get("atr_period", 14))) if df is not None else None
            stop_price = risk_engine.entry_fixed_stop(symbol, fill_price, entry_atr)
            risk_engine.record_stop(symbol, fill_price, stop_price)
            print(f"[{datetime.now()}] Stop recorded for {symbol}: ${stop_price:.2f} (entry ${fill_price:.2f})")
        elif action == "SELL":
            # keep the stop if a partial fill leaves a residual position
            try:
                remaining = broker.get_position(symbol)
            except Exception:
                remaining = None
            if remaining is None or float(getattr(remaining, "qty", 0) or 0) <= 0:
                risk_engine.clear_stop(symbol)
                _clear_pending_exit(journal, symbol)  # exit intent satisfied
        print(f"[{datetime.now()}] Confirmed paper fill for {symbol}: {action} {fill_qty:.6f} @ {fill_price:.2f}")
        try:
            notional = fill_qty * fill_price
            # realized P&L on a full exit: equity move tells the money story
            emoji = "🟢" if action == "BUY" else "🔴"
            pnl_txt = ""
            if action == "SELL":
                from bot.report import compute_pnl_and_winrate
                stats = compute_pnl_and_winrate(journal.get_trades())
                pnl_txt = (f" | total so far {'+' if stats['total_pnl'] >= 0 else '-'}"
                           f"${abs(stats['total_pnl']):,.2f}")
            send_notification(
                f"{emoji} Tier 1 {action}: {notional:,.2f} of {symbol} @ ${fill_price:,.2f}{pnl_txt}",
                config
            )
        except Exception as notify_err:
            print(f"[{datetime.now()}] Post-trade Discord alert failed: {notify_err}")
        return True
    else:
        # journal non-fills so the audit trail matches the broker's order history
        filled_qty = float(getattr(confirmed, "filled_qty", None) or 0)
        journal.log_trade(
            timestamp=datetime.now(timezone.utc).isoformat(), symbol=symbol, action=action,
            qty=filled_qty, price=float(getattr(confirmed, "filled_avg_price", None) or 0.0),
            reasoning=f"{reasoning} [order ended as {status or 'pending'}; no fill]",
            fee=0.0, order_id=str(getattr(order, "id", "")),
            status=status or "pending",
        )
        print(f"[{datetime.now()}] Order {getattr(order, 'id', 'unknown')} ended as {status or 'pending'}; journaled as non-fill")
        if action == "SELL":
            _add_pending_exit(journal, symbol)
            _bump_exit_attempt(journal, symbol)
        return False


def _atr(df, period):
    """Compute ATR from closed bars; return None when the data is insufficient."""
    if df is None or period <= 0 or len(df) < period + 1:
        return None
    close = df["close"]
    high = df["high"] if "high" in df else close
    low = df["low"] if "low" in df else close
    ranges = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    value = ranges.rolling(period).mean().iloc[-1]
    return float(value) if value == value and value > 0 else None


def run_agent_cycle():
    """One AI agent cycle (babysitter + scout) — shadow mode by default."""
    from bot.agent import TradingAgent
    broker = make_broker(config)
    journal = TradeJournal()
    agent = TradingAgent(config, broker, journal=journal)
    agent.run_cycle()
    # Heartbeat fallback: the trade cycle normally posts the hourly heartbeat,
    # but GitHub's scheduler under-delivers 15-min workflows. The agent runs
    # hourly on a native cron that delivers reliably — if no heartbeat landed
    # this hour, post it here so the owner always gets one per hour.
    try:
        send_heartbeat(broker, journal)
    except Exception as e:
        print(f"[{datetime.now()}] Heartbeat fallback failed: {e}")


def run_scanner_cycle():
    """One Polymarket scanner cycle (read-only API, paper bets)."""
    from bot.polymarket import scan, settle_open_bets
    from bot.wallet import BettingWallet
    journal = TradeJournal()
    try:
        settle_open_bets(config, journal=journal)
    except Exception as e:
        print(f"[{datetime.now()}] Bet settlement failed: {e}")
    scan(config, journal=journal)
    try:
        wallet = BettingWallet(config, journal=journal)
        wallet.snapshot()
        from bot.brief import money_line, emoji_for
        v = wallet.valuation()
        delta = v["equity"] - wallet.start_cash
        bets_txt = f" | {v['open_bets']} open bet(s)" if v["open_bets"] else " | no open bets"
        send_notification(f"{emoji_for(delta)} {money_line('Tier 3 Bets', v['equity'], wallet.start_cash)}{bets_txt}", config)
    except Exception as e:
        print(f"[{datetime.now()}] Wallet snapshot failed: {e}")


def main():
    """Main entry point: run the trading cycle every 15 minutes."""
    # Run immediately on start
    run_trading_cycle()

    # Schedule to run every 15 minutes
    schedule.every(15).minutes.do(run_trading_cycle)

    print("Scheduler started. Press Ctrl+C to exit.")
    try:
        while True:
            schedule.run_pending()
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nExiting...")


if __name__ == "__main__":
    main()
