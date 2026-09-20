import os
import json
import time
import secrets
import logging
import asyncio

from http.server import BaseHTTPRequestHandler

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

BOT_TOKEN = os.environ.get("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is missing")


REQUEST_TTL = 15 * 60

# V1 temporary storage.
# WARNING:
# Vercel serverless instances are not persistent storage.
REQUESTS = {}
AUTHORIZED_TARGETS = {}
BACKUP = {}


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

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
                callback_data="copy_start",
            )
        ],
        [
            InlineKeyboardButton(
                "♻️ Restore",
                callback_data="restore",
            ),
            InlineKeyboardButton(
                "📊 Current Profile",
                callback_data="current",
            ),
        ],
    ])


def permission_keyboard(request_id):

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ APPROVE",
                callback_data=f"approve:{request_id}",
            ),
            InlineKeyboardButton(
                "❌ DENY",
                callback_data=f"deny:{request_id}",
            ),
        ]
    ])


# ============================================================
# START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.effective_user:
        return

    user = update.effective_user

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
        "🐱 CopyCat\n\n"
        "Welcome.\n\n"
        "This bot can create an authorized profile replica "
        "using information Telegram makes available to the bot.\n\n"
        "Choose an action:",
        reply_markup=main_menu(),
    )


# ============================================================
# COPYCAT
# ============================================================

async def copycat(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    cleanup_requests()

    if not update.effective_user:
        return

    if not update.message:
        return

    requester = update.effective_user

    args = context.args

    if not args:

        await update.message.reply_text(
            "🐱 CopyCat\n\n"
            "Usage:\n"
            "/copycat @username\n\n"
            "The target must have interacted with "
            "this bot before authorization can be requested."
        )

        return

    target_text = args[0].strip()

    target = None

    # --------------------------------------------------------
    # Find by username
    # --------------------------------------------------------

    if target_text.startswith("@"):

        wanted = target_text[1:].lower()

        for user_id, data in AUTHORIZED_TARGETS.items():

            username = data.get("username")

            if username and username.lower() == wanted:

                target = data
                break

    # --------------------------------------------------------
    # Find by Telegram ID
    # --------------------------------------------------------

    elif target_text.lstrip("-").isdigit():

        target_id = int(target_text)

        target = AUTHORIZED_TARGETS.get(target_id)

    # --------------------------------------------------------
    # Target not found
    # --------------------------------------------------------

    if not target:

        await update.message.reply_text(
            "❌ I can't resolve that target.\n\n"
            "The user must first open this bot and press "
            "/start before the bot can request authorization."
        )

        return

    # --------------------------------------------------------
    # Don't copy yourself
    # --------------------------------------------------------

    if target["id"] == requester.id:

        await update.message.reply_text(
            "😅 That's your own profile."
        )

        return

    # --------------------------------------------------------
    # Create permission request
    # --------------------------------------------------------

    request_id = new_request_id()

    REQUESTS[request_id] = {
        "requester_id": requester.id,
        "target_id": target["id"],
        "target_username": target.get("username"),
        "created_at": time.time(),
        "status": "pending",
    }

    # --------------------------------------------------------
    # Notify target
    # --------------------------------------------------------

    try:

        await context.bot.send_message(
            chat_id=target["id"],
            text=(
                "🔐 COPYCAT REQUEST\n\n"
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

        logger.exception(
            "Could not contact target: %s",
            exc,
        )

        await update.message.reply_text(
            "❌ I couldn't contact the target.\n\n"
            "They need to start this bot first."
        )

        return

    await update.message.reply_text(
        "⏳ Permission request sent.\n\n"
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

    if not query:
        return

    await query.answer()

    data = query.data

    if not data or ":" not in data:
        return

    action, request_id = data.split(":", 1)

    request = REQUESTS.get(request_id)

    if not request:

        await query.edit_message_text(
            "⌛ This request has expired."
        )

        return

    target_id = request["target_id"]

    # Only the requested target can respond.

    if query.from_user.id != target_id:

        await query.answer(
            "You are not the target of this request.",
            show_alert=True,
        )

        return

    requester_id = request["requester_id"]

    # --------------------------------------------------------
    # DENY
    # --------------------------------------------------------

    if action == "deny":

        request["status"] = "denied"

        await query.edit_message_text(
            "❌ COPYCAT REQUEST DENIED\n\n"
            "No profile information will be copied."
        )

        try:

            await context.bot.send_message(
                requester_id,
                "❌ The target denied your CopyCat request.",
            )

        except Exception:
            pass

        REQUESTS.pop(request_id, None)

        return

    # --------------------------------------------------------
    # APPROVE
    # --------------------------------------------------------

    if action == "approve":

        request["status"] = "approved"
        request["approved_at"] = time.time()

        if target_id in AUTHORIZED_TARGETS:

            AUTHORIZED_TARGETS[target_id]["authorized"] = True

        await query.edit_message_text(
            "✅ COPYCAT REQUEST APPROVED\n\n"
            "Authorization recorded.\n\n"
            "No API credentials or Telegram session "
            "were requested or collected."
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
# PROFILE PHOTO
# ============================================================

async def capture_profile_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.effective_user:
        return

    user = update.effective_user

    try:

        photos = await context.bot.get_user_profile_photos(
            user_id=user.id,
            limit=1,
        )

        if not photos.photos:
            return

        # Highest available resolution.

        photo = photos.photos[0][-1]

        if user.id not in AUTHORIZED_TARGETS:

            AUTHORIZED_TARGETS[user.id] = {
                "id": user.id,
                "username": user.username,
                "first_name": user.first_name or "",
                "last_name": user.last_name or "",
                "authorized": True,
                "updated": time.time(),
            }

        AUTHORIZED_TARGETS[user.id]["photo_file_id"] = (
            photo.file_id
        )

    except Exception as exc:

        logger.exception(
            "Could not capture profile photo: %s",
            exc,
        )


# ============================================================
# APPLY PROFILE
# ============================================================

async def apply_profile(
    context: ContextTypes.DEFAULT_TYPE,
    profile: dict,
):

    bot = context.bot

    # --------------------------------------------------------
    # Backup
    # --------------------------------------------------------

    current_name = await bot.get_my_name()
    current_description = await bot.get_my_description()
    current_short_description = (
        await bot.get_my_short_description()
    )

    BACKUP["name"] = current_name.name
    BACKUP["description"] = (
        current_description.description
    )
    BACKUP["short_description"] = (
        current_short_description.short_description
    )

    # --------------------------------------------------------
    # Name
    # --------------------------------------------------------

    first_name = profile.get(
        "first_name",
        "",
    ).strip()

    last_name = profile.get(
        "last_name",
        "",
    ).strip()

    name = first_name

    if last_name:
        name = f"{first_name} {last_name}"

    name = name[:64]

    if name:

        await bot.set_my_name(
            name=name
        )

    # --------------------------------------------------------
    # Description
    # --------------------------------------------------------

    about = profile.get(
        "about",
        "",
    ).strip()

    if about:

        await bot.set_my_description(
            description=about[:512]
        )

        await bot.set_my_short_description(
            short_description=about[:120]
        )

    # --------------------------------------------------------
    # Profile photo
    # --------------------------------------------------------

    photo_file_id = profile.get(
        "photo_file_id"
    )

    if photo_file_id:

        tg_file = await bot.get_file(
            photo_file_id
        )

        photo_bytes = await tg_file.download_as_bytearray()

        profile_photo = InputProfilePhotoStatic(
            photo=bytes(photo_bytes)
        )

        await bot.set_my_profile_photo(
            photo=profile_photo
        )


# ============================================================
# RESTORE
# ============================================================

async def restore(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:

        if update.callback_query:
            await update.callback_query.message.reply_text(
                "❌ Restore cannot be executed here."
            )

        return

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
            description=BACKUP.get(
                "description",
                "",
            )
        )

        await context.bot.set_my_short_description(
            short_description=BACKUP.get(
                "short_description",
                "",
            )
        )

        await update.message.reply_text(
            "♻️ Profile restored."
        )

    except Exception as exc:

        logger.exception(
            "Restore failed: %s",
            exc,
        )

        await update.message.reply_text(
            f"❌ Restore failed:\n{exc}"
        )


# ============================================================
# STATUS
# ============================================================

async def status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    bot = context.bot

    name = await bot.get_my_name()

    description = await bot.get_my_description()

    short_description = (
        await bot.get_my_short_description()
    )

    text = (
        "📊 Current Bot Profile\n\n"
        f"👤 Name: {name.name}\n"
        f"📝 Description: "
        f"{description.description or '(empty)'}\n"
        f"📌 Short description: "
        f"{short_description.short_description or '(empty)'}\n\n"
        "🔗 Username: controlled by Telegram/BotFather"
    )

    if update.message:

        await update.message.reply_text(text)

    elif update.callback_query:

        await update.callback_query.message.reply_text(
            text
        )


# ============================================================
# BUTTON HANDLER
# ============================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    if not query:
        return

    if query.data == "copy_start":

        await query.answer()

        await query.message.reply_text(
            "🐱 CopyCat\n\n"
            "Use:\n"
            "/copycat @username\n\n"
            "The target must have started this bot first."
        )

        return

    if query.data == "restore":

        await query.answer()

        # Restore using the bot context directly.

        if not BACKUP:

            await query.message.reply_text(
                "❌ No backup exists yet."
            )

            return

        try:

            await context.bot.set_my_name(
                name=BACKUP.get("name", "")
            )

            await context.bot.set_my_description(
                description=BACKUP.get(
                    "description",
                    "",
                )
            )

            await context.bot.set_my_short_description(
                short_description=BACKUP.get(
                    "short_description",
                    "",
                )
            )

            await query.message.reply_text(
                "♻️ Profile restored."
            )

        except Exception as exc:

            logger.exception(
                "Button restore failed: %s",
                exc,
            )

            await query.message.reply_text(
                f"❌ Restore failed:\n{exc}"
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

    logger.error(
        "Unhandled Telegram error",
        exc_info=context.error,
    )


# ============================================================
# APPLICATION
# ============================================================

application = (
    Application.builder()
    .token(BOT_TOKEN)
    .updater(None)
    .build()
)


application.add_handler(
    CommandHandler(
        "start",
        start,
    )
)

application.add_handler(
    CommandHandler(
        "copycat",
        copycat,
    )
)

application.add_handler(
    CommandHandler(
        "restore",
        restore,
    )
)

application.add_handler(
    CommandHandler(
        "status",
        status,
    )
)

application.add_handler(
    CallbackQueryHandler(
        permission_callback,
        pattern=r"^(approve|deny):",
    )
)

application.add_handler(
    CallbackQueryHandler(
        button_handler,
        pattern=r"^(copy_start|restore|current)$",
    )
)

application.add_handler(
    MessageHandler(
        filters.PHOTO & filters.ChatType.PRIVATE,
        capture_profile_photo,
    )
)

application.add_error_handler(
    error_handler
)


# ============================================================
# INITIALIZATION
# ============================================================

_initialized = False
_init_lock = asyncio.Lock()


async def ensure_application():

    global _initialized

    async with _init_lock:

        if not _initialized:

            await application.initialize()

            await application.start()

            _initialized = True

            logger.info(
                "Telegram application initialized"
            )


# ============================================================
# PROCESS TELEGRAM UPDATE
# ============================================================

async def process_update(data):

    await ensure_application()

    update = Update.de_json(
        data,
        application.bot,
    )

    await application.process_update(
        update
    )


# ============================================================
# VERCEL HTTP HANDLER
# ============================================================

class handler(BaseHTTPRequestHandler):

    def send_json(
        self,
        status_code,
        data,
    ):

        body = json.dumps(
            data
        ).encode("utf-8")

        self.send_response(
            status_code
        )

        self.send_header(
            "Content-Type",
            "application/json",
        )

        self.send_header(
            "Content-Length",
            str(len(body)),
        )

        self.end_headers()

        self.wfile.write(
            body
        )

    # --------------------------------------------------------
    # GET
    # --------------------------------------------------------

   