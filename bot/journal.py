import sqlite3
import os
from datetime import datetime
from bot.errors import JournalError

class TradeJournal:
    def __init__(self, db_path="data/trades.db"):
        # Ensure data directory exists
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db_path = db_path
        self._init_db()
    
    def _init_db(self):
        """Initialize the database and create table if not exists."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS trades (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        action TEXT NOT NULL,
                        qty REAL NOT NULL,
                        price REAL NOT NULL,
                        reasoning TEXT
                    )
                """)
                conn.commit()
        except Exception as e:
            raise JournalError(f"Failed to initialize database: {e}")
    
    def log_trade(self, timestamp, symbol, action, qty, price, reasoning):
        """
        Log a trade to the journal.
        :param timestamp: ISO format string
        :param symbol: e.g., "BTC/USD"
        :param action: "BUY" or "SELL"
        :param qty: quantity
        :param price: price per unit
        :param reasoning: string explaining the trade
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO trades (timestamp, symbol, action, qty, price, reasoning)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (timestamp, symbol, action, qty, price, reasoning))
                conn.commit()
        except Exception as e:
            raise JournalError(f"Failed to log trade: {e}")
    
    def get_trades(self, symbol=None, limit=None):
        """
        Retrieve trades from the journal.
        :param symbol: optional symbol to filter
        :param limit: optional limit on number of rows
        :return: list of tuples (id, timestamp, symbol, action, qty, price, reasoning)
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                if symbol:
                    if limit:
                        cursor.execute("""
                            SELECT * FROM trades WHERE symbol=? ORDER BY timestamp DESC LIMIT ?
                        """, (symbol, limit))
                    else:
                        cursor.execute("""
                            SELECT * FROM trades WHERE symbol=? ORDER BY timestamp DESC
                        """, (symbol,))
                else:
                    if limit:
                        cursor.execute("""
                            SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?
                        """, (limit,))
                    else:
                        cursor.execute("""
                            SELECT * FROM trades ORDER BY timestamp DESC
                        """)
                return cursor.fetchall()
        except Exception as e:
            raise JournalError(f"Failed to retrieve trades: {e}")