import functools
import logging

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ChatMemberHandler,
    filters,
)

from config import BOT_TOKEN, API_BASE_URL, API_BASE_FILE_URL
import db
from handlers import admin, user, channel


def _admin_only(func):
    """Wrapper to restrict callback handlers to admins only."""
    @functools.wraps(func)
    async def wrapper(update: Update, context):
        if not db.is_admin(update.effective_user.id):
            await update.callback_query.answer("⛔ 无权操作", show_alert=True)
            return
        return await func(update, context)
    return wrapper

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


class _DynamicAdminFilter(filters.UpdateFilter):
    """Filter that checks admin status against the in-memory cache (updated dynamically)."""
    def filter(self, update: Update) -> bool:
        return update.effective_user is not None and db.is_admin(update.effective_user.id)


_admin_filter = _DynamicAdminFilter()


def _admin_media_filter():
    return (
        filters.Document.ALL
        | filters.PHOTO
        | filters.VIDEO
        | filters.AUDIO
        | filters.VOICE
        | filters.ANIMATION
    )


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set. Set BOT_TOKEN in .env file before running.")

    builder = Application.builder().token(BOT_TOKEN)
    if API_BASE_URL:
        builder = builder.base_url(API_BASE_URL).base_file_url(API_BASE_FILE_URL or API_BASE_URL)
        logger.info("Using local Bot API server: %s", API_BASE_URL)
    app = builder.build()

    # /start command
    app.add_handler(CommandHandler("start", user.start))

    # /admin command for authentication
    app.add_handler(CommandHandler("admin", admin.authenticate))

    # Track bot being added/removed from channels
    app.add_handler(ChatMemberHandler(channel.track_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

    # ---- Admin callbacks (all wrapped with admin check) ----
    app.add_handler(CallbackQueryHandler(_admin_only(admin.back_main), pattern=r"^back_main$"))
    app.add_handler(CallbackQueryHandler(_admin_only(admin.upload_start), pattern=r"^upload_start$"))
    app.add_handler(CallbackQueryHandler(_admin_only(admin.upload_done), pattern=r"^upload_done$"))
    app.add_handler(CallbackQueryHandler(_admin_only(admin.file_list), pattern=r"^file_list:\d+$"))
    app.add_handler(CallbackQueryHandler(_admin_only(admin.file_detail), pattern=r"^file_detail:\d+$"))
    app.add_handler(CallbackQueryHandler(_admin_only(admin.file_preview), pattern=r"^file_preview:\d+$"))
    app.add_handler(CallbackQueryHandler(_admin_only(admin.file_delete_confirm), pattern=r"^file_delete_confirm:\d+$"))
    app.add_handler(CallbackQueryHandler(_admin_only(admin.file_delete), pattern=r"^file_delete:\d+$"))
    app.add_handler(CallbackQueryHandler(_admin_only(admin.toggle_hidden), pattern=r"^toggle_hidden:\d+$"))
    app.add_handler(CallbackQueryHandler(_admin_only(admin.toggle_protect), pattern=r"^toggle_protect:\d+$"))
    app.add_handler(CallbackQueryHandler(_admin_only(admin.set_desc_start), pattern=r"^set_desc:\d+$"))
    app.add_handler(CallbackQueryHandler(_admin_only(admin.broadcast_start), pattern=r"^broadcast_start$"))
    app.add_handler(CallbackQueryHandler(_admin_only(admin.broadcast_confirm), pattern=r"^broadcast_confirm$"))
    app.add_handler(CallbackQueryHandler(_admin_only(admin.stats), pattern=r"^stats$"))

    # Code generation callbacks (admin only)
    app.add_handler(CallbackQueryHandler(_admin_only(admin.gen_code_start), pattern=r"^gen_code_start:\d+$"))
    app.add_handler(CallbackQueryHandler(_admin_only(admin.gen_code_qty), pattern=r"^gen_code_qty:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(_admin_only(admin.gen_code_custom_qty), pattern=r"^gen_code_custom_qty:\d+$"))
    app.add_handler(CallbackQueryHandler(_admin_only(admin.gen_code_custom_quota), pattern=r"^gen_code_custom_quota:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(_admin_only(admin.gen_code_do), pattern=r"^gen_code_do:\d+:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(_admin_only(admin.code_list), pattern=r"^code_list:\d+:\d+$"))

    # Watermark config callbacks (admin only)
    app.add_handler(CallbackQueryHandler(_admin_only(admin.wm_menu), pattern=r"^wm_menu$"))
    app.add_handler(CallbackQueryHandler(_admin_only(admin.wm_toggle), pattern=r"^wm_toggle$"))
    app.add_handler(CallbackQueryHandler(_admin_only(admin.wm_set_text), pattern=r"^wm_set_text$"))
    app.add_handler(CallbackQueryHandler(_admin_only(admin.wm_set_size), pattern=r"^wm_set_size$"))
    app.add_handler(CallbackQueryHandler(_admin_only(admin.wm_size_val), pattern=r"^wm_size_val:\d+$"))
    app.add_handler(CallbackQueryHandler(_admin_only(admin.wm_size_custom), pattern=r"^wm_size_custom$"))
    app.add_handler(CallbackQueryHandler(_admin_only(admin.wm_set_position), pattern=r"^wm_set_position$"))
    app.add_handler(CallbackQueryHandler(_admin_only(admin.wm_pos_val), pattern=r"^wm_pos_val:"))
    app.add_handler(CallbackQueryHandler(_admin_only(admin.wm_set_opacity), pattern=r"^wm_set_opacity$"))
    app.add_handler(CallbackQueryHandler(_admin_only(admin.wm_opacity_val), pattern=r"^wm_opacity_val:"))
    app.add_handler(CallbackQueryHandler(_admin_only(admin.wm_opacity_custom), pattern=r"^wm_opacity_custom$"))
    app.add_handler(CallbackQueryHandler(_admin_only(admin.wm_set_color), pattern=r"^wm_set_color$"))
    app.add_handler(CallbackQueryHandler(_admin_only(admin.wm_color_val), pattern=r"^wm_color_val:"))
    app.add_handler(CallbackQueryHandler(_admin_only(admin.wm_color_custom), pattern=r"^wm_color_custom$"))
    app.add_handler(CallbackQueryHandler(_admin_only(admin.wm_set_rotation), pattern=r"^wm_set_rotation$"))
    app.add_handler(CallbackQueryHandler(_admin_only(admin.wm_rot_val), pattern=r"^wm_rot_val:"))
    app.add_handler(CallbackQueryHandler(_admin_only(admin.wm_rot_custom), pattern=r"^wm_rot_custom$"))
    app.add_handler(CallbackQueryHandler(_admin_only(admin.wm_set_font), pattern=r"^wm_set_font$"))
    app.add_handler(CallbackQueryHandler(_admin_only(admin.wm_font_val), pattern=r"^wm_font_val:"))
    app.add_handler(CallbackQueryHandler(_admin_only(admin.wm_preview), pattern=r"^wm_preview$"))

    # ---- Per-group channel callbacks (admin only) ----
    app.add_handler(CallbackQueryHandler(_admin_only(admin.fg_ch_menu), pattern=r"^fg_ch_menu:\d+$"))
    app.add_handler(CallbackQueryHandler(_admin_only(admin.fg_ch_add), pattern=r"^fg_ch_add:\d+$"))
    app.add_handler(CallbackQueryHandler(_admin_only(admin.fg_ch_select), pattern=r"^fg_ch_sel:"))
    app.add_handler(CallbackQueryHandler(_admin_only(admin.fg_ch_remove), pattern=r"^fg_ch_rm:"))

    # ---- Channel management callbacks (admin only) ----
    app.add_handler(CallbackQueryHandler(_admin_only(channel.channel_manage_menu), pattern=r"^channel_manage$"))
    app.add_handler(CallbackQueryHandler(_admin_only(channel.channel_detail), pattern=r"^ch_detail:"))
    app.add_handler(CallbackQueryHandler(_admin_only(channel.channel_unbind), pattern=r"^ch_unbind:"))

    # ---- User subscription check callback ----
    app.add_handler(CallbackQueryHandler(user.check_subscription_callback, pattern=r"^check_sub:"))

    # ---- Admin message handlers ----
    app.add_handler(MessageHandler(_admin_filter & _admin_media_filter(), _handle_admin_message))
    app.add_handler(MessageHandler(_admin_filter & filters.TEXT & ~filters.COMMAND, admin.handle_admin_text))

    # ---- User text (treat as extraction code) ----
    app.add_handler(MessageHandler(~_admin_filter & filters.TEXT & ~filters.COMMAND, user.handle_user_text))

    app.post_init = _post_init
    app.post_shutdown = _post_shutdown
    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


async def _post_init(application: Application):
    await db.init_db()
    logger.info("Database initialized.")


async def _post_shutdown(application: Application):
    await db.close_db()
    logger.info("Database closed.")


async def _handle_admin_message(update: Update, context):
    user_id = update.effective_user.id
    state = admin._get_state(user_id)

    if state["state"] == admin.STATE_UPLOADING:
        await admin.handle_admin_file(update, context)
    elif state["state"] == admin.STATE_BROADCAST:
        await admin.handle_admin_broadcast_file(update, context)


if __name__ == "__main__":
    main()
