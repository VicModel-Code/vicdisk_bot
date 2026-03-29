from telegram import Update, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import db


def _extract_status_change(chat_member_update: ChatMemberUpdated) -> tuple[bool, bool] | None:
    """Extract whether the bot's admin status changed.
    MY_CHAT_MEMBER events always concern the bot itself, so no is_bot check needed."""
    old = chat_member_update.old_chat_member
    new = chat_member_update.new_chat_member
    was_admin = old.status in ("administrator", "creator")
    is_admin = new.status in ("administrator", "creator")
    if was_admin != is_admin:
        return was_admin, is_admin
    return None


async def track_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Track when bot is added/removed as admin in channels/groups."""
    result = _extract_status_change(update.my_chat_member)
    if result is None:
        return

    chat = update.my_chat_member.chat
    _, is_now_admin = result

    if is_now_admin:
        invite_link = ""
        if chat.type != "private":
            try:
                # Use get_chat to fetch existing invite link without overwriting it
                full_chat = await context.bot.get_chat(chat.id)
                invite_link = full_chat.invite_link or ""
                if not invite_link:
                    invite_link = await context.bot.export_chat_invite_link(chat.id)
            except Exception:
                pass
        await db.upsert_bot_channel(chat.id, chat.title or "", chat.type, invite_link)
    else:
        await db.remove_bot_channel(chat.id)
        # bot 失去管理员，同步移除对应的前置频道，避免用户被锁定
        await db.remove_prerequisite_channel(chat.id)


# ---- Channel management UI (admin) ----

async def channel_manage_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all channels/groups where bot is admin."""
    query = update.callback_query
    await query.answer()

    bot_username = context.bot.username
    channels = await db.get_bot_admin_channels()

    text = "📢 频道/群组管理\n\n"
    if channels:
        text += f"Bot 已加入 {len(channels)} 个频道/群组：\n"
    else:
        text += "Bot 尚未加入任何频道/群组\n"

    buttons = []
    for ch in channels:
        icon = "📢" if ch["type"] == "channel" else "👥"
        title = ch["title"] or str(ch["chat_id"])
        buttons.append([
            InlineKeyboardButton(f"{icon} {title}", callback_data=f"ch_detail:{ch['chat_id']}"),
        ])

    buttons.append([
        InlineKeyboardButton("➕ 添加到频道", url=f"https://t.me/{bot_username}?startchannel=true&admin=manage_chat+invite_users"),
        InlineKeyboardButton("➕ 添加到群组", url=f"https://t.me/{bot_username}?startgroup=true&admin=manage_chat+invite_users"),
    ])
    buttons.append([InlineKeyboardButton("🔙 返回主菜单", callback_data="back_main")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))


async def channel_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show detail of a channel/group with unbind option."""
    query = update.callback_query
    await query.answer()

    chat_id = int(query.data.split(":")[1])
    channels = await db.get_bot_admin_channels()
    ch = next((c for c in channels if c["chat_id"] == chat_id), None)

    if not ch:
        await query.edit_message_text("该频道/群组已不存在。")
        return

    icon = "📢 频道" if ch["type"] == "channel" else "👥 群组"
    title = ch["title"] or str(ch["chat_id"])
    link = ch.get("invite_link", "")

    text = (
        f"{icon}: {title}\n"
        f"ID: {ch['chat_id']}\n"
    )
    if link:
        text += f"链接: {link}\n"

    buttons = [
        [InlineKeyboardButton("🗑 解绑", callback_data=f"ch_unbind:{ch['chat_id']}")],
        [InlineKeyboardButton("🔙 返回列表", callback_data="channel_manage")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))


async def channel_unbind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove bot from tracking this channel/group."""
    query = update.callback_query
    await query.answer()

    chat_id = int(query.data.split(":")[1])
    await db.remove_bot_channel(chat_id)
    await db.remove_prerequisite_channel(chat_id)
    await db.remove_file_group_channel_all(chat_id)

    buttons = [[InlineKeyboardButton("🔙 返回列表", callback_data="channel_manage")]]
    await query.edit_message_text("✅ 已解绑", reply_markup=InlineKeyboardMarkup(buttons))


