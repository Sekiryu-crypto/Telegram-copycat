"""
CopyCat bot - Vercel webhook (api/index.py)

Flow:
  1. Target opens the bot and sends /start  -> stored in Redis
  2. Requester sends /copycat @username     -> target gets an APPROVE / DENY message
  3. Target approves                        -> requester receives name, bio, username
     and ALL visible profile photos (oldest -> newest), and the bot's own
     profile (name, description, photo) is updated. /restore undoes it.

Environment variables (Vercel -> Settings -> Environment Variables):
  BOT_TOKEN                   required  (from @BotFather)
  UPSTASH_REDIS_REST_URL      required for reliability (free Upstash Redis)
  UPSTASH_REDIS_REST_TOKEN    required for reliability
  OWNER_ID                    recommended (your numeric Telegram ID, only you can use /copycat)
  WEBHOOK_SECRET              optional (same value used in setWebhook secret_token)
"""

import os
import json
import time
import html
import secrets
import asyncio
import logging
from http.server import BaseHTTPRequestHandler

import httpx
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()
OWNER_ID = os.environ.get("OWNER_ID", "").strip()

REDIS_URL = (
    os.environ.get("UPSTASH_REDIS_REST_URL")
    or os.environ.get("KV_REST_API_URL")
    or ""
).strip().rstrip("/")

REDIS_TOKEN = (
    os.environ.get("UPSTASH_REDIS_REST_TOKEN")
    or os.environ.get("KV_REST_API_TOKEN")
    or ""
).strip()

REQUEST_TTL = 15 * 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

esc = html.escape
HTML = ParseMode.HTML


# ============================================================
# STORAGE (Upstash Redis REST, memory fallback)
# ============================================================
# Vercel functions are stateless, so real storage is needed.
# Without Redis the bot still runs, but approvals can be lost
# between requests.

_MEM = {}


async def _redis(*args):
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            REDIS_URL,
            headers={"Authorization": f"Bearer {REDIS_TOKEN}"},
            json=[str(a) for a in args],
        )
        response.raise_for_status()
        body = response.json()
        if isinstance(body, dict) and body.get("error"):
            raise RuntimeError(body["error"])
        return body.get("result")


async def db_get(key):
    if REDIS_URL and REDIS_TOKEN:
        raw = await _redis("GET", key)
        return json.loads(raw) if raw else None

    item = _MEM.get(key)
    if not item:
        return None
    value, expires = item
    if expires and expires < time.time():
        _MEM.pop(key, None)
        return None
    return value


async def db_set(key, value, ex=None):
    if REDIS_URL and REDIS_TOKEN:
        args = ["SET", key, json.dumps(value)]
        if ex:
            args += ["EX", ex]
        await _redis(*args)
        return

    _MEM[key] = (value, time.time() + ex if ex else None)


async def db_delete(key):
    if REDIS_URL and REDIS_TOKEN:
        await _redis("DEL", key)
        return
    _MEM.pop(key, None)


# ============================================================
# HELPERS
# ============================================================

def allowed(user_id):
    """If OWNER_ID is set, only that user can use the bot's actions."""
    return (not OWNER_ID) or str(user_id) == OWNER_ID


def full_name(first, last):
    return " ".join(p for p in [first or "", last or ""] if p).strip()


async def reply(update, context, text, **kwargs):
    chat = update.effective_chat
    if not chat:
        return
    await context.bot.send_message(chat_id=chat.id, text=text, **kwargs)


def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🐱 Copy Profile", callback_data="copy_start")],
        [
            InlineKeyboardButton("♻️ Restore", callback_data="restore"),
            InlineKeyboardButton("📊 Current Profile", callback_data="current"),
        ],
    ])


def permission_keyboard(request_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ APPROVE", callback_data=f"approve:{request_id}"),
        InlineKeyboardButton("❌ DENY", callback_data=f"deny:{request_id}"),
    ]])


async def register_user(user):
    data = {
        "id": user.id,
        "username": user.username or "",
        "first_name": user.first_name or "",
        "last_name": user.last_name or "",
        "updated": time.time(),
    }
    await db_set(f"user:{user.id}", data)
    if user.username:
        await db_set(f"uname:{user.username.lower()}", user.id)


async def resolve_target(text):
    text = text.strip().lstrip("@")
    if not text:
        return None

    if text.isdigit():
        return await db_get(f"user:{int(text)}")

    wanted = text.lower()
    uid = await db_get(f"uname:{wanted}")
    if not uid:
        return None

    stored = await db_get(f"user:{uid}")
    if stored and (stored.get("username") or "").lower() == wanted:
        return stored
    return None


async def set_bot_photo(file_bytes):
    """Bot API setMyProfilePhoto via raw HTTP (independent of library version)."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/setMyProfilePhoto"
    photo_spec = json.dumps({"type": "static", "photo": "attach://pic"})
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            url,
            data={"photo": photo_spec},
            files={"pic": ("pfp.jpg", file_bytes, "image/jpeg")},
        )
    body = response.json()
    if not body.get("ok"):
        raise RuntimeError(body.get("description", "unknown error"))


# ============================================================
# /start
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    await register_user(user)

    await reply(
        update,
        context,
        "🐱 <b>CopyCat</b>\n\n"
        "You are registered, so other people can now request permission "
        "to copy your public profile.\n\n"
        "Nothing is ever copied without your approval.\n\n"
        "Choose an action:",
        parse_mode=HTML,
        reply_markup=main_menu(),
    )


# ============================================================
# /copycat
# ============================================================

async def copycat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    await register_user(user)

    if not allowed(user.id):
        await reply(update, context, "⛔ Only the bot owner can request copies.")
        return

    if not context.args:
        await reply(
            update,
            context,
            "🐱 CopyCat\n\nUsage:\n/copycat @username\n\n"
            "The target must have pressed /start in this bot first.",
        )
        return

    target = await resolve_target(context.args[0])

    if not target:
        await reply(
            update,
            context,
            "❌ I can't find that user.\n\n"
            "They must open this bot and press /start first.",
        )
        return

    if target["id"] == user.id:
        await reply(update, context, "😅 That's your own profile.")
        return

    request_id = secrets.token_urlsafe(8)

    await db_set(
        f"req:{request_id}",
        {
            "requester_id": user.id,
            "target_id": target["id"],
            "created_at": time.time(),
        },
        ex=REQUEST_TTL,
    )

    who = esc(full_name(user.first_name, user.last_name) or "Someone")
    handle = f" (@{esc(user.username)})" if user.username else ""

    try:
        await context.bot.send_message(
            chat_id=target["id"],
            text=(
                "🔐 <b>COPYCAT REQUEST</b>\n\n"
                f"<b>{who}</b>{handle} (ID <code>{user.id}</code>) "
                "asks permission to copy your profile:\n"
                "• display name\n• bio\n• username (shown, not transferable)\n"
                "• all profile photos visible to this bot\n\n"
                "Nothing is copied unless you approve.\n"
                "This does NOT share your phone number, password, API keys "
                "or Telegram session.\n\n"
                "The request expires in 15 minutes."
            ),
            parse_mode=HTML,
            reply_markup=permission_keyboard(request_id),
        )
    except Exception as exc:
        await db_delete(f"req:{request_id}")
        logger.warning("Could not contact target: %s", exc)
        await reply(
            update,
            context,
            "❌ I couldn't contact the target.\n\nThey need to press /start in this bot.",
        )
        return

    await reply(
        update,
        context,
        "⏳ Permission request sent.\n\nWaiting for the target to approve (15 minutes).",
    )


# ============================================================
# APPROVE / DENY
# ============================================================

async def permission_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return

    action, _, request_id = query.data.partition(":")

    request = await db_get(f"req:{request_id}")

    if not request:
        await query.answer("This request has expired.", show_alert=True)
        try:
            await query.edit_message_text("⌛ This request has expired.")
        except Exception:
            pass
        return

    if query.from_user.id != request["target_id"]:
        await query.answer("You are not the target of this request.", show_alert=True)
        return

    await query.answer()

    # Consume the request so a double-tap can't run it twice.
    await db_delete(f"req:{request_id}")

    requester_id = request["requester_id"]

    if action == "deny":
        await query.edit_message_text(
            "❌ COPYCAT REQUEST DENIED\n\nNothing will be copied."
        )
        try:
            await context.bot.send_message(
                requester_id, "❌ The target denied your CopyCat request."
            )
        except Exception:
            pass
        return

    if action == "approve":
        await query.edit_message_text(
            "✅ COPYCAT REQUEST APPROVED\n\n"
            "Only your public profile info was used. "
            "No credentials or sessions were requested."
        )
        try:
            await perform_copy(context.bot, requester_id, request["target_id"])
        except Exception as exc:
            logger.exception("perform_copy failed: %s", exc)
            try:
                await context.bot.send_message(
                    requester_id, f"⚠️ Copy failed: {exc}"
                )
            except Exception:
                pass


# ============================================================
# COPY PROCESS
# ============================================================

async def ensure_backup(bot):
    """Save the bot's original profile once (never overwrite with a copy)."""
    if await db_get("backup"):
        return

    name = await bot.get_my_name()
    description = await bot.get_my_description()
    short_description = await bot.get_my_short_description()

    await db_set(
        "backup",
        {
            "name": name.name,
            "description": description.description,
            "short_description": short_description.short_description,
        },
    )


async def perform_copy(bot, requester_id, target_id):
    stored = await db_get(f"user:{target_id}") or {}

    first = stored.get("first_name", "")
    last = stored.get("last_name", "")
    username = stored.get("username", "")
    bio = ""

    # Fresh info straight from Telegram.
    try:
        chat = await bot.get_chat(target_id)
        first = chat.first_name or first
        last = chat.last_name or ""
        username = chat.username or ""
        bio = getattr(chat, "bio", None) or ""
    except Exception as exc:
        logger.warning("get_chat failed: %s", exc)

    # All visible profile photos. Telegram returns newest first.
    file_ids = []
    try:
        photos = await bot.get_user_profile_photos(user_id=target_id, limit=100)
        file_ids = [sizes[-1].file_id for sizes in photos.photos]
    except Exception as exc:
        logger.warning("get_user_profile_photos failed: %s", exc)

    newest_first = list(file_ids)
    oldest_first = list(reversed(file_ids))

    name = full_name(first, last)

    # --------------------------------------------------------
    # Report to requester
    # --------------------------------------------------------

    await bot.send_message(
        chat_id=requester_id,
        text=(
            "✅ <b>Target approved your request</b>\n\n"
            f"👤 Name: {esc(name) or '(none)'}\n"
            f"🔗 Username: {('@' + esc(username)) if username else '(none)'}\n"
            f"📝 Bio: {esc(bio) or '(empty)'}\n"
            f"🖼 Photos: {len(file_ids)}\n\n"
            "Usernames are unique, so they can't be copied - only shown."
        ),
        parse_mode=HTML,
    )

    results = []

    # --------------------------------------------------------
    # Send all photos in order (oldest -> newest)
    # --------------------------------------------------------

    if oldest_first:
        try:
            for i in range(0, len(oldest_first), 10):
                chunk = oldest_first[i:i + 10]
                if len(chunk) == 1:
                    await bot.send_photo(chat_id=requester_id, photo=chunk[0])
                else:
                    await bot.send_media_group(
                        chat_id=requester_id,
                        media=[InputMediaPhoto(f) for f in chunk],
                    )
                await asyncio.sleep(0.5)
            results.append(
                f"✅ {len(oldest_first)} photo(s) sent, oldest → newest "
                "(upload them in this order so the newest ends up as current)"
            )
        except Exception as exc:
            results.append(f"⚠️ Sending photos failed: {exc}")
    else:
        results.append("ℹ️ No visible profile photos (none set, or hidden by privacy).")

    # --------------------------------------------------------
    # Apply to the bot's own profile
    # --------------------------------------------------------

    can_apply = True
    try:
        await ensure_backup(bot)
    except Exception as exc:
        can_apply = False
        results.append(f"⚠️ Backup failed, bot profile not changed: {exc}")

    if can_apply:
        if name:
            try:
                await bot.set_my_name(name=name[:64])
                results.append("✅ Bot name updated")
            except Exception as exc:
                results.append(f"⚠️ Bot name failed: {exc}")

        try:
            await bot.set_my_description(description=bio[:512])
            await bot.set_my_short_description(short_description=bio[:120])
            results.append("✅ Bot description updated")
        except Exception as exc:
            results.append(f"⚠️ Bot description failed: {exc}")

        if newest_first:
            try:
                tg_file = await bot.get_file(newest_first[0])
                photo_bytes = bytes(await tg_file.download_as_bytearray())
                await set_bot_photo(photo_bytes)
                results.append("✅ Bot photo updated")
            except Exception as exc:
                results.append(f"⚠️ Bot photo failed: {exc}")

        results.append("ℹ️ Bot username is controlled by @BotFather and can't be changed here.")

    await bot.send_message(
        chat_id=requester_id,
        text="🐱 <b>CopyCat result</b>\n\n" + "\n".join(esc(r) for r in results)
        + "\n\nUse /restore to bring back the original bot name and description.",
        parse_mode=HTML,
    )


# ============================================================
# /restore
# ============================================================

async def restore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    if not allowed(user.id):
        await reply(update, context, "⛔ Only the bot owner can restore.")
        return

    backup = await db_get("backup")

    if not backup:
        await reply(update, context, "❌ No backup exists yet.")
        return

    try:
        await context.bot.set_my_name(name=backup.get("name", "")[:64])
        await context.bot.set_my_description(description=backup.get("description", ""))
        await context.bot.set_my_short_description(
            short_description=backup.get("short_description", "")
        )
        await db_delete("backup")
    except Exception as exc:
        logger.exception("Restore failed: %s", exc)
        await reply(update, context, f"❌ Restore failed:\n{exc}")
        return

    await reply(
        update,
        context,
        "♻️ Name and description restored.\n\n"
        "The Bot API can't read the bot's original photo, so set it again "
        "in @BotFather if you want it back.",
    )


# ============================================================
# /status
# ============================================================

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot = context.bot

    name = await bot.get_my_name()
    description = await bot.get_my_description()
    short_description = await bot.get_my_short_description()

    await reply(
        update,
        context,
        "📊 <b>Current Bot Profile</b>\n\n"
        f"👤 Name: {esc(name.name)}\n"
        f"📝 Description: {esc(description.description or '(empty)')}\n"
        f"📌 Short description: {esc(short_description.short_description or '(empty)')}\n\n"
        "🔗 Username: controlled by @BotFather",
        parse_mode=HTML,
    )


# ============================================================
# BUTTONS
# ============================================================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    await query.answer()

    if query.data == "copy_start":
        await reply(
            update,
            context,
            "🐱 CopyCat\n\nUse:\n/copycat @username\n\n"
            "The target must have pressed /start in this bot first.",
        )
    elif query.data == "restore":
        await restore(update, context)
    elif query.data == "current":
        await status(update, context)


# ============================================================
# ERRORS
# ============================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled error", exc_info=context.error)

    if isinstance(update, Update) and update.effective_chat:
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ Something went wrong. Please try again.",
            )
        except Exception:
            pass


# ============================================================
# APPLICATION (built per request - required for serverless)
# ============================================================

def build_app():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .updater(None)
        .job_queue(None)
        .connect_timeout(10)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(10)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("copycat", copycat))
    app.add_handler(CommandHandler("restore", restore))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(
        CallbackQueryHandler(permission_callback, pattern=r"^(approve|deny):")
    )
    app.add_handler(
        CallbackQueryHandler(button_handler, pattern=r"^(copy_start|restore|current)$")
    )
    app.add_error_handler(error_handler)
    return app


async def process_update(data):
    app = build_app()
    async with app:  # initialize() ... shutdown()
        update = Update.de_json(data, app.bot)
        await app.process_update(update)


# ============================================================
# VERCEL HTTP HANDLER
# ============================================================

class handler(BaseHTTPRequestHandler):

    def _send(self, status_code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send(
            200,
            {
                "ok": True,
                "service": "CopyCat bot",
                "token_set": bool(BOT_TOKEN),
                "redis_configured": bool(REDIS_URL and REDIS_TOKEN),
                "owner_locked": bool(OWNER_ID),
            },
        )

    def do_POST(self):
        if not BOT_TOKEN:
            self._send(500, {"ok": False, "error": "BOT_TOKEN is missing"})
            return

        if WEBHOOK_SECRET:
            received = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if received != WEBHOOK_SECRET:
                self._send(403, {"ok": False, "error": "bad secret"})
                return

        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw or b"{}")
            asyncio.run(process_update(data))
        except Exception as exc:
            logger.exception("Webhook processing failed: %s", exc)

        # Always answer 200 so Telegram doesn't retry the same update forever.
        self._send(200, {"ok": True})
