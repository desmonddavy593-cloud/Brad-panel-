import os
import re
import resource
import shutil
import signal
import subprocess
import sys
import time
import zipfile

import psutil

from db import DATA_DIR

BOTS_DIR = os.path.join(DATA_DIR, "bots")
os.makedirs(BOTS_DIR, exist_ok=True)
MAX_MEM_MB = int(os.getenv("BOT_MAX_MEM_MB", "512"))

procs = {}  # bot_id -> Popen

_BAD = re.compile(
    r"xmrig|stratum\+tcp|cryptonight|minerd|panel\.db|/proc/|RAILWAY_|ADMIN_IDS|"
    + re.escape(os.path.abspath(DATA_DIR)),
    re.I,
)


# ---------- fichiers ----------
def extract_zip(zpath, dest, max_total=30 * 1024 * 1024):
    base = os.path.realpath(dest)
    with zipfile.ZipFile(zpath) as z:
        total = 0
        for i in z.infolist():
            total += i.file_size
            if total > max_total:
                raise ValueError("Archive trop volumineuse (30 MB max décompressée)")
            if not os.path.realpath(os.path.join(dest, i.filename)).startswith(base + os.sep):
                raise ValueError("Chemin invalide dans l'archive")
        z.extractall(dest)


def locate(dest):
    """Retourne (dossier_racine, fichier_principal|None)."""
    root = dest
    items = [i for i in os.listdir(root) if i != "__MACOSX"]
    if len(items) == 1 and os.path.isdir(os.path.join(root, items[0])):
        root = os.path.join(root, items[0])
    py = [f for f in os.listdir(root) if f.endswith(".py")]
    for c in ("main.py", "bot.py", "app.py", "index.py"):
        if c in py:
            return root, c
    if len(py) == 1:
        return root, py[0]
    return root, None


def scan(root):
    """Vérification basique. Retourne la liste des problèmes trouvés."""
    issues = []
    for dp, _, files in os.walk(root):
        if "_libs" in dp:
            continue
        for f in files:
            p = os.path.join(dp, f)
            rel = os.path.relpath(p, root)
            if f == "requirements.txt":
                for line in open(p, errors="ignore"):
                    s = line.strip()
                    if s and not s.startswith("#") and re.search(r"^-|git\+|https?:|@|/", s):
                        issues.append(f"requirements.txt : ligne interdite « {s[:40]} »")
            if f.endswith((".py", ".sh", ".txt", ".json", ".env", ".cfg")):
                try:
                    txt = open(p, errors="ignore").read(1_000_000)
                except OSError:
                    continue
                m = _BAD.search(txt)
                if m:
                    issues.append(f"{rel} : motif interdit « {m.group(0)[:30]} »")
    return issues[:5]


# ---------- processus ----------
def _limits():
    mem = MAX_MEM_MB * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
    resource.setrlimit(resource.RLIMIT_FSIZE, (50 * 1024 * 1024, 50 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def alive(bid):
    p = procs.get(bid)
    return p is not None and p.poll() is None


def start(bid, token, root, entry):
    """Bloquant (pip peut prendre du temps) -> à appeler via asyncio.to_thread."""
    stop(bid)
    libs = os.path.join(root, "_libs")
    req = os.path.join(root, "requirements.txt")
    if os.path.exists(req) and not os.path.isdir(libs):
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--only-binary=:all:", "--no-input",
                 "-q", "--target", libs, "-r", req],
                capture_output=True, text=True, timeout=240,
            )
        except subprocess.TimeoutExpired:
            return False, "Installation des dépendances trop longue."
        if r.returncode != 0:
            shutil.rmtree(libs, ignore_errors=True)
            return False, "Dépendances : " + r.stderr[-300:]
    env = {
        "PATH": os.environ.get("PATH", ""),
        "BOT_TOKEN": token,
        "TOKEN": token,
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": libs,
        "HOME": root,
        "LANG": "C.UTF-8",
    }
    log = open(os.path.join(root, "bot.log"), "ab")
    log.write(f"\n--- démarrage {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n".encode())
    log.flush()
    procs[bid] = subprocess.Popen(
        [sys.executable, entry], cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, preexec_fn=_limits, start_new_session=True,
    )
    return True, ""


def stop(bid):
    p = procs.pop(bid, None)
    if not p:
        return
    try:
        os.killpg(p.pid, signal.SIGTERM)
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def stats(bid):
    p = procs.get(bid)
    if not p or p.poll() is not None:
        return None
    try:
        ps = psutil.Process(p.pid)
        allp = [ps] + ps.children(recursive=True)
        for x in allp:
            x.cpu_percent(None)
        time.sleep(0.3)
        return {
            "cpu": sum(x.cpu_percent(None) for x in allp),
            "ram": sum(x.memory_info().rss for x in allp) / 1024 / 1024,
            "uptime": time.time() - ps.create_time(),
        }
    except psutil.Error:
        return None


def logs(root, n=3000):
    try:
        with open(os.path.join(root, "bot.log"), "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - n))
            return f.read().decode(errors="replace")
    except OSError:
        return ""


def wipe(bid, dest):
    stop(bid)
    shutil.rmtree(dest, ignore_errors=True)
