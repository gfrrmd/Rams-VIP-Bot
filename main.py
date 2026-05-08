import os
import re
import io
import asyncio
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes, ConversationHandler
)
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PhoneCodeExpiredError
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument, MessageMediaSticker
from telethon.tl.functions.channels import GetFullChannelRequest

from database import (
    init_db, upsert_user,
    save_user_session, get_user_session, delete_user_session,
    is_subscribed, get_subscription_info, activate_subscription, revoke_subscription,
    get_user_by_username
)

# ── ENV VARS ──────────────────────────────────────────
BOT_TOKEN   = os.environ["BOT_TOKEN"]
ADMIN_ID    = int(os.environ["ADMIN_ID"])

DEVICE_MODEL     = "iPhone 17 Pro Max"
SYSTEM_VERSION   = "iOS 18.3.2"
APP_VERSION      = "11.4.1"
LANG_CODE        = "id"
SYSTEM_LANG_CODE = "id-ID"

API_ID_STEP, API_HASH_STEP, PHONE_STEP, CODE_STEP, PASSWORD_STEP = range(5)

waiting_restore = set()
waiting_gift    = set()  # set of ADMIN_ID waiting for gift target
waiting_revoke  = set()  # set of ADMIN_ID waiting for revoke target

temp_store   = {}
active_clients = {}

# Regex untuk parse link Telegram channel/post
# Support: t.me/username/123, t.me/c/1234567890/123
TG_LINK_RE = re.compile(
    r"(?:https?://)?t\.me/"
    r"(?:(?P<username>[a-zA-Z0-9_]+)/(?P<msg_id>\d+)|"
    r"c/(?P<channel_id>\d+)/(?P<msg_id2>\d+))"
)


# ── HELPERS ──────────────────────────────────────────
def build_client(api_id, api_hash, session_string=""):
    return TelegramClient(
        StringSession(session_string), api_id, api_hash,
        device_model=DEVICE_MODEL, system_version=SYSTEM_VERSION,
        app_version=APP_VERSION, lang_code=LANG_CODE,
        system_lang_code=SYSTEM_LANG_CODE
    )


def escape_md(text):
    if not text:
        return "Unknown"
    for ch in ["[", "]", "(", ")", "*", "_", "`"]:
        text = text.replace(ch, f"\\{ch}")
    return text


def is_view_once(message):
    media = message.media
    if isinstance(media, (MessageMediaPhoto, MessageMediaDocument)):
        return bool(getattr(media, "ttl_seconds", None))
    return False


def is_no_forward(message):
    """Cek apakah pesan memiliki flag noforwards (protected content)."""
    return bool(getattr(message, "noforwards", False))


def main_keyboard(uid):
    rows = [
        [InlineKeyboardButton("⚙️ Setup Session", callback_data="menu_setup")],
        [
            InlineKeyboardButton("💎 Beli VIP", callback_data="menu_beli"),
            InlineKeyboardButton("⌛️ Status Langganan", callback_data="menu_subscription"),
        ],
    ]
    if uid == ADMIN_ID:
        rows.append([InlineKeyboardButton("👤 Menu Admin", callback_data="menu_admin")])
    return InlineKeyboardMarkup(rows)


def admin_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📦 Backup DB", callback_data="admin_backup"),
            InlineKeyboardButton("♻️ Restore DB", callback_data="admin_restore"),
        ],
        [
            InlineKeyboardButton("🎁 Gift VIP", callback_data="admin_gift"),
            InlineKeyboardButton("🚫 Revoke VIP", callback_data="admin_revoke"),
        ],
        [InlineKeyboardButton("🔙 Kembali", callback_data="menu_back")],
    ])


async def start_client_for_user(user_id, api_id, api_hash, string_session):
    old = active_clients.get(user_id)
    if old and old.is_connected():
        await old.disconnect()

    client = build_client(api_id, api_hash, string_session)
    await client.start()

    # ── .dl HANDLER ─────────────────────────────────────────────
    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.dl$"))
    async def dl_handler(event):
        if not is_subscribed(user_id):
            await event.client.send_message(
                "me",
                "❌ Akses `.dl` membutuhkan langganan VIP aktif."
            )
            return
        await event.delete()
        if not event.is_reply:
            return
        replied = await event.get_reply_message()
        if not replied or not replied.media:
            return
        sender = await replied.get_sender()
        if sender:
            first   = getattr(sender, "first_name", "") or ""
            last    = getattr(sender, "last_name", "") or ""
            display = escape_md((f"{first} {last}").strip() or "Unknown")
            mention = f"[{display}](tg://user?id={sender.id})"
        else:
            mention = "Unknown"
        chat       = await event.get_chat()
        chat_title = escape_md(getattr(chat, "title", None) or "Private Chat")
        caption    = f"📥 **Dari:** {mention}\n💬 **Chat:** {chat_title}"

        # Coba forward dulu, kalau gagal download
        if not is_no_forward(replied):
            try:
                await client.forward_messages("me", replied)
                await client.send_message("me", caption, parse_mode="markdown")
                return
            except Exception:
                pass

        # Fallback: download manual
        try:
            media_bytes = await client.download_media(replied.media, bytes)
        except Exception:
            return
        if not media_bytes:
            return
        file_obj = io.BytesIO(media_bytes)
        if isinstance(replied.media, MessageMediaPhoto):
            file_obj.name = "photo.jpg"
        else:
            fname = "media.mp4"
            try:
                for attr in replied.media.document.attributes:
                    if hasattr(attr, "file_name") and attr.file_name:
                        fname = attr.file_name
                        break
            except Exception:
                pass
            file_obj.name = fname
        await client.send_file("me", file=file_obj, caption=caption, parse_mode="markdown")

    # ── .copy HANDLER ─────────────────────────────────────────────
    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.copy\s+(https?://t\.me/\S+)$"))
    async def copy_handler(event):
        if not is_subscribed(user_id):
            await event.client.send_message(
                "me",
                "❌ Akses `.copy` membutuhkan langganan VIP aktif."
            )
            return
        await event.delete()

        url = event.pattern_match.group(1).strip()
        m = TG_LINK_RE.match(url)
        if not m:
            await client.send_message("me", "❌ Link tidak valid. Gunakan format: `.copy https://t.me/channel/123`")
            return

        # Tentukan channel dan message ID
        username_part  = m.group("username")
        msg_id_part    = m.group("msg_id")
        channel_id_part = m.group("channel_id")
        msg_id2_part   = m.group("msg_id2")

        if username_part and msg_id_part:
            channel_ref = username_part
            msg_id = int(msg_id_part)
        elif channel_id_part and msg_id2_part:
            channel_ref = int(channel_id_part)
            msg_id = int(msg_id2_part)
        else:
            await client.send_message("me", "❌ Format link tidak dikenali.")
            return

        status_msg = await client.send_message("me", "⏳ Mengambil pesan...")

        try:
            msg = await client.get_messages(channel_ref, ids=msg_id)
        except Exception as e:
            await status_msg.edit(f"❌ Gagal mengambil pesan: `{e}`")
            return

        if msg is None:
            await status_msg.edit("❌ Pesan tidak ditemukan.")
            return

        await status_msg.delete()

        # ── Proses berdasarkan tipe konten ─────────────────────────
        # 1. Teks saja (tanpa media)
        if not msg.media:
            text_content = msg.text or msg.message or ""
            if text_content:
                await client.send_message("me", f"📋 **Dari channel:**\n\n{text_content}", parse_mode="markdown")
            else:
                await client.send_message("me", "⚠️ Pesan kosong atau tidak ada konten.")
            return

        # 2. Coba forward langsung dulu (lebih cepat, hemat bandwidth)
        if not is_no_forward(msg):
            try:
                await client.forward_messages("me", msg)
                return
            except Exception:
                pass  # Kalau gagal, lanjut ke metode download

        # 3. Download manual (untuk restricted/noforwards)
        try:
            if isinstance(msg.media, MessageMediaPhoto):
                # Foto
                media_bytes = await client.download_media(msg.media, bytes)
                if media_bytes:
                    file_obj = io.BytesIO(media_bytes)
                    file_obj.name = "photo.jpg"
                    caption = msg.text or ""
                    await client.send_file("me", file=file_obj, caption=caption)

            elif isinstance(msg.media, MessageMediaDocument):
                doc = msg.media.document
                # Cek apakah stiker
                is_sticker = any(
                    getattr(attr, "stickerset", None) is not None
                    for attr in doc.attributes
                )
                # Cek apakah animated sticker (.tgs) atau video sticker
                mime = getattr(doc, "mime_type", "") or ""

                if is_sticker or "sticker" in mime:
                    # Stiker: download dan kirim sebagai stiker
                    media_bytes = await client.download_media(msg.media, bytes)
                    if media_bytes:
                        file_obj = io.BytesIO(media_bytes)
                        # Tentukan ekstensi berdasarkan mime
                        if "webp" in mime:
                            file_obj.name = "sticker.webp"
                        elif "tgsticker" in mime or "application/x-tgsticker" in mime:
                            file_obj.name = "sticker.tgs"
                        elif "video" in mime:
                            file_obj.name = "sticker.webm"
                        else:
                            file_obj.name = "sticker.webp"
                        await client.send_file("me", file=file_obj, force_document=False)
                else:
                    # Dokumen / video / audio biasa
                    fname = "document"
                    for attr in doc.attributes:
                        if hasattr(attr, "file_name") and attr.file_name:
                            fname = attr.file_name
                            break
                    # Beri ekstensi dari mime kalau tidak ada
                    if "." not in fname:
                        ext_map = {
                            "video/mp4": ".mp4",
                            "audio/mpeg": ".mp3",
                            "audio/ogg": ".ogg",
                            "application/pdf": ".pdf",
                            "video/webm": ".webm",
                        }
                        fname += ext_map.get(mime, "")
                    media_bytes = await client.download_media(msg.media, bytes)
                    if media_bytes:
                        file_obj = io.BytesIO(media_bytes)
                        file_obj.name = fname
                        caption = msg.text or ""
                        await client.send_file(
                            "me", file=file_obj,
                            caption=caption,
                            force_document=False
                        )
            else:
                # Media lain (voice, gif, etc.) — coba forward, kalau gagal skip
                try:
                    await client.forward_messages("me", msg)
                except Exception:
                    await client.send_message("me", "⚠️ Tipe media ini tidak didukung untuk di-copy.")

        except Exception as e:
            await client.send_message("me", f"❌ Gagal mendownload media: `{e}`")

    active_clients[user_id] = client
    print(f"✅ Client aktif untuk user {user_id}")
    asyncio.ensure_future(client.run_until_disconnected())


# ── POST INIT ─────────────────────────────────────────
async def post_init(app):
    try:
        init_db()
        print("✅ Database siap.")
    except Exception as e:
        print(f"❌ Gagal init database: {e}")
        return
    try:
        from database import get_conn
        conn = get_conn()
        cur  = conn.cursor()
        cur.execute("SELECT user_id, api_id, api_hash, string_session FROM sessions")
        rows = cur.fetchall()
        conn.close()
    except Exception as e:
        print(f"❌ Gagal load sessions: {e}")
        return
    if not rows:
        print("ℹ️ Tidak ada session tersimpan.")
        return
    print(f"🔄 Memuat {len(rows)} session tersimpan...")
    for row in rows:
        try:
            await start_client_for_user(row[0], row[1], row[2], row[3])
        except Exception as e:
            print(f"⚠️ Gagal load session user {row[0]}: {e}")
    print("✅ Semua session berhasil dimuat!")


# ── COMMAND HANDLERS ──────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    user = update.effective_user
    upsert_user(uid, user.username, user.full_name)
    session = get_user_session(uid)
    client  = active_clients.get(uid)
    if session and client and client.is_connected():
        status = "✅ *Aktif*"
    elif session:
        status = "⚠️ *Session tersimpan, client belum terhubung*"
    else:
        status = "❌ *Belum diatur*"
    if is_subscribed(uid):
        info       = get_subscription_info(uid)
        expired    = datetime.fromisoformat(info[1])
        sub_status = f"\n💳 Langganan: ✅ Aktif s/d *{expired.strftime('%d %b %Y')}*"
    else:
        sub_status = "\n💳 Langganan: ❌ Belum berlangganan"
    await update.message.reply_text(
        f"👋 *Selamat datang di Rams VIP Bot!*\n\nStatus session: {status}{sub_status}\n\nPilih menu di bawah:",
        parse_mode="Markdown",
        reply_markup=main_keyboard(uid)
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    temp_store.pop(uid, None)
    waiting_restore.discard(uid)
    waiting_gift.discard(uid)
    waiting_revoke.discard(uid)
    await update.message.reply_text(
        "❌ *Dibatalkan.*",
        parse_mode="Markdown",
        reply_markup=main_keyboard(uid)
    )
    return ConversationHandler.END


async def cmd_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /revoke <user_id atau @username>"""
    uid = update.effective_user.id
    if uid != ADMIN_ID:
        await update.message.reply_text("❌ Kamu tidak memiliki izin.")
        return
    args = context.args
    if not args:
        await update.message.reply_text(
            "⚠️ Format: `/revoke <user_id atau @username>`",
            parse_mode="Markdown"
        )
        return
    target_str = args[0].strip()
    target_id  = _resolve_target(target_str)
    if target_id is None:
        await update.message.reply_text(
            f"❌ User `{target_str}` tidak ditemukan di database.",
            parse_mode="Markdown"
        )
        return
    if not is_subscribed(target_id):
        await update.message.reply_text(
            f"⚠️ User `{target_id}` tidak memiliki langganan VIP aktif.",
            parse_mode="Markdown"
        )
        return
    revoke_subscription(target_id)
    await update.message.reply_text(
        f"✅ VIP user `{target_id}` berhasil dicabut.",
        parse_mode="Markdown",
        reply_markup=admin_keyboard()
    )


async def cmd_gift(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /gift <user_id atau @username> [days]"""
    uid = update.effective_user.id
    if uid != ADMIN_ID:
        await update.message.reply_text("❌ Kamu tidak memiliki izin.")
        return
    args = context.args
    if not args:
        await update.message.reply_text(
            "⚠️ Format: `/gift <user_id atau @username> [days]`\n"
            "Contoh: `/gift 123456789 30` atau `/gift @username`",
            parse_mode="Markdown"
        )
        return
    target_str = args[0].strip()
    days = int(args[1]) if len(args) > 1 and args[1].isdigit() else 30
    target_id = _resolve_target(target_str)
    if target_id is None:
        await update.message.reply_text(
            f"❌ User `{target_str}` tidak ditemukan di database.",
            parse_mode="Markdown"
        )
        return
    expired = activate_subscription(target_id, days=days)
    await update.message.reply_text(
        f"🎁 VIP berhasil diberikan ke `{target_id}` selama *{days} hari*\n"
        f"Aktif hingga: *{expired.strftime('%d %b %Y')}*",
        parse_mode="Markdown",
        reply_markup=admin_keyboard()
    )


def _resolve_target(target_str: str):
    """Resolve user_id dari ID angka atau @username."""
    if target_str.lstrip("@").isdigit() and not target_str.startswith("@"):
        return int(target_str)
    # Bisa berupa @username atau username biasa
    return get_user_by_username(target_str)


# ── SETUP CONVERSATION ────────────────────────────────
async def cmd_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_subscribed(uid):
        await update.message.reply_text(
            "❌ Kamu belum berlangganan VIP.\n\n"
            "Untuk berlangganan VIP, silakan hubungi admin.",
            parse_mode="Markdown", reply_markup=main_keyboard(uid)
        )
        return ConversationHandler.END
    temp_store.pop(uid, None)
    await update.message.reply_text(
        "🔧 *Setup Session Telegram*\n\n"
        "Ketik /cancel kapan saja untuk membatalkan.\n\n"
        "━━━━━━━━━━━━━━━━━\n"
        "*Langkah 1 dari 5 — API ID*\n\n"
        "📌 Kunjungi https://my.telegram.org → *API development tools*\n"
        "Salin angka di kolom *App api_id* dan kirim di sini:",
        parse_mode="Markdown"
    )
    return API_ID_STEP


async def setup_api_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("❌ API ID harus berupa angka. Coba lagi:")
        return API_ID_STEP
    temp_store.setdefault(uid, {})["api_id"] = int(text)
    await update.message.reply_text(
        "✅ API ID tersimpan!\n\n"
        "━━━━━━━━━━━━━━━━━\n"
        "*Langkah 2 dari 5 — API Hash*\n\n"
        "Salin teks panjang di kolom *App api_hash* (32 karakter) dan kirim:",
        parse_mode="Markdown"
    )
    return API_HASH_STEP


async def setup_api_hash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    temp_store.setdefault(uid, {})["api_hash"] = update.message.text.strip()
    await update.message.reply_text(
        "✅ API Hash tersimpan!\n\n"
        "━━━━━━━━━━━━━━━━━\n"
        "*Langkah 3 dari 5 — Nomor HP*\n\n"
        "Format: `+628xxxxxxxxxx`\n\nKirim nomor HP kamu:",
        parse_mode="Markdown"
    )
    return PHONE_STEP


async def setup_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    phone = update.message.text.strip()
    data  = temp_store.get(uid, {})
    client = build_client(data["api_id"], data["api_hash"])
    try:
        await client.connect()
        result = await client.send_code_request(phone)
        temp_store[uid]["phone"]      = phone
        temp_store[uid]["phone_hash"] = result.phone_code_hash
        temp_store[uid]["client"]     = client
        await update.message.reply_text(
            "📨 Kode OTP dikirim ke Telegram kamu!\n\n"
            "━━━━━━━━━━━━━━━━━\n"
            "*Langkah 4 dari 5 — Kode OTP*\n\n"
            "Ketik kode *dengan spasi* (contoh: `1 2 3 4 5`):",
            parse_mode="Markdown"
        )
        return CODE_STEP
    except Exception as e:
        await client.disconnect()
        temp_store.pop(uid, None)
        await update.message.reply_text(f"❌ Gagal kirim OTP: `{e}`\nSilakan /setup ulang.", parse_mode="Markdown", reply_markup=main_keyboard(uid))
        return ConversationHandler.END


async def setup_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    code = update.message.text.strip().replace(" ", "")
    data = temp_store.get(uid, {})
    client = data.get("client")
    try:
        await client.sign_in(data["phone"], code, phone_code_hash=data["phone_hash"])
    except SessionPasswordNeededError:
        await update.message.reply_text(
            "🔐 Akun ini menggunakan 2FA.\n\n"
            "*Langkah 5 dari 5 — Password 2FA*\n\nKirim password 2FA kamu:",
            parse_mode="Markdown"
        )
        return PASSWORD_STEP
    except (PhoneCodeInvalidError, PhoneCodeExpiredError):
        await client.disconnect()
        temp_store.pop(uid, None)
        await update.message.reply_text("❌ Kode OTP salah/kadaluarsa. Silakan /setup ulang.")
        return ConversationHandler.END
    except Exception as e:
        await client.disconnect()
        temp_store.pop(uid, None)
        await update.message.reply_text(f"❌ Error: `{e}`", parse_mode="Markdown")
        return ConversationHandler.END
    return await _finish_setup(update, uid, data, client)


async def setup_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid      = update.effective_user.id
    password = update.message.text.strip()
    data     = temp_store.get(uid, {})
    client   = data.get("client")
    try:
        await client.sign_in(password=password)
    except Exception as e:
        await client.disconnect()
        temp_store.pop(uid, None)
        await update.message.reply_text(f"❌ Password 2FA salah: `{e}`\nSilakan /setup ulang.", parse_mode="Markdown")
        return ConversationHandler.END
    return await _finish_setup(update, uid, data, client)


async def _finish_setup(update, uid, data, client):
    string_session = client.session.save()
    save_user_session(uid, data["api_id"], data["api_hash"], string_session)
    await start_client_for_user(uid, data["api_id"], data["api_hash"], string_session)
    temp_store.pop(uid, None)
    await update.message.reply_text(
        "✅ *Setup berhasil!*\n\n"
        "Session kamu sudah aktif.\n\n"
        "📌 *Cara pakai fitur:*\n"
        "• Reply pesan lalu ketik `.dl` → download/forward ke Saved Messages\n"
        "• Ketik `.copy <link>` → copy konten dari channel restricted ke Saved Messages\n\n"
        "Contoh: `.copy https://t.me/channelname/123`",
        parse_mode="Markdown",
        reply_markup=main_keyboard(uid)
    )
    return ConversationHandler.END


# ── CALLBACK HANDLER ──────────────────────────────────────────────
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid   = query.from_user.id
    data  = query.data
    await query.answer()

    if data == "menu_beli":
        await query.edit_message_text(
            "💎 *Cara Berlangganan VIP*\n\n"
            "Untuk mendapatkan akses VIP, silakan hubungi admin secara langsung.\n\n"
            "Admin akan mengaktifkan VIP kamu setelah konfirmasi pembayaran.\n\n"
            f"👤 Hubungi Admin: tg://user?id={ADMIN_ID}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💬 Chat Admin", url=f"tg://user?id={ADMIN_ID}")],
                [InlineKeyboardButton("🔙 Kembali", callback_data="menu_back")],
            ])
        )
    elif data == "menu_subscription":
        await _do_subscription(query, uid)
    elif data == "menu_setup":
        if not is_subscribed(uid):
            await query.edit_message_text(
                "❌ Kamu belum berlangganan VIP.\n\nHubungi admin untuk berlangganan.",
                parse_mode="Markdown", reply_markup=main_keyboard(uid)
            )
            return
        await query.edit_message_text(
            "📱 Gunakan /setup untuk mengatur session Telegram kamu.",
            reply_markup=main_keyboard(uid)
        )
    elif data == "menu_admin":
        if uid != ADMIN_ID:
            return
        await query.edit_message_text(
            "👤 *Menu Admin*\n\nPilih tindakan:",
            parse_mode="Markdown", reply_markup=admin_keyboard()
        )
    elif data == "menu_back":
        await query.edit_message_text(
            "👋 *Menu Utama*\n\nPilih menu di bawah:",
            parse_mode="Markdown", reply_markup=main_keyboard(uid)
        )
    elif data == "admin_backup":
        if uid != ADMIN_ID: return
        await _do_backup(query, context)
    elif data == "admin_restore":
        if uid != ADMIN_ID: return
        waiting_restore.add(ADMIN_ID)
        await query.edit_message_text(
            "📥 *Mode Restore Aktif*\n\nKirim file `.sql` hasil backup.\n_/cancel untuk batal._",
            parse_mode="Markdown"
        )
    elif data == "admin_gift":
        if uid != ADMIN_ID: return
        waiting_gift.add(ADMIN_ID)
        await query.edit_message_text(
            "🎁 *Gift VIP*\n\n"
            "Kirim target dalam format:\n"
            "`<user_id> [days]` atau `@username [days]`\n\n"
            "Contoh: `123456789 30` atau `@johndoe 30`\n"
            "_(Default 30 hari jika tidak diisi)_\n\n_/cancel untuk batal._",
            parse_mode="Markdown"
        )
    elif data == "admin_revoke":
        if uid != ADMIN_ID: return
        waiting_revoke.add(ADMIN_ID)
        await query.edit_message_text(
            "🚫 *Revoke VIP*\n\n"
            "Kirim user_id atau @username yang ingin dicabut VIP-nya:\n\n"
            "Contoh: `123456789` atau `@johndoe`\n\n_/cancel untuk batal._",
            parse_mode="Markdown"
        )


# ── HELPER: SUBSCRIPTION ──────────────────────────────
async def _do_subscription(query, uid):
    if is_subscribed(uid):
        info    = get_subscription_info(uid)
        expired = datetime.fromisoformat(info[1])
        text    = (
            f"✅ *Status Langganan Aktif*\n\n"
            f"📅 Aktif hingga: *{expired.strftime('%d %b %Y')}*\n"
            f"⏳ Sisa: *{(expired - datetime.now()).days} hari*"
        )
    else:
        text = (
            "❌ *Kamu belum berlangganan VIP.*\n\n"
            "Hubungi admin untuk berlangganan."
        )
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=main_keyboard(uid))


# ── HELPER: BACKUP ────────────────────────────────────
async def _do_backup(query, context):
    try:
        from database import get_conn
        conn = get_conn()
        cur  = conn.cursor()
        tables = ["users", "sessions", "subscriptions"]
        sql_lines = []
        for table in tables:
            cur.execute(f"SELECT * FROM {table}")
            rows = cur.fetchall()
            cur.execute(
                f"SELECT column_name FROM information_schema.columns "
                f"WHERE table_name='{table}' ORDER BY ordinal_position"
            )
            cols = [r[0] for r in cur.fetchall()]
            for row in rows:
                vals = ", ".join(
                    "NULL" if v is None else f"'{str(v).replace(chr(39), chr(39)*2)}'"
                    for v in row
                )
                sql_lines.append(
                    f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({vals}) ON CONFLICT DO NOTHING;"
                )
        conn.close()
        sql_content = "\n".join(sql_lines)
        file_obj = io.BytesIO(sql_content.encode())
        file_obj.name = f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.sql"
        await context.bot.send_document(
            chat_id=query.message.chat_id,
            document=file_obj,
            caption="📦 Backup database berhasil."
        )
        await query.edit_message_text("✅ Backup selesai.", reply_markup=admin_keyboard())
    except Exception as e:
        await query.edit_message_text(f"❌ Backup gagal: {e}", reply_markup=admin_keyboard())


# ── MESSAGE HANDLER ───────────────────────────────────
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    # Admin: Restore DB
    if uid == ADMIN_ID and uid in waiting_restore:
        waiting_restore.discard(uid)
        if not update.message.document:
            await update.message.reply_text("❌ Kirim file .sql yang valid.")
            return
        file = await context.bot.get_file(update.message.document.file_id)
        buf  = io.BytesIO()
        await file.download_to_memory(buf)
        sql  = buf.getvalue().decode()
        try:
            from database import get_conn
            conn = get_conn()
            cur  = conn.cursor()
            for stmt in sql.split(";"):
                stmt = stmt.strip()
                if stmt:
                    cur.execute(stmt)
            conn.commit()
            conn.close()
            await update.message.reply_text("✅ Restore berhasil!", reply_markup=admin_keyboard())
        except Exception as e:
            await update.message.reply_text(f"❌ Restore gagal: {e}", reply_markup=admin_keyboard())
        return

    # Admin: Gift VIP via menu
    if uid == ADMIN_ID and uid in waiting_gift:
        waiting_gift.discard(uid)
        text = update.message.text.strip() if update.message.text else ""
        parts = text.split()
        if not parts:
            await update.message.reply_text("❌ Input tidak valid.", reply_markup=admin_keyboard())
            return
        target_str = parts[0]
        days = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 30
        target_id = _resolve_target(target_str)
        if target_id is None:
            await update.message.reply_text(
                f"❌ User `{target_str}` tidak ditemukan.",
                parse_mode="Markdown", reply_markup=admin_keyboard()
            )
            return
        expired = activate_subscription(target_id, days=days)
        await update.message.reply_text(
            f"🎁 VIP diberikan ke `{target_id}` selama *{days} hari*\n"
            f"Aktif hingga: *{expired.strftime('%d %b %Y')}*",
            parse_mode="Markdown", reply_markup=admin_keyboard()
        )
        return

    # Admin: Revoke VIP via menu
    if uid == ADMIN_ID and uid in waiting_revoke:
        waiting_revoke.discard(uid)
        text = update.message.text.strip() if update.message.text else ""
        if not text:
            await update.message.reply_text("❌ Input tidak valid.", reply_markup=admin_keyboard())
            return
        target_id = _resolve_target(text)
        if target_id is None:
            await update.message.reply_text(
                f"❌ User `{text}` tidak ditemukan di database.",
                parse_mode="Markdown", reply_markup=admin_keyboard()
            )
            return
        if not is_subscribed(target_id):
            await update.message.reply_text(
                f"⚠️ User `{target_id}` tidak punya VIP aktif.",
                parse_mode="Markdown", reply_markup=admin_keyboard()
            )
            return
        revoke_subscription(target_id)
        await update.message.reply_text(
            f"✅ VIP user `{target_id}` berhasil dicabut.",
            parse_mode="Markdown", reply_markup=admin_keyboard()
        )
        return


# ── MAIN ──────────────────────────────────────────────
def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    setup_conv = ConversationHandler(
        entry_points=[CommandHandler("setup", cmd_setup)],
        states={
            API_ID_STEP:   [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_api_id)],
            API_HASH_STEP: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_api_hash)],
            PHONE_STEP:    [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_phone)],
            CODE_STEP:     [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_code)],
            PASSWORD_STEP: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_password)],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            CommandHandler("start", cmd_start),
        ],
        allow_reentry=True,
    )

    app.add_handler(setup_conv)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("revoke", cmd_revoke))
    app.add_handler(CommandHandler("gift", cmd_gift))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_handler))

    print("🤖 Rams VIP Bot berjalan...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
