import asyncio
import io
import logging
import math

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.error import Forbidden

import db
from config import ADMIN_SECRET, PAGE_SIZE, BROADCAST_DELAY

logger = logging.getLogger(__name__)
from utils import generate_unique_code


def _escape_md(text: str) -> str:
    """Escape Markdown V1 special characters in user-provided text."""
    for ch in r"_*`[]()":
        text = text.replace(ch, f"\\{ch}")
    return text

# ---- State management ----
_admin_states: dict[int, dict] = {}

STATE_IDLE = "idle"
STATE_UPLOAD_TITLE = "upload_title"
STATE_UPLOADING = "uploading"
STATE_BROADCAST = "broadcast"
STATE_DESCRIPTION = "description"
STATE_CH_RENAME = "ch_rename"
STATE_WELCOME_TEXT = "welcome_text"
STATE_WELCOME_MEDIA = "welcome_media"
STATE_GEN_CODE_AMOUNT = "gen_code_amount"
STATE_GEN_CODE_QUOTA = "gen_code_quota"
STATE_WM_TEXT = "wm_text"
STATE_WM_FONT_SIZE = "wm_font_size"
STATE_WM_OPACITY = "wm_opacity"
STATE_WM_COLOR = "wm_color"
STATE_WM_ROTATION = "wm_rotation"
STATE_WM_FONT_PATH = "wm_font_path"


def _is_admin(user_id: int) -> bool:
    return db.is_admin(user_id)


def _get_state(user_id: int) -> dict:
    return _admin_states.get(user_id, {"state": STATE_IDLE})


def _set_state(user_id: int, state: str, **kwargs):
    _admin_states[user_id] = {"state": state, **kwargs}


def _clear_state(user_id: int):
    _admin_states.pop(user_id, None)


# ---- Admin authentication ----

async def authenticate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /admin <secret> command for admin authentication."""
    user = update.effective_user
    chat_id = update.effective_chat.id

    if not context.args:
        # No secret provided: if already admin, show panel
        if _is_admin(user.id):
            await send_admin_panel(update, context)
        else:
            await update.message.reply_text("用法: /admin <密钥>")
        return

    secret = context.args[0]

    # Delete message containing the secret immediately for security
    try:
        await update.message.delete()
    except Exception:
        pass

    if not ADMIN_SECRET:
        await context.bot.send_message(chat_id, "⚠️ 服务端未配置 ADMIN_SECRET，请联系部署者。")
        return

    if secret != ADMIN_SECRET:
        await context.bot.send_message(chat_id, "❌ 密钥错误。")
        return

    added = await db.add_admin(user.id, user.username or "")
    if added:
        await context.bot.send_message(chat_id, "✅ 管理员认证成功！发送 /start 进入管理面板。")
    else:
        await context.bot.send_message(chat_id, "ℹ️ 你已经是管理员了。")


# ---- Admin panel ----

async def send_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "📁 网盘管理面板"
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📤 存文件", callback_data="upload_start"),
            InlineKeyboardButton("📋 文件列表", callback_data="file_list:0"),
        ],
        [
            InlineKeyboardButton("📢 频道/群组", callback_data="channel_manage"),
            InlineKeyboardButton("📣 广播消息", callback_data="broadcast_start"),
        ],
        [
            InlineKeyboardButton("💧 水印设置", callback_data="wm_menu"),
            InlineKeyboardButton("📊 统计信息", callback_data="stats"),
        ],
        [
            InlineKeyboardButton("📝 启动文案", callback_data="welcome_menu"),
        ],
    ])

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard)
    elif update.message:
        await update.message.reply_text(text, reply_markup=keyboard)


async def back_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _clear_state(update.effective_user.id)
    await send_admin_panel(update, context)


# ---- Upload flow ----

async def upload_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _set_state(update.effective_user.id, STATE_UPLOAD_TITLE)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ 取消", callback_data="back_main")],
    ])
    await query.edit_message_text(
        "📤 请输入文件夹标题：",
        reply_markup=keyboard,
    )


async def _upload_and_get_file_id(bot, admin_chat_id: int, file_type: str, data: bytes) -> str | None:
    """Upload watermarked file to an admin chat to obtain a file_id, then delete the message."""
    try:
        if file_type == "photo":
            msg = await bot.send_photo(
                chat_id=admin_chat_id, photo=io.BytesIO(data), disable_notification=True,
            )
            new_file_id = msg.photo[-1].file_id
        elif file_type == "video":
            msg = await bot.send_video(
                chat_id=admin_chat_id, video=io.BytesIO(data), disable_notification=True,
            )
            new_file_id = msg.video.file_id
        else:
            return None
        try:
            await bot.delete_message(chat_id=admin_chat_id, message_id=msg.message_id)
        except Exception:
            logger.debug("Failed to delete watermark temp message in chat %s", admin_chat_id)
        return new_file_id
    except Forbidden:
        return None
    except Exception as e:
        logger.debug("Upload to admin %s failed: %s", admin_chat_id, e)
        return None


async def _apply_watermark_if_enabled(file_id: str, file_type: str, bot) -> tuple[str, bool]:
    """Download file, apply watermark if enabled, re-upload, return (new_file_id, success).
    Returns (original_file_id, False) if watermark is disabled, not applicable, or failed."""
    if file_type not in ("photo", "video"):
        return file_id, False

    wm = await db.get_watermark_config()
    if not wm["enabled"] or not wm["text"]:
        return file_id, False

    from watermark import apply_watermark_to_image, apply_watermark_to_video

    admin_ids = db.get_admin_ids()
    if not admin_ids:
        logger.warning("No admins registered, cannot process watermark")
        return file_id, False

    try:
        tg_file = await bot.get_file(file_id)
        file_bytes = await tg_file.download_as_bytearray()

        wm_kwargs = {
            "text": wm["text"],
            "font_size": wm["font_size"],
            "position": wm["position"],
            "opacity": wm["opacity"],
            "color": wm["color"],
            "rotation": wm["rotation"],
            "font_path": wm["font_path"],
        }

        # Run CPU-bound watermark processing in executor to avoid blocking the event loop
        loop = asyncio.get_running_loop()

        if file_type == "photo":
            result = await loop.run_in_executor(
                None, lambda: apply_watermark_to_image(bytes(file_bytes), **wm_kwargs)
            )
        elif file_type == "video":
            result = await loop.run_in_executor(
                None, lambda: apply_watermark_to_video(bytes(file_bytes), **wm_kwargs)
            )
            if result is None:
                return file_id, False
        else:
            return file_id, False

        # Try each admin until one succeeds (first admin may not have started the bot)
        for admin_id in admin_ids:
            new_file_id = await _upload_and_get_file_id(bot, admin_id, file_type, result)
            if new_file_id:
                return new_file_id, True

        logger.warning("Watermark upload failed: no admin chat accepted the file")

    except Exception as e:
        logger.warning("Watermark failed for %s: %s, using original", file_type, e)

    return file_id, False


async def handle_admin_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not _is_admin(user_id):
        return
    state = _get_state(user_id)
    if state["state"] != STATE_UPLOADING:
        return

    msg = update.message
    file_id = file_type = file_name = None
    _doc_media_type = None  # "photo"/"video" if document has image/video MIME

    if msg.document:
        file_id, file_type, file_name = msg.document.file_id, "document", msg.document.file_name or ""
        mime = msg.document.mime_type or ""
        if mime.startswith("image/"):
            _doc_media_type = "photo"
        elif mime.startswith("video/"):
            _doc_media_type = "video"
    elif msg.photo:
        file_id, file_type, file_name = msg.photo[-1].file_id, "photo", ""
    elif msg.video:
        file_id, file_type, file_name = msg.video.file_id, "video", msg.video.file_name or ""
    elif msg.audio:
        file_id, file_type, file_name = msg.audio.file_id, "audio", msg.audio.file_name or ""
    elif msg.voice:
        file_id, file_type, file_name = msg.voice.file_id, "voice", ""
    elif msg.animation:
        file_id, file_type, file_name = msg.animation.file_id, "animation", msg.animation.file_name or ""

    if not file_id:
        await msg.reply_text("⚠️ 不支持的文件类型，请发送文档/图片/视频/音频。")
        return

    # Apply watermark for images and videos (including documents with image/video MIME)
    wm_type = _doc_media_type or file_type
    if wm_type in ("photo", "video"):
        wm = await db.get_watermark_config()
        if wm["enabled"] and wm["text"]:
            status_msg = await msg.reply_text("💧 正在添加水印...")
            file_id, wm_ok = await _apply_watermark_if_enabled(file_id, wm_type, context.bot)
            try:
                if wm_ok:
                    await status_msg.edit_text("✅ 水印已添加")
                    # file_id now points to a photo/video, update file_type accordingly
                    file_type = wm_type
                else:
                    await status_msg.edit_text("⚠️ 水印添加失败，将使用原文件")
            except Exception:
                pass

    state["files"].append({"file_id": file_id, "file_type": file_type, "file_name": file_name})

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ 完成上传", callback_data="upload_done")],
        [InlineKeyboardButton("❌ 取消", callback_data="back_main")],
    ])
    await msg.reply_text(f"📎 已接收 {len(state['files'])} 个文件，继续发送或点击完成。", reply_markup=keyboard)


async def upload_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    state = _get_state(user_id)

    if state["state"] != STATE_UPLOADING or not state.get("files"):
        await query.edit_message_text("⚠️ 没有接收到任何文件。")
        _clear_state(user_id)
        return

    # 立即提取数据并清除状态，防止双击创建重复文件组
    files = state["files"]
    title = state.get("title", "")
    _clear_state(user_id)

    # Save file group with title as description
    group_id = await db.create_file_group(user_id, description=title)
    for i, f in enumerate(files):
        await db.add_file_to_group(group_id, f["file_id"], f["file_type"], f["file_name"], i)

    # Auto-generate a default extraction code (unlimited uses)
    default_code = await generate_unique_code()
    await db.create_code(group_id, default_code, max_uses=0)

    # Auto-generate a share code for user browsing/sharing
    share_code_str = await generate_unique_code()
    await db.create_code(group_id, share_code_str, max_uses=0, code_type="share")

    bot_username = context.bot.username
    share_link = f"https://t.me/{bot_username}?start={default_code}"

    desc = _escape_md(title) if title else "无"
    text = (
        f"✅ 创建成功！\n\n"
        f"📁 标题: {desc}\n"
        f"📎 文件数: {len(files)}\n"
        f"🔗 默认分享链接:\n`{share_link}`\n"
    )

    fg_channels = await db.get_file_group_channels(group_id)
    group = await db.get_file_group(group_id)
    is_hidden = group.get("is_hidden", 0) if group else 0
    is_protected = group.get("protect_content", 0) if group else 0
    visibility_text = "👁 在列表显示" if is_hidden else "🙈 从列表隐藏"
    protect_text = "🔓 允许转发/保存" if is_protected else "🔒 禁止转发/保存"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("👁 预览文件", callback_data=f"file_preview:{group_id}")],
        [InlineKeyboardButton("🔗 生成额外链接", callback_data=f"gen_code_start:{group_id}")],
        [InlineKeyboardButton("📋 查看所有链接", callback_data=f"code_list:{group_id}:0")],
        [InlineKeyboardButton(f"📢 前置频道 ({len(fg_channels)})", callback_data=f"fg_ch_menu:{group_id}")],
        [InlineKeyboardButton(visibility_text, callback_data=f"toggle_hidden:{group_id}")],
        [InlineKeyboardButton(protect_text, callback_data=f"toggle_protect:{group_id}")],
        [InlineKeyboardButton("📄 改标题", callback_data=f"set_desc:{group_id}")],
        [InlineKeyboardButton("🗑 删除文件组", callback_data=f"file_delete_confirm:{group_id}")],
        [InlineKeyboardButton("🔙 返回主菜单", callback_data="back_main")],
    ])
    await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")


# ---- Generate codes flow ----

async def gen_code_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ask how many codes to generate."""
    query = update.callback_query
    await query.answer()
    group_id = int(query.data.split(":")[1])

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1 个", callback_data=f"gen_code_qty:{group_id}:1"),
            InlineKeyboardButton("5 个", callback_data=f"gen_code_qty:{group_id}:5"),
            InlineKeyboardButton("10 个", callback_data=f"gen_code_qty:{group_id}:10"),
        ],
        [
            InlineKeyboardButton("20 个", callback_data=f"gen_code_qty:{group_id}:20"),
            InlineKeyboardButton("50 个", callback_data=f"gen_code_qty:{group_id}:50"),
            InlineKeyboardButton("自定义数量", callback_data=f"gen_code_custom_qty:{group_id}"),
        ],
        [InlineKeyboardButton("🔙 返回", callback_data=f"file_detail:{group_id}")],
    ])
    await query.edit_message_text("🔗 要生成几个提取链接？", reply_markup=keyboard)


async def gen_code_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """After choosing quantity, ask for quota per code."""
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    group_id = int(parts[1])
    amount = int(parts[2])

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1 次", callback_data=f"gen_code_do:{group_id}:{amount}:1"),
            InlineKeyboardButton("5 次", callback_data=f"gen_code_do:{group_id}:{amount}:5"),
            InlineKeyboardButton("10 次", callback_data=f"gen_code_do:{group_id}:{amount}:10"),
        ],
        [
            InlineKeyboardButton("50 次", callback_data=f"gen_code_do:{group_id}:{amount}:50"),
            InlineKeyboardButton("100 次", callback_data=f"gen_code_do:{group_id}:{amount}:100"),
            InlineKeyboardButton("不限次数", callback_data=f"gen_code_do:{group_id}:{amount}:0"),
        ],
        [InlineKeyboardButton("自定义次数", callback_data=f"gen_code_custom_quota:{group_id}:{amount}")],
        [InlineKeyboardButton("🔙 返回", callback_data=f"gen_code_start:{group_id}")],
    ])
    await query.edit_message_text(
        f"将生成 {amount} 个链接。\n\n每个链接可被多少人使用后失效？",
        reply_markup=keyboard,
    )


async def gen_code_custom_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin wants to type a custom number of codes."""
    query = update.callback_query
    await query.answer()
    group_id = int(query.data.split(":")[1])
    _set_state(update.effective_user.id, STATE_GEN_CODE_AMOUNT, group_id=group_id)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ 取消", callback_data="back_main")],
    ])
    await query.edit_message_text("请输入要生成的链接数量（数字）：", reply_markup=keyboard)


async def gen_code_custom_quota(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin wants to type a custom quota."""
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    group_id = int(parts[1])
    amount = int(parts[2])
    _set_state(update.effective_user.id, STATE_GEN_CODE_QUOTA, group_id=group_id, amount=amount)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ 取消", callback_data="back_main")],
    ])
    await query.edit_message_text("请输入每个链接的使用次数（数字，0=不限）：", reply_markup=keyboard)


async def gen_code_do(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Actually generate codes."""
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    group_id = int(parts[1])
    amount = int(parts[2])
    max_uses = int(parts[3])

    await _generate_codes_and_reply(query, context, group_id, amount, max_uses)


async def _generate_codes_and_reply(query, context, group_id: int, amount: int, max_uses: int):
    """Generate codes and send result."""
    group = await db.get_file_group(group_id)
    if not group:
        await query.edit_message_text("❌ 文件组已被删除。")
        return

    bot_username = context.bot.username

    codes = []
    for _ in range(amount):
        code = await generate_unique_code()
        await db.create_code(group_id, code, max_uses)
        codes.append(code)

    quota_text = f"{max_uses} 次" if max_uses > 0 else "不限"

    if amount <= 10:
        links = "\n".join(
            f"`https://t.me/{bot_username}?start={c}`" for c in codes
        )
        text = (
            f"✅ 已生成 {amount} 个提取链接\n"
            f"📊 每个链接可使用: {quota_text}\n\n"
            f"{links}"
        )
    else:
        # Too many to display inline, show first 5 + summary
        links = "\n".join(
            f"`https://t.me/{bot_username}?start={c}`" for c in codes[:5]
        )
        text = (
            f"✅ 已生成 {amount} 个提取链接\n"
            f"📊 每个链接可使用: {quota_text}\n\n"
            f"前 5 个链接:\n{links}\n\n"
            f"...共 {amount} 个，可在文件详情中查看全部"
        )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 继续生成", callback_data=f"gen_code_start:{group_id}")],
        [InlineKeyboardButton("📋 查看所有链接", callback_data=f"code_list:{group_id}:0")],
        [InlineKeyboardButton("🔙 返回主菜单", callback_data="back_main")],
    ])
    await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")


# ---- Code list for a file group ----

async def code_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all codes for a file group with pagination."""
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    group_id = int(parts[1])
    page = int(parts[2])

    page_codes, total = await db.get_codes_by_group_page(group_id, page, PAGE_SIZE)
    total_pages = max(1, math.ceil(total / PAGE_SIZE))

    if total == 0:
        text = "暂无提取链接"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔗 生成链接", callback_data=f"gen_code_start:{group_id}")],
            [InlineKeyboardButton("🔙 返回", callback_data=f"file_detail:{group_id}")],
        ])
        await query.edit_message_text(text, reply_markup=keyboard)
        return

    text = f"🔗 提取链接 (第 {page + 1}/{total_pages} 页，共 {total} 个)\n\n"
    for c in page_codes:
        quota = f"{c['used_count']}/{c['max_uses']}" if c["max_uses"] > 0 else f"{c['used_count']}/∞"
        status = "❌ 已用完" if (c["max_uses"] > 0 and c["used_count"] >= c["max_uses"]) else "✅"
        text += f"{status} `{c['code']}` ({quota})\n"

    buttons = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"code_list:{group_id}:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"code_list:{group_id}:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("🔗 生成更多链接", callback_data=f"gen_code_start:{group_id}")])
    buttons.append([InlineKeyboardButton("🔙 返回文件详情", callback_data=f"file_detail:{group_id}")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")


# ---- Description ----

async def set_desc_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    group_id = int(query.data.split(":")[1])
    _set_state(update.effective_user.id, STATE_DESCRIPTION, group_id=group_id)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ 取消", callback_data="back_main")],
    ])
    await query.edit_message_text("📄 请输入文件描述：", reply_markup=keyboard)


# ---- Admin text input router ----

async def handle_admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not _is_admin(user_id):
        return

    state = _get_state(user_id)
    text = update.message.text.strip()

    if state["state"] == STATE_UPLOAD_TITLE:
        _set_state(user_id, STATE_UPLOADING, files=[], title=text)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ 完成上传", callback_data="upload_done")],
            [InlineKeyboardButton("❌ 取消", callback_data="back_main")],
        ])
        await update.message.reply_text(
            f"📁 标题: {text}\n\n📤 请发送文件（支持文档/图片/视频/音频）\n可连续发送多个文件，完成后点击下方按钮。",
            reply_markup=keyboard,
        )
        return

    elif state["state"] == STATE_DESCRIPTION:
        group_id = state["group_id"]
        await db.update_file_group_description(group_id, text)
        _clear_state(user_id)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔗 生成提取链接", callback_data=f"gen_code_start:{group_id}")],
            [InlineKeyboardButton("🔙 返回主菜单", callback_data="back_main")],
        ])
        await update.message.reply_text("✅ 描述已添加。", reply_markup=keyboard)

    elif state["state"] == STATE_CH_RENAME:
        chat_id = state["chat_id"]
        await db.update_bot_channel_title(chat_id, text)
        _clear_state(user_id)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 返回频道详情", callback_data=f"ch_detail:{chat_id}")],
        ])
        await update.message.reply_text(f"✅ 名称已更新为：{text}", reply_markup=keyboard)

    elif state["state"] == STATE_WELCOME_TEXT:
        await db.update_welcome_text(text)
        _clear_state(user_id)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 返回文案设置", callback_data="welcome_menu")],
        ])
        await update.message.reply_text("✅ 启动文案已更新。", reply_markup=keyboard)

    elif state["state"] == STATE_GEN_CODE_AMOUNT:
        if not text.isdigit() or int(text) < 1:
            await update.message.reply_text("⚠️ 请输入一个正整数。")
            return
        amount = int(text)
        group_id = state["group_id"]
        # Now ask for quota
        _set_state(user_id, STATE_GEN_CODE_QUOTA, group_id=group_id, amount=amount)
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("1 次", callback_data=f"gen_code_do:{group_id}:{amount}:1"),
                InlineKeyboardButton("10 次", callback_data=f"gen_code_do:{group_id}:{amount}:10"),
                InlineKeyboardButton("不限", callback_data=f"gen_code_do:{group_id}:{amount}:0"),
            ],
            [InlineKeyboardButton("自定义次数", callback_data=f"gen_code_custom_quota:{group_id}:{amount}")],
        ])
        await update.message.reply_text(
            f"将生成 {amount} 个链接。\n每个链接可被多少人使用后失效？",
            reply_markup=keyboard,
        )

    elif state["state"] == STATE_GEN_CODE_QUOTA:
        if not text.isdigit():
            await update.message.reply_text("⚠️ 请输入一个非负整数（0=不限）。")
            return
        max_uses = int(text)
        group_id = state["group_id"]
        amount = state["amount"]
        _clear_state(user_id)

        group = await db.get_file_group(group_id)
        if not group:
            await update.message.reply_text("❌ 文件组已被删除。")
            return

        bot_username = context.bot.username
        codes = []
        for _ in range(amount):
            code = await generate_unique_code()
            await db.create_code(group_id, code, max_uses)
            codes.append(code)

        quota_text = f"{max_uses} 次" if max_uses > 0 else "不限"
        links = "\n".join(f"`https://t.me/{bot_username}?start={c}`" for c in codes[:10])
        text_msg = f"✅ 已生成 {amount} 个提取链接（每个可使用 {quota_text}）\n\n{links}"
        if amount > 10:
            text_msg += f"\n\n...共 {amount} 个"

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔗 继续生成", callback_data=f"gen_code_start:{group_id}")],
            [InlineKeyboardButton("📋 查看所有链接", callback_data=f"code_list:{group_id}:0")],
            [InlineKeyboardButton("🔙 返回主菜单", callback_data="back_main")],
        ])
        await update.message.reply_text(text_msg, reply_markup=keyboard, parse_mode="Markdown")

    elif state["state"] == STATE_BROADCAST:
        await _do_broadcast_text(update, context, text)

    elif state["state"] == STATE_WM_TEXT:
        await db.update_watermark_config(text=text)
        _clear_state(user_id)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 返回水印设置", callback_data="wm_menu")],
        ])
        await update.message.reply_text(f"✅ 水印文字已设置为: {text}", reply_markup=keyboard)

    elif state["state"] == STATE_WM_FONT_SIZE:
        if not text.isdigit() or int(text) < 8 or int(text) > 200:
            await update.message.reply_text("⚠️ 请输入 8-200 之间的数字。")
            return
        await db.update_watermark_config(font_size=int(text))
        _clear_state(user_id)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 返回水印设置", callback_data="wm_menu")],
        ])
        await update.message.reply_text(f"✅ 字体大小已设置为: {text}", reply_markup=keyboard)

    elif state["state"] == STATE_WM_OPACITY:
        try:
            val = float(text)
            if not 0.0 <= val <= 1.0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("⚠️ 请输入 0.0 到 1.0 之间的小数。")
            return
        await db.update_watermark_config(opacity=val)
        _clear_state(user_id)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 返回水印设置", callback_data="wm_menu")],
        ])
        await update.message.reply_text(f"✅ 透明度已设置为: {val}", reply_markup=keyboard)

    elif state["state"] == STATE_WM_COLOR:
        color = text.strip()
        if not color.startswith("#"):
            color = "#" + color
        color = color.upper()
        if len(color) not in (4, 7) or not all(c in "0123456789ABCDEF" for c in color[1:]):
            await update.message.reply_text("⚠️ 无效的 HEX 颜色，请输入如 #FF5500 的格式。")
            return
        await db.update_watermark_config(color=color)
        _clear_state(user_id)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 返回水印设置", callback_data="wm_menu")],
        ])
        await update.message.reply_text(f"✅ 颜色已设置为: {color}", reply_markup=keyboard)

    elif state["state"] == STATE_WM_ROTATION:
        try:
            val = int(text)
            if not -180 <= val <= 180:
                raise ValueError
        except ValueError:
            await update.message.reply_text("⚠️ 请输入 -180 到 180 之间的整数。")
            return
        await db.update_watermark_config(rotation=val)
        _clear_state(user_id)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 返回水印设置", callback_data="wm_menu")],
        ])
        await update.message.reply_text(f"✅ 倾斜角度已设置为: {val}°", reply_markup=keyboard)

    elif state["state"] == STATE_WM_FONT_PATH:
        import os
        path = text.strip()
        if not os.path.isfile(path):
            await update.message.reply_text("⚠️ 文件不存在，请检查路径。")
            return
        await db.update_watermark_config(font_path=path)
        _clear_state(user_id)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 返回水印设置", callback_data="wm_menu")],
        ])
        await update.message.reply_text(f"✅ 字体路径已设置。", reply_markup=keyboard)

    else:
        # IDLE 状态：尝试作为提取码处理，失败则显示管理面板
        code_row = await db.get_code_by_code(text)
        if code_row:
            from handlers.user import _send_files
            await _send_files(update.effective_chat.id, text, context.bot)
        else:
            await send_admin_panel(update, context)


# ---- File list ----

async def file_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    page = int(query.data.split(":")[1])
    groups, total = await db.get_file_groups_page(page, PAGE_SIZE, include_hidden=True)
    total_pages = max(1, math.ceil(total / PAGE_SIZE))

    if not groups:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 返回主菜单", callback_data="back_main")],
        ])
        await query.edit_message_text("📋 暂无文件", reply_markup=keyboard)
        return

    text = f"📋 文件列表 (第 {page + 1}/{total_pages} 页，共 {total} 组)\n\n"
    buttons = []
    for g in groups:
        desc = g.get("description", "") or f"文件组 #{g['id']}"
        hidden_mark = "🙈 " if g.get("is_hidden") else ""
        label = f"{hidden_mark}{desc} ({g['file_count']} 文件 / {g['code_count']} 链接)"
        buttons.append([InlineKeyboardButton(label, callback_data=f"file_detail:{g['id']}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"file_list:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"file_list:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("🔙 返回主菜单", callback_data="back_main")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))


async def file_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    group_id = int(query.data.split(":")[1])
    group = await db.get_file_group(group_id)
    if not group:
        await query.edit_message_text("❌ 文件组不存在。")
        return

    files = await db.get_files_by_group(group_id)
    codes = await db.get_codes_by_group(group_id)

    active_codes = [c for c in codes if c["max_uses"] == 0 or c["used_count"] < c["max_uses"]]
    total_uses = sum(c["used_count"] for c in codes)
    share_uses = await db.get_share_extract_count(group_id)
    normal_uses = total_uses - share_uses

    bot_username = context.bot.username
    # Show the first active unlimited normal code as default share link
    default_link = ""
    for c in codes:
        if c["max_uses"] == 0 and c.get("code_type", "normal") != "share":
            default_link = f"https://t.me/{bot_username}?start={c['code']}"
            break
    if not default_link and active_codes:
        default_link = f"https://t.me/{bot_username}?start={active_codes[0]['code']}"

    desc = _escape_md(group.get("description") or "无")
    is_hidden = group.get("is_hidden", 0)
    is_protected = group.get("protect_content", 0)
    visibility_status = "🙈 已隐藏" if is_hidden else "👁 列表可见"
    protect_status = "🔒 禁止转发/保存" if is_protected else "🔓 允许转发/保存"

    text = (
        f"📁 文件详情\n\n"
        f"📄 标题: {desc}\n"
        f"📎 文件数: {len(files)}\n"
        f"🔗 链接数: {len(codes)} (有效: {len(active_codes)})\n"
        f"📊 总提取次数: {total_uses}\n"
        f"  ├ 🔗 分享链接: {share_uses} 次\n"
        f"  └ 🔑 提取码: {normal_uses} 次\n"
        f"📍 状态: {visibility_status}\n"
        f"🛡 权限: {protect_status}\n"
        f"🕐 创建时间: {group['created_at']}\n"
    )
    if default_link:
        text += f"\n🔗 默认分享链接:\n`{default_link}`\n"

    fg_channels = await db.get_file_group_channels(group_id)
    visibility_text = "👁 在列表显示" if is_hidden else "🙈 从列表隐藏"
    protect_text = "🔓 允许转发/保存" if is_protected else "🔒 禁止转发/保存"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("👁 预览文件", callback_data=f"file_preview:{group_id}")],
        [InlineKeyboardButton("🔗 生成额外链接", callback_data=f"gen_code_start:{group_id}")],
        [InlineKeyboardButton("📋 查看所有链接", callback_data=f"code_list:{group_id}:0")],
        [InlineKeyboardButton(f"📢 前置频道 ({len(fg_channels)})", callback_data=f"fg_ch_menu:{group_id}")],
        [InlineKeyboardButton(visibility_text, callback_data=f"toggle_hidden:{group_id}")],
        [InlineKeyboardButton(protect_text, callback_data=f"toggle_protect:{group_id}")],
        [InlineKeyboardButton("📄 改标题", callback_data=f"set_desc:{group_id}")],
        [InlineKeyboardButton("🗑 删除文件组", callback_data=f"file_delete_confirm:{group_id}")],
        [InlineKeyboardButton("🔙 返回列表", callback_data="file_list:0")],
    ])
    await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")


async def toggle_hidden(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle file group visibility in the list."""
    query = update.callback_query
    await query.answer()
    group_id = int(query.data.split(":")[1])
    await db.toggle_file_group_hidden(group_id)
    await file_detail(update, context)


async def toggle_protect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle file group protect_content (forward/save restriction)."""
    query = update.callback_query
    await query.answer()
    group_id = int(query.data.split(":")[1])
    await db.toggle_file_group_protect(group_id)
    await file_detail(update, context)


async def file_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("正在发送预览...")
    group_id = int(query.data.split(":")[1])
    files = await db.get_files_by_group(group_id)
    if not files:
        await context.bot.send_message(update.effective_chat.id, "❌ 文件组不存在或没有文件。")
        return

    group = await db.get_file_group(group_id)
    desc = group.get("description", "") if group else ""
    chat_id = update.effective_chat.id
    bot = context.bot

    from telegram import InputMediaPhoto, InputMediaVideo, InputMediaDocument, InputMediaAudio

    COMPAT = {"photo": "media", "video": "media", "document": "doc", "audio": "audio"}
    INPUT_CLS = {"photo": InputMediaPhoto, "video": InputMediaVideo, "document": InputMediaDocument, "audio": InputMediaAudio}
    SEND_METHOD = {"document": "send_document", "photo": "send_photo", "video": "send_video",
                   "audio": "send_audio", "voice": "send_voice", "animation": "send_animation"}

    batches = []
    cur_batch, cur_compat = [], None
    for f in files:
        compat = COMPAT.get(f["file_type"])
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
        first_compat = COMPAT.get(batch[0]["file_type"])
        if len(batch) == 1 or not first_compat:
            for f in batch:
                try:
                    cap = f"📁 {desc}" if desc and not caption_used else None
                    method = getattr(bot, SEND_METHOD.get(f["file_type"], "send_document"))
                    await method(chat_id, f["file_id"], caption=cap)
                    if cap:
                        caption_used = True
                except Exception as e:
                    await bot.send_message(chat_id, f"⚠️ 预览失败: {e}")
            continue
        try:
            items = []
            for i, f in enumerate(batch):
                cap = f"📁 {desc}" if desc and not caption_used and i == 0 else None
                cls = INPUT_CLS.get(f["file_type"], InputMediaDocument)
                items.append(cls(media=f["file_id"], caption=cap))
            await bot.send_media_group(chat_id, items)
            if desc and not caption_used:
                caption_used = True
        except Exception as e:
            await bot.send_message(chat_id, f"⚠️ 预览失败: {e}")


async def file_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    group_id = int(query.data.split(":")[1])

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ 确认删除", callback_data=f"file_delete:{group_id}"),
            InlineKeyboardButton("❌ 取消", callback_data=f"file_detail:{group_id}"),
        ],
    ])
    await query.edit_message_text("⚠️ 确认删除此文件组及其所有链接？此操作不可撤销。", reply_markup=keyboard)


async def file_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    group_id = int(query.data.split(":")[1])

    group = await db.get_file_group(group_id)
    if not group:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 返回列表", callback_data="file_list:0")],
        ])
        await query.edit_message_text("❌ 文件组已不存在。", reply_markup=keyboard)
        return

    await db.delete_file_group(group_id)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 返回列表", callback_data="file_list:0")],
    ])
    await query.edit_message_text("✅ 文件组已删除。", reply_markup=keyboard)


# ---- Broadcast ----

async def broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, active = await db.get_user_count()
    _set_state(update.effective_user.id, STATE_BROADCAST, files=[], texts=[])

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ 取消", callback_data="back_main")],
    ])
    await query.edit_message_text(
        f"📣 广播消息\n\n当前活跃用户: {active}\n\n请发送要广播的内容（支持文字/图片/视频/文件）：",
        reply_markup=keyboard,
    )


def _broadcast_item_count(state: dict) -> int:
    return len(state.get("files", [])) + len(state.get("texts", []))


def _broadcast_confirm_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ 确认发送", callback_data="broadcast_confirm"),
            InlineKeyboardButton("❌ 取消", callback_data="back_main"),
        ],
    ])


async def handle_admin_broadcast_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not _is_admin(user_id):
        return
    state = _get_state(user_id)
    if state["state"] != STATE_BROADCAST:
        return

    msg = update.message
    file_id = file_type = file_name = None
    if msg.document:
        file_id, file_type, file_name = msg.document.file_id, "document", msg.document.file_name or ""
    elif msg.photo:
        file_id, file_type, file_name = msg.photo[-1].file_id, "photo", ""
    elif msg.video:
        file_id, file_type, file_name = msg.video.file_id, "video", msg.video.file_name or ""
    elif msg.audio:
        file_id, file_type, file_name = msg.audio.file_id, "audio", msg.audio.file_name or ""
    elif msg.voice:
        file_id, file_type, file_name = msg.voice.file_id, "voice", ""
    elif msg.animation:
        file_id, file_type, file_name = msg.animation.file_id, "animation", msg.animation.file_name or ""

    if not file_id:
        return

    files = state.get("files", [])
    files.append({"file_id": file_id, "file_type": file_type, "file_name": file_name})
    state["files"] = files

    _, active = await db.get_user_count()
    count = _broadcast_item_count(state)
    await msg.reply_text(
        f"📎 已接收 {count} 条内容，继续发送可追加。\n确认向 {active} 位活跃用户广播？",
        reply_markup=_broadcast_confirm_keyboard(),
    )


async def _do_broadcast_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id
    state = _get_state(user_id)
    texts = state.get("texts", [])
    texts.append(text)
    state["texts"] = texts

    _, active = await db.get_user_count()
    count = _broadcast_item_count(state)
    await update.message.reply_text(
        f"📎 已接收 {count} 条内容，继续发送可追加。\n确认向 {active} 位活跃用户广播？",
        reply_markup=_broadcast_confirm_keyboard(),
    )


async def _broadcast_to_user(bot, user_id: int, files: list, texts: list):
    """Send broadcast content to a single user using media groups.
    First text is used as caption on the first file; remaining texts sent separately."""
    from telegram import InputMediaPhoto, InputMediaVideo, InputMediaDocument, InputMediaAudio

    COMPAT = {"photo": "media", "video": "media", "document": "doc", "audio": "audio"}
    INPUT_CLS = {"photo": InputMediaPhoto, "video": InputMediaVideo,
                 "document": InputMediaDocument, "audio": InputMediaAudio}
    SEND = {"document": "send_document", "photo": "send_photo", "video": "send_video",
            "audio": "send_audio", "voice": "send_voice", "animation": "send_animation"}

    # Combine all texts into one caption; if no files, send as messages
    caption = "\n".join(texts) if texts else None
    caption_used = False

    if files:
        batches = []
        cur_batch, cur_compat = [], None
        for f in files:
            compat = COMPAT.get(f["file_type"])
            if compat and compat == cur_compat and len(cur_batch) < 10:
                cur_batch.append(f)
            else:
                if cur_batch:
                    batches.append(cur_batch)
                cur_batch = [f]
                cur_compat = compat
        if cur_batch:
            batches.append(cur_batch)

        for batch in batches:
            first_compat = COMPAT.get(batch[0]["file_type"])
            if len(batch) == 1 or not first_compat:
                for f in batch:
                    cap = caption if caption and not caption_used else None
                    method = getattr(bot, SEND.get(f["file_type"], "send_document"))
                    await method(user_id, f["file_id"], caption=cap)
                    if cap:
                        caption_used = True
            else:
                items = []
                for i, f in enumerate(batch):
                    cap = caption if caption and not caption_used and i == 0 else None
                    cls = INPUT_CLS.get(f["file_type"], InputMediaDocument)
                    items.append(cls(media=f["file_id"], caption=cap))
                    if cap:
                        caption_used = True
                await bot.send_media_group(user_id, items)

    # Send texts only if no files consumed the caption
    if not caption_used and caption:
        await bot.send_message(user_id, caption)


async def broadcast_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    state = _get_state(user_id)

    if state["state"] != STATE_BROADCAST:
        await query.edit_message_text("⚠️ 广播已过期，请重新操作。")
        return

    # 立即提取数据并清除状态，防止双击触发重复广播
    files = state.get("files", [])
    texts = state.get("texts", [])
    _clear_state(user_id)

    if not files and not texts:
        await query.edit_message_text("⚠️ 没有可广播的内容，请重新操作。")
        return

    await query.edit_message_text("📣 广播中...")

    users = await db.get_all_active_users()
    users = [u for u in users if not db.is_admin(u["user_id"])]
    success = failed = 0

    for u in users:
        try:
            await _broadcast_to_user(context.bot, u["user_id"], files, texts)
            success += 1
        except Forbidden:
            await db.mark_user_blocked(u["user_id"])
            failed += 1
        except Exception:
            failed += 1
        await asyncio.sleep(BROADCAST_DELAY)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 返回主菜单", callback_data="back_main")],
    ])
    await context.bot.send_message(
        update.effective_chat.id,
        f"✅ 广播完成\n\n成功: {success}\n失败: {failed}",
        reply_markup=keyboard,
    )


# ---- Stats ----

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    total_users, active_users = await db.get_user_count()
    file_count = await db.get_file_group_count()
    channels = await db.get_prerequisite_channels()

    text = (
        f"📊 统计信息\n\n"
        f"👥 总用户数: {total_users}\n"
        f"✅ 活跃用户: {active_users}\n"
        f"🚫 已屏蔽: {total_users - active_users}\n"
        f"📁 文件组数: {file_count}\n"
        f"📢 前置频道: {len(channels)}"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 返回主菜单", callback_data="back_main")],
    ])
    await query.edit_message_text(text, reply_markup=keyboard)


# ---- Welcome Config ----

def _welcome_keyboard(cfg: dict) -> InlineKeyboardMarkup:
    has_text = bool(cfg.get("text"))
    has_media = bool(cfg.get("media_file_id"))
    buttons = [
        [InlineKeyboardButton("✏️ 修改文案", callback_data="welcome_set_text")],
        [InlineKeyboardButton("🖼 设置媒体", callback_data="welcome_set_media")],
    ]
    if has_text:
        buttons.append([InlineKeyboardButton("🗑 清除文案", callback_data="welcome_clear_text")])
    if has_media:
        buttons.append([InlineKeyboardButton("🗑 清除媒体", callback_data="welcome_clear_media")])
    buttons.append([InlineKeyboardButton("👁 预览", callback_data="welcome_preview")])
    buttons.append([InlineKeyboardButton("🔙 返回主菜单", callback_data="back_main")])
    return InlineKeyboardMarkup(buttons)


async def welcome_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _clear_state(update.effective_user.id)  # 取消任何进行中的文案/媒体输入状态
    cfg = await db.get_welcome_config()
    text_preview = cfg.get("text", "") or "（未设置）"
    media_status = f"✅ 已设置（{cfg.get('media_type', '')}）" if cfg.get("media_file_id") else "（未设置）"
    text = (
        f"📝 启动文案设置\n\n"
        f"📄 文案内容:\n{text_preview[:200]}\n\n"
        f"🖼 媒体: {media_status}\n\n"
        f"普通用户 /start 时会先看到此内容，再展示文件列表。"
    )
    await query.edit_message_text(text, reply_markup=_welcome_keyboard(cfg))


async def welcome_set_text_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _set_state(update.effective_user.id, STATE_WELCOME_TEXT)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ 取消", callback_data="welcome_menu")],
    ])
    await query.edit_message_text("✏️ 请发送启动文案内容（支持换行）：", reply_markup=keyboard)


async def welcome_set_media_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _set_state(update.effective_user.id, STATE_WELCOME_MEDIA)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ 取消", callback_data="welcome_menu")],
    ])
    await query.edit_message_text("🖼 请发送媒体文件（图片或视频）：", reply_markup=keyboard)


async def handle_welcome_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle media file sent when in STATE_WELCOME_MEDIA."""
    msg = update.message
    user_id = update.effective_user.id

    file_id, file_type = None, None
    if msg.photo:
        file_id = msg.photo[-1].file_id
        file_type = "photo"
    elif msg.video:
        file_id = msg.video.file_id
        file_type = "video"
    elif msg.document:
        file_id = msg.document.file_id
        file_type = "document"
    else:
        await msg.reply_text("⚠️ 仅支持图片、视频或文件，请重新发送。")
        return

    await db.update_welcome_media(file_id, file_type)
    _clear_state(user_id)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 返回文案设置", callback_data="welcome_menu")],
    ])
    await msg.reply_text("✅ 媒体已设置。", reply_markup=keyboard)


async def welcome_clear_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await db.clear_welcome_media()
    await welcome_menu(update, context)


async def welcome_clear_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await db.update_welcome_text("")
    await welcome_menu(update, context)


async def welcome_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a preview of the welcome content to the admin."""
    query = update.callback_query
    cfg = await db.get_welcome_config()
    if not cfg.get("text") and not cfg.get("media_file_id"):
        await query.answer("⚠️ 尚未设置任何启动文案内容", show_alert=True)
        return
    await query.answer("正在发送预览...")
    chat_id = update.effective_chat.id
    await _send_welcome_content(chat_id, cfg, context.bot)


async def _send_welcome_content(chat_id: int, cfg: dict, bot):
    """Send welcome text+media to the given chat. Used by admin preview and user /start."""
    text = cfg.get("text", "")
    file_id = cfg.get("media_file_id", "")
    media_type = cfg.get("media_type", "")

    if not text and not file_id:
        return  # Nothing configured, skip

    if file_id:
        send_map = {
            "photo": bot.send_photo,
            "video": bot.send_video,
            "document": bot.send_document,
        }
        send_fn = send_map.get(media_type, bot.send_document)
        await send_fn(chat_id, file_id, caption=text or None)
    else:
        await bot.send_message(chat_id, text)


# ---- Watermark Settings ----

_POSITION_LABELS = {
    "center": "居中",
    "top-left": "左上",
    "top-right": "右上",
    "bottom-left": "左下",
    "bottom-right": "右下",
    "tiled": "平铺",
}


async def wm_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show watermark config panel."""
    query = update.callback_query
    await query.answer()
    _clear_state(update.effective_user.id)

    wm = await db.get_watermark_config()
    status = "✅ 已开启" if wm["enabled"] else "❌ 已关闭"
    pos_label = _POSITION_LABELS.get(wm["position"], wm["position"])

    text = (
        f"💧 水印设置\n\n"
        f"状态: {status}\n"
        f"文字: {_escape_md(wm['text']) or '未设置'}\n"
        f"字体大小: {wm['font_size']}\n"
        f"位置: {pos_label}\n"
        f"透明度: {wm['opacity']}\n"
        f"颜色: {wm['color']}\n"
        f"倾斜度: {wm['rotation']}°\n"
        f"字体路径: {_escape_md(wm['font_path']) or '默认'}\n"
    )

    toggle_text = "❌ 关闭水印" if wm["enabled"] else "✅ 开启水印"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle_text, callback_data="wm_toggle")],
        [
            InlineKeyboardButton("📝 文字", callback_data="wm_set_text"),
            InlineKeyboardButton("🔤 字号", callback_data="wm_set_size"),
        ],
        [
            InlineKeyboardButton("📍 位置", callback_data="wm_set_position"),
            InlineKeyboardButton("👁 透明度", callback_data="wm_set_opacity"),
        ],
        [
            InlineKeyboardButton("🎨 颜色", callback_data="wm_set_color"),
            InlineKeyboardButton("📐 倾斜", callback_data="wm_set_rotation"),
        ],
        [InlineKeyboardButton("🔤 字体路径", callback_data="wm_set_font")],
        [InlineKeyboardButton("👁 预览水印", callback_data="wm_preview")],
        [InlineKeyboardButton("🔙 返回主菜单", callback_data="back_main")],
    ])
    await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")


async def wm_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    wm = await db.get_watermark_config()
    await db.update_watermark_config(enabled=not wm["enabled"])
    await wm_menu(update, context)


async def wm_set_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _set_state(update.effective_user.id, STATE_WM_TEXT)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ 取消", callback_data="wm_menu")],
    ])
    await query.edit_message_text("请输入水印文字：", reply_markup=keyboard)


async def wm_set_size(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("20", callback_data="wm_size_val:20"),
            InlineKeyboardButton("28", callback_data="wm_size_val:28"),
            InlineKeyboardButton("36", callback_data="wm_size_val:36"),
        ],
        [
            InlineKeyboardButton("48", callback_data="wm_size_val:48"),
            InlineKeyboardButton("64", callback_data="wm_size_val:64"),
            InlineKeyboardButton("自定义", callback_data="wm_size_custom"),
        ],
        [InlineKeyboardButton("🔙 返回", callback_data="wm_menu")],
    ])
    await query.edit_message_text("选择字体大小：", reply_markup=keyboard)


async def wm_size_val(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    val = int(query.data.split(":")[1])
    await db.update_watermark_config(font_size=val)
    await wm_menu(update, context)


async def wm_size_custom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _set_state(update.effective_user.id, STATE_WM_FONT_SIZE)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ 取消", callback_data="wm_menu")],
    ])
    await query.edit_message_text("请输入字体大小（数字）：", reply_markup=keyboard)


async def wm_set_position(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("左上", callback_data="wm_pos_val:top-left"),
            InlineKeyboardButton("居中", callback_data="wm_pos_val:center"),
            InlineKeyboardButton("右上", callback_data="wm_pos_val:top-right"),
        ],
        [
            InlineKeyboardButton("左下", callback_data="wm_pos_val:bottom-left"),
            InlineKeyboardButton("平铺", callback_data="wm_pos_val:tiled"),
            InlineKeyboardButton("右下", callback_data="wm_pos_val:bottom-right"),
        ],
        [InlineKeyboardButton("🔙 返回", callback_data="wm_menu")],
    ])
    await query.edit_message_text("选择水印位置：", reply_markup=keyboard)


async def wm_pos_val(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    val = query.data.split(":")[1]
    await db.update_watermark_config(position=val)
    await wm_menu(update, context)


async def wm_set_opacity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("10%", callback_data="wm_opacity_val:0.1"),
            InlineKeyboardButton("20%", callback_data="wm_opacity_val:0.2"),
            InlineKeyboardButton("30%", callback_data="wm_opacity_val:0.3"),
        ],
        [
            InlineKeyboardButton("50%", callback_data="wm_opacity_val:0.5"),
            InlineKeyboardButton("70%", callback_data="wm_opacity_val:0.7"),
            InlineKeyboardButton("自定义", callback_data="wm_opacity_custom"),
        ],
        [InlineKeyboardButton("🔙 返回", callback_data="wm_menu")],
    ])
    await query.edit_message_text("选择透明度：", reply_markup=keyboard)


async def wm_opacity_val(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    val = float(query.data.split(":")[1])
    await db.update_watermark_config(opacity=val)
    await wm_menu(update, context)


async def wm_opacity_custom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _set_state(update.effective_user.id, STATE_WM_OPACITY)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ 取消", callback_data="wm_menu")],
    ])
    await query.edit_message_text("请输入透明度（0.0-1.0 之间的小数）：", reply_markup=keyboard)


async def wm_set_color(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⬜ 白色", callback_data="wm_color_val:#FFFFFF"),
            InlineKeyboardButton("⬛ 黑色", callback_data="wm_color_val:#000000"),
            InlineKeyboardButton("🔴 红色", callback_data="wm_color_val:#FF0000"),
        ],
        [
            InlineKeyboardButton("🔵 蓝色", callback_data="wm_color_val:#0000FF"),
            InlineKeyboardButton("🟢 绿色", callback_data="wm_color_val:#00FF00"),
            InlineKeyboardButton("🟡 黄色", callback_data="wm_color_val:#FFFF00"),
        ],
        [InlineKeyboardButton("自定义 (HEX)", callback_data="wm_color_custom")],
        [InlineKeyboardButton("🔙 返回", callback_data="wm_menu")],
    ])
    await query.edit_message_text("选择水印颜色：", reply_markup=keyboard)


async def wm_color_val(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    val = query.data.split(":")[1]
    await db.update_watermark_config(color=val)
    await wm_menu(update, context)


async def wm_color_custom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _set_state(update.effective_user.id, STATE_WM_COLOR)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ 取消", callback_data="wm_menu")],
    ])
    await query.edit_message_text("请输入颜色 HEX 值（如 #FF5500）：", reply_markup=keyboard)


async def wm_set_rotation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("0°", callback_data="wm_rot_val:0"),
            InlineKeyboardButton("15°", callback_data="wm_rot_val:15"),
            InlineKeyboardButton("30°", callback_data="wm_rot_val:30"),
        ],
        [
            InlineKeyboardButton("45°", callback_data="wm_rot_val:45"),
            InlineKeyboardButton("-30°", callback_data="wm_rot_val:-30"),
            InlineKeyboardButton("-45°", callback_data="wm_rot_val:-45"),
        ],
        [InlineKeyboardButton("自定义", callback_data="wm_rot_custom")],
        [InlineKeyboardButton("🔙 返回", callback_data="wm_menu")],
    ])
    await query.edit_message_text("选择倾斜角度：", reply_markup=keyboard)


async def wm_rot_val(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    val = int(query.data.split(":")[1])
    await db.update_watermark_config(rotation=val)
    await wm_menu(update, context)


async def wm_rot_custom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _set_state(update.effective_user.id, STATE_WM_ROTATION)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ 取消", callback_data="wm_menu")],
    ])
    await query.edit_message_text("请输入倾斜角度（-180 到 180）：", reply_markup=keyboard)


async def wm_set_font(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _set_state(update.effective_user.id, STATE_WM_FONT_PATH)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("使用默认字体", callback_data="wm_font_val:")],
        [InlineKeyboardButton("❌ 取消", callback_data="wm_menu")],
    ])
    await query.edit_message_text(
        "请输入字体文件的完整路径（如 /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf）\n\n"
        "或点击下方按钮使用默认字体：",
        reply_markup=keyboard,
    )


async def wm_font_val(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _clear_state(update.effective_user.id)
    val = query.data.split(":", 1)[1] if ":" in query.data else ""
    await db.update_watermark_config(font_path=val)
    await wm_menu(update, context)


async def wm_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate a preview watermark image and send it."""
    query = update.callback_query
    await query.answer("生成预览中...")

    wm = await db.get_watermark_config()
    if not wm["text"]:
        await context.bot.send_message(
            update.effective_chat.id, "⚠️ 请先设置水印文字。"
        )
        return

    from watermark import apply_watermark_to_image
    from PIL import Image as PILImage

    # Generate a sample 800x600 gray image
    sample = PILImage.new("RGB", (800, 600), (180, 180, 180))
    buf = io.BytesIO()
    sample.save(buf, format="JPEG")
    sample_bytes = buf.getvalue()

    wm_kwargs = {
        "text": wm["text"],
        "font_size": wm["font_size"],
        "position": wm["position"],
        "opacity": wm["opacity"],
        "color": wm["color"],
        "rotation": wm["rotation"],
        "font_path": wm["font_path"],
    }
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None, lambda: apply_watermark_to_image(sample_bytes, **wm_kwargs)
    )

    await context.bot.send_photo(
        chat_id=update.effective_chat.id,
        photo=io.BytesIO(result),
        caption="💧 水印预览",
    )


# ---- Per-group prerequisite channels ----

async def fg_ch_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show prerequisite channels for a specific file group."""
    query = update.callback_query
    await query.answer()
    group_id = int(query.data.split(":")[1])

    channels = await db.get_file_group_channels(group_id)

    text = f"📢 文件组 #{group_id} 前置频道\n\n"
    if channels:
        for i, ch in enumerate(channels, 1):
            text += f"{i}. {ch['title'] or '未知'} ({ch['channel_link'] or ch['channel_id']})\n"
    else:
        text += "暂未设置（将仅使用全局前置频道）\n"

    buttons = []
    for ch in channels:
        buttons.append([InlineKeyboardButton(
            f"❌ 移除: {ch['title'] or ch['channel_id']}",
            callback_data=f"fg_ch_rm:{group_id}:{ch['channel_id']}",
        )])
    buttons.append([InlineKeyboardButton("➕ 添加频道", callback_data=f"fg_ch_add:{group_id}")])
    buttons.append([InlineKeyboardButton("🔙 返回文件详情", callback_data=f"file_detail:{group_id}")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))


async def fg_ch_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show bot_channels for quick select + deep link buttons."""
    query = update.callback_query
    await query.answer()
    group_id = int(query.data.split(":")[1])

    bot_username = context.bot.username
    bot_channels = await db.get_bot_admin_channels()
    existing_ids = {ch["channel_id"] for ch in await db.get_file_group_channels(group_id)}
    available = [ch for ch in bot_channels if ch["chat_id"] not in existing_ids]

    buttons = []
    if available:
        for ch in available:
            buttons.append([InlineKeyboardButton(
                f"📢 {ch['title'] or ch['chat_id']}",
                callback_data=f"fg_ch_sel:{group_id}:{ch['chat_id']}",
            )])

    buttons.append([
        InlineKeyboardButton("➕ 添加到频道", url=f"https://t.me/{bot_username}?startchannel=true&admin=manage_chat+invite_users"),
        InlineKeyboardButton("➕ 添加到群组", url=f"https://t.me/{bot_username}?startgroup=true&admin=manage_chat+invite_users"),
    ])
    buttons.append([InlineKeyboardButton("🔙 返回", callback_data=f"fg_ch_menu:{group_id}")])

    text = "选择已有频道，或点击下方按钮将 Bot 添加到新频道/群组：\n\n"
    if not available:
        text += "暂无可选频道，请先通过下方按钮添加 Bot，添加后返回此页面即可看到。"

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))


async def fg_ch_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add selected channel as group prerequisite."""
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    group_id = int(parts[1])
    channel_id = int(parts[2])

    bot_channels = await db.get_bot_admin_channels()
    target = next((ch for ch in bot_channels if ch["chat_id"] == channel_id), None)

    if not target:
        await query.edit_message_text("频道不存在或 Bot 已不是管理员。")
        return

    invite_link = target.get("invite_link", "")
    if not invite_link:
        try:
            chat = await context.bot.get_chat(channel_id)
            invite_link = chat.invite_link or ""
        except Exception:
            pass

    await db.add_file_group_channel(group_id, channel_id, invite_link, target.get("title", ""))

    text = f"✅ 已添加前置频道: {target.get('title', channel_id)}"
    buttons = [[InlineKeyboardButton("🔙 返回频道管理", callback_data=f"fg_ch_menu:{group_id}")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))


async def fg_ch_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove a group prerequisite channel."""
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    group_id = int(parts[1])
    channel_id = int(parts[2])

    await db.remove_file_group_channel(group_id, channel_id)

    text = "✅ 已移除前置频道"
    buttons = [[InlineKeyboardButton("🔙 返回频道管理", callback_data=f"fg_ch_menu:{group_id}")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
