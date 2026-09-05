import os
from datetime import datetime, timedelta, timezone
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from bot.errors import BrokerError

class AlpacaBroker:
    def __init__(self, api_key, secret_key):
        # Guard: ensure we are using paper trading endpoint
        self.trading_client = TradingClient(api_key, secret_key, paper=True)
        self.data_client = CryptoHistoricalDataClient()

    def get_crypto_bars(self, symbol, timeframe, limit):
        """
        Fetch historical crypto bars for a given symbol.
        :param symbol: e.g., "BTC/USD"
        :param timeframe: TimeFrame object (e.g., TimeFrame(15, TimeFrameUnit.Minute))
        :param limit: number of bars to fetch
        :return: pandas DataFrame of bars
        """
        try:
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