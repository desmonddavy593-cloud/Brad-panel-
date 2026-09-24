import asyncio
import html
import logging
import os
import re
import shutil
import time

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

import db
import runner

logging.basicConfig(level=logging.INFO)

# ---------- config ----------
TOKEN = os.environ["BOT_TOKEN"]
ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
WELCOME_COINS = int(os.getenv("WELCOME_COINS", "100"))
DEPLOY_COST = int(os.getenv("DEPLOY_COST", "20"))
DAY_COST = int(os.getenv("DAY_COST", "10"))       # coût de 24h d'hébergement
REF_BONUS = int(os.getenv("REF_BONUS", "50"))
MAX_REFS = int(os.getenv("MAX_REFS", "20"))       # anti-abus parrainage
MAX_BOTS = int(os.getenv("MAX_BOTS_PER_USER", "3"))
PREMIUM_MAX_BOTS = int(os.getenv("PREMIUM_MAX_BOTS", "3"))
REQUIRE_APPROVAL = os.getenv("REQUIRE_APPROVAL", "1") == "1"
MAX_UPLOAD = 20 * 1024 * 1024
TOKEN_RE = re.compile(r"^\d{6,12}:[\w-]{30,}$")

STATUS = {
    "running": "🟢 En ligne", "stopped": "🔴 Arrêté", "pending": "🟡 En attente de validation",
    "rejected": "⛔ Refusé", "crashed": "🟠 Planté",
}

r = Router()


class Deploy(StatesGroup):
    file = State()
    token = State()


# ---------- helpers ----------
def esc(s):
    return html.escape(str(s))


def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, url=d) if d.startswith("http")
         else InlineKeyboardButton(text=t, callback_data=d) for t, d in row]
        for row in rows
    ])


def dur(s):
    s = int(s)
    return f"{s // 3600}h {(s % 3600) // 60:02d}m"


async def edit(cq: CallbackQuery, text, markup=None):
    try:
        if cq.message.photo:  # l'accueil est une photo : on la remplace par un message texte
            try:
                await cq.message.delete()
            except Exception:
                pass
            await cq.message.answer(text, reply_markup=markup)
        else:
            await cq.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        pass


TG_CHANNEL = os.getenv("LINK_TG_CHANNEL", "https://t.me/+hmrEUs5totIyMzhk")
TG_GROUP = os.getenv("LINK_TG_GROUP", "https://t.me/+0U1iB2uBXcJiOWFk")
WA_CHANNEL = os.getenv("LINK_WA_CHANNEL", "https://whatsapp.com/channel/0029VbCmpwK89inpJICAG21A")
WA_COMMUNITY = os.getenv("LINK_WA_COMMUNITY", "https://chat.whatsapp.com/IdqsjNUpc6s0DgAEvKGV1S")
WA_GROUP = os.getenv("LINK_WA_GROUP", "https://chat.whatsapp.com/GMADVR2wFJp90J5KqkPf6F?s=cl&p=a&mlu=4&ilr=4")
WELCOME_IMAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "welcome.jpg")
_welcome_file_id = None


def home_markup(uid):
    rows = [
        [("📢 Canal Telegram", TG_CHANNEL), ("💬 Groupe Telegram", TG_GROUP)],
        [("📱 Chaîne WhatsApp", WA_CHANNEL)],
        [("👥 Communauté WhatsApp", WA_COMMUNITY), ("💬 Groupe WhatsApp", WA_GROUP)],
        [("🤖 Déployer un bot", "deploy"), ("📂 Mes bots", "mybots")],
        [("👤 Mon compte", "acct"), ("🎁 Parrainage", "ref")],
        [("🎟️ Code cadeau", "gift")],
    ]
    if is_admin(uid):
        rows.append([("🛠️ Admin", "admin")])
    return kb(*rows)


def home_text(uid, first_name="", locked=False):
    u = db.get_user(uid) or {"coins": 0}
    name = esc(first_name or "")
    hello = f"Bienvenue, {name} !" if name else "Bienvenue !"
    return ("━━━━━━━━━━━━━━━━━━\n"
            "👑 <b>BRAD SOCIETY</b> 👑\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"👋 <b>{hello}</b>\n\n"
            "<blockquote>Envoie ton bot, on s'occupe de le faire tourner. "
            "Tu gères tout depuis Telegram : démarrage, arrêt, logs.</blockquote>\n\n"
            f"🎁 <b>{WELCOME_COINS} 🪙 offerts</b> pour bien commencer\n"
            f"🪙 <b>Ton solde : {u['coins']} 🪙</b>\n\n"
            + ("🔒 <b>Rejoins le canal et le groupe Telegram</b> ci-dessous, puis touche "
               "« ✅ J'ai rejoint » pour utiliser le bot."
               if locked else "Rejoins la communauté juste en dessous 👇"))


async def send_home(bot: Bot, chat_id, uid, first_name=""):
    """Accueil : image + texte + boutons (texte seul si l'image est absente)."""
    global _welcome_file_id
    locked = bool(await missing_chats(bot, uid))
    text = home_text(uid, first_name, locked)
    markup = gate_markup() if locked else home_markup(uid)
    custom = db.get_setting("welcome_photo")  # image choisie par un admin
    if custom:
        try:
            await bot.send_photo(chat_id, custom, caption=text, reply_markup=markup)
            return
        except Exception:
            logging.exception("image d'accueil personnalisée")
    if _welcome_file_id or os.path.exists(WELCOME_IMAGE):
        try:
            msg = await bot.send_photo(chat_id, _welcome_file_id or FSInputFile(WELCOME_IMAGE),
                                       caption=text, reply_markup=markup)
            if msg.photo:
                _welcome_file_id = msg.photo[-1].file_id
            return
        except Exception:
            logging.exception("image d'accueil")
    await bot.send_message(chat_id, text, reply_markup=markup)


def owned(uid, bid):
    b = db.get_bot(bid)
    return b if b and (b["owner"] == uid or is_admin(uid)) else None


# ---------- /start ----------
@r.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    uid = m.from_user.id
    if not db.get_user(uid):
        ref = None
        if command.args and command.args.startswith("ref_"):
            try:
                ref = int(command.args[4:])
            except ValueError:
                ref = None
        if ref == uid or not (ref and db.get_user(ref)):
            ref = None
        db.create_user(uid, m.from_user.username, WELCOME_COINS, ref)
        if ref and db.ref_count(ref) <= MAX_REFS:
            db.add_coins(ref, REF_BONUS)
            try:
                await m.bot.send_message(ref, f"🎁 Nouveau filleul ! +{REF_BONUS} 🪙")
            except Exception:
                pass
        await m.answer(f"🎉 Bienvenue ! Tu reçois <b>{WELCOME_COINS} 🪙</b> gratuits.")
    await send_home(m.bot, m.chat.id, uid, m.from_user.first_name)


@r.callback_query(F.data == "home")
async def cb_home(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await cq.message.delete()
    except Exception:
        pass
    await send_home(cq.bot, cq.from_user.id, cq.from_user.id, cq.from_user.first_name)
    await cq.answer()


# ---------- compte / parrainage ----------
@r.callback_query(F.data == "acct")
async def cb_acct(cq: CallbackQuery):
    u = db.get_user(cq.from_user.id)
    n = len(db.user_bots(u["id"]))
    await edit(cq, (f"👤 <b>Mon compte</b>\n\nID : <code>{u['id']}</code>\n🪙 Solde : <b>{u['coins']}</b>\n"
                    f"🤖 Bots : {n}/{bot_limit(u['id'])}{' ⭐ Premium' if db.is_premium(u['id']) else ''}\n\n<b>Tarifs</b>\nDéploiement : {DEPLOY_COST} 🪙\n"
                    f"24h d'hébergement : {DAY_COST} 🪙"), kb([("⬅️ Retour", "home")]))


@r.callback_query(F.data == "ref")
async def cb_ref(cq: CallbackQuery):
    me = await cq.bot.me()
    uid = cq.from_user.id
    await edit(cq, (f"🎁 <b>Parrainage</b>\n\n+{REF_BONUS} 🪙 par filleul (max {MAX_REFS}).\n"
                    f"Filleuls : {db.ref_count(uid)}\n\nTon lien :\n"
                    f"https://t.me/{me.username}?start=ref_{uid}"), kb([("⬅️ Retour", "home")]))


# ---------- déploiement ----------
@r.callback_query(F.data == "deploy")
async def cb_deploy(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit(cq, ("🤖 <b>Déployer un bot</b>\n\nQuel type de bot veux-tu déployer ?\n\n"
                    "🟢 <b>Bot Node.js</b> : ton bot n'utilise pas de token Telegram. "
                    "Aucun token ne sera demandé.\n\n"
                    "🤖 <b>Bot Telegram</b> : ton bot est un bot Telegram, même s'il est en Node.js. "
                    "Le panel te demandera son token."),
               kb([("🟢 Bot Node.js", "deploy:node"), ("🤖 Bot Telegram", "deploy:tg")],
                  [("⬅️ Retour", "home")]))


@r.callback_query(F.data.startswith("deploy:"))
async def cb_deploy_kind(cq: CallbackQuery, state: FSMContext):
    kind = cq.data.split(":")[1]
    if kind not in ("node", "tg"):
        return await cq.answer()
    uid = cq.from_user.id
    limit = bot_limit(uid)
    if len(db.user_bots(uid)) >= limit:
        return await cq.answer(f"Limite de {limit} bot(s) atteinte.", show_alert=True)
    if db.get_user(uid)["coins"] < DEPLOY_COST:
        return await cq.answer(f"Il faut {DEPLOY_COST} 🪙 pour déployer.", show_alert=True)
    await state.set_state(Deploy.file)
    await state.update_data(kind=kind)
    title = "🟢 <b>Bot Node.js</b>" if kind == "node" else "🤖 <b>Bot Telegram</b>"
    tail = ("Aucun token Telegram ne sera demandé." if kind == "node"
            else "Je te demanderai ensuite le token du bot (BotFather).")
    await edit(cq, (f"{title}\n\nEnvoie ton code :\n• un fichier <code>.py</code> ou <code>.js</code>, ou\n"
                    "• un <code>.zip</code> (avec <code>main.py</code>, <code>index.js</code> ou <code>package.json</code>, "
                    "et un <code>requirements.txt</code> si besoin). Sans <code>node_modules</code> : "
                    "il est installé automatiquement.\n\nMax 20 Mo. Python ou Node.js.\n" + tail),
               kb([("❌ Annuler", "home")]))


def bot_label(root, entry):
    """Nom affiché pour un bot sans token Telegram."""
    import json
    try:
        with open(os.path.join(root, "package.json"), encoding="utf-8", errors="ignore") as f:
            n = json.load(f).get("name")
        if isinstance(n, str) and n.strip():
            return n.strip()[:40]
    except (OSError, ValueError, AttributeError):
        pass
    return os.path.basename(root.rstrip("/"))[:40] or entry


@r.message(Deploy.file, F.document)
async def deploy_file(m: Message, state: FSMContext):
    d = m.document
    name = (d.file_name or "").lower()
    if not name.endswith((".py", ".js", ".zip")):
        return await m.answer("Envoie un fichier .py, .js ou .zip.")
    if d.file_size and d.file_size > MAX_UPLOAD:
        return await m.answer("Fichier trop gros (20 Mo max).")
    dest = os.path.join(runner.BOTS_DIR, f"{m.from_user.id}_{int(time.time())}")
    tmp = dest + ".tmp"
    os.makedirs(dest)
    try:
        await m.bot.download(d, destination=tmp)
        if name.endswith(".zip"):
            await asyncio.to_thread(runner.extract_zip, tmp, dest)
        else:
            safe = re.sub(r"[^\w.-]", "_", os.path.basename(name))
            shutil.copy(tmp, os.path.join(dest, safe))
    except Exception as e:
        shutil.rmtree(dest, ignore_errors=True)
        return await m.answer(f"❌ Fichier invalide : {esc(e)}")
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    root, entry = runner.locate(dest)
    if not entry:
        shutil.rmtree(dest, ignore_errors=True)
        return await m.answer("❌ Aucun fichier principal trouvé (main.py, bot.py, app.py, index.js…).")
    issues = await asyncio.to_thread(runner.scan, root)
    if issues:
        shutil.rmtree(dest, ignore_errors=True)
        await state.clear()
        return await m.answer("⛔ Code refusé à la vérification :\n" + "\n".join(f"• {esc(i)}" for i in issues))
    kind = (await state.get_data()).get("kind", "tg")
    await state.update_data(dest=dest, root=root, entry=entry)
    if kind == "node":
        await m.answer(f"✅ Code reçu (<code>{esc(entry)}</code>). Déploiement…")
        return await finish_deploy(m, state, "")
    await state.set_state(Deploy.token)
    await m.answer(f"✅ Code reçu (<code>{esc(entry)}</code>).\n\nEnvoie maintenant le <b>token</b> de ton bot "
                   "Telegram (BotFather). Ton message sera supprimé.")


@r.message(Deploy.file)
async def deploy_file_wrong(m: Message):
    await m.answer("Envoie un fichier .py, .js ou .zip (ou /start pour annuler).")


@r.message(Deploy.token, F.text)
async def deploy_token(m: Message, state: FSMContext):
    tok = m.text.strip()
    try:
        await m.delete()
    except Exception:
        pass
    if not TOKEN_RE.match(tok):
        return await m.answer("Token invalide. Réessaie (ou /start pour annuler).")
    await finish_deploy(m, state, tok)


async def finish_deploy(m: Message, state: FSMContext, tok: str):
    """tok vide = bot sans token Telegram (bouton Bot Node.js)."""
    data = await state.get_data()
    if tok:
        tb = Bot(tok)
        try:
            me = await tb.get_me()
        except Exception:
            return await m.answer("❌ Token refusé par Telegram. Vérifie-le.")
        finally:
            await tb.session.close()
        name = "@" + me.username
    else:
        name = bot_label(data["root"], data["entry"])
    uid = m.from_user.id
    if not db.spend(uid, DEPLOY_COST):
        shutil.rmtree(data["dest"], ignore_errors=True)
        await state.clear()
        return await m.answer("Solde insuffisant.")
    status = "pending" if REQUIRE_APPROVAL and not is_admin(uid) else "stopped"
    bid = db.add_bot(uid, name, data["dest"], data["root"], data["entry"], tok, status)
    await state.clear()
    if status == "pending":
        await m.answer(f"📦 <b>{esc(name)}</b> envoyé. En attente de validation par un admin.",
                       reply_markup=kb([("📂 Mes bots", "mybots")]))
        for a in all_admins():
            try:
                await m.bot.send_message(
                    a, f"🆕 Bot #{bid} {esc(name)} de <code>{uid}</code> à valider.",
                    reply_markup=kb([(f"✅ Approuver #{bid}", f"adm:ok:{bid}"), (f"❌ Refuser #{bid}", f"adm:no:{bid}")]))
            except Exception:
                pass
    else:
        await m.answer(f"✅ <b>{esc(name)}</b> déployé. Lance-le depuis le panel.",
                       reply_markup=kb([(f"Ouvrir #{bid}", f"bot:{bid}")]))


# ---------- mes bots ----------
@r.callback_query(F.data == "mybots")
async def cb_mybots(cq: CallbackQuery):
    bots = db.user_bots(cq.from_user.id)
    if not bots:
        return await edit(cq, "📂 Tu n'as aucun bot.", kb([("🤖 Déployer un bot", "deploy")], [("⬅️ Retour", "home")]))
    rows = [[(f"{STATUS[b['status']][:2]} {b['name']}", f"bot:{b['id']}")] for b in bots]
    rows.append([("⬅️ Retour", "home")])
    await edit(cq, "📂 <b>Mes bots</b>", kb(*rows))


async def show_bot(cq: CallbackQuery, b):
    if b["status"] == "running" and not runner.alive(b["id"]):
        db.set_status(b["id"], "crashed")
        b = db.get_bot(b["id"])
    t = f"🤖 <b>{esc(b['name'])}</b>\n\nStatut : {STATUS[b['status']]}\n"
    if b["status"] == "running":
        st = await asyncio.to_thread(runner.stats, b["id"])
        if st:
            t += f"CPU : {st['cpu']:.0f} %\nRAM : {st['ram']:.0f} MB\nUptime : {dur(st['uptime'])}\n"
    if b["paid_until"] > time.time():
        t += f"Payé pour : {dur(b['paid_until'] - time.time())}\n"
    i = b["id"]
    if b["status"] == "running":
        rows = [[("🔄 Redémarrer", f"act:restart:{i}"), ("⏹️ Arrêter", f"act:stop:{i}")]]
    elif b["status"] in ("stopped", "crashed"):
        rows = [[("▶️ Démarrer", f"act:start:{i}")]]
    else:
        rows = []
    rows += [[("📋 Logs", f"act:logs:{i}"), ("📊 Actualiser", f"bot:{i}")],
             [("🗑️ Supprimer", f"act:del:{i}"), ("⬅️ Retour", "mybots")]]
    await edit(cq, t, kb(*rows))


@r.callback_query(F.data.startswith("bot:"))
async def cb_bot(cq: CallbackQuery):
    b = owned(cq.from_user.id, int(cq.data.split(":")[1]))
    if not b:
        return await cq.answer("Introuvable.", show_alert=True)
    await show_bot(cq, b)
    await cq.answer()


async def launch(b):
    """Démarre un bot (facture 24h si besoin). Retourne (ok, message)."""
    now = time.time()
    need = b["paid_until"] <= now
    if need and db.get_user(b["owner"])["coins"] < DAY_COST:
        return False, f"Il faut {DAY_COST} 🪙 pour 24h d'hébergement."
    ok, err = await asyncio.to_thread(runner.start, b["id"], b["token"], b["root"], b["entry"])
    if not ok:
        return False, err
    if need:
        db.spend(b["owner"], DAY_COST)
        db.set_paid(b["id"], now + 86400)
    db.set_status(b["id"], "running")
    return True, ""


@r.callback_query(F.data.startswith("act:"))
async def cb_act(cq: CallbackQuery):
    _, action, sid = cq.data.split(":")
    b = owned(cq.from_user.id, int(sid))
    if not b:
        return await cq.answer("Introuvable.", show_alert=True)
    if action in ("start", "restart", "stop") and b["status"] in ("pending", "rejected"):
        return await cq.answer("Bot non validé.", show_alert=True)

    if action in ("start", "restart"):
        await cq.answer("Démarrage…")
        ok, msg = await launch(b)
        if not ok:
            db.set_status(b["id"], "stopped")
            await cq.message.answer(f"❌ {esc(msg)}")
    elif action == "stop":
        await asyncio.to_thread(runner.stop, b["id"])
        db.set_status(b["id"], "stopped")
        await cq.answer("Arrêté.")
    elif action == "logs":
        txt = runner.logs(b["root"]) or "(aucun log)"
        await cq.answer()
        return await cq.message.answer(f"📋 <b>Logs {esc(b['name'])}</b>\n<pre>{esc(txt[-3500:])}</pre>")
    elif action == "del":
        return await edit(cq, f"🗑️ Supprimer <b>{esc(b['name'])}</b> ? Action définitive.",
                          kb([("✅ Oui, supprimer", f"act:delok:{b['id']}"), ("❌ Non", f"bot:{b['id']}")]))
    elif action == "delok":
        await asyncio.to_thread(runner.wipe, b["id"], b["dest"])
        db.del_bot(b["id"])
        await cq.answer("Supprimé.")
        return await cb_mybots(cq)
    await show_bot(cq, db.get_bot(b["id"]))


# ---------- admin ----------
def is_admin(uid):
    return uid in ADMINS or db.is_db_admin(uid)


def all_admins():
    return set(ADMINS) | set(db.admin_ids())


def bot_limit(uid):
    return PREMIUM_MAX_BOTS if db.is_premium(uid) else MAX_BOTS


@r.callback_query(F.data == "admin")
async def cb_admin(cq: CallbackQuery, state: FSMContext = None):
    if not is_admin(cq.from_user.id):
        return await cq.answer()
    if state:
        await state.clear()
    c, s = db.counts(), db.user_stats()
    await edit(cq, (f"🛠️ <b>Administration</b>\n\n👥 {s['total']} utilisateurs · "
                    f"🤖 {c['bots']} bots (🟢 {c['running']})"), admin_menu_kb())


# ---------- interface administrateur ----------
class AdminFlow(StatesGroup):
    wait = State()
    photo = State()


PROMPTS = {
    "search": "🔎 Envoie l'ID ou le @pseudo de l'utilisateur.",
    "add_admin": "➕ Envoie l'ID ou le @pseudo du futur administrateur (il doit avoir ouvert le bot).",
    "prem_add": "⭐ Envoie l'ID ou le @pseudo de l'utilisateur à passer premium.",
    "prem_del": "➖ Envoie l'ID ou le @pseudo de l'utilisateur à qui retirer le premium.",
    "coins": ("🪙 Envoie : ID_ou_@pseudo montant\n"
              "Ex : 123456789 50 (donner) ou 123456789 -20 (retirer)"),
    "ban": "🚫 Envoie l'ID ou le @pseudo de l'utilisateur à bannir.",
    "unban": "✅ Envoie l'ID ou le @pseudo de l'utilisateur à débannir.",
    "bc": "📢 Envoie le message à diffuser à tous les utilisateurs (texte simple).",
    "code_new": ("🎟️ Envoie : CODE coins utilisations [jours]\n"
                 "Ex : BIENVENUE 50 20 7  (écris auto à la place du code pour en générer un)"),
    "code_del": "🗑️ Envoie le code à supprimer.",
}
_tasks = set()


def uname(u):
    return ("@" + esc(u["username"])) if u and u["username"] else "(sans pseudo)"


def admin_menu_kb():
    rows = [[("👥 Utilisateurs", "ad:users"), ("🤖 Bots", "ad:bots")],
            [("🎟️ Codes cadeaux", "ad:codes"), ("🖼️ Image du menu", "ad:img")],
            [("⭐ Premium", "ad:prem"), ("👮 Administrateurs", "ad:admins")],
            [("🪙 Coins", "ad:ask:coins"), ("🚫 Bannir", "ad:ban")],
            [("📢 Diffusion", "ad:ask:bc"), ("🔒 Obligation de rejoindre", "ad:gate")]]
    pend = db.counts()["pending"]
    if pend:
        rows.insert(0, [(f"🟡 À valider ({pend})", "ad:pending")])
    rows.append([("⬅️ Retour", "home")])
    return kb(*rows)


def admin_bots_screen():
    bots = db.all_bots(15)
    back = [("⬅️ Admin", "admin")]
    if not bots:
        return "🤖 <b>Bots</b>\n\nAucun bot.", kb(back)
    lines, rows = [], []
    for b in bots:
        lines.append(f"{STATUS[b['status']][:2]} #{b['id']} {esc(b['name'])} · <code>{b['owner']}</code>")
        if b["status"] in ("running", "crashed"):
            rows.append([(f"⏹️ Arrêter #{b['id']} {b['name'][:20]}", f"ad:stop:{b['id']}")])
    rows.append(back)
    return "🤖 <b>Bots</b> (15 derniers)\n\n" + "\n".join(lines), kb(*rows)


def make_code(args):
    import secrets
    try:
        p = args.split()
        code, coins, uses = p[0].upper(), int(p[1]), int(p[2])
        days = int(p[3]) if len(p) > 3 else 0
        if coins <= 0 or uses <= 0 or days < 0:
            raise ValueError
    except (IndexError, ValueError):
        return False, "Format : CODE coins utilisations [jours]. Ex : BIENVENUE 50 20 7"
    if code == "AUTO":
        code = "BRAD-" + secrets.token_hex(3).upper()
    if not re.fullmatch(r"[A-Z0-9_-]{3,32}", code):
        return False, "Code invalide (A-Z, 0-9, - et _ ; 3 à 32 caractères)."
    expires = time.time() + days * 86400 if days else None
    if not db.create_code(code, coins, uses, expires):
        return False, "Ce code existe déjà."
    return True, (f"✅ Code créé : <code>{code}</code>\n{coins} 🪙 · {uses} utilisation(s)"
                  + (f" · expire dans {days} j" if days else ""))


async def do_broadcast(bot: Bot, admin_id, text):
    ok = fail = 0
    for uid in db.all_user_ids():
        if db.is_banned(uid):
            continue
        try:
            await bot.send_message(uid, text, parse_mode=None)
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.06)
    try:
        await bot.send_message(admin_id, f"📢 Diffusion terminée : {ok} envoyés, {fail} échecs.")
    except Exception:
        pass


@r.callback_query(F.data.startswith("ad:"))
async def cb_ad(cq: CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        return await cq.answer()
    parts = cq.data.split(":")
    act = parts[1]
    data = await state.get_data()
    await state.clear()
    back = [("⬅️ Admin", "admin")]

    if act == "users":
        s, c = db.user_stats(), db.counts()
        text = (f"👥 <b>Utilisateurs</b>\n\nTotal : <b>{s['total']}</b>\nNouveaux (24 h) : {s['day']}\n"
                f"Nouveaux (7 jours) : {s['week']}\n⭐ Premium : {s['premium']}\n🚫 Bannis : {s['banned']}\n"
                f"👮 Admins ajoutés : {s['admins']}\n\n🤖 Bots : {c['bots']} (🟢 {c['running']} en ligne)")
        return await edit(cq, text, kb([("🔎 Chercher un utilisateur", "ad:ask:search")], back))

    if act in ("bots", "stop"):
        if act == "stop":
            b = db.get_bot(int(parts[2]))
            if b:
                await asyncio.to_thread(runner.stop, b["id"])
                db.set_status(b["id"], "stopped")
                try:
                    await cq.bot.send_message(b["owner"], f"⏹️ Ton bot {esc(b['name'])} a été arrêté par un administrateur.")
                except Exception:
                    pass
            await cq.answer("Arrêté.")
        text, mk = admin_bots_screen()
        return await edit(cq, text, mk)

    if act == "codes":
        lines = []
        for c in db.list_codes(10):
            exp = time.strftime("%d/%m", time.localtime(c["expires"])) if c["expires"] else "∞"
            lines.append(f"<code>{c['code']}</code> · {c['coins']} 🪙 · {c['used']}/{c['max_uses']} · exp {exp}")
        return await edit(cq, "🎟️ <b>Codes cadeaux</b>\n\n" + ("\n".join(lines) or "Aucun code."),
                          kb([("➕ Créer", "ad:ask:code_new"), ("🗑️ Supprimer", "ad:ask:code_del")], back))

    if act == "img":
        sub = parts[2] if len(parts) > 2 else ""
        if sub == "set":
            await state.set_state(AdminFlow.photo)
            return await edit(cq, "📷 Envoie la nouvelle image du menu <b>comme photo</b> (pas comme fichier).",
                              kb([("❌ Annuler", "admin")]))
        if sub == "rm":
            db.del_setting("welcome_photo")
            await cq.answer("Image personnalisée retirée.")
        if db.get_setting("welcome_photo"):
            status = "✅ Image personnalisée active"
        elif os.path.exists(WELCOME_IMAGE):
            status = "🖼️ Image par défaut"
        else:
            status = "Aucune image (texte seul)"
        return await edit(cq, f"🖼️ <b>Image du menu</b>\n\n{status}",
                          kb([("📷 Changer l'image", "ad:img:set")],
                             [("🗑️ Retirer la personnalisée", "ad:img:rm")], back))

    if act == "prem":
        ids = db.premium_ids(20)
        lines = [f"⭐ <code>{i}</code> {uname(db.get_user(i))}" for i in ids]
        return await edit(cq, (f"⭐ <b>Premium</b> ({len(ids)}) : jusqu'à {PREMIUM_MAX_BOTS} bots\n\n"
                               + ("\n".join(lines) or "Aucun utilisateur premium.")),
                          kb([("➕ Nommer premium", "ad:ask:prem_add"), ("➖ Retirer", "ad:ask:prem_del")], back))

    if act == "rm":
        db.del_admin(int(parts[2]))
        await cq.answer("Administrateur retiré.")
        act = "admins"

    if act == "admins":
        lines = [f"👑 <code>{i}</code> (propriétaire)" for i in sorted(ADMINS)]
        rows = [[("➕ Nommer un administrateur", "ad:ask:add_admin")]]
        for i in db.admin_ids():
            lines.append(f"👮 <code>{i}</code> {uname(db.get_user(i))}")
            rows.append([(f"❌ Retirer {i}", f"ad:rm:{i}")])
        rows.append(back)
        return await edit(cq, "👮 <b>Administrateurs</b>\n\n" + ("\n".join(lines) or "Aucun."), kb(*rows))

    if act == "ban":
        ids = db.banned_ids(20)
        lines = [f"🚫 <code>{i}</code> {uname(db.get_user(i))}" for i in ids]
        return await edit(cq, f"🚫 <b>Bannis</b> ({len(ids)})\n\n" + ("\n".join(lines) or "Personne."),
                          kb([("🚫 Bannir", "ad:ask:ban"), ("✅ Débannir", "ad:ask:unban")], back))

    if act == "gate":
        sub = parts[2] if len(parts) > 2 else ""
        if sub in ("on", "off"):
            db.set_setting("gate", "1" if sub == "on" else "0")
        lines = []
        try:
            me = await cq.bot.me()
        except Exception:
            me = None
        for c in REQUIRED_CHATS:
            try:
                cm = await cq.bot.get_chat_member(c, me.id)
                good = cm.status in ("administrator", "creator")
                lines.append(f"{chat_label(c)} : " + ("✅ vérifiable (bot admin)" if good else "⚠️ bot non administrateur"))
            except Exception:
                lines.append(f"{chat_label(c)} : ❌ introuvable (ID faux ou bot absent)")
        toggle = ("🔓 Désactiver", "ad:gate:off") if gate_on() else ("🔒 Activer", "ad:gate:on")
        return await edit(cq, ("🔒 <b>Obligation de rejoindre</b>\n\nÉtat : "
                               + ("🔒 <b>activée</b>" if gate_on() else "🔓 <b>désactivée</b>") + "\n\n"
                               + "\n".join(lines)
                               + "\n\nLe bot doit être administrateur du canal et du groupe pour vérifier les membres."),
                          kb([toggle], back))

    if act == "pending":
        rows = [[(f"✅ #{b['id']} {b['name']}", f"adm:ok:{b['id']}"), (f"❌ #{b['id']}", f"adm:no:{b['id']}")]
                for b in db.bots_by_status("pending")]
        rows.append(back)
        return await edit(cq, "🟡 <b>Bots à valider</b>", kb(*rows))

    if act == "ask":
        key = parts[2] if len(parts) > 2 else ""
        if key not in PROMPTS:
            return await cq.answer()
        await state.set_state(AdminFlow.wait)
        await state.update_data(act=key)
        return await edit(cq, PROMPTS[key], kb([("❌ Annuler", "admin")]))

    if act == "bcgo":
        text = data.get("bc_text")
        if not text:
            return await cq.answer("Rien à envoyer.", show_alert=True)
        await cq.answer("Envoi en cours…")
        task = asyncio.create_task(do_broadcast(cq.bot, cq.from_user.id, text))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
        return await edit(cq, "📢 Diffusion lancée. Je te préviens à la fin.", kb(back))

    await cq.answer()


@r.message(AdminFlow.wait, F.text)
async def admin_text(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return await state.clear()
    text = m.text.strip()
    back = kb([("⬅️ Admin", "admin")])
    if text.startswith("/"):
        await state.clear()
        return await m.answer("Action annulée.", reply_markup=back)
    act = (await state.get_data()).get("act")

    if act == "bc":
        await state.update_data(bc_text=text)
        n = len([i for i in db.all_user_ids() if not db.is_banned(i)])
        return await m.answer(f"📢 <b>Aperçu</b>\n\n{esc(text)}\n\nEnvoyer à <b>{n}</b> utilisateurs ?",
                              reply_markup=kb([(f"✅ Envoyer à {n}", "ad:bcgo"), ("❌ Annuler", "admin")]))

    if act == "code_new":
        ok, msg = make_code(text)
        if ok:
            await state.clear()
        return await m.answer(msg, reply_markup=back if ok else None)

    if act == "code_del":
        found = db.del_code(text.upper())
        await state.clear()
        return await m.answer("🗑️ Code supprimé." if found else "Code introuvable.", reply_markup=back)

    target = text
    amount = 0
    if act == "coins":
        words = text.split()
        try:
            amount, target = int(words[-1]), " ".join(words[:-1])
            if amount == 0:
                raise ValueError
        except (ValueError, IndexError):
            return await m.answer("Format : ID_ou_@pseudo montant (ex : 123456789 50). Réessaie ou annule.")

    u = db.find_user(target)
    if not u:
        return await m.answer("Utilisateur introuvable (il doit avoir ouvert le bot). Réessaie ou annule.")
    tid = u["id"]
    await state.clear()

    async def notify(msg):
        try:
            await m.bot.send_message(tid, msg)
        except Exception:
            pass

    if act == "search":
        info = (f"🔎 <b>Utilisateur</b>\n\nID : <code>{tid}</code>\nPseudo : {uname(u)}\n"
                f"🪙 Solde : {u['coins']}\n🤖 Bots : {len(db.user_bots(tid))}\n"
                f"⭐ Premium : {'oui' if db.is_premium(tid) else 'non'}\n"
                f"🚫 Banni : {'oui' if db.is_banned(tid) else 'non'}\n"
                f"👮 Admin : {'oui' if is_admin(tid) else 'non'}")
        return await m.answer(info, reply_markup=back)
    if act == "add_admin":
        if is_admin(tid):
            return await m.answer("Cet utilisateur est déjà administrateur.", reply_markup=back)
        db.add_admin(tid, m.from_user.id)
        await notify("👮 Tu es maintenant administrateur. Ouvre /start puis « 🛠️ Admin ».")
        return await m.answer(f"✅ {uname(u)} <code>{tid}</code> est administrateur.", reply_markup=back)
    if act == "prem_add":
        db.set_premium(tid)
        await notify(f"⭐ Tu es maintenant premium ! Tu peux déployer jusqu'à {PREMIUM_MAX_BOTS} bots.")
        return await m.answer(f"✅ {uname(u)} <code>{tid}</code> est premium.", reply_markup=back)
    if act == "prem_del":
        found = db.del_premium(tid)
        return await m.answer("✅ Premium retiré." if found else "Cet utilisateur n'était pas premium.", reply_markup=back)
    if act == "coins":
        new = db.adjust_coins(tid, amount)
        verb = "ajouté" if amount > 0 else "retiré"
        await notify(f"🪙 Un administrateur a {verb} {abs(amount)} 🪙 sur ton compte. Solde : {new}")
        return await m.answer(f"✅ {uname(u)} <code>{tid}</code> : solde = <b>{new} 🪙</b>", reply_markup=back)
    if act == "ban":
        if is_admin(tid):
            return await m.answer("Impossible de bannir un administrateur.", reply_markup=back)
        db.ban(tid)
        for b in db.user_bots(tid):
            await asyncio.to_thread(runner.stop, b["id"])
            if b["status"] == "running":
                db.set_status(b["id"], "stopped")
        return await m.answer(f"🚫 {uname(u)} <code>{tid}</code> est banni (ses bots sont arrêtés).", reply_markup=back)
    if act == "unban":
        found = db.unban(tid)
        if found:
            await notify("✅ Ton accès a été rétabli.")
        return await m.answer("✅ Utilisateur débanni." if found else "Cet utilisateur n'était pas banni.", reply_markup=back)
    await m.answer("Action inconnue.", reply_markup=back)


@r.message(AdminFlow.photo, F.photo)
async def admin_photo(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return await state.clear()
    db.set_setting("welcome_photo", m.photo[-1].file_id)
    await state.clear()
    await m.answer("✅ Image du menu mise à jour.", reply_markup=kb([("👀 Voir l'accueil", "home")], [("⬅️ Admin", "admin")]))


@r.message(AdminFlow.photo)
async def admin_photo_wrong(m: Message):
    await m.answer("Envoie l'image comme photo (pas comme fichier), ou /start pour annuler.")


@r.callback_query(F.data.startswith("adm:"))
async def cb_adm(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        return await cq.answer()
    _, act, sid = cq.data.split(":")
    b = db.get_bot(int(sid))
    if not b or b["status"] != "pending":
        return await cq.answer("Déjà traité.", show_alert=True)
    if act == "ok":
        db.set_status(b["id"], "stopped")
        msg = f"✅ Ton bot {b['name']} est validé. Tu peux le démarrer."
    else:
        await asyncio.to_thread(runner.wipe, b["id"], b["dest"])
        db.del_bot(b["id"])
        db.add_coins(b["owner"], DEPLOY_COST)
        msg = f"❌ Ton bot {b['name']} a été refusé. {DEPLOY_COST} 🪙 remboursés."
    try:
        await cq.bot.send_message(b["owner"], msg)
    except Exception:
        pass
    await cq.answer("Fait.")
    await cb_admin(cq)


@r.message(Command("addcoins"))
async def cmd_addcoins(m: Message, command: CommandObject):
    if not is_admin(m.from_user.id):
        return
    try:
        uid, n = map(int, command.args.split())
    except Exception:
        return await m.answer("Usage : /addcoins &lt;id&gt; &lt;n&gt;")
    if not db.get_user(uid):
        return await m.answer("Utilisateur inconnu.")
    db.add_coins(uid, n)
    await m.answer(f"OK : {n} 🪙 → {uid}")


@r.message(Command("stopbot"))
async def cmd_stopbot(m: Message, command: CommandObject):
    if not is_admin(m.from_user.id):
        return
    try:
        bid = int(command.args)
    except Exception:
        return await m.answer("Usage : /stopbot &lt;bot_id&gt;")
    await asyncio.to_thread(runner.stop, bid)
    db.set_status(bid, "stopped")
    await m.answer(f"Bot #{bid} arrêté.")


@r.message(Command("broadcast"))
async def cmd_broadcast(m: Message, command: CommandObject):
    if not is_admin(m.from_user.id) or not command.args:
        return
    ok = 0
    for uid in db.all_user_ids():
        try:
            await m.bot.send_message(uid, command.args)
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await m.answer(f"Envoyé à {ok} utilisateurs.")


# ---------- codes cadeaux ----------
class Gift(StatesGroup):
    code = State()


_gift_fails = {}  # anti-brute-force : uid -> horodatages des échecs
GIFT_ERRORS = {
    "invalid": "❌ Code invalide.",
    "expired": "⌛ Ce code a expiré.",
    "exhausted": "😕 Ce code a atteint son nombre maximum d'utilisations.",
    "already": "⚠️ Tu as déjà utilisé ce code.",
}


@r.callback_query(F.data == "gift")
async def cb_gift(cq: CallbackQuery, state: FSMContext):
    await state.set_state(Gift.code)
    await edit(cq, "🎟️ <b>Code cadeau</b>\n\nEnvoie ton code pour recevoir des 🪙.",
               kb([("❌ Annuler", "home")]))


@r.message(Command("newcode"))
async def cmd_newcode(m: Message, command: CommandObject):
    if not is_admin(m.from_user.id):
        return
    import secrets
    usage = "Usage : /newcode &lt;CODE ou auto&gt; &lt;coins&gt; &lt;utilisations max&gt; [jours]"
    try:
        p = (command.args or "").split()
        code, coins, uses = p[0].upper(), int(p[1]), int(p[2])
        days = int(p[3]) if len(p) > 3 else 0
        if coins <= 0 or uses <= 0 or days < 0:
            raise ValueError
    except (IndexError, ValueError):
        return await m.answer(usage)
    if code == "AUTO":
        code = "BRAD-" + secrets.token_hex(3).upper()
    if not re.fullmatch(r"[A-Z0-9_-]{3,32}", code):
        return await m.answer("Code invalide (A-Z, 0-9, - et _ ; 3 à 32 caractères).")
    expires = time.time() + days * 86400 if days else None
    if not db.create_code(code, coins, uses, expires):
        return await m.answer("Ce code existe déjà.")
    await m.answer(f"✅ Code créé : <code>{code}</code>\n{coins} 🪙 · {uses} utilisation(s)"
                   + (f" · expire dans {days} j" if days else ""))


@r.message(Command("codes"))
async def cmd_codes(m: Message):
    if not is_admin(m.from_user.id):
        return
    rows = db.list_codes()
    if not rows:
        return await m.answer("Aucun code.")
    lines = []
    for c in rows:
        exp = time.strftime("%d/%m", time.localtime(c["expires"])) if c["expires"] else "∞"
        lines.append(f"<code>{c['code']}</code> · {c['coins']} 🪙 · {c['used']}/{c['max_uses']} · exp {exp}")
    await m.answer("🎟️ <b>Codes</b>\n" + "\n".join(lines))


@r.message(Gift.code, F.text)
async def gift_code(m: Message, state: FSMContext):
    uid, now = m.from_user.id, time.time()
    fails = [t for t in _gift_fails.get(uid, []) if now - t < 3600]
    if len(fails) >= 5:
        return await m.answer("⛔ Trop d'essais. Réessaie dans une heure.")
    status, coins = db.redeem_code(uid, m.text)
    if status != "ok":
        fails.append(now)
        _gift_fails[uid] = fails
        return await m.answer(GIFT_ERRORS[status] + "\nRéessaie, ou /start pour annuler.")
    await state.clear()
    await m.answer(f"🎉 Code validé ! <b>+{coins} 🪙</b>", reply_markup=kb([("🏠 Menu", "home")]))


# ---------- accès : bannis, admins, premium ----------
class BanGuard(BaseMiddleware):
    """Bloque les utilisateurs bannis (les admins ne sont jamais bloqués)."""
    async def __call__(self, handler, event, data):
        u = getattr(event, "from_user", None)
        if u and db.is_banned(u.id) and not is_admin(u.id):
            try:
                if isinstance(event, CallbackQuery):
                    await event.answer("⛔ Ton accès est suspendu.", show_alert=True)
                else:
                    await event.answer("⛔ Ton accès est suspendu.")
            except Exception:
                pass
            return
        return await handler(event, data)


r.message.outer_middleware(BanGuard())
r.callback_query.outer_middleware(BanGuard())


# ---------- obligation de rejoindre (canal + groupe Telegram) ----------
REQUIRED_CHATS = [int(x) for x in os.getenv("REQUIRED_CHATS", "-1004286759333,-1004401259043")
                  .replace(" ", "").split(",") if x]
_member_cache = {}  # (uid, chat) -> horodatage de la dernière vérification positive
_warned = {}        # chat -> horodatage du dernier avertissement envoyé aux admins


def gate_on():
    return bool(REQUIRED_CHATS) and db.get_setting("gate") != "0"


def chat_label(c):
    return "📢 Canal Telegram" if REQUIRED_CHATS and c == REQUIRED_CHATS[0] else "💬 Groupe Telegram"


def gate_markup():
    return kb([("📢 Canal Telegram", TG_CHANNEL), ("💬 Groupe Telegram", TG_GROUP)],
              [("📱 Chaîne WhatsApp", WA_CHANNEL)],
              [("👥 Communauté WhatsApp", WA_COMMUNITY), ("💬 Groupe WhatsApp", WA_GROUP)],
              [("✅ J'ai rejoint", "joined")])


async def warn_admins(bot: Bot, chat, err):
    """Prévient les admins (max 1 fois par heure) quand l'adhésion ne peut pas être vérifiée."""
    now = time.time()
    if now - _warned.get(chat, 0) < 3600:
        return
    _warned[chat] = now
    logging.warning("Vérification impossible pour %s : %s", chat, err)
    for a in all_admins():
        try:
            await bot.send_message(a, f"⚠️ Impossible de vérifier l'adhésion à {chat_label(chat)} ({chat}). "
                                      "Ajoute le bot comme administrateur de ce canal/groupe. "
                                      "En attendant, l'accès n'est pas bloqué.")
        except Exception:
            pass


async def is_member(bot: Bot, chat, uid):
    now = time.time()
    if now - _member_cache.get((uid, chat), 0) < 300:
        return True
    try:
        cm = await bot.get_chat_member(chat, uid)
    except Exception as e:  # bot non admin, ID faux… : on ne bloque pas les utilisateurs pour ça
        await warn_admins(bot, chat, e)
        return True
    ok = cm.status in ("creator", "administrator", "member") or (
        cm.status == "restricted" and getattr(cm, "is_member", False))
    if ok:
        _member_cache[(uid, chat)] = now
    return ok


async def missing_chats(bot: Bot, uid):
    if not gate_on() or is_admin(uid):
        return []
    return [c for c in REQUIRED_CHATS if not await is_member(bot, c, uid)]


class JoinGuard(BaseMiddleware):
    """Bloque les utilisateurs qui n'ont pas rejoint le canal et le groupe (sauf /start et « J'ai rejoint »)."""
    async def __call__(self, handler, event, data):
        u = getattr(event, "from_user", None)
        if not u or getattr(u, "is_bot", False) or is_admin(u.id) or not gate_on():
            return await handler(event, data)
        if isinstance(event, CallbackQuery):
            if event.data in ("joined", "home"):
                return await handler(event, data)
        elif (getattr(event, "text", None) or "").startswith("/start"):
            return await handler(event, data)
        if not await missing_chats(event.bot, u.id):
            return await handler(event, data)
        if isinstance(event, CallbackQuery):
            await event.answer("🔒 Rejoins d'abord le canal et le groupe Telegram (voir /start).", show_alert=True)
        else:
            await send_home(event.bot, u.id, u.id, getattr(u, "first_name", ""))
        return


r.message.outer_middleware(JoinGuard())
r.callback_query.outer_middleware(JoinGuard())


@r.callback_query(F.data == "joined")
async def cb_joined(cq: CallbackQuery):
    uid = cq.from_user.id
    missing = await missing_chats(cq.bot, uid)
    if missing:
        names = " et ".join(chat_label(c) for c in missing)
        return await cq.answer(f"🔒 Il te manque : {names}. Rejoins puis réessaie.", show_alert=True)
    await cq.answer("✅ Bienvenue !")
    try:
        await cq.message.delete()
    except Exception:
        pass
    await send_home(cq.bot, uid, uid, cq.from_user.first_name)


# ---------- surveillance ----------
async def monitor(bot: Bot):
    while True:
        await asyncio.sleep(30)
        now = time.time()
        for b in db.bots_by_status("running"):
            try:
                if not runner.alive(b["id"]):
                    db.set_status(b["id"], "crashed")
                    await bot.send_message(b["owner"], f"🟠 Ton bot {esc(b['name'])} s'est arrêté (plantage). Consulte les logs.")
                elif b["paid_until"] <= now:
                    if db.spend(b["owner"], DAY_COST):
                        db.set_paid(b["id"], now + 86400)
                    else:
                        await asyncio.to_thread(runner.stop, b["id"])
                        db.set_status(b["id"], "stopped")
                        await bot.send_message(b["owner"], f"⏹️ {esc(b['name'])} arrêté : plus assez de 🪙.")
            except Exception:
                logging.exception("monitor")


async def restore():
    """Après un redéploiement du panel, relance les bots qui tournaient."""
    for b in db.bots_by_status("running"):
        ok, _ = await asyncio.to_thread(runner.start, b["id"], b["token"], b["root"], b["entry"])
        if not ok:
            db.set_status(b["id"], "crashed")


async def main():
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(r)
    await restore()
    asyncio.create_task(monitor(bot))
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
