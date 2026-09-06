import os
import time
from datetime import datetime, timedelta, timezone
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from bot.errors import BrokerError


def make_broker(cfg):
    """Build the configured broker adapter from config.yaml `broker.name`."""
    broker_cfg = getattr(cfg, "broker", None) or {}
    name = str(broker_cfg.get("name", "alpaca")).lower()
    if name == "binance_paper":
        from bot.binance_paper import BinancePaperBroker
        return BinancePaperBroker(cfg)
    if name == "alpaca":
        cfg.require("ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY")
        return AlpacaBroker(cfg.alpaca_api_key_id, cfg.alpaca_api_secret_key)
    raise BrokerError(f"Unknown broker '{name}' (expected alpaca or binance_paper)")

class AlpacaBroker:
    def __init__(self, api_key, secret_key):
        # Guard: ensure we are using paper trading endpoint
        self.trading_client = TradingClient(api_key, secret_key, paper=True)
        self.data_client = CryptoHistoricalDataClient()

    def get_crypto_bars(self, symbol, timeframe, limit):
        """
        Fetch historical crypto bars for a given symbol.
        :param symbol: e.g., "BTC/USD"
        :param timeframe: neutral bot.timeframe object or Alpaca TimeFrame
        :param limit: number of bars to fetch
        :return: pandas DataFrame of bars
        """
        try:
            timeframe = self._to_alpaca_timeframe(timeframe)
            # Explicit start/end window: a bare limit request returns far fewer
            # bars than requested (free-tier paging quirk), which silently
            # starves the SMA calculations.
            minutes = self._timeframe_minutes(timeframe)
            end = datetime.now(timezone.utc)
            start = end - timedelta(minutes=minutes * (limit + 10))
            request_params = CryptoBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=timeframe,
                start=start,
                end=end,
                limit=1000
            )
            bars = self.data_client.get_crypto_bars(request_params)
            return bars.df
        except Exception as e:
            raise BrokerError(f"Failed to fetch bars for {symbol}: {e}")

    @staticmethod
    def _to_alpaca_timeframe(timeframe):
        """Accept neutral bot.timeframe objects or pass Alpaca ones through."""
        from bot.timeframe import Minutes, Hours, Days
        if isinstance(timeframe, Minutes):
            return TimeFrame(timeframe.value_count, TimeFrameUnit.Minute)
        if isinstance(timeframe, Hours):
            return TimeFrame(timeframe.value_count // 60, TimeFrameUnit.Hour)
        if isinstance(timeframe, Days):
            return TimeFrame(max(1, timeframe.value_count // 1440), TimeFrameUnit.Day)
        return timeframe

    @staticmethod
    def _timeframe_minutes(timeframe):
        unit = getattr(timeframe, "timeframe_unit", None) or getattr(timeframe, "unit_value", None)
        val = getattr(timeframe, "value_count", None)
        try:
            from alpaca.data.enums import TimeFrameUnit as TFU
            if timeframe.timeframe_unit == TFU.Minute:
                return int(timeframe.value_count)
            if timeframe.timeframe_unit == TFU.Hour:
                return int(timeframe.value_count) * 60
            if timeframe.timeframe_unit == TFU.Day:
                return int(timeframe.value_count) * 1440
        except Exception:
            pass
        return 15  # conservative default: assume 15-minute bars

    def place_order(self, symbol, qty, side):
        """
        Place a market order.
        :param symbol: e.g., "BTC/USD"
        :param qty: quantity to buy/sell
        :param side: OrderSide.BUY or OrderSide.SELL
        :return: order object
        """
        try:
            order_side = OrderSide.BUY if str(side).upper() == "BUY" else OrderSide.SELL
            market_order_data = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=order_side,
                time_in_force=TimeInForce.GTC
            )
            order = self.trading_client.submit_order(order_data=market_order_data)
            return order
        except Exception as e:
            raise BrokerError(f"Failed to place order for {symbol}: {e}")

    def await_terminal_order(self, order_id, timeout_seconds=15):
        """Poll a paper order briefly and return its broker-confirmed terminal state."""
        terminal = {"filled", "canceled", "cancelled", "expired", "rejected", "done_for_day"}
        deadline = time.monotonic() + max(1, timeout_seconds)
        last = None
        while time.monotonic() < deadline:
            try:
                last = self.trading_client.get_order_by_id(order_id)
            except Exception as e:
                # transient read errors: keep polling until the deadline
                print(f"[broker] transient error reading order {order_id}: {e}")
                time.sleep(1)
                continue
            raw_status = getattr(last, "status", "")
            status = str(getattr(raw_status, "value", raw_status)).lower()
            if status in terminal:
                return last
            time.sleep(1)
        return last

    def get_account(self):
        return self.trading_client.get_account()

    def get_all_positions(self):
        return list(self.trading_client.get_all_positions())

    def get_position(self, symbol):
        """
        Get current position for a symbol.
        Crypto positions are stored without the slash (ETH/USD -> ETHUSD),
        so both formats are tried.
        :return: position object or None if no position
        """
        for candidate in (symbol, symbol.replace("/", "")):
            try:
                return self.trading_client.get_open_position(candidate)
            except Exception as e:
                msg = str(e).lower()
                if "position does not exist" in msg or "not found" in msg or "404" in msg:
                    continue
                raise BrokerError(f"Failed to get position for {symbol}: {e}")
        return None
