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


# ---- codes cadeaux
_c.executescript("""
CREATE TABLE IF NOT EXISTS codes(
  code TEXT PRIMARY KEY, coins INTEGER NOT NULL, max_uses INTEGER NOT NULL,
  used INTEGER NOT NULL DEFAULT 0, expires REAL, created REAL
);
CREATE TABLE IF NOT EXISTS code_uses(
  code TEXT NOT NULL, uid INTEGER NOT NULL, ts REAL, PRIMARY KEY(code, uid)
);
""")


def create_code(code, coins, max_uses, expires=None):
    with _l:
        if _c.execute("SELECT 1 FROM codes WHERE code=?", (code,)).fetchone():
            return False
        _c.execute("INSERT INTO codes(code,coins,max_uses,expires,created) VALUES(?,?,?,?,?)",
                   (code, coins, max_uses, expires, time.time()))
        return True


def list_codes(limit=30):
    return _x("SELECT * FROM codes ORDER BY created DESC LIMIT ?", (limit,))[0]


def redeem_code(uid, code):
    """Retourne (statut, coins) ; statut = ok | invalid | expired | exhausted | already."""
    code = (code or "").strip().upper()
    with _l:
        _c.execute("BEGIN IMMEDIATE")
        try:
            row = _c.execute("SELECT * FROM codes WHERE code=?", (code,)).fetchone()
            if not row:
                res = ("invalid", 0)
            elif row["expires"] and row["expires"] < time.time():
                res = ("expired", 0)
            elif row["used"] >= row["max_uses"]:
                res = ("exhausted", 0)
            elif _c.execute("SELECT 1 FROM code_uses WHERE code=? AND uid=?", (code, uid)).fetchone():
                res = ("already", 0)
            else:
                _c.execute("INSERT INTO code_uses(code,uid,ts) VALUES(?,?,?)", (code, uid, time.time()))
                _c.execute("UPDATE codes SET used=used+1 WHERE code=?", (code,))
                _c.execute("UPDATE users SET coins=coins+? WHERE id=?", (row["coins"], uid))
                res = ("ok", row["coins"])
            _c.execute("COMMIT")
            return res
        except Exception:
            _c.execute("ROLLBACK")
            raise


# ---- administration : admins, premium, bannis, réglages
_c.executescript("""
CREATE TABLE IF NOT EXISTS admins(id INTEGER PRIMARY KEY, added_by INTEGER, ts REAL);
CREATE TABLE IF NOT EXISTS premium(id INTEGER PRIMARY KEY, ts REAL);
CREATE TABLE IF NOT EXISTS banned(id INTEGER PRIMARY KEY, ts REAL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
""")


def is_db_admin(uid):
    return _one("SELECT 1 x FROM admins WHERE id=?", (uid,)) is not None


def add_admin(uid, by):
    _x("INSERT OR IGNORE INTO admins(id,added_by,ts) VALUES(?,?,?)", (uid, by, time.time()))


def del_admin(uid):
    return _x("DELETE FROM admins WHERE id=?", (uid,))[2] == 1


def admin_ids():
    return [r["id"] for r in _x("SELECT id FROM admins ORDER BY ts")[0]]


def is_premium(uid):
    return _one("SELECT 1 x FROM premium WHERE id=?", (uid,)) is not None


def set_premium(uid):
    _x("INSERT OR IGNORE INTO premium(id,ts) VALUES(?,?)", (uid, time.time()))


def del_premium(uid):
    return _x("DELETE FROM premium WHERE id=?", (uid,))[2] == 1


def premium_ids(limit=20):
    return [r["id"] for r in _x("SELECT id FROM premium ORDER BY ts DESC LIMIT ?", (limit,))[0]]


def is_banned(uid):
    return _one("SELECT 1 x FROM banned WHERE id=?", (uid,)) is not None


def ban(uid):
    _x("INSERT OR IGNORE INTO banned(id,ts) VALUES(?,?)", (uid, time.time()))


def unban(uid):
    return _x("DELETE FROM banned WHERE id=?", (uid,))[2] == 1


def banned_ids(limit=20):
    return [r["id"] for r in _x("SELECT id FROM banned ORDER BY ts DESC LIMIT ?", (limit,))[0]]


def get_setting(key):
    row = _one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else None


def set_setting(key, value):
    _x("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
       (key, value))


def del_setting(key):
    _x("DELETE FROM settings WHERE key=?", (key,))


def find_user(q):
    """Cherche un utilisateur par ID numérique ou @pseudo."""
    q = (q or "").strip()
    if q.startswith("@"):
        return _one("SELECT * FROM users WHERE lower(username)=lower(?)", (q[1:],))
    if q.lstrip("-").isdigit():
        return _one("SELECT * FROM users WHERE id=?", (int(q),))
    return None


def adjust_coins(uid, n):
    """Ajoute (ou retire si n < 0) des coins, sans passer sous 0. Retourne le nouveau solde."""
    _x("UPDATE users SET coins=MAX(0, coins+?) WHERE id=?", (n, uid))
    return get_user(uid)["coins"]


def del_code(code):
    _x("DELETE FROM code_uses WHERE code=?", (code,))
    return _x("DELETE FROM codes WHERE code=?", (code,))[2] == 1


def all_bots(limit=15):
    return _x("SELECT * FROM bots ORDER BY id DESC LIMIT ?", (limit,))[0]


def user_stats():
    now = time.time()

    def n(sql, *args):
        return _one(sql, args)["c"]

    return {
        "total": n("SELECT COUNT(*) c FROM users"),
        "day": n("SELECT COUNT(*) c FROM users WHERE created>=?", now - 86400),
        "week": n("SELECT COUNT(*) c FROM users WHERE created>=?", now - 7 * 86400),
        "premium": n("SELECT COUNT(*) c FROM premium"),
        "banned": n("SELECT COUNT(*) c FROM banned"),
        "admins": n("SELECT COUNT(*) c FROM admins"),
    }
