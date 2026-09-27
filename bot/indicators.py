"""Deterministic indicator helpers shared across tiers.

Pure functions on close series (list or pandas Series) — no side effects,
no I/O. Everything returns None when data is insufficient so callers can
pass nulls straight into LLM dossiers (fail-open for information, the
risk rails stay fail-closed elsewhere).
"""
import math


def _coerce(closes):
    """Floats from a close series, or None if any value is unusable.

    The module contract is that insufficient or malformed data yields None so
    callers can pass it straight into an LLM dossier. A None or non-numeric
    element used to raise, and a NaN flowed through every computation.
    """
    if closes is None:
        return None
    try:
        vals = [float(c) for c in closes]
    except (TypeError, ValueError):
        return None
    if any(v != v for v in vals):  # NaN
        return None
    return vals


def rsi(closes, period=14):
    """Wilder-smoothed RSI. None when fewer than period+1 closes."""
    vals = _coerce(closes)
    if vals is None:
        return None
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


PERIODS_PER_YEAR = {
    "1m": 525600, "5m": 105120, "15m": 35040, "30m": 17520,
    "1h": 8760, "4h": 2190, "1d": 365, "1w": 52,
}


def realized_volatility_pct(closes, lookback=None, periods_per_year=None):
    """Annualized realized volatility (%) from log returns.

    `periods_per_year` scales the per-bar standard deviation to an annual
    figure; it is REQUIRED for the result to mean what the name says. Omitting
    it returns a per-bar number, which for daily data is understated by a
    factor of sqrt(365) -- about 19x. There is no safe default: the same
    series means different things at different sampling rates.

    None when fewer than 3 closes, any non-positive price, or when
    periods_per_year is not supplied.
    """
    if closes is None or not periods_per_year:
        return None
    vals = _coerce(closes)
    if vals is None:
        return None
    vals = vals[-(lookback or len(vals)):]
    if len(vals) < 3 or any(v <= 0 for v in vals):
        return None
    rets = [math.log(vals[i + 1] / vals[i]) for i in range(len(vals) - 1)]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(float(periods_per_year)) * 100.0
