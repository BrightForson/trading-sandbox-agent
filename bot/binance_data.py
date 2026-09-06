"""Binance market data: keyless public klines via data-api.binance.vision.

The market-data half of the broker seam. No API keys, no signing, no
geo-restricted endpoints — the same free data Binance publishes for
backtesting. Provides get_crypto_bars() with the Alpaca call shape
(symbol like "BTC/USD", timeframe objects, limit), returning a DataFrame
with the columns the strategies already expect.
"""
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

from bot.errors import BrokerError

BASE_URL = "https://data-api.binance.vision/api/v3"

SYMBOL_MAP = {"BTC/USD": "BTCUSDT", "ETH/USD": "ETHUSDT", "SOL/USD": "SOLUSDT"}

_INTERVALS = {"15Min": "15m", "1Day": "1d"}
_INTERVAL_MINUTES = {"15m": 15, "1d": 1440}


def _interval_for(timeframe):
    """Map any timeframe-like input to a Binance interval string."""
    if timeframe is None:
        return "15m"
    if isinstance(timeframe, str):
        if timeframe in _INTERVALS:
            return _INTERVALS[timeframe]
        if timeframe in _INTERVAL_MINUTES:
            return timeframe
        if timeframe.endswith("Min"):
            return f"{int(timeframe[:-3])}m"
        if timeframe.endswith("Hour"):
            return f"{int(timeframe[:-4]) * 60}m"
        if timeframe in ("1Day", "Day", "1d", "d"):
            return "1d"
        return "15m"
    minutes = getattr(timeframe, "value_count", None)
    if minutes is not None:
        try:
            minutes = int(minutes)
            unit = getattr(timeframe, "timeframe_unit", None)
            from alpaca.data.enums import TimeFrameUnit as TFU
            if unit == TFU.Hour:
                minutes *= 60
            elif unit == TFU.Day:
                minutes *= 1440
        except Exception:
            pass
        if minutes >= 1440:
            return "1d"
        if minutes >= 60:
            return f"{minutes // 60}h"
        return f"{minutes}m"
    return "15m"


def interval_minutes(interval):
    return _INTERVAL_MINUTES.get(interval, 15)


def to_binance_symbol(symbol):
    return SYMBOL_MAP.get(symbol, symbol.replace("/", ""))


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
    """Drop-in market-data adapter shaped like the Alpaca broker's data half.

    get_crypto_bars(symbol, timeframe, limit) -> DataFrame indexed by
    UTC timestamp with open/high/low/close/volume, newest last — exactly
    what get_strategies()/simulate() consume. Binance also includes the
    still-forming candle as the last row (like Alpaca), and callers
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

    def fetch_history(self, symbol, days, interval="15m"):
        """Deep history for backtests: page backward until `days` covered.

        Binance serves 1000 klines/request ending at endTime; we walk the
        endTime back until the requested window is filled.
        """
        iv = interval_minutes(interval) * 60_000
        end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        start_ms = end_ms - int(days) * 86_400_000
        frames = []
        cursor = end_ms
        for _ in range(200):
            if cursor <= start_ms:
                break
            df = get_klines(symbol, interval=interval, limit=1000, end_time=cursor)
            if df is None or df.empty:
                break
            first_ms = int(df.index[0].timestamp() * 1000)
            frames.append(df)
            if first_ms >= cursor:
                break
            cursor = first_ms
        if not frames:
            return None
        out = pd.concat(frames)
        out = out[~out.index.duplicated(keep="last")].sort_index()
        out = out[(out.index >= pd.Timestamp(start_ms, unit="ms", tz="UTC"))
                  & (out.index < pd.Timestamp(end_ms, unit="ms", tz="UTC"))]
        # drop the still-forming bar
        if not out.empty:
            out = out.iloc[:-1]
        return out
