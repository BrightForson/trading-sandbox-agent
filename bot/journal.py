import sqlite3
import os
from contextlib import contextmanager
from datetime import datetime
from bot.errors import JournalError

# Every writer here is a short single-statement transaction against a file that
# ~30 TradeJournal() constructions per run re-open, and the parallel workflows
# merge it as one self-contained git binary. That last constraint is why
# journal_mode stays DELETE: WAL would leave the newest rows in trades.db-wal
# until a checkpoint, and tools/safe_commit.sh stages data/trades.db alone and
# resolves conflicts by extracting the :1:/:2:/:3: stages of that one file, so a
# -wal sidecar would commit a stale journal and defeat the union merge.
_BUSY_TIMEOUT_S = 30.0

# Legal identifiers for _ensure_columns, which interpolates both a table name and
# a free-form type definition into DDL. Every current caller passes a literal, so
# this is a trap for the next caller rather than a live injection, but the
# definition half is the dangerous one: a string carrying "); DROP TABLE ..." is
# just as valid as "REAL NOT NULL DEFAULT 0" to an f-string.
_SCHEMA_COLUMNS = {
    "trades": frozenset({
        "id", "timestamp", "symbol", "action", "qty", "price", "reasoning",
        "fee", "order_id", "status",
    }),
    "proposals": frozenset({
        "id", "timestamp", "source", "kind", "symbol", "action", "notional",
        "confidence", "rationale", "exec_status", "exec_timestamp",
        "exec_price", "simulated_pnl", "context_json", "entry_price",
        "btc_entry_price", "expiry_timestamp", "closed_price",
        "benchmark_return",
    }),
    "bets": frozenset({
        "id", "timestamp", "market", "question", "side", "price", "stake",
        "outcome", "payout", "notes", "fee", "estimated_probability",
        "expected_value",
    }),
}

_COLUMN_DEFINITIONS = frozenset({
    "TEXT", "REAL", "INTEGER",
    "TEXT NOT NULL DEFAULT 'filled'",
    "TEXT NOT NULL DEFAULT 'open'",
    "TEXT NOT NULL DEFAULT 'shadow'",
    "TEXT NOT NULL DEFAULT 'fresh'",
    "REAL NOT NULL DEFAULT 0",
})


class TradeJournal:
    def __init__(self, db_path="data/trades.db"):
        # Ensure data directory exists (bare filenames have no dirname)
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.db_path = db_path
        self._init_db()

    @contextmanager
    def _conn(self):
        """Commit/rollback like `with sqlite3.connect(...)` and also close.

        The `with` statement on a connection only ends a transaction; the handle
        itself survives until the GC runs, so the previous 23 call sites leaked a
        file descriptor each. Closing deterministically also means SQLite can
        release its locks at a predictable point rather than at collection.
        """
        conn = sqlite3.connect(self.db_path, timeout=_BUSY_TIMEOUT_S)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        """Initialize the database and create table if not exists."""
        try:
            with self._conn() as conn:
                cursor = conn.cursor()
                # Python's sqlite3 only auto-begins a transaction for DML, so
                # without this the six CREATE TABLEs and three ALTER TABLEs below
                # were eight independent autocommit write transactions each
                # taking an exclusive lock. _init_db runs on every one of the ~30
                # TradeJournal() constructions in a cycle, and a schema that dies
                # half-migrated leaves a table missing columns for good.
                cursor.execute("BEGIN")
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
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS tier4_cards (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        name TEXT,
                        detail TEXT,
                        status TEXT NOT NULL DEFAULT 'fresh'
                    )
                """)
                # get_open_proposals filters on (source, exec_status) and orders
                # by timestamp. With no index that plan is a full SCAN plus a temp
                # B-tree sort, and it runs twice per agent cycle. Cheap at 338
                # rows, quadratic in a table that only ever grows.
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_proposals_open
                    ON proposals(source, exec_status, timestamp)
                """)
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_proposals_symbol_status
                    ON proposals(symbol, source, exec_status)
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
        allowed = _SCHEMA_COLUMNS.get(table)
        if allowed is None:
            raise JournalError(f"Unknown table for column migration: {table!r}")
        existing = {row[1] for row in cursor.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            # Validate before short-circuiting on "already present", so an
            # unrecognised identifier or definition is rejected on every call
            # rather than only on the first migration that needed it.
            if name not in allowed:
                raise JournalError(
                    f"Refusing to add unknown column {table}.{name}"
                )
            if definition not in _COLUMN_DEFINITIONS:
                raise JournalError(
                    f"Refusing unrecognised column definition for "
                    f"{table}.{name}: {definition!r}"
                )
            if name in existing:
                continue
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
            with self._conn() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO trades (timestamp, symbol, action, qty, price, reasoning, fee, order_id, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (timestamp, symbol, action, qty, price, reasoning, fee, order_id, status))
                conn.commit()
                return cursor.lastrowid
        except Exception as e:
            raise JournalError(f"Failed to log trade: {e}")

    def log_proposal(self, timestamp, source, kind, symbol, action, notional,
                     confidence, rationale, exec_status="shadow", context_json=None,
                     entry_price=None, btc_entry_price=None, expiry_timestamp=None):
        """Log an AI agent proposal (shadow mode default)."""
        try:
            with self._conn() as conn:
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
            with self._conn() as conn:
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
            with self._conn() as conn:
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
            with self._conn() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO bets (timestamp, market, question, side, price, stake, outcome, notes,
                                      fee, estimated_probability, expected_value)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (timestamp, market, question, side, price, stake, outcome, notes, fee,
                      estimated_probability, expected_value))
                conn.commit()
                return cursor.lastrowid
        except Exception as e:
            raise JournalError(f"Failed to log bet: {e}")

    def update_bet(self, bet_id, outcome, payout):
        try:
            with self._conn() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE bets SET outcome=?, payout=? WHERE id=?
                """, (outcome, payout, bet_id))
                conn.commit()
        except Exception as e:
            raise JournalError(f"Failed to update bet: {e}")

    def get_open_bets(self):
        try:
            with self._conn() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM bets WHERE outcome='open'")
                return cursor.fetchall()
        except Exception as e:
            raise JournalError(f"Failed to retrieve open bets: {e}")

    def has_open_bet(self, market, side):
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT 1 FROM bets WHERE outcome='open' AND market=? AND side=? LIMIT 1",
                    (market, side),
                ).fetchone()
                return row is not None
        except Exception as e:
            raise JournalError(f"Failed to check open bet: {e}")

    def open_bet_exposure(self):
        try:
            with self._conn() as conn:
                row = conn.execute("SELECT COALESCE(SUM(stake + fee), 0) FROM bets WHERE outcome='open'").fetchone()
                return float(row[0] or 0.0)
        except Exception as e:
            raise JournalError(f"Failed to calculate open bet exposure: {e}")

    def get_open_proposals(self, limit=500):
        """Return actionable agent proposals awaiting their fixed evaluation time.

        Bounded because this is the only unbounded reader in the journal and it
        runs twice per agent cycle. The cap is safe for the evaluation path
        specifically because of the ASC ordering: the oldest rows are the most
        overdue, so truncating the tail drops the least urgent, never the ones
        whose evaluation window is being missed. It is NOT safe for the
        suppression check in agent.scout, which asks about one symbol and is
        served by get_open_proposal_for_symbol instead.
        """
        try:
            with self._conn() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, timestamp, symbol, action, notional, confidence, rationale,
                           entry_price, btc_entry_price, expiry_timestamp
                    FROM proposals
                    WHERE source='ai_agent' AND exec_status='open'
                    ORDER BY timestamp ASC
                    LIMIT ?
                """, (limit,))
                return cursor.fetchall()
        except Exception as e:
            raise JournalError(f"Failed to retrieve open proposals: {e}")

    def get_open_proposal_for_symbol(self, symbol):
        """Return open ai_agent proposals for one symbol, or [].

        Exists because the caller only ever wants an existence check per symbol.
        Filtering in SQL keeps the answer correct no matter how deep the open set
        grows, which a global LIMIT cannot promise.
        """
        try:
            with self._conn() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, timestamp, symbol, action, notional, confidence, rationale,
                           entry_price, btc_entry_price, expiry_timestamp
                    FROM proposals
                    WHERE source='ai_agent' AND exec_status='open' AND symbol=?
                    ORDER BY timestamp ASC
                """, (symbol,))
                return cursor.fetchall()
        except Exception as e:
            raise JournalError(f"Failed to retrieve open proposals for {symbol}: {e}")

    def proposal_scorecard(self):
        try:
            with self._conn() as conn:
                row = conn.execute("""
                    SELECT COUNT(*),
                           COALESCE(SUM(simulated_pnl), 0),
                           AVG(benchmark_return),
                           SUM(CASE WHEN simulated_pnl > 0 THEN 1 ELSE 0 END),
                           AVG(CASE WHEN entry_price > 0 AND closed_price > 0
                                    THEN (closed_price - entry_price) / entry_price END)
                    FROM proposals
                    WHERE source='ai_agent' AND exec_status='evaluated'
                """).fetchone()
                total, pnl, benchmark, wins, avg_return = row
                return {
                    "evaluated": int(total or 0),
                    "net_pnl": float(pnl or 0.0),
                    "avg_benchmark_return_pct": float(benchmark or 0.0) * 100,
                    "avg_return_pct": float(avg_return or 0.0) * 100,
                    "win_rate_pct": (float(wins or 0) / total * 100) if total else 0.0,
                }
        except Exception as e:
            raise JournalError(f"Failed to build proposal scorecard: {e}")

    def bet_scorecard(self):
        try:
            with self._conn() as conn:
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
            with self._conn() as conn:
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
            with self._conn() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT value FROM meta WHERE key=?", (key,))
                row = cursor.fetchone()
                return row[0] if row else None
        except Exception as e:
            raise JournalError(f"Failed to get meta '{key}': {e}")
    
    def set_meta(self, key, value):
        """Set a meta value by key (upsert)."""
        try:
            with self._conn() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                    (key, value)
                )
                conn.commit()
        except Exception as e:
            raise JournalError(f"Failed to set meta '{key}': {e}")

    def set_meta_many(self, pairs):
        """Upsert several meta keys in ONE transaction.

        Every tier ledger is a cash key and a positions key written together, and
        set_meta opens its own connection and commits per call. So a two-key save
        was two transactions: a crash between them left cash debited with the
        positions unchanged, which reads as free money rather than as corruption.
        The cash and positions keys of a tier must never be observed disagreeing,
        so anything that saves both goes through here.

        pairs: iterable of (key, value).
        """
        pairs = list(pairs)
        if not pairs:
            return
        try:
            with self._conn() as conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                    [(str(k), v) for k, v in pairs],
                )
        except Exception as e:
            raise JournalError(
                f"Failed to set {len(pairs)} meta values "
                f"({', '.join(str(k) for k, _ in pairs)}): {e}"
            )

    def get_all_bets(self):
        """All bets across every outcome, oldest first."""
        try:
            with self._conn() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM bets ORDER BY timestamp ASC")
                return cursor.fetchall()
        except Exception as e:
            raise JournalError(f"Failed to retrieve bets: {e}")

    def log_wallet_snapshot(self, timestamp, epoch, cash, locked, equity):
        """Persist one wallet valuation point for trend reporting."""
        try:
            with self._conn() as conn:
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
            with self._conn() as conn:
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

    # ---------------- tier 4 research cards ----------------

    def log_tier4_card(self, timestamp, kind, symbol, name, detail, status="fresh"):
        """Log a Tier 4 memecoin research card (human-review input only)."""
        try:
            with self._conn() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO tier4_cards (timestamp, kind, symbol, name, detail, status)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (timestamp, kind, symbol, name, detail, status))
                conn.commit()
        except Exception as e:
            raise JournalError(f"Failed to log tier4 card: {e}")

    def get_tier4_cards(self, status="fresh"):
        """Recent research cards; default: fresh (not expired/shown).
        Pass status=None for all rows regardless of status."""
        try:
            with self._conn() as conn:
                cursor = conn.cursor()
                if status is not None:
                    cursor.execute(
                        "SELECT * FROM tier4_cards WHERE status=? ORDER BY timestamp DESC",
                        (status,))
                else:
                    cursor.execute("SELECT * FROM tier4_cards ORDER BY timestamp DESC")
                return cursor.fetchall()
        except Exception as e:
            raise JournalError(f"Failed to retrieve tier4 cards: {e}")

    def has_tier4_card(self, kind, symbol, status="fresh"):
        """True if a card with this (kind, symbol) exists in the given status."""
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT 1 FROM tier4_cards WHERE kind=? AND symbol=? AND status=? LIMIT 1",
                    (kind, symbol, status or "fresh"),
                ).fetchone()
                return row is not None
        except Exception as e:
            raise JournalError(f"Failed to check tier4 card: {e}")

    def expire_tier4_card(self, card_id):
        """Mark a card as expired (dedupe bookkeeping; history preserved)."""
        try:
            with self._conn() as conn:
                conn.execute("UPDATE tier4_cards SET status='expired' WHERE id=?",
                             (card_id,))
                conn.commit()
        except Exception as e:
            raise JournalError(f"Failed to expire tier4 card: {e}")
