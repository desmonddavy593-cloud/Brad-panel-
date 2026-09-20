import asyncio
import html
import logging
import os
import re
import shutil
import time

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

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
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
    ])


def dur(s):
    s = int(s)
    return f"{s // 3600}h {(s % 3600) // 60:02d}m"


async def edit(cq: CallbackQuery, text, markup=None):
    try:
        await cq.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        pass


def home_markup(uid):
    rows = [[("🤖 Déployer un bot", "deploy"), ("📂 Mes bots", "mybots")],
            [("👤 Mon compte", "acct"), ("🎁 Parrainage", "ref")]]
    if uid in ADMINS:
        rows.append([("🛠️ Admin", "admin")])
    return kb(*rows)


def home_text(uid):
    u = db.get_user(uid)
    return (f"🤖 <b>Brad Society Panel</b>\n\nDépose ton bot Telegram, on l'héberge.\n"
            f"🪙 Solde : <b>{u['coins']}</b>")


def owned(uid, bid):
    b = db.get_bot(bid)
    return b if b and (b["owner"] == uid or uid in ADMINS) else None


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
    await m.answer(home_text(uid), reply_markup=home_markup(uid))


@r.callback_query(F.data == "home")
async def cb_home(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit(cq, home_text(cq.from_user.id), home_markup(cq.from_user.id))


# ---------- compte / parrainage ----------
@r.callback_query(F.data == "acct")
async def cb_acct(cq: CallbackQuery):
    u = db.get_user(cq.from_user.id)
    n = len(db.user_bots(u["id"]))
    await edit(cq, (f"👤 <b>Mon compte</b>\n\nID : <code>{u['id']}</code>\n🪙 Solde : <b>{u['coins']}</b>\n"
                    f"🤖 Bots : {n}/{MAX_BOTS}\n\n<b>Tarifs</b>\nDéploiement : {DEPLOY_COST} 🪙\n"
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
    uid = cq.from_user.id
    if len(db.user_bots(uid)) >= MAX_BOTS:
        return await cq.answer(f"Limite de {MAX_BOTS} bots atteinte.", show_alert=True)
    if db.get_user(uid)["coins"] < DEPLOY_COST:
        return await cq.answer(f"Il faut {DEPLOY_COST} 🪙 pour déployer.", show_alert=True)
    await state.set_state(Deploy.file)
    await edit(cq, ("🤖 <b>Déployer un bot</b>\n\nEnvoie ton code :\n• un fichier <code>.py</code> ou <code>.js</code>, ou\n"
                    "• un <code>.zip</code> (avec <code>main.py</code>, <code>index.js</code> ou <code>package.json</code>, "
                    "et un <code>requirements.txt</code> si besoin). Sans <code>node_modules</code> : il est installé automatiquement.\n\nMax 20 Mo. Python ou Node.js.\n"
                    "Ton code lira le token via la variable <code>BOT_TOKEN</code>."),
               kb([("❌ Annuler", "home")]))


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
        return await m.answer("❌ Aucun fichier principal trouvé (main.py, bot.py, app.py…).")
    issues = await asyncio.to_thread(runner.scan, root)
    if issues:
        shutil.rmtree(dest, ignore_errors=True)
        await state.clear()
        return await m.answer("⛔ Code refusé à la vérification :\n" + "\n".join(f"• {esc(i)}" for i in issues))
    await state.update_data(dest=dest, root=root, entry=entry)
    await state.set_state(Deploy.token)
    await m.answer(f"✅ Code reçu (<code>{esc(entry)}</code>).\n\nEnvoie maintenant le <b>token</b> de ton bot "
                   "(BotFather). Ton message sera supprimé.")


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
    data = await state.get_data()
    tb = Bot(tok)
    try:
        me = await tb.get_me()
    except Exception:
        return await m.answer("❌ Token refusé par Telegram. Vérifie-le.")
    finally:
        await tb.session.close()
    uid = m.from_user.id
    if not db.spend(uid, DEPLOY_COST):
        shutil.rmtree(data["dest"], ignore_errors=True)
        await state.clear()
        return await m.answer("Solde insuffisant.")
    status = "pending" if REQUIRE_APPROVAL and uid not in ADMINS else "stopped"
    bid = db.add_bot(uid, "@" + me.username, data["dest"], data["root"], data["entry"], tok, status)
    await state.clear()
    if status == "pending":
        await m.answer(f"📦 <b>@{esc(me.username)}</b> envoyé. En attente de validation par un admin.",
                       reply_markup=kb([("📂 Mes bots", "mybots")]))
        for a in ADMINS:
            try:
                await m.bot.send_message(
                    a, f"🆕 Bot #{bid} @{esc(me.username)} de <code>{uid}</code> à valider.",
                    reply_markup=kb([(f"✅ Approuver #{bid}", f"adm:ok:{bid}"), (f"❌ Refuser #{bid}", f"adm:no:{bid}")]))
            except Exception:
                pass
    else:
        await m.answer(f"✅ <b>@{esc(me.username)}</b> déployé. Lance-le depuis le panel.",
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
    return uid in ADMINS


@r.callback_query(F.data == "admin")
async def cb_admin(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        return await cq.answer()
    c = db.counts()
    rows = []
    for b in db.bots_by_status("pending"):
        rows.append([(f"✅ #{b['id']} {b['name']}", f"adm:ok:{b['id']}"), (f"❌ #{b['id']}", f"adm:no:{b['id']}")])
    rows.append([("⬅️ Retour", "home")])
    await edit(cq, (f"🛠️ <b>Admin</b>\n\n👥 Utilisateurs : {c['users']}\n🤖 Bots : {c['bots']} "
                    f"(🟢 {c['running']})\n🟡 À valider : {c['pending']}\n\n"
                    "/addcoins &lt;id&gt; &lt;n&gt;\n/stopbot &lt;bot_id&gt;\n/broadcast &lt;texte&gt;"), kb(*rows))


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
        return await m.answer("Usage : /addcoins <id> <n>")
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
        return await m.answer("Usage : /stopbot <bot_id>")
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
