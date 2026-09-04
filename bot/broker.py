import os
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
            request_params = CryptoBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=timeframe,
                limit=limit
            )
            bars = self.data_client.get_crypto_bars(request_params)
            return bars.df
        except Exception as e:
            raise BrokerError(f"Failed to fetch bars for {symbol}: {e}")

    def place_order(self, symbol, qty, side):
        """
        Place a market order.
        :param symbol: e.g., "BTC/USD"
        :param qty: quantity to buy/sell
        :param side: OrderSide.BUY or OrderSide.SELL
        :return: order object
        """
        try:
            market_order_data = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=side,
                time_in_force=TimeInForce.GTC
            )
            order = self.trading_client.submit_order(order_data=market_order_data)
            return order
        except Exception as e:
            raise BrokerError(f"Failed to place order for {symbol}: {e}")

    def get_position(self, symbol):
        """
        Get current position for a symbol.
        :return: position object or None if no position
        """
        try:
            position = self.trading_client.get_open_position(symbol)
            return position
        except Exception as e:
            # If the position does not exist, Alpaca throws an error. We return None.
            if "position does not exist" in str(e):
                return None
            else:
                raise BrokerError(f"Failed to get position for {symbol}: {e}")