"""Timeframe abstraction for market-data clients.

Consumers pass opaque timeframe objects to get_crypto_bars() and stay
import-clean of any vendor SDK. BinanceDataClient converts them to a
Binance interval string.
"""


class Minutes:
    def __init__(self, n):
        self.value_count = int(n)


class Hours:
    def __init__(self, n):
        self.value_count = int(n) * 60


class Days:
    def __init__(self, n=1):
        self.value_count = int(n) * 1440


class _TimeframeUnit:
    Minute = "minute"
    Hour = "hour"
    Day = "day"


def make_timeframe(spec):
    """Build a timeframe object from a config string like '15Min' or '1Day'.

    Unrecognised specs raise instead of falling back to 15 minutes. A config
    typo ('15min', '1h', '15MIN', 'hourly') used to silently produce 15m
    bars, so the bot computed SMAs over the wrong history, fetched the wrong
    window and reported P&L without error anywhere.
    """
    if isinstance(spec, Minutes) or isinstance(spec, Hours) or isinstance(spec, Days):
        return spec
    s = str(spec).strip()
    if not s:
        raise ValueError("empty timeframe")
    low = s.lower()
    for suffix, cls in (("min", Minutes), ("minute", Minutes),
                        ("hour", Hours), ("day", Days), ("week", Days),
                        ("m", Minutes), ("h", Hours), ("d", Days), ("w", Days)):
        if low.endswith(suffix):
            head = s[: len(s) - len(suffix)].strip()
            if not head:
                return cls(1)
            try:
                n = int(head)
            except ValueError:
                raise ValueError(f"unrecognized timeframe: {spec!r}")
            if n <= 0:
                raise ValueError(f"non-positive timeframe: {spec!r}")
            return cls(n)
    raise ValueError(f"unrecognized timeframe: {spec!r}")
