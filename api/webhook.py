import os
import json
import time
import secrets
import logging
from typing import Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputProfilePhotoStatic,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.environ["BOT_TOKEN"]

# Simple in-memory storage for V1/testing.
# IMPORTANT: Vercel instances are not persistent.
# Replace this with PostgreSQL/Redis for production.
REQUESTS = {}
AUTHORIZED_TARGETS = {}
BACKUP = {}

REQUEST_TTL = 15 * 60

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ============================================================
# HELPERS
# ============================================================

def cleanup_requests():
    now = time.time()

    expired = [
        request_id
        for request_id, data in REQUESTS.items()
        if now - data["created_at"] > REQUEST_TTL
    ]

    for request_id in expired:
        REQUESTS.pop(request_id, None)


def new_request_id():
    return secrets.token_urlsafe(8)


def main_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🐱 Copy Profile",
                callback_data="copy_start"
            )
        ],
        [
            InlineKeyboardButton(
                "♻️ Restore",
                callback_data="restore"
            ),
            InlineKeyboardButton(
                "📊 Current Profile",
                callback_data="current"
            )
        ]
    ])


def permission_keyboard(request_id: str):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ APPROVE",
                callback_data=f"approve:{request_id}"
            ),
            InlineKeyboardButton(
                "❌ DENY",
                callback_data=f"deny:{request_id}"
            )
        ]
    ])


# ============================================================
# /START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # Save the user's currently available Telegram identity.
    # This does NOT give us their API credentials.
    AUTHORIZED_TARGETS[user.id] = {
        "id": user.id,
        "username": user.username,
        "first_name": user.first_name or "",
        "last_name": user.last_name or "",
        "photo_file_id": None,
        "authorized": True,
        "updated": time.time(),
    }

    await update.message.reply_text(
        "🐱 *CopyCat*\n\n"
        "Welcome.\n\n"
        "This bot can create an authorized profile replica "
        "using information Telegram makes available to the bot.\n\n"
        "Choose an action:",
        parse_mode="Markdown",
        reply_markup=main_menu(),
    )


# ============================================================
# COPY COMMAND
# ============================================================

async def copycat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cleanup_requests()

    args = context.args

    if not args:
        await update.message.reply_text(
            "🐱 *CopyCat*\n\n"
            "Usage:\n"
            "`/copycat @username`\n\n"
            "The target must have interacted with this bot "
            "before authorization can be requested.",
            parse_mode="Markdown",
        )
        return

    target_text = args[0].strip()

    requester = update.effective_user

    # Find target among users who have actually interacted
    target = None

    if target_text.startswith("@"):
        wanted = target_text[1:].lower()

        for user_id, data in AUTHORIZED_TARGETS.items():
            username = data.get("username")

            if username and username.lower() == wanted:
                target = data
                break

    elif target_text.lstrip("-").isdigit():
        target_id = int(target_text)

        target = AUTHORIZED_TARGETS.get(target_id)

    if not target:
        await update.message.reply_text(
            "❌ I can't resolve that target.\n\n"
            "The user must first open this bot and press /start "
            "before the bot can request authorization from them."
        )
        return

    if target["id"] == requester.id:
        await update.message.reply_text(
            "😅 That's your own profile."
        )
        return

    request_id = new_request_id()

    REQUESTS[request_id] = {
        "requester_id": requester.id,
        "target_id": target["id"],
        "target_username": target.get("username"),
        "created_at": time.time(),
        "status": "pending",
    }

    # Notify the target
    try:
        await context.bot.send_message(
            chat_id=target["id"],
            text=(
                "🔐 *COPYCAT REQUEST*\n\n"
                f"User `{requester.id}` requested permission "
                "to use your available profile information.\n\n"
                "Nothing will be copied until you approve.\n\n"
                "This authorization does NOT provide your "
                "API ID, API hash, password, phone number, "
                "or Telegram session."
            ),
            parse_mode="Markdown",
            reply_markup=permission_keyboard(request_id),
        )
    except Exception as exc:
        REQUESTS.pop(request_id, None)

        await update.message.reply_text(
            "❌ I couldn't contact the target.\n\n"
            "They need to start the bot first."
        )

        logger.exception("Could not contact target: %s", exc)
        return

    await update.message.reply_text(
        "⏳ *Permission request sent.*\n\n"
        f"Request ID: `{request_id}`\n\n"
        "Waiting for the target to approve.",
        parse_mode="Markdown",
    )


# ============================================================
# APPROVE / DENY
# ============================================================

async def permission_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    await query.answer()

    data = query.data

    if ":" not in data:
        return

    action, request_id = data.split(":", 1)

    request = REQUESTS.get(request_id)

    if not request:
        await query.edit_message_text(
            "⌛ This request has expired."
        )
        return

    target_id = request["target_id"]

    # Only the intended target can approve/deny.
    if query.from_user.id != target_id:
        await query.answer(
            "You are not the target of this request.",
            show_alert=True,
        )
        return

    requester_id = request["requester_id"]

    if action == "deny":

        request["status"] = "denied"

        await query.edit_message_text(
            "❌ *COPYCAT REQUEST DENIED*\n\n"
            "No profile information will be copied.",
            parse_mode="Markdown",
        )

        try:
            await context.bot.send_message(
                requester_id,
                "❌ The target denied your CopyCat request."
            )
        except Exception:
            pass

        REQUESTS.pop(request_id, None)
        return

    if action == "approve":

        request["status"] = "approved"
        request["approved_at"] = time.time()

        AUTHORIZED_TARGETS[target_id]["authorized"] = True

        await query.edit_message_text(
            "✅ *COPYCAT REQUEST APPROVED*\n\n"
            "Authorization recorded.\n\n"
            "No API credentials or Telegram session "
            "were requested or collected.",
            parse_mode="Markdown",
        )

        try:
            await context.bot.send_message(
                requester_id,
                "✅ Target approved your request.\n\n"
                "The authorized profile information can now "
                "be used by the CopyCat process."
            )
        except Exception:
            pass


# ============================================================
# COPY PHOTO
# ============================================================

async def capture_profile_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """
    Optional method for targets to provide their current
    profile photo to the bot.

    A normal bot cannot arbitrarily download a user's complete
    Telegram profile-photo history.
    """

    user = update.effective_user

    photos = await context.bot.get_user_profile_photos(
        user_id=user.id,
        limit=1,
    )

    if not photos.photos:
        return

    # Smallest available size.
    photo = photos.photos[0][-1]

    AUTHORIZED_TARGETS.setdefault(user.id, {})

    AUTHORIZED_TARGETS[user.id]["photo_file_id"] = photo.file_id


# ============================================================
# APPLY PROFILE
# ============================================================

async def apply_profile(
    context: ContextTypes.DEFAULT_TYPE,
    profile: dict,
):
    bot = context.bot

    # --------------------------------------------------------
    # Backup current bot profile
    # --------------------------------------------------------

    current_name = await bot.get_my_name()
    current_description = await bot.get_my_description()
    current_short_description = await bot.get_my_short_description()

    BACKUP["name"] = current_name.name
    BACKUP["description"] = current_description.description
    BACKUP["short_description"] = (
        current_short_description.short_description
    )

    # --------------------------------------------------------
    # Name
    # --------------------------------------------------------

    name = profile.get("first_name", "").strip()

    last_name = profile.get("last_name", "").strip()

    if last_name:
        name = f"{name} {last_name}"

    # Telegram bot name max is 64 chars.
    name = name[:64]

    if name:
        await bot.set_my_name(name=name)

    # --------------------------------------------------------
    # Description
    # --------------------------------------------------------

    about = profile.get("about", "")

    if about:
        await bot.set_my_description(
            description=about[:512]
        )

        await bot.set_my_short_description(
            short_description=about[:120]
        )

    # --------------------------------------------------------
    # Photo
    # --------------------------------------------------------

    photo_file_id = profile.get("photo_file_id")

    if photo_file_id:

        tg_file = await bot.get_file(photo_file_id)

        photo_bytes = await tg_file.download_as_bytearray()

        photo = InputProfilePhotoStatic(
            photo=bytes(photo_bytes)
        )

        await bot.set_my_profile_photo(
            photo=photo
        )


# ============================================================
# /RESTORE
# ============================================================

async def restore(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not BACKUP:
        await update.message.reply_text(
            "❌ No backup exists yet."
        )
        return

    try:

        await context.bot.set_my_name(
            name=BACKUP.get("name", "")
        )

        await context.bot.set_my_description(
            description=BACKUP.get("description", "")
        )

        await context.bot.set_my_short_description(
            short_description=BACKUP.get(
                "short_description",
                ""
            )
        )

        await update.message.reply_text(
            "♻️ *Profile restored.*",
            parse_mode="Markdown",
        )

    except Exception as exc:

        logger.exception("Restore failed")

        await update.message.reply_text(
            f"❌ Restore failed:\n`{exc}`",
            parse_mode="Markdown",
        )


# ============================================================
# /STATUS
# ============================================================

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):

    bot = context.bot

    name = await bot.get_my_name()
    description = await bot.get_my_description()
    short_description = await bot.get_my_short_description()

    await update.message.reply_text(
        "📊 *Current Bot Profile*\n\n"
        f"👤 Name: `{name.name}`\n"
        f"📝 Description: `{description.description or '(empty)'}`\n"
        f"📌 Short description: "
        f"`{short_description.short_description or '(empty)'}`\n\n"
        "🔗 Username: controlled by Telegram/BotFather",
        parse_mode="Markdown",
    )


# ============================================================
# BUTTONS
# ============================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    if query.data == "copy_start":

        await query.answer()

        await query.message.reply_text(
            "🐱 *CopyCat*\n\n"
            "Use:\n"
            "`/copycat @username`\n\n"
            "The target must have started this bot first.",
            parse_mode="Markdown",
        )

        return

    if query.data == "restore":

        await query.answer()

        await restore(
            update,
            context,
        )

        return

    if query.data == "current":

        await query.answer()

        await status(
            update,
            context,
        )

        return


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):

    logger.exception(
        "Unhandled Telegram error",
        exc_info=context.error,
    )


# ============================================================
# APPLICATION
# ============================================================

application = (
    Application.builder()
    .token(BOT_TOKEN)
    .build()
)

application.add_handler(
    CommandHandler("start", start)
)

application.add_handler(
    CommandHandler("copycat", copycat)
)

application.add_handler(
    CommandHandler("restore", restore)
)

application.add_handler(
    CommandHandler("status", status)
)

application.add_handler(
    CallbackQueryHandler(
        permission_callback,
        pattern=r"^(approve|deny):"
    )
)

application.add_handler(
    CallbackQueryHandler(
        button_handler,
        pattern=r"^(copy_start|restore|current)$"
    )
)

application.add_handler(
    MessageHandler(
        filters.PHOTO & filters.ChatType.PRIVATE,
        capture_profile_photo,
    )
)

application.add_error_handler(error_handler)


# ============================================================
# VERCEL HANDLER
# ============================================================

async def process_update(data):

    update = Update.de_json(
        data,
        application.bot,
    )

    await application.initialize()

    if not application.running:
        await application.start()

    await application.process_update(update)


def handler(request):

    import asyncio

    if request.method == "GET":
        return {
            "statusCode": 200,
            "body": "CopyCat webhook is alive."
        }

    if request.method != "POST":
        return {
            "statusCode": 405,
            "body": "Method Not Allowed"
        }

    try:

        data = request.get_json()

        asyncio.run(
            process_update(data)
        )

        return {
            "statusCode": 200,
            "body": "OK"
        }

    except Exception as exc:

        logger.exception(
            "Webhook processing failed"
        )

        return {
            "statusCode": 500,
            "body": json.dumps({
                "error": str(exc)
            }),
        } 