"""Strategy registry: pluggable signal generators.

Each strategy exposes evaluate(symbol, df, cfg) -> list of Signal dicts:
    {"action": "BUY"|"SELL", "symbol": str, "qty_basis": "notional"|"full_position",
     "reasoning": str}

The trader loop is strategy-agnostic: it iterates registered strategies,
validates their signals through the risk module, then executes.

Cross signals are edge-triggered here (fire on the exact transition bar);
the trading loop carries the persistent relation state that catches
transitions missed by late or failed cycles.
"""
from bot.strategy import check_crossover


def sma_cross(symbol, df, cfg):
    """SMA20/50 crossover: BUY on golden cross (flat), SELL on death cross (holding)."""
    fast, slow = cfg.sma_fast, cfg.sma_slow
    if len(df) < slow + 1:
        return []
    signal, prev_fast, prev_slow, curr_fast, curr_slow = check_crossover(df, fast, slow)
    if signal is None:
        return []
    reasoning = (
        f"[sma_cross] {signal} cross: SMA{fast} {prev_fast:.2f} "
        f"{'above' if signal == 'golden' else 'below'} SMA{slow} {prev_slow:.2f} "
        f"-> now {curr_fast:.2f} vs {curr_slow:.2f}"
    )
    if signal == "golden":
        return [{"action": "BUY", "symbol": symbol, "qty_basis": "notional", "reasoning": reasoning}]
    return [{"action": "SELL", "symbol": symbol, "qty_basis": "full_position", "reasoning": reasoning}]


REGISTRY = {
    "sma_cross": sma_cross,
}


def get_strategies(names):
    """Resolve strategy names to callables; unknown names are skipped with a warning."""
    out = []
    for name in names:
        fn = REGISTRY.get(name)
        if fn is None:
            print(f"[strategy registry] unknown strategy '{name}', skipping")
            continue
        out.append((name, fn))
    return out
