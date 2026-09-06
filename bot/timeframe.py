"""Timeframe abstraction shared across broker adapters.

Consumers pass opaque timeframe objects to broker.get_crypto_bars() and
stay import-clean of any vendor SDK. AlpacaBroker converts to its
TimeFrame; BinanceDataClient converts to a Binance interval string.
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
    """Build a timeframe object from a config string like '15Min' or '1Day'."""
    if isinstance(spec, Minutes) or isinstance(spec, Hours) or isinstance(spec, Days):
        return spec
    s = str(spec)
    if s.endswith("Min"):
        return Minutes(int(s[:-3]))
    if s.endswith("Hour"):
        return Hours(int(s[:-4]))
    if s.endswith("Day"):
        return Days(int(s[:-3]) if s[:-3] else 1)
    return Minutes(15)
