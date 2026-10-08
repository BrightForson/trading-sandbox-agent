"""Binance market data: keyless public klines via data-api.binance.vision.

No API keys, no signing, no geo-restricted endpoints — the same free
data Binance publishes for backtesting. Provides get_crypto_bars()
(symbol like "BTC/USD", timeframe objects, limit), returning an OHLCV
DataFrame.
"""
import time

import pandas as pd
import requests

from bot.errors import BrokerError

BASE_URL = "https://data-api.binance.vision/api/v3"

SYMBOL_MAP = {"BTC/USD": "BTCUSDT", "ETH/USD": "ETHUSDT", "SOL/USD": "SOLUSDT"}

_INTERVALS = {"15Min": "15m", "1Day": "1d"}
# Binance interval -> minutes. Used for lookback maths, so it must know every
# interval the mapper can emit; the old dict knew only 15m/1d and answered 15
# for everything else.
_INTERVAL_MINUTES = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "6h": 360, "8h": 480, "12h": 720,
    "1d": 1440, "3d": 4320, "1w": 10080,
}
_ALIASES = {
    "min": "Min", "minute": "Min", "hour": "Hour", "day": "Day", "d": "Day",
    "m": "Min", "h": "Hour",
}


def _interval_for(timeframe):
    """Map any timeframe-like input to a Binance interval string.

    IDEMPOTENT by construction: an already-normalized Binance interval
    ("1h", "4h", "1d") maps to itself, so passing the result through again is
    a no-op. The old version collapsed anything it did not explicitly know
    back to "15m", and binance_paper applies it twice, so "1Hour" became "1h"
    on the first pass and then "15m" on the second — the strategy silently
    fetched 15m bars whatever the config said. sma_slow=50 against 15m bars
    means check_crossover returns [] and the bot never trades, with no error.

    An unrecognised spec raises rather than defaulting, because a silent
    wrong-timeframe fallback produces plausible-looking output for the wrong
    data.
    """
    if timeframe is None:
        return "15m"
    if isinstance(timeframe, str):
        spec = timeframe.strip()
        if not spec:
            raise ValueError("empty timeframe")
        low = spec.lower()
        # already a Binance interval
        if low in _INTERVAL_MINUTES:
            return low
        # case-insensitive known specs, with and without a leading number
        for known, norm in _INTERVALS.items():
            if low == known.lower():
                return norm
        unit = None
        for suffix, name in _ALIASES.items():
            if low.endswith(suffix):
                unit = name
                spec = spec[: len(spec) - len(suffix)].strip()
                break
        if unit is None:
            for name in ("Min", "Hour", "Day", "Week"):
                if low.endswith(name.lower()):
                    unit = name
                    spec = spec[: len(spec) - len(name)].strip()
                    break
        if unit is not None:
            amount = int(spec) if spec else 1
            if amount <= 0:
                raise ValueError(f"non-positive timeframe: {timeframe!r}")
            total = amount * {"Min": 1, "Hour": 60, "Day": 1440, "Week": 10080}[unit]
            if total >= 10080:
                return "1w"
            if total >= 1440:
                return "1d"
            if total >= 60:
                return f"{total // 60}h"
            return f"{total}m"
        raise ValueError(f"unrecognized timeframe: {timeframe!r}")
    # bot.timeframe objects expose value_count already in minutes
    value_count = getattr(timeframe, "value_count", None)
    if value_count is not None:
        try:
            return _interval_for(f"{int(value_count)}m")
        except (TypeError, ValueError):
            raise ValueError(f"unrecognized timeframe: {timeframe!r}")
    # other timeframe objects expose amount + unit
    raw_amount = getattr(timeframe, "amount", None)
    unit = getattr(timeframe, "unit", None)
    unit_name = getattr(unit, "name", None) or str(unit or "")
    if raw_amount is not None and unit_name:
        try:
            amount = int(raw_amount)
        except (TypeError, ValueError):
            raise ValueError(f"unrecognized timeframe amount: {raw_amount!r}")
        _SCALE = {"Minute": 1, "Hour": 60, "Day": 1440, "Week": 10080}
        scale = _SCALE.get(unit_name) or _SCALE.get(unit_name.capitalize())
        if scale is None:
            try:
                scale = _SCALE[str(int(unit)).capitalize()]
            except (TypeError, ValueError, KeyError):
                raise ValueError(f"unrecognized timeframe unit: {unit!r}")
        return _interval_for(f"{amount}{unit_name}")
    raise ValueError(f"unrecognized timeframe: {timeframe!r}")


def interval_minutes(interval):
    """Minutes per bar. Raises for an unknown interval rather than guessing."""
    key = str(interval).lower()
    if key not in _INTERVAL_MINUTES:
        raise ValueError(f"unknown interval: {interval!r}")
    return _INTERVAL_MINUTES[key]


def to_binance_symbol(symbol):
    # Binance spot quotes against USDT: any "BASE/USD" becomes "BASEUSDT".
    # Slash-less inputs (e.g. memecoin tickers priced via CoinGecko instead)
    # pass through untouched.
    if symbol in SYMBOL_MAP:
        return SYMBOL_MAP[symbol]
    if symbol.endswith("/USD"):
        return symbol[:-4] + "USDT"
    return symbol.replace("/", "")


def get_klines(symbol, interval="15m", limit=500, end_time=None):
    """Fetch closed klines for a Binance symbol string (e.g. BTCUSDT).

    Binance returns the in-progress candle too; the caller drops it the
    same way the live loop always has.
    """
    params = {"symbol": to_binance_symbol(symbol), "interval": interval,
              "limit": min(1000, max(1, int(limit)))}
    if end_time is not None:
        params["endTime"] = int(end_time)
    last_err = None
    for attempt in range(3):
        try:
            r = requests.get(f"{BASE_URL}/klines", params=params, timeout=15)
            if r.status_code == 429 or r.status_code == 418:
                retry = float(r.headers.get("retry-after", 2 ** attempt))
                time.sleep(min(30, max(1, retry)))
                last_err = f"rate limited (HTTP {r.status_code}) on attempt {attempt + 1}"
                continue
            r.raise_for_status()
            rows = r.json()
            cols = ["open_time", "open", "high", "low", "close", "volume",
                    "close_time", "qav", "trades", "tbb", "tbq", "ignore"]
            df = pd.DataFrame(rows, columns=cols)
            for c in ("open", "high", "low", "close", "volume"):
                df[c] = df[c].astype(float)
            df.index = pd.to_datetime(df["open_time"], unit="ms", utc=True)
            df.index.name = "timestamp"
            return df.drop(columns=["open_time", "close_time", "qav",
                                    "trades", "tbb", "tbq", "ignore"])
        except BrokerError:
            raise
        except Exception as e:
            last_err = e
            time.sleep(1 + attempt)
    raise BrokerError(f"Failed to fetch klines for {symbol}: {last_err}")


class BinanceDataClient:
    """Market-data client for the agent, shadow account, gates and chat.

    get_crypto_bars(symbol, timeframe, limit) -> DataFrame indexed by
    UTC timestamp with open/high/low/close/volume, newest last. Binance
    also includes the still-forming candle as the last row, and callers
    already drop it.
    """

    def __init__(self, cfg=None):
        self.cfg = cfg

    def get_crypto_bars(self, symbol, timeframe, limit):
        interval = _interval_for(timeframe)
        df = get_klines(symbol, interval=interval, limit=limit)
        if df is None or df.empty:
            raise BrokerError(f"No bars returned for {symbol}")
        return df

    def last_close(self, symbol, interval="15m", limit=2):
        """Last CLOSED candle close (drops the forming candle)."""
        df = get_klines(symbol, interval=interval, limit=limit)
        if df is None or df.empty:
            return None
        return float(df["close"].iloc[-2])
