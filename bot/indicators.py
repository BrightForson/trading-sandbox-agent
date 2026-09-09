"""Deterministic indicator helpers shared across tiers.

Pure functions on close series (list or pandas Series) — no side effects,
no I/O. Everything returns None when data is insufficient so callers can
pass nulls straight into LLM dossiers (fail-open for information, the
risk rails stay fail-closed elsewhere).
"""
import math


def rsi(closes, period=14):
    """Wilder-smoothed RSI. None when fewer than period+1 closes."""
    if closes is None:
        return None
    vals = [float(c) for c in closes]
    if len(vals) < period + 1:
        return None
    deltas = [vals[i + 1] - vals[i] for i in range(len(vals) - 1)]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else None
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def realized_volatility_pct(closes, lookback=None):
    """Annualized realized volatility (%) from log returns.

    None when fewer than 3 closes or any non-positive price.
    """
    if closes is None:
        return None
    vals = [float(c) for c in closes][-(lookback or len(closes)):]
    if len(vals) < 3 or any(v <= 0 for v in vals):
        return None
    rets = [math.log(vals[i + 1] / vals[i]) for i in range(len(vals) - 1)]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * 100.0
