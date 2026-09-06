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
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS wallet_snapshots (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT NOT NULL,
                        epoch INTEGER NOT NULL DEFAULT 1,
                        cash REAL NOT NULL,
                        locked REAL NOT NULL,
                        equity REAL NOT NULL
                    )
                """)
                self._ensure_columns(cursor, "trades", {
                    "fee": "REAL NOT NULL DEFAULT 0",
                    "order_id": "TEXT",
                    "status": "TEXT NOT NULL DEFAULT 'filled'",
                })
                self._ensure_columns(cursor, "proposals", {
                    "context_json": "TEXT",
                    "entry_price": "REAL",
                    "btc_entry_price": "REAL",
                    "expiry_timestamp": "TEXT",
                    "closed_price": "REAL",
                    "benchmark_return": "REAL",
                })
                self._ensure_columns(cursor, "bets", {
                    "fee": "REAL NOT NULL DEFAULT 0",
                    "estimated_probability": "REAL",
                    "expected_value": "REAL",
                })
                conn.commit()
        except Exception as e:
            raise JournalError(f"Failed to initialize database: {e}")

    @staticmethod
    def _ensure_columns(cursor, table, columns):
        existing = {row[1] for row in cursor.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in existing:
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    
    def log_trade(self, timestamp, symbol, action, qty, price, reasoning, fee=0.0,
                  order_id=None, status="filled"):
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
                    INSERT INTO trades (timestamp, symbol, action, qty, price, reasoning, fee, order_id, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (timestamp, symbol, action, qty, price, reasoning, fee, order_id, status))
                conn.commit()
        except Exception as e:
            raise JournalError(f"Failed to log trade: {e}")

    def log_proposal(self, timestamp, source, kind, symbol, action, notional,
                     confidence, rationale, exec_status="shadow", context_json=None,
                     entry_price=None, btc_entry_price=None, expiry_timestamp=None):
        """Log an AI agent proposal (shadow mode default)."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO proposals (timestamp, source, kind, symbol, action,
                                           notional, confidence, rationale, exec_status, context_json,
                                           entry_price, btc_entry_price, expiry_timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (timestamp, source, kind, symbol, action, notional,
                      confidence, rationale, exec_status, context_json, entry_price,
                      btc_entry_price, expiry_timestamp))
                conn.commit()
                return cursor.lastrowid
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
                             exec_price=None, simulated_pnl=None, closed_price=None,
                             benchmark_return=None):
        """Mark what happened to a proposal (e.g., paper-executed outcome)."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE proposals SET exec_status=?, exec_timestamp=?, exec_price=?, simulated_pnl=?,
                    closed_price=?, benchmark_return=?
                    WHERE id=?
                """, (exec_status, exec_timestamp, exec_price, simulated_pnl,
                      closed_price, benchmark_return, proposal_id))
                conn.commit()
        except Exception as e:
            raise JournalError(f"Failed to update proposal: {e}")

    def log_bet(self, timestamp, market, question, side, price, stake, outcome="open",
                payout=None, notes=None, fee=0.0, estimated_probability=None,
                expected_value=None):
        """Log a paper prediction-market bet."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO bets (timestamp, market, question, side, price, stake, outcome, notes,
                                      fee, estimated_probability, expected_value)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (timestamp, market, question, side, price, stake, outcome, notes, fee,
                      estimated_probability, expected_value))
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

    def has_open_bet(self, market, side):
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT 1 FROM bets WHERE outcome='open' AND market=? AND side=? LIMIT 1",
                    (market, side),
                ).fetchone()
                return row is not None
        except Exception as e:
            raise JournalError(f"Failed to check open bet: {e}")

    def open_bet_exposure(self):
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute("SELECT COALESCE(SUM(stake + fee), 0) FROM bets WHERE outcome='open'").fetchone()
                return float(row[0] or 0.0)
        except Exception as e:
            raise JournalError(f"Failed to calculate open bet exposure: {e}")

    def get_open_proposals(self):
        """Return actionable agent proposals awaiting their fixed evaluation time."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, timestamp, symbol, action, notional, confidence, rationale,
                           entry_price, btc_entry_price, expiry_timestamp
                    FROM proposals
                    WHERE source='ai_agent' AND exec_status='open'
                    ORDER BY timestamp ASC
                """)
                return cursor.fetchall()
        except Exception as e:
            raise JournalError(f"Failed to retrieve open proposals: {e}")

    def proposal_scorecard(self):
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute("""
                    SELECT COUNT(*),
                           COALESCE(SUM(simulated_pnl), 0),
                           AVG(benchmark_return),
                           SUM(CASE WHEN simulated_pnl > 0 THEN 1 ELSE 0 END)
                    FROM proposals
                    WHERE source='ai_agent' AND exec_status='evaluated'
                """).fetchone()
                total, pnl, benchmark, wins = row
                return {
                    "evaluated": int(total or 0),
                    "net_pnl": float(pnl or 0.0),
                    "avg_benchmark_return_pct": float(benchmark or 0.0) * 100,
                    "win_rate_pct": (float(wins or 0) / total * 100) if total else 0.0,
                }
        except Exception as e:
            raise JournalError(f"Failed to build proposal scorecard: {e}")

    def bet_scorecard(self):
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute("""
                    SELECT SUM(CASE WHEN outcome IN ('won', 'lost') THEN 1 ELSE 0 END),
                           SUM(CASE WHEN outcome IN ('won', 'lost') THEN payout - stake - fee ELSE 0 END),
                           SUM(CASE WHEN outcome='won' THEN 1 ELSE 0 END)
                    FROM bets
                """).fetchone()
                total, pnl, wins = row
                return {
                    "settled": int(total or 0),
                    "net_pnl": float(pnl or 0.0),
                    "win_rate_pct": (float(wins or 0) / total * 100) if total else 0.0,
                    "open_exposure": self.open_bet_exposure(),
                }
        except Exception as e:
            raise JournalError(f"Failed to build bet scorecard: {e}")
    
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

    def get_all_bets(self):
        """All bets across every outcome, oldest first."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM bets ORDER BY timestamp ASC")
                return cursor.fetchall()
        except Exception as e:
            raise JournalError(f"Failed to retrieve bets: {e}")

    def log_wallet_snapshot(self, timestamp, epoch, cash, locked, equity):
        """Persist one wallet valuation point for trend reporting."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO wallet_snapshots (timestamp, epoch, cash, locked, equity)
                    VALUES (?, ?, ?, ?, ?)
                """, (timestamp, epoch, cash, locked, equity))
                conn.commit()
        except Exception as e:
            raise JournalError(f"Failed to log wallet snapshot: {e}")

    def get_wallet_snapshots(self, epoch=None, limit=None):
        """Wallet valuation history, oldest first, optionally per epoch."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                q = "SELECT * FROM wallet_snapshots"
                params = []
                if epoch is not None:
                    q += " WHERE epoch=?"
                    params.append(epoch)
                q += " ORDER BY timestamp DESC"
                if limit:
                    q += " LIMIT ?"
                    params.append(limit)
                cursor.execute(q, params)
                return cursor.fetchall()[::-1]
        except Exception as e:
            raise JournalError(f"Failed to retrieve wallet snapshots: {e}")
