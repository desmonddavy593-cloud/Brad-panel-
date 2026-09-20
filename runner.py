import json
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
    r"xmrig|stratum\+tcp|cryptonight|minerd|panel\.db|/proc/|RAILWAY_|"
    r"(?<![\w.~])" + re.escape(os.path.abspath(DATA_DIR)) + r"(?![\w-])",
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
    """Retourne (dossier_racine, commande|None). Commande = 'py:fichier', 'node:fichier' ou 'npm:start'."""
    root = dest
    items = [i for i in os.listdir(root) if i != "__MACOSX"]
    if len(items) == 1 and os.path.isdir(os.path.join(root, items[0])):
        root = os.path.join(root, items[0])
    files = os.listdir(root)
    # Node.js
    if "package.json" in files:
        try:
            pj = json.load(open(os.path.join(root, "package.json"), errors="ignore"))
        except (OSError, ValueError):
            pj = {}
        if isinstance(pj.get("scripts"), dict) and pj["scripts"].get("start"):
            return root, "npm:start"
        main = pj.get("main")
        if isinstance(main, str) and os.path.isfile(os.path.join(root, main)):
            return root, "node:" + main
    js = [f for f in files if f.endswith((".js", ".mjs", ".cjs"))]
    for c in ("index.js", "main.js", "bot.js", "app.js", "server.js"):
        if c in js:
            return root, "node:" + c
    # Python
    py = [f for f in files if f.endswith(".py")]
    for c in ("main.py", "bot.py", "app.py", "index.py"):
        if c in py:
            return root, "py:" + c
    if len(py) == 1 and not js:
        return root, "py:" + py[0]
    if len(js) == 1 and not py:
        return root, "node:" + js[0]
    return root, None


def scan(root):
    """Vérification basique. Retourne la liste des problèmes trouvés."""
    issues = []
    for dp, _, files in os.walk(root):
        if "_libs" in dp or "node_modules" in dp:
            continue
        for f in files:
            p = os.path.join(dp, f)
            rel = os.path.relpath(p, root)
            if f == "requirements.txt":
                for line in open(p, errors="ignore"):
                    s = line.strip()
                    if s and not s.startswith("#") and re.search(r"^-|git\+|https?:|@|/", s):
                        issues.append(f"requirements.txt : ligne interdite « {s[:40]} »")
            if f.endswith((".py", ".js", ".mjs", ".cjs", ".sh", ".txt", ".json", ".env", ".cfg")):
                try:
                    txt = open(p, errors="ignore").read(1_000_000)
                except OSError:
                    continue
                m = _BAD.search(txt)
                if m:
                    issues.append(f"{rel} : motif interdit « {m.group(0)[:30]} »")
    return issues[:5]


# ---------- processus ----------
def _limits_py():
    mem = MAX_MEM_MB * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
    resource.setrlimit(resource.RLIMIT_FSIZE, (50 * 1024 * 1024, 50 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _limits_node():
    # Node réserve beaucoup de mémoire virtuelle : pas de RLIMIT_AS (la limite passe par NODE_OPTIONS).
    resource.setrlimit(resource.RLIMIT_FSIZE, (200 * 1024 * 1024, 200 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def alive(bid):
    p = procs.get(bid)
    return p is not None and p.poll() is None


def _install_node(root, base_env):
    marker = os.path.join(root, ".deps_installed")
    if os.path.exists(marker) or not os.path.exists(os.path.join(root, "package.json")):
        return True, ""
    # node_modules fournis par l'utilisateur : ignorés (binaires d'une autre plateforme, ex. Termux/ARM)
    shutil.rmtree(os.path.join(root, "node_modules"), ignore_errors=True)
    env = dict(base_env, npm_config_cache=os.path.join(root, ".npm-cache"))
    try:
        r = subprocess.run(
            ["npm", "install", "--omit=dev", "--no-audit", "--no-fund", "--loglevel=error"],
            cwd=root, env=env, capture_output=True, text=True, timeout=900,
        )
    except subprocess.TimeoutExpired:
        return False, "npm install trop long (15 min max)."
    except FileNotFoundError:
        return False, "Node.js/npm introuvable sur le serveur."
    if r.returncode != 0:
        return False, "npm install : " + (r.stderr or r.stdout)[-300:]
    open(marker, "w").close()
    return True, ""


def start(bid, token, root, entry):
    """Bloquant (pip/npm peuvent prendre du temps) -> à appeler via asyncio.to_thread."""
    stop(bid)
    kind, _, target = entry.partition(":")
    if not target:  # ancien format sans préfixe
        kind, target = "py", entry
    libs = os.path.join(root, "_libs")
    base_env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": root,
        "LANG": "C.UTF-8",
    }
    if kind == "py":
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
        cmd = [sys.executable, target]
        limits = _limits_py
    else:
        ok, err = _install_node(root, base_env)
        if not ok:
            return False, err
        cmd = ["npm", "start"] if kind == "npm" else ["node", target]
        limits = _limits_node
    env = dict(
        base_env,
        BOT_TOKEN=token,
        TOKEN=token,
        TELEGRAM_BOT_TOKEN=token,
        PORT=str(20000 + bid),
        PYTHONUNBUFFERED="1",
        PYTHONPATH=libs,
        NODE_ENV="production",
        NODE_OPTIONS=f"--max-old-space-size={max(64, MAX_MEM_MB * 3 // 4)}",
    )
    if not token:
        for k in ("BOT_TOKEN", "TOKEN", "TELEGRAM_BOT_TOKEN"):
            env.pop(k, None)
    log = open(os.path.join(root, "bot.log"), "ab")
    log.write(f"\n--- démarrage {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n".encode())
    log.flush()
    procs[bid] = subprocess.Popen(
        cmd, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, preexec_fn=limits, start_new_session=True,
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
