"""Strategy registry: pluggable signal generators.

Each strategy exposes evaluate(symbol, df, cfg) -> list of Signal dicts:
    {"action": "BUY"|"SELL", "symbol": str, "qty_basis": "notional"|"full_position",
     "reasoning": str}

The trader loop is strategy-agnostic: it iterates registered strategies,
validates their signals through the risk module, then executes.

The action and qty_basis values are part of a contract with the trader, and
they are bare strings -- nothing in the loop rejects one it does not
understand, because an unrecognised action falls through to "no action
needed" and reads as a decision. So the check happens here, at the producer,
where a mistake is a typo rather than a silent no-op.

Sizing is deliberately NOT the strategy's business. The trader sizes a SELL
from the broker position and a BUY through risk.size_for_atr, so qty_basis
records the strategy's intent and no consumer reads it.

Cross signals are edge-triggered here (fire on the exact transition bar);
the trading loop carries the persistent relation state that catches
transitions missed by late or failed cycles.
"""
from bot.strategy import check_crossover

ACTIONS = ("BUY", "SELL")
QTY_BASES = ("notional", "full_position")


def _signal(action, symbol, qty_basis, reasoning):
    """Build a signal, refusing any value the trader cannot act on."""
    if action not in ACTIONS:
        raise ValueError(f"unknown signal action {action!r}; expected one of {ACTIONS}")
    if qty_basis not in QTY_BASES:
        raise ValueError(f"unknown qty_basis {qty_basis!r}; expected one of {QTY_BASES}")
    return {"action": action, "symbol": symbol, "qty_basis": qty_basis, "reasoning": reasoning}


def sma_cross(symbol, df, cfg):
    """SMA20/50 crossover: BUY on golden cross (flat), SELL on death cross (holding)."""
    fast, slow = cfg.sma_fast, cfg.sma_slow
    if len(df) < slow + 1:
        return []
    signal, prev_fast, prev_slow, curr_fast, curr_slow = check_crossover(df, fast, slow)
    if signal not in ("golden", "death"):
        # Anything else is a cross this registry does not understand. Falling
        # through to the SELL branch turned a renamed or misspelt signal into
        # a sell, which is the worst available default for unparseable input.
        return []
    reasoning = (
        f"[sma_cross] {signal} cross: SMA{fast} {prev_fast:.2f} "
        f"{'above' if signal == 'golden' else 'below'} SMA{slow} {prev_slow:.2f} "
        f"-> now {curr_fast:.2f} vs {curr_slow:.2f}"
    )
    if signal == "golden":
        return [_signal("BUY", symbol, "notional", reasoning)]
    return [_signal("SELL", symbol, "full_position", reasoning)]


REGISTRY = {
    "sma_cross": sma_cross,
}


def get_strategies(names):
    """Resolve strategy names to callables.

    An unknown name raises. It used to be printed and skipped, which left an
    empty list that the trader reported as "no strategies registered" and then
    exited 0 -- a dead config was indistinguishable from a quiet market.
    """
    out = []
    for name in names:
        fn = REGISTRY.get(name)
        if fn is None:
            raise ValueError(
                f"unknown strategy {name!r}; active_strategies must name one of "
                f"{sorted(REGISTRY)}"
            )
        out.append((name, fn))
    return out
