import os
import asyncio
import logging
from datetime import timedelta
from telegram import Update, ChatPermissions
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PORT = int(os.getenv("PORT", "10000"))
OWNER_ID = 8283878643
DEFAULT_AUTO_DELETE_SECONDS = 30
MAX_WARNINGS = 3

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("SanskariManager")

timer_by_chat: dict[int, int | None] = {}
last_bot_msg_by_chat: dict[int, int] = {}
active_tasks_by_chat: dict[int, set[asyncio.Task]] = {}
group_link_by_chat: dict[int, str] = {}
awaiting_link_owner: set[tuple[int, int]] = set()
warnings_by_chat_user: dict[tuple[int, int], int] = {}


def is_owner(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id == OWNER_ID)


def get_timer(chat_id: int) -> int | None:
    return timer_by_chat.get(chat_id, DEFAULT_AUTO_DELETE_SECONDS)


async def delete_after_delay(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, delay: int
):
    task = asyncio.current_task()
    if task:
        active_tasks_by_chat.setdefault(chat_id, set()).add(task)
    try:
        await asyncio.sleep(delay)
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.warning(f"Auto-delete failed ({chat_id}/{message_id}): {e}")
    finally:
        if task and chat_id in active_tasks_by_chat:
            active_tasks_by_chat[chat_id].discard(task)


def schedule_autodelete(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int
):
    delay = get_timer(chat_id)
    if delay and delay > 0:
        asyncio.create_task(delete_after_delay(context, chat_id, message_id, delay))


async def safe_delete(message):
    if not message:
        return
    try:
        await message.delete()
    except Exception as e:
        logger.warning(f"Delete failed ({message.message_id}): {e}")


async def send_simple_relay(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str | None = None,
    reply_to_id: int | None = None,
):
    msg = update.message
    chat_id = update.effective_chat.id
    sent = None

    source_media_msg = (
        msg.reply_to_message if (msg.reply_to_message and not text) else msg
    )
    caption = text if text is not None else (msg.caption or None)

    try:
        if source_media_msg.photo:
            sent = await context.bot.send_photo(
                chat_id=chat_id,
                photo=source_media_msg.photo[-1].file_id,
                caption=caption,
                reply_to_message_id=reply_to_id,
            )
        elif source_media_msg.document:
            sent = await context.bot.send_document(
                chat_id=chat_id,
                document=source_media_msg.document.file_id,
                caption=caption,
                reply_to_message_id=reply_to_id,
            )
        elif source_media_msg.video:
            sent = await context.bot.send_video(
                chat_id=chat_id,
                video=source_media_msg.video.file_id,
                caption=caption,
                reply_to_message_id=reply_to_id,
            )
        elif source_media_msg.voice:
            sent = await context.bot.send_voice(
                chat_id=chat_id,
                voice=source_media_msg.voice.file_id,
                caption=caption,
                reply_to_message_id=reply_to_id,
            )
        elif source_media_msg.sticker:
            sent = await context.bot.send_sticker(
                chat_id=chat_id,
                sticker=source_media_msg.sticker.file_id,
                reply_to_message_id=reply_to_id,
            )
        elif text:
            sent = await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_to_message_id=reply_to_id,
            )
    except Exception as e:
        logger.warning(f"Relay failed in chat {chat_id}: {e}")
        return None

    if sent:
        last_bot_msg_by_chat[chat_id] = sent.message_id
        schedule_autodelete(context, chat_id, sent.message_id)
    return sent


# --- Chat & Auto-Delete Commands (Owner Only) ---


async def chat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update) or not update.message:
        return
    text = " ".join(context.args).strip() if context.args else None
    msg = update.message
    has_media = bool(
        msg.photo or msg.document or msg.video or msg.voice or msg.sticker
    )
    if not text and not has_media:
        return
    await safe_delete(msg)
    await send_simple_relay(update, context, text=text, reply_to_id=None)


async def reply_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update) or not update.message or not update.message.reply_to_message:
        return
    text = " ".join(context.args).strip() if context.args else None
    msg = update.message
    target = msg.reply_to_message
    has_media = bool(
        msg.photo
        or msg.document
        or msg.video
        or msg.voice
        or msg.sticker
        or target.photo
        or target.document
        or target.video
        or target.voice
        or target.sticker
    )
    if not text and not has_media:
        return
    await safe_delete(msg)
    await send_simple_relay(
        update, context, text=text, reply_to_id=target.message_id
    )


async def edit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update) or not update.message:
        return
    chat_id = update.effective_chat.id
    new_text = " ".join(context.args).strip()
    await safe_delete(update.message)
    last_id = last_bot_msg_by_chat.get(chat_id)
    if not new_text or not last_id:
        return
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=last_id, text=new_text
        )
    except Exception as e:
        logger.warning(f"Edit failed: {e}")


async def del_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update) or not update.message:
        return
    target = update.message.reply_to_message
    await safe_delete(update.message)
    if target:
        await safe_delete(target)


async def timer_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update) or not update.message or not context.args:
        return
    chat_id = update.effective_chat.id
    arg = context.args[0].lower()
    if arg == "off":
        timer_by_chat[chat_id] = None
        await update.message.reply_text("Auto-delete: OFF")
    elif arg.isdigit() and int(arg) > 0:
        sec = int(arg)
        timer_by_chat[chat_id] = sec
        await update.message.reply_text(f"Auto-delete set to {sec}s")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update) or not update.message:
        return
    chat_id = update.effective_chat.id
    sec = get_timer(chat_id)
    queue_count = len(active_tasks_by_chat.get(chat_id, set()))
    link_status = "Set" if chat_id in group_link_by_chat else "Not set"
    timer_text = f"{sec}s" if sec else "OFF"
    await update.message.reply_text(
        f"Timer: {timer_text}\nActive auto-delete tasks: {queue_count}\nLink: {link_status}"
    )


# --- /id Command (Owner Only: Reply for Name/Username/ID, Else Chat ID) ---


async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update) or not update.message:
        return
    target_user = (
        update.message.reply_to_message.from_user
        if update.message.reply_to_message
        else None
    )
    if target_user:
        full_name = target_user.full_name
        username = f"@{target_user.username}" if target_user.username else "None"
        await update.message.reply_text(
            f"Name: {full_name}\nUsername: {username}\nID: {target_user.id}"
        )
    else:
        chat = update.effective_chat
        await update.message.reply_text(
            f"Chat ID: {chat.id}\nYour ID: {update.effective_user.id}"
        )


# --- /link Command (Owner Sets Link, Any User Fetches in Group) ---


async def link_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_chat or not update.effective_user:
        return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if user_id == OWNER_ID:
        if context.args:
            group_link_by_chat[chat_id] = context.args[0].strip()
            await update.message.reply_text("Link set kar diya gaya hai.")
        else:
            awaiting_link_owner.add((chat_id, OWNER_ID))
            await update.message.reply_text("Link set karo (link bhejein):")
        return

    saved_link = group_link_by_chat.get(chat_id)
    if saved_link:
        await update.message.reply_text(saved_link)
    else:
        await update.message.reply_text("Abhi koi link set nahi hai.")


async def handle_pending_link(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    if not update.message or not update.effective_chat or not update.effective_user:
        return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    key = (chat_id, user_id)

    if user_id == OWNER_ID and key in awaiting_link_owner:
        text = (update.message.text or "").strip()
        if text:
            group_link_by_chat[chat_id] = text
            awaiting_link_owner.discard(key)
            await update.message.reply_text("Link set kar diya gaya hai.")


# --- Moderation & Group Commands (Owner Only) ---


async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update) or not update.message or not update.message.reply_to_message:
        return
    u = update.message.reply_to_message.from_user
    if not u:
        return
    warns = warnings_by_chat_user.get((update.effective_chat.id, u.id), 0)
    uname = f"@{u.username}" if u.username else "None"
    await update.message.reply_text(
        f"Name: {u.full_name}\nUsername: {uname}\nID: {u.id}\nWarnings: {warns}/{MAX_WARNINGS}"
    )


async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update) or not update.message or not update.message.reply_to_message:
        return
    u = update.message.reply_to_message.from_user
    if not u:
        return
    reason = " ".join(context.args).strip() if context.args else "No reason"
    try:
        await context.bot.ban_chat_member(chat_id=update.effective_chat.id, user_id=u.id)
        await update.message.reply_text(
            f"Banned {u.full_name} ({u.id}) | Reason: {reason}"
        )
    except Exception as e:
        logger.warning(f"Ban failed: {e}")


async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update) or not update.message or not update.message.reply_to_message:
        return
    u = update.message.reply_to_message.from_user
    if not u:
        return
    try:
        await context.bot.unban_chat_member(
            chat_id=update.effective_chat.id, user_id=u.id, only_if_banned=True
        )
        await update.message.reply_text(f"Unbanned {u.full_name} ({u.id})")
    except Exception as e:
        logger.warning(f"Unban failed: {e}")


async def kick_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update) or not update.message or not update.message.reply_to_message:
        return
    u = update.message.reply_to_message.from_user
    if not u:
        return
    chat_id = update.effective_chat.id
    try:
        await context.bot.ban_chat_member(chat_id=chat_id, user_id=u.id)
        await context.bot.unban_chat_member(chat_id=chat_id, user_id=u.id)
        await update.message.reply_text(f"Kicked {u.full_name} ({u.id})")
    except Exception as e:
        logger.warning(f"Kick failed: {e}")


async def mute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update) or not update.message or not update.message.reply_to_message:
        return
    u = update.message.reply_to_message.from_user
    if not u:
        return
    until_date = None
    if context.args:
        raw = context.args[0].lower()
        try:
            if raw.endswith("m"):
                until_date = update.message.date + timedelta(minutes=int(raw[:-1]))
            elif raw.endswith("h"):
                until_date = update.message.date + timedelta(hours=int(raw[:-1]))
            elif raw.endswith("d"):
                until_date = update.message.date + timedelta(days=int(raw[:-1]))
        except ValueError:
            until_date = None

    try:
        await context.bot.restrict_chat_member(
            chat_id=update.effective_chat.id,
            user_id=u.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until_date,
        )
        await update.message.reply_text(f"Muted {u.full_name} ({u.id})")
    except Exception as e:
        logger.warning(f"Mute failed: {e}")


async def unmute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update) or not update.message or not update.message.reply_to_message:
        return
    u = update.message.reply_to_message.from_user
    if not u:
        return
    perms = ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_video_notes=True,
        can_send_voice_notes=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
    )
    try:
        await context.bot.restrict_chat_member(
            chat_id=update.effective_chat.id, user_id=u.id, permissions=perms
        )
        await update.message.reply_text(f"Unmuted {u.full_name} ({u.id})")
    except Exception as e:
        logger.warning(f"Unmute failed: {e}")


async def warning_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update) or not update.message or not update.message.reply_to_message:
        return
    u = update.message.reply_to_message.from_user
    if not u:
        return
    chat_id = update.effective_chat.id
    key = (chat_id, u.id)
    count = warnings_by_chat_user.get(key, 0) + 1
    warnings_by_chat_user[key] = count
    if count >= MAX_WARNINGS:
        warnings_by_chat_user[key] = 0
        try:
            await context.bot.ban_chat_member(chat_id=chat_id, user_id=u.id)
        except Exception as e:
            logger.warning(f"Auto-ban on max warnings failed: {e}")
        await update.message.reply_text(
            f"{u.full_name} ({u.id}) reached {MAX_WARNINGS}/{MAX_WARNINGS} warnings and was banned."
        )
    else:
        await update.message.reply_text(
            f"Warning {count}/{MAX_WARNINGS} for {u.full_name} ({u.id})"
        )


async def warns_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update) or not update.message or not update.message.reply_to_message:
        return
    u = update.message.reply_to_message.from_user
    if not u:
        return
    count = warnings_by_chat_user.get((update.effective_chat.id, u.id), 0)
    await update.message.reply_text(
        f"{u.full_name} ({u.id}) has {count}/{MAX_WARNINGS} warnings."
    )


async def resetwarns_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update) or not update.message or not update.message.reply_to_message:
        return
    u = update.message.reply_to_message.from_user
    if not u:
        return
    warnings_by_chat_user[(update.effective_chat.id, u.id)] = 0
    await update.message.reply_text(f"Warnings reset for {u.full_name} ({u.id}).")


async def pin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update) or not update.message or not update.message.reply_to_message:
        return
    try:
        await context.bot.pin_chat_message(
            chat_id=update.effective_chat.id,
            message_id=update.message.reply_to_message.message_id,
            disable_notification=True,
        )
    except Exception as e:
        logger.warning(f"Pin failed: {e}")
    await safe_delete(update.message)


async def unpin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update) or not update.message:
        return
    try:
        if update.message.reply_to_message:
            await context.bot.unpin_chat_message(
                chat_id=update.effective_chat.id,
                message_id=update.message.reply_to_message.message_id,
            )
        else:
            await context.bot.unpin_all_chat_messages(chat_id=update.effective_chat.id)
    except Exception as e:
        logger.warning(f"Unpin failed: {e}")
    await safe_delete(update.message)


async def purge_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update) or not update.message or not update.message.reply_to_message:
        return
    chat_id = update.effective_chat.id
    start_id = update.message.reply_to_message.message_id
    end_id = update.message.message_id
    msg_ids = list(range(start_id, end_id + 1))
    try:
        await context.bot.delete_messages(chat_id=chat_id, message_ids=msg_ids)
    except Exception as e:
        logger.warning(f"Purge failed: {e}")


async def lock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update) or not update.message:
        return
    try:
        await context.bot.set_chat_permissions(
            chat_id=update.effective_chat.id,
            permissions=ChatPermissions(can_send_messages=False),
        )
        await update.message.reply_text("Group locked.")
    except Exception as e:
        logger.warning(f"Lock failed: {e}")


async def unlock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update) or not update.message:
        return
    perms = ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_video_notes=True,
        can_send_voice_notes=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
    )
    try:
        await context.bot.set_chat_permissions(
            chat_id=update.effective_chat.id, permissions=perms
        )
        await update.message.reply_text("Group unlocked.")
    except Exception as e:
        logger.warning(f"Unlock failed: {e}")


async def slowmode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update) or not update.message or not context.args:
        return
    arg = context.args[0].lower()
    delay = 0 if arg == "off" else (int(arg) if arg.isdigit() else None)
    if delay is None:
        return
    try:
        await context.bot.set_chat_slow_mode_delay(
            chat_id=update.effective_chat.id, slow_mode_delay=delay
        )
        await update.message.reply_text(
            f"Slowmode: {'OFF' if delay == 0 else f'{delay}s'}"
        )
    except Exception as e:
        logger.warning(f"Slowmode failed: {e}")


# --- Lightweight HTTP Server for Render / UptimeRobot 200 OK ---


async def _handle_http_client(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
):
    try:
        await reader.read(1024)
        body = b"OK"
        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n\r\n" + body)
            writer.write(response)
        await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def start_health_server(app: Application):
    server = await asyncio.start_server(
        _handle_http_client, host="0.0.0.0", port=PORT
    )
    app.bot_data["health_server"] = server
    logger.info(f"Health server listening on 0.0.0.0:{PORT}")


async def stop_health_server(app: Application):
    server = app.bot_data.get("health_server")
    if server:
        server.close()
        await server.wait_closed()


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is required.")

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(start_health_server)
        .post_shutdown(stop_health_server)
        .build()
    )

    app.add_handler(CommandHandler("chat", chat_cmd))
    app.add_handler(CommandHandler("reply", reply_cmd))
    app.add_handler(CommandHandler("edit", edit_cmd))
    app.add_handler(CommandHandler("del", del_cmd))
    app.add_handler(CommandHandler("timer", timer_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("id", id_cmd))
    app.add_handler(CommandHandler("link", link_cmd))

    app.add_handler(CommandHandler("info", info_cmd))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CommandHandler("kick", kick_cmd))
    app.add_handler(CommandHandler("mute", mute_cmd))
    app.add_handler(CommandHandler("unmute", unmute_cmd))
    app.add_handler(CommandHandler("warning", warning_cmd))
    app.add_handler(CommandHandler("warns", warns_cmd))
    app.add_handler(CommandHandler("resetwarns", resetwarns_cmd))
    app.add_handler(CommandHandler("pin", pin_cmd))
    app.add_handler(CommandHandler("unpin", unpin_cmd))
    app.add_handler(CommandHandler("purge", purge_cmd))
    app.add_handler(CommandHandler("lock", lock_cmd))
    app.add_handler(CommandHandler("unlock", unlock_cmd))
    app.add_handler(CommandHandler("slowmode", slowmode_cmd))

    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_pending_link)
    )

    logger.info("Sanskari Manager started.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
