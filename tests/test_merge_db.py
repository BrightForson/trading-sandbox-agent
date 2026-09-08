"""Tests for tools/merge_db.py 3-way journal merge + tools/safe_commit.sh semantics.

Covers the 2026-09-08 deep-dive findings F1/F2: the rebase ours/theirs
inversion and the existence-based meta union that silently rolled back
ledger state on CI push conflicts.
"""
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
from merge_db import merge  # noqa: E402

SCHEMA = """
CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL, action TEXT NOT NULL, qty REAL NOT NULL, price REAL NOT NULL, reasoning TEXT);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE proposals (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
    source TEXT NOT NULL, kind TEXT NOT NULL, symbol TEXT, action TEXT NOT NULL);
CREATE TABLE bets (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
    market TEXT NOT NULL, side TEXT NOT NULL);
CREATE TABLE tier4_cards (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
    kind TEXT NOT NULL, symbol TEXT NOT NULL, name TEXT, detail TEXT, status TEXT DEFAULT 'fresh');
CREATE TABLE wallet_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
    epoch INTEGER NOT NULL, cash REAL NOT NULL, locked REAL NOT NULL, equity REAL NOT NULL);
"""


def make_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def set_meta(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
    conn.commit()


def get_meta(path, key):
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def test_remote_only_meta_key_inserted(tmp_path):
    local = make_db(tmp_path / "local.db")
    set_meta(local, "paper_cash", "100")
    local.close()
    remote = make_db(tmp_path / "remote.db")
    set_meta(remote, "paper_cash", "100")
    set_meta(remote, "brand_new_key", "42")
    remote.close()
    merge(str(tmp_path / "local.db"), str(tmp_path / "remote.db"))
    assert get_meta(tmp_path / "local.db", "brand_new_key") == "42"
    assert get_meta(tmp_path / "local.db", "paper_cash") == "100"


def test_three_way_local_update_survives(tmp_path):
    """Both forks start from base; local updates a shared key -> local wins (old 2-way union dropped it)."""
    base = make_db(tmp_path / "base.db")
    set_meta(base, "paper_cash", "100")
    set_meta(base, "paper_positions", "{}")
    base.close()
    local = make_db(tmp_path / "local.db")
    for k, v in [("paper_cash", "100"), ("paper_positions", "{}")]:
        set_meta(local, k, v)
    set_meta(local, "paper_cash", "90")  # local bought something
    local.close()
    remote = make_db(tmp_path / "remote.db")
    for k, v in [("paper_cash", "100"), ("paper_positions", "{}")]:
        set_meta(remote, k, v)
    remote.close()
    merge(str(tmp_path / "local.db"), str(tmp_path / "remote.db"), str(tmp_path / "base.db"))
    assert get_meta(tmp_path / "local.db", "paper_cash") == "90"


def test_three_way_remote_update_taken(tmp_path):
    """Remote updated a shared key, local didn't -> remote value taken."""
    base = make_db(tmp_path / "base.db")
    set_meta(base, "shadow_cash", "80")
    base.close()
    local = make_db(tmp_path / "local.db")
    set_meta(local, "shadow_cash", "80")
    local.close()
    remote = make_db(tmp_path / "remote.db")
    set_meta(remote, "shadow_cash", "70")
    remote.close()
    merge(str(tmp_path / "local.db"), str(tmp_path / "remote.db"), str(tmp_path / "base.db"))
    assert get_meta(tmp_path / "local.db", "shadow_cash") == "70"


def test_three_way_both_changed_local_wins(tmp_path):
    base = make_db(tmp_path / "base.db")
    set_meta(base, "t4_cash", "40")
    base.close()
    local = make_db(tmp_path / "local.db")
    set_meta(local, "t4_cash", "28")
    local.close()
    remote = make_db(tmp_path / "remote.db")
    set_meta(remote, "t4_cash", "36")
    remote.close()
    merge(str(tmp_path / "local.db"), str(tmp_path / "remote.db"), str(tmp_path / "base.db"))
    # deterministic rule: local (resolving run, newest) wins
    assert get_meta(tmp_path / "local.db", "t4_cash") == "28"


def test_tavily_counters_take_max(tmp_path):
    base = make_db(tmp_path / "base.db")
    set_meta(base, "tavily_count_2026-09-08", "5")
    base.close()
    local = make_db(tmp_path / "local.db")
    set_meta(local, "tavily_count_2026-09-08", "7")
    local.close()
    remote = make_db(tmp_path / "remote.db")
    set_meta(remote, "tavily_count_2026-09-08", "9")
    remote.close()
    merge(str(tmp_path / "local.db"), str(tmp_path / "remote.db"), str(tmp_path / "base.db"))
    assert get_meta(tmp_path / "local.db", "tavily_count_2026-09-08") == "9"


def test_row_tables_union_without_duplicates(tmp_path):
    local = make_db(tmp_path / "local.db")
    local.execute("INSERT INTO trades (timestamp,symbol,action,qty,price) VALUES ('t1','BTC/USD','BUY',0.001,100000)")
    local.execute("INSERT INTO tier4_cards (timestamp,kind,symbol) VALUES ('t1','trending','dogwifhat')")
    local.commit()
    local.close()
    remote = make_db(tmp_path / "remote.db")
    remote.execute("INSERT INTO trades (timestamp,symbol,action,qty,price) VALUES ('t1','BTC/USD','BUY',0.001,100000)")
    remote.execute("INSERT INTO trades (timestamp,symbol,action,qty,price) VALUES ('t2','ETH/USD','SELL',0.01,3000)")
    remote.execute("INSERT INTO tier4_cards (timestamp,kind,symbol) VALUES ('t2','spike','pepe')")
    remote.commit()
    remote.close()
    merge(str(tmp_path / "local.db"), str(tmp_path / "remote.db"))
    conn = sqlite3.connect(tmp_path / "local.db")
    n_trades = conn.execute("SELECT count(*) FROM trades").fetchone()[0]
    syms = {r[0] for r in conn.execute("SELECT symbol FROM trades")}
    cards = {r[0] for r in conn.execute("SELECT symbol FROM tier4_cards")}
    conn.close()
    assert n_trades == 2
    assert syms == {"BTC/USD", "ETH/USD"}
    assert cards == {"dogwifhat", "pepe"}


def test_e2e_rebase_conflict_both_sides_survive(tmp_path):
    """Reproduces the exact CI scenario: two clones both commit trades.db, second
    rebases onto first, safe_commit's stage-based resolution must keep both sides'
    rows AND both sides' concurrent meta updates."""
    def run(*args, cwd, check=True):
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        return subprocess.run(args, cwd=cwd, check=check, capture_output=True,
                              env=env, text=True)

    run("git", "init", "--bare", "-b", "main", str(tmp_path / "origin.git"), cwd=tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    db = "data/trades.db"

    def init_db(path):
        conn = sqlite3.connect(path)
        conn.executescript(SCHEMA)
        set_meta(conn, "paper_cash", "100")
        set_meta(conn, "shadow_cash", "80")
        conn.close()

    run("git", "init", "-b", "main", cwd=repo)
    (repo / "data").mkdir()
    init_db(repo / db)
    run("git", "add", ".", cwd=repo)
    run("git", "commit", "-m", "init", cwd=repo)
    run("git", "remote", "add", "origin", str(tmp_path / "origin.git"), cwd=repo)
    run("git", "push", "-u", "origin", "main", cwd=repo)

    # both sides clone the SAME base state (the CI race)
    upstream = tmp_path / "upstream"
    run("git", "clone", str(tmp_path / "origin.git"), str(upstream), cwd=tmp_path)
    local = tmp_path / "local"
    run("git", "clone", str(tmp_path / "origin.git"), str(local), cwd=tmp_path)

    # upstream run: updates paper_cash (their meta write) + adds a trade row, pushes first
    conn = sqlite3.connect(upstream / db)
    set_meta(conn, "paper_cash", "95")
    conn.execute("INSERT INTO trades (timestamp,symbol,action,qty,price) VALUES ('u1','SOL/USD','BUY',0.5,100)")
    conn.commit()
    conn.close()
    run("git", "add", ".", cwd=upstream)
    run("git", "commit", "-m", "upstream journal", cwd=upstream)
    run("git", "push", "origin", "main", cwd=upstream)

    # local run (the resolver): updates shadow_cash + adds a different trade, from the same base
    conn = sqlite3.connect(local / db)
    set_meta(conn, "shadow_cash", "75")
    conn.execute("INSERT INTO trades (timestamp,symbol,action,qty,price) VALUES ('l1','ETH/USD','BUY',0.01,3000)")
    conn.commit()
    conn.close()
    run("git", "add", ".", cwd=local)
    run("git", "commit", "-m", "local journal", cwd=local)

    # the exact conflict path from tools/safe_commit.sh
    run("git", "pull", "--rebase", "origin", "main", cwd=local, check=False)  # -> conflict
    status = run("git", "status", "--porcelain", cwd=local).stdout
    assert "UU data/trades.db" in status, f"expected binary conflict, got: {status}"

    tmp_dir = tmp_path / "resolve"
    tmp_dir.mkdir()
    with open(tmp_dir / "base.db", "wb") as f:
        subprocess.run(["git", "show", ":1:data/trades.db"], cwd=local, stdout=f, check=True)
    with open(tmp_dir / "upstream.db", "wb") as f:
        subprocess.run(["git", "show", ":2:data/trades.db"], cwd=local, stdout=f, check=True)
    with open(tmp_dir / "ours.db", "wb") as f:
        subprocess.run(["git", "show", ":3:data/trades.db"], cwd=local, stdout=f, check=True)

    tools = os.path.join(os.path.dirname(__file__), "..", "tools")
    subprocess.run([sys.executable, os.path.join(tools, "merge_db.py"),
                    str(tmp_dir / "ours.db"), str(tmp_dir / "upstream.db"),
                    "--base", str(tmp_dir / "base.db")], check=True)
    shutil.copy(tmp_dir / "ours.db", local / db)
    run("git", "add", "data/trades.db", cwd=local)
    run("git", "-c", "core.editor=true", "rebase", "--continue", cwd=local)

    conn = sqlite3.connect(local / db)
    rows = {r[0] for r in conn.execute("SELECT symbol FROM trades")}
    cash = conn.execute("SELECT value FROM meta WHERE key='paper_cash'").fetchone()[0]
    shadow = conn.execute("SELECT value FROM meta WHERE key='shadow_cash'").fetchone()[0]
    conn.close()
    # both sides' trade rows survive...
    assert rows == {"SOL/USD", "ETH/USD"}
    # ...AND both sides' concurrent meta updates survive (the old code lost one)
    assert cash == "95"
    assert shadow == "75"
