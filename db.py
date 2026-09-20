import os
import sqlite3
import threading
import time

DATA_DIR = os.getenv("DATA_DIR", "./data")
os.makedirs(DATA_DIR, exist_ok=True)

_c = sqlite3.connect(os.path.join(DATA_DIR, "panel.db"), check_same_thread=False, isolation_level=None)
_c.row_factory = sqlite3.Row
_l = threading.Lock()

_c.executescript("""
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT, coins INTEGER NOT NULL DEFAULT 0,
  ref_by INTEGER, created REAL
);
CREATE TABLE IF NOT EXISTS bots(
  id INTEGER PRIMARY KEY AUTOINCREMENT, owner INTEGER NOT NULL, name TEXT,
  dest TEXT, root TEXT, entry TEXT, token TEXT, status TEXT,
  paid_until REAL DEFAULT 0, created REAL
);
""")


def _x(sql, args=()):
    with _l:
        cur = _c.execute(sql, args)
        return cur.fetchall(), cur.lastrowid, cur.rowcount


def _one(sql, args=()):
    rows = _x(sql, args)[0]
    return rows[0] if rows else None


# ---- users
def get_user(uid):
    return _one("SELECT * FROM users WHERE id=?", (uid,))


def create_user(uid, username, coins, ref_by=None):
    _x("INSERT OR IGNORE INTO users(id,username,coins,ref_by,created) VALUES(?,?,?,?,?)",
       (uid, username, coins, ref_by, time.time()))


def add_coins(uid, n):
    _x("UPDATE users SET coins=coins+? WHERE id=?", (n, uid))


def spend(uid, n):
    """Débite n coins si le solde suffit. Retourne True/False."""
    return _x("UPDATE users SET coins=coins-? WHERE id=? AND coins>=?", (n, uid, n))[2] == 1


def ref_count(uid):
    return _one("SELECT COUNT(*) c FROM users WHERE ref_by=?", (uid,))["c"]


def all_user_ids():
    return [r["id"] for r in _x("SELECT id FROM users")[0]]


# ---- bots
def add_bot(owner, name, dest, root, entry, token, status):
    return _x("INSERT INTO bots(owner,name,dest,root,entry,token,status,created) VALUES(?,?,?,?,?,?,?,?)",
              (owner, name, dest, root, entry, token, status, time.time()))[1]


def get_bot(bid):
    return _one("SELECT * FROM bots WHERE id=?", (bid,))


def user_bots(uid):
    return _x("SELECT * FROM bots WHERE owner=? ORDER BY id", (uid,))[0]


def bots_by_status(status):
    return _x("SELECT * FROM bots WHERE status=?", (status,))[0]


def set_status(bid, status):
    _x("UPDATE bots SET status=? WHERE id=?", (status, bid))


def set_paid(bid, ts):
    _x("UPDATE bots SET paid_until=? WHERE id=?", (ts, bid))


def del_bot(bid):
    _x("DELETE FROM bots WHERE id=?", (bid,))


def counts():
    return {
        "users": _one("SELECT COUNT(*) c FROM users")["c"],
        "bots": _one("SELECT COUNT(*) c FROM bots")["c"],
        "running": _one("SELECT COUNT(*) c FROM bots WHERE status='running'")["c"],
        "pending": _one("SELECT COUNT(*) c FROM bots WHERE status='pending'")["c"],
    }
