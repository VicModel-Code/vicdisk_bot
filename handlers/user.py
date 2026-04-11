import logging
import math
import time

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    InputMediaPhoto, InputMediaVideo, InputMediaDocument, InputMediaAudio,
)
from telegram.ext import ContextTypes

import db
from config import PAGE_SIZE
from utils import generate_unique_code

logger = logging.getLogger(__name__)

# 用户速率限制：每用户在 _RATE_WINDOW 秒内最多 _RATE_LIMIT 次请求
_RATE_LIMIT = 5
_RATE_WINDOW = 30  # seconds
_user_timestamps: dict[int, list[float]] = {}


_last_cleanup: float = 0.0
_CLEANUP_INTERVAL = 300  # 每5分钟清理一次过期记录


def _is_rate_limited(user_id: int) -> bool:
    """Check if user has exceeded rate limit. Returns True if limited."""
    global _last_cleanup
    now = time.monotonic()

    # 定期清理所有过期条目，防止内存泄漏
    if now - _last_cleanup > _CLEANUP_INTERVAL:
        _last_cleanup = now
        stale = [uid for uid, ts in _user_timestamps.items() if not any(now - t < _RATE_WINDOW for t in ts)]
        for uid in stale:
            del _user_timestamps[uid]

    timestamps = _user_timestamps.get(user_id, [])
    # Remove expired entries
    timestamps = [t for t in timestamps if now - t < _RATE_WINDOW]
    if len(timestamps) >= _RATE_LIMIT:
        _user_timestamps[user_id] = timestamps
        return True
    timestamps.append(now)
    _user_timestamps[user_id] = timestamps
    return False


async def _check_subscription(user_id: int, bot, group_id: int | None = None) -> list[dict]:
    """Check global prerequisite channels + per-group channels."""
    channels = await db.get_prerequisite_channels()

    # Merge per-group channels (deduplicate by channel_id)
    if group_id:
        seen = {ch["channel_id"] for ch in channels}
        for gc in await db.get_file_group_channels(group_id):
            if gc["channel_id"] not in seen:
                channels.append(gc)

    not_joined = []
    for ch in channels:
        try:
            member = await bot.get_chat_member(ch["channel_id"], user_id)
            if member.status in ("left", "kicked"):
                not_joined.append(ch)
        except Exception:
            logger.warning("无法检查用户 %s 在频道 %s 的订阅状态，跳过", user_id, ch["channel_id"])
    return not_joined


_COMPAT_GROUP = {
    "photo": "media", "video": "media",
    "document": "doc",
    "audio": "audio",
}

_INPUT_MEDIA_CLS = {
    "photo": InputMediaPhoto,
    "video": InputMediaVideo,
    "document": InputMediaDocument,
    "audio": InputMediaAudio,
}

_SEND_METHOD_NAME = {
    "document": "send_document",
    "photo": "send_photo",
    "video": "send_video",
    "audio": "send_audio",
    "voice": "send_voice",
    "animation": "send_animation",
}


async def _send_files(chat_id: int, code_str: str, bot):
    """Validate code, increment usage, send files as media groups."""
    valid, code_row = await db.is_code_valid(code_str)

    if code_row is None:
        await bot.send_message(chat_id, "❌ 提取码无效或文件已被删除。")
        return

    if not valid:
        await bot.send_message(chat_id, "❌ 该链接已达到使用次数上限，无法提取。")
        return

    group = await db.get_file_group(code_row["group_id"])
    if not group:
        await bot.send_message(chat_id, "❌ 文件不存在。")
        return

    files = await db.get_files_by_group(group["id"])
    if not files:
        await bot.send_message(chat_id, "❌ 该提取码下没有文件。")
        return

    # Atomically increment usage count (handles race condition)
    if not await db.increment_code_usage(code_row["id"]):
        await bot.send_message(chat_id, "❌ 该链接刚刚被用完了，请联系管理员获取新链接。")
        return

    desc = group.get("description", "")
    protect = bool(group.get("protect_content", 0))

    # Build batches of consecutive compatible files for media groups
    batches = []
    cur_batch = []
    cur_compat = None

    for f in files:
        compat = _COMPAT_GROUP.get(f["file_type"])
        if compat and compat == cur_compat and len(cur_batch) < 10:
            cur_batch.append(f)
        else:
            if cur_batch:
                batches.append(cur_batch)
            cur_batch = [f]
            cur_compat = compat
    if cur_batch:
        batches.append(cur_batch)

    caption_used = False

    for batch in batches:
        first_compat = _COMPAT_GROUP.get(batch[0]["file_type"])

        # Single file or non-groupable type → send individually
        if len(batch) == 1 or not first_compat:
            for f in batch:
                try:
                    cap = f"📁 {desc}" if desc and not caption_used else None
                    method = getattr(bot, _SEND_METHOD_NAME.get(f["file_type"], "send_document"))
                    await method(chat_id, f["file_id"], caption=cap, protect_content=protect)
                    if cap:
                        caption_used = True
                except Exception as e:
                    logger.error("发送文件失败 chat_id=%s file_id=%s: %s", chat_id, f["file_id"], e)
                    await bot.send_message(chat_id, "⚠️ 发送文件失败，请稍后重试或联系管理员。")
            continue

        # Multiple compatible files → send as media group
        try:
            media_items = []
            for i, f in enumerate(batch):
                cap = f"📁 {desc}" if desc and not caption_used and i == 0 else None
                cls = _INPUT_MEDIA_CLS.get(f["file_type"], InputMediaDocument)
                media_items.append(cls(media=f["file_id"], caption=cap))
            await bot.send_media_group(chat_id, media_items, protect_content=protect)
            if desc and not caption_used:
                caption_used = True
        except Exception as e:
            logger.error("发送媒体组失败 chat_id=%s: %s", chat_id, e)
            await bot.send_message(chat_id, "⚠️ 发送文件失败，请稍后重试或联系管理员。")

    # If description wasn't attached as caption (e.g. all files failed), send as text
    if desc and not caption_used:
        await bot.send_message(chat_id, f"📁 {desc}", protect_content=protect)



async def _edit_query_message(query, text: str, reply_markup=None, parse_mode: str | None = None):
    """Edit a callback query's message — uses caption edit for media messages."""
    msg = query.message
    if msg.photo or msg.video or msg.document or msg.audio or msg.animation:
        await query.edit_message_caption(
            caption=text[:1024], reply_markup=reply_markup, parse_mode=parse_mode
        )
    else:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)


async def _subscription_prompt(not_joined: list[dict], code: str, bot, back_data: str = "") -> tuple[str, InlineKeyboardMarkup]:
    text = "❌ 请先订阅以下频道后再提取文件：\n"
    buttons = []
    for ch in not_joined:
        title = ch.get("title", "频道")
        # 尝试生成一次性邀请链接
        link = ""
        try:
            invite = await bot.create_chat_invite_link(
                ch["channel_id"], member_limit=1,
            )
            link = invite.invite_link
        except Exception:
            link = ch.get("channel_link", "")
        if link:
            buttons.append([InlineKeyboardButton(f"📢 {title}", url=link)])
        else:
            text += f"  · {title}（暂无链接，请搜索频道名加入）\n"
    buttons.append([InlineKeyboardButton("✅ 我已订阅", callback_data=f"check_sub:{code}")])
    if back_data:
        buttons.append([InlineKeyboardButton("⬅️ 返回", callback_data=back_data)])
    return text, InlineKeyboardMarkup(buttons)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await db.upsert_user(user.id, user.username or "", user.first_name or "")

    code = context.args[0] if context.args else None

    if db.is_admin(user.id):
        if code:
            await _send_files(update.effective_chat.id, code, context.bot)
            return
        from handlers.admin import send_admin_panel
        await send_admin_panel(update, context)
        return

    if not code:
        cfg = await db.get_welcome_config()
        text, markup = await _build_user_file_list(0, welcome_cfg=cfg)
        file_id = cfg.get("media_file_id", "")
        media_type = cfg.get("media_type", "")
        if file_id:
            send_map = {
                "photo": context.bot.send_photo,
                "video": context.bot.send_video,
                "document": context.bot.send_document,
            }
            send_fn = send_map.get(media_type, context.bot.send_document)
            # Telegram caption limit is 1024 chars; truncate if necessary
            caption = text[:1024] if text else None
            await send_fn(update.effective_chat.id, file_id, caption=caption, reply_markup=markup)
        else:
            await update.message.reply_text(text, reply_markup=markup)
        return

    if _is_rate_limited(user.id):
        await update.message.reply_text("⚠️ 请求过于频繁，请稍后再试。")
        return

    # Look up group_id for per-group channel check
    code_row = await db.get_code_by_code(code)
    group_id = code_row["group_id"] if code_row else None

    not_joined = await _check_subscription(user.id, context.bot, group_id)
    if not_joined:
        text, markup = await _subscription_prompt(not_joined, code, context.bot)
        await update.message.reply_text(text, reply_markup=markup)
        return

    await _send_files(update.effective_chat.id, code, context.bot)


async def handle_user_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if db.is_admin(user.id):
        return

    await db.upsert_user(user.id, user.username or "", user.first_name or "")

    if _is_rate_limited(user.id):
        await update.message.reply_text("⚠️ 请求过于频繁，请稍后再试。")
        return

    code = update.message.text.strip()

    valid, code_row = await db.is_code_valid(code)
    if code_row is None:
        await update.message.reply_text("❌ 未找到该提取码，请检查后重试。")
        return
    if not valid:
        await update.message.reply_text("❌ 该链接已达到使用次数上限。")
        return

    not_joined = await _check_subscription(user.id, context.bot, code_row["group_id"])
    if not_joined:
        text, markup = await _subscription_prompt(not_joined, code, context.bot)
        await update.message.reply_text(text, reply_markup=markup)
        return

    await _send_files(update.effective_chat.id, code, context.bot)


async def check_subscription_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    code = query.data.split(":")[1]
    user = update.effective_user

    code_row = await db.get_code_by_code(code)
    group_id = code_row["group_id"] if code_row else None

    not_joined = await _check_subscription(user.id, context.bot, group_id)
    if not_joined:
        names = "、".join(ch.get("title", "未知频道") for ch in not_joined)
        await query.answer(f"❌ 检测不通过，您尚未订阅：{names}", show_alert=True)
        back_data = f"uf_detail:{group_id}" if (code_row and code_row.get("code_type") == "share") else ""
        text, markup = await _subscription_prompt(not_joined, code, context.bot, back_data=back_data)
        await _edit_query_message(query, text, reply_markup=markup)
        return

    await _edit_query_message(query, "✅ 订阅验证通过，正在发送文件...")
    await _send_files(update.effective_chat.id, code, context.bot)
    cfg = await db.get_welcome_config()
    text, markup = await _build_user_file_list(0, welcome_cfg=cfg)
    await context.bot.send_message(update.effective_chat.id, text, reply_markup=markup)


# ---- User file browsing ----

async def _build_user_file_list(page: int, welcome_cfg: dict | None = None) -> tuple[str, InlineKeyboardMarkup | None]:
    """Build paginated file list for non-admin users."""
    groups, total = await db.get_file_groups_page(page, PAGE_SIZE, include_hidden=False)
    total_pages = max(1, math.ceil(total / PAGE_SIZE))

    welcome_text = (welcome_cfg or {}).get("text", "") or "👋 欢迎使用网盘机器人"

    if not groups:
        return f"{welcome_text}\n\n暂无可用文件，请通过提取链接获取文件。", None

    text = f"{welcome_text}\n\n📋 文件列表 (第 {page + 1}/{total_pages} 页，共 {total} 个)\n"
    buttons = []
    for g in groups:
        desc = g.get("description", "") or f"文件组 #{g['id']}"
        label = f"📁 {desc} ({g['file_count']} 个文件)"
        buttons.append([InlineKeyboardButton(label, callback_data=f"uf_detail:{g['id']}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"uf_list:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"uf_list:{page + 1}"))
    if nav:
        buttons.append(nav)

    return text, InlineKeyboardMarkup(buttons)


async def user_file_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle pagination for user file list."""
    query = update.callback_query
    await query.answer()
    page = int(query.data.split(":")[1])
    cfg = await db.get_welcome_config()
    text, markup = await _build_user_file_list(page, welcome_cfg=cfg)
    await _edit_query_message(query, text, reply_markup=markup)


async def user_file_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show file detail to user with extract and share buttons."""
    query = update.callback_query
    await query.answer()

    group_id = int(query.data.split(":")[1])
    group = await db.get_file_group(group_id)
    if not group or group.get("is_hidden"):
        await _edit_query_message(query, "❌ 文件不存在或已隐藏。")
        return

    files = await db.get_files_by_group(group_id)
    desc = group.get("description", "") or "无标题"

    text = (
        f"📁 {desc}\n\n"
        f"📄 文件数量: {len(files)}\n"
        f"📅 上传时间: {group['created_at']}\n"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📥 提取文件", callback_data=f"uf_extract:{group_id}"),
            InlineKeyboardButton("🔗 分享链接", callback_data=f"uf_share:{group_id}"),
        ],
        [InlineKeyboardButton("⬅️ 返回列表", callback_data="uf_list:0")],
    ])

    await _edit_query_message(query, text, reply_markup=keyboard)


async def _get_or_create_share_code(group_id: int) -> dict:
    """Get existing share code or create one for the group."""
    share_code = await db.get_share_code(group_id)
    if share_code:
        return share_code
    code_str = await generate_unique_code()
    await db.create_code(group_id, code_str, max_uses=0, code_type="share")
    return await db.get_code_by_code(code_str)


async def user_extract(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User clicks extract from file detail - send files via share code."""
    query = update.callback_query
    user = update.effective_user

    if _is_rate_limited(user.id):
        await query.answer("⚠️ 请求过于频繁，请稍后再试。", show_alert=True)
        return

    await query.answer()
    group_id = int(query.data.split(":")[1])

    group = await db.get_file_group(group_id)
    if not group or group.get("is_hidden"):
        await _edit_query_message(query, "❌ 文件不存在或已隐藏。")
        return

    share_code = await _get_or_create_share_code(group_id)
    code_str = share_code["code"]

    # Check subscription
    not_joined = await _check_subscription(user.id, context.bot, group_id)
    if not_joined:
        text, markup = await _subscription_prompt(not_joined, code_str, context.bot, back_data=f"uf_detail:{group_id}")
        await _edit_query_message(query, text, reply_markup=markup)
        return

    await _edit_query_message(query, "✅ 正在发送文件...")
    await _send_files(update.effective_chat.id, code_str, context.bot)
    cfg = await db.get_welcome_config()
    text, markup = await _build_user_file_list(0, welcome_cfg=cfg)
    await context.bot.send_message(update.effective_chat.id, text, reply_markup=markup)


async def user_share(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show share link to user."""
    query = update.callback_query
    await query.answer()

    group_id = int(query.data.split(":")[1])
    group = await db.get_file_group(group_id)
    if not group or group.get("is_hidden"):
        await _edit_query_message(query, "❌ 文件不存在或已隐藏。")
        return

    share_code = await _get_or_create_share_code(group_id)
    bot_username = context.bot.username
    link = f"https://t.me/{bot_username}?start={share_code['code']}"

    desc = group.get("description", "") or "无标题"
    text = (
        f"📁 {desc}\n\n"
        f"🔗 分享链接:\n`{link}`\n\n"
        f"将此链接发送给朋友即可提取文件。"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ 返回详情", callback_data=f"uf_detail:{group_id}")],
    ])

    await _edit_query_message(query, text, reply_markup=keyboard, parse_mode="Markdown")
