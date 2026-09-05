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
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS meta (
                        key TEXT PRIMARY KEY,
                        value TEXT
                    )
                """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS proposals (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT NOT NULL,
                        source TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        symbol TEXT,
                        action TEXT NOT NULL,
                        notional REAL,
                        confidence REAL,
                        rationale TEXT,
                        exec_status TEXT NOT NULL DEFAULT 'shadow',
                        exec_timestamp TEXT,
                        exec_price REAL,
                        simulated_pnl REAL
                    )
                """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS bets (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT NOT NULL,
                        market TEXT NOT NULL,
                        question TEXT,
                        side TEXT NOT NULL,
                        price REAL NOT NULL,
                        stake REAL NOT NULL,
                        outcome TEXT NOT NULL DEFAULT 'open',
                        payout REAL,
                        notes TEXT
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

    def log_proposal(self, timestamp, source, kind, symbol, action, notional,
                     confidence, rationale, exec_status="shadow"):
        """Log an AI agent proposal (shadow mode default)."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO proposals (timestamp, source, kind, symbol, action,
                                           notional, confidence, rationale, exec_status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (timestamp, source, kind, symbol, action, notional,
                      confidence, rationale, exec_status))
                conn.commit()
        except Exception as e:
            raise JournalError(f"Failed to log proposal: {e}")

    def get_proposals(self, symbol=None, kind=None, limit=None):
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                q = "SELECT * FROM proposals WHERE 1=1"
                params = []
                if symbol:
                    q += " AND symbol=?"
                    params.append(symbol)
                if kind:
                    q += " AND kind=?"
                    params.append(kind)
                q += " ORDER BY timestamp DESC"
                if limit:
                    q += " LIMIT ?"
                    params.append(limit)
                cursor.execute(q, params)
                return cursor.fetchall()
        except Exception as e:
            raise JournalError(f"Failed to retrieve proposals: {e}")

    def update_proposal_exec(self, proposal_id, exec_status, exec_timestamp=None,
                             exec_price=None, simulated_pnl=None):
        """Mark what happened to a proposal (e.g., paper-executed outcome)."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE proposals SET exec_status=?, exec_timestamp=?, exec_price=?, simulated_pnl=?
                    WHERE id=?
                """, (exec_status, exec_timestamp, exec_price, simulated_pnl, proposal_id))
                conn.commit()
        except Exception as e:
            raise JournalError(f"Failed to update proposal: {e}")

    def log_bet(self, timestamp, market, question, side, price, stake, outcome="open",
                payout=None, notes=None):
        """Log a paper prediction-market bet."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO bets (timestamp, market, question, side, price, stake, outcome, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (timestamp, market, question, side, price, stake, outcome, notes))
                conn.commit()
        except Exception as e:
            raise JournalError(f"Failed to log bet: {e}")

    def update_bet(self, bet_id, outcome, payout):
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE bets SET outcome=?, payout=? WHERE id=?
                """, (outcome, payout, bet_id))
                conn.commit()
        except Exception as e:
            raise JournalError(f"Failed to update bet: {e}")

    def get_open_bets(self):
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM bets WHERE outcome='open'")
                return cursor.fetchall()
        except Exception as e:
            raise JournalError(f"Failed to retrieve open bets: {e}")
    
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
    
    def get_meta(self, key):
        """Get a meta value by key, or None if not set."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT value FROM meta WHERE key=?", (key,))
                row = cursor.fetchone()
                return row[0] if row else None
        except Exception as e:
            raise JournalError(f"Failed to get meta '{key}': {e}")
    
    def set_meta(self, key, value):
        """Set a meta value by key (upsert)."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                    (key, value)
                )
                conn.commit()
        except Exception as e:
            raise JournalError(f"Failed to set meta '{key}': {e}")
