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
from telethon.tl.types import (
    MessageMediaPhoto, MessageMediaDocument,
    DocumentAttributeVideo, DocumentAttributeFilename,
    InputPeerChannel
)

from database import (
    init_db, upsert_user,
    save_user_session, get_user_session, delete_user_session,
    is_subscribed, get_subscription_info, activate_subscription, revoke_subscription,
    get_user_by_username
)

# ── ENV VARS ──────────────────────────────────────────────────────
BOT_TOKEN   = os.environ["BOT_TOKEN"]
ADMIN_ID    = int(os.environ["ADMIN_ID"])

DEVICE_MODEL     = "iPhone 17 Pro Max"
SYSTEM_VERSION   = "iOS 18.3.2"
APP_VERSION      = "11.4.1"
LANG_CODE        = "id"
SYSTEM_LANG_CODE = "id-ID"

API_ID_STEP, API_HASH_STEP, PHONE_STEP, CODE_STEP, PASSWORD_STEP = range(5)

waiting_restore = set()
waiting_gift    = set()
waiting_revoke  = set()

temp_store     = {}
active_clients = {}

# ── DEDUP & LOCK: mencegah .dl diproses dua kali ──────────────────────
# Key: user_id → asyncio.Lock (satu proses .dl dalam satu waktu per user)
dl_locks: dict[int, asyncio.Lock] = {}
# Key: user_id → set of event.id yang sudah diproses (TTL manual: max 50 item)
dl_seen:  dict[int, set]          = {}


TG_LINK_RE = re.compile(
    r"(?:https?://)?t\.me/"
    r"(?:c/(?P<channel_id>\d+)/(?P<msg_id2>\d+)|"
    r"(?P<username>[a-zA-Z0-9_]+)/(?P<msg_id>\d+))"
)


# ── HELPERS ───────────────────────────────────────────────────────
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


def is_no_forward(message):
    return bool(getattr(message, "noforwards", False))


def is_sticker_doc(doc):
    if doc is None:
        return False
    mime = getattr(doc, "mime_type", "") or ""
    has_stickerset = any(
        getattr(attr, "stickerset", None) is not None
        for attr in getattr(doc, "attributes", [])
    )
    return has_stickerset or "sticker" in mime


def get_video_attributes(doc):
    if doc is None:
        return None
    for attr in getattr(doc, "attributes", []):
        if isinstance(attr, DocumentAttributeVideo):
            return attr
    return None


def get_file_name(doc):
    if doc is None:
        return None
    for attr in getattr(doc, "attributes", []):
        if isinstance(attr, DocumentAttributeFilename):
            return attr.file_name
    return None


def _dl_dedup_check(user_id: int, event_id: int) -> bool:
    """
    Return True jika event ini SUDAH diproses (duplikat), False jika belum.
    Otomatis batasi ukuran set ke 50 entry.
    """
    seen = dl_seen.setdefault(user_id, set())
    if event_id in seen:
        return True  # duplikat!
    seen.add(event_id)
    if len(seen) > 50:
        # Buang separuh entry terlama
        to_remove = list(seen)[:25]
        for x in to_remove:
            seen.discard(x)
    return False


def main_keyboard(uid):
    rows = [
        [InlineKeyboardButton("⚙️ Setup Session", callback_data="menu_setup")],
        [
            InlineKeyboardButton("💎 Beli VIP", callback_data="menu_beli"),
            InlineKeyboardButton("⌛️ Status Langganan", callback_data="menu_subscription"),
        ],
        [InlineKeyboardButton("📖 Cara Penggunaan", callback_data="menu_guide")],
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


GUIDE_TEXT = (
    "📖 *Panduan Penggunaan Rams VIP Bot*\n\n"
    "━━━━━━━━━━━━━━━━━\n"
    "🔹 *Command `.dl` \u2014 Download Media*\n"
    "Digunakan untuk mendownload media *view-once* (sekali lihat) atau "
    "media dari chat *restricted* yang tidak bisa di-forward.\n\n"
    "*Cara pakai:*\n"
    "1. Buka chat yang ada media view-once atau restricted\n"
    "2. *Reply* (balas) pesan media tersebut\n"
    "3. Ketik `.dl` lalu kirim\n"
    "4. Media akan otomatis tersimpan di *Saved Messages* kamu\n\n"
    "⚠️ *Catatan:* Pastikan kamu sudah reply ke pesan medianya, bukan ke pesan teks biasa.\n\n"
    "━━━━━━━━━━━━━━━━━\n"
    "🔹 *Command `.copy` \u2014 Copy dari Channel/Grup*\n"
    "Digunakan untuk menyalin konten (foto, video, dokumen, teks) dari "
    "channel atau grup yang *tidak mengizinkan forward*.\n\n"
    "*Cara pakai:*\n"
    "1. Buka pesan yang ingin di-copy di channel/grup\n"
    "2. Salin link pesan tersebut (klik kanan pesan \u2192 *Copy Link*)\n"
    "3. Ketik `.copy <link>` di chat mana saja\n"
    "4. Konten akan dikirim ke *Saved Messages* kamu\n\n"
    "*Format link yang didukung:*\n"
    "\u2022 Public: `.copy https://t.me/namaChannel/123`\n"
    "\u2022 Private: `.copy https://t.me/c/1234567890/123`\n\n"
    "⚠️ *Catatan:* Untuk channel private, akun kamu harus sudah *bergabung* ke channel tersebut.\n\n"
    "━━━━━━━━━━━━━━━━━\n"
    "💡 *Tips:*\n"
    "\u2022 Semua hasil download dikirim ke *Saved Messages* (pesan tersimpan) akun Telegram kamu\n"
    "\u2022 Pastikan session sudah di-setup via /setup sebelum menggunakan fitur ini\n"
    "\u2022 Fitur ini hanya tersedia untuk pengguna VIP aktif"
)


async def start_client_for_user(user_id, api_id, api_hash, string_session):
    old = active_clients.get(user_id)
    if old and old.is_connected():
        await old.disconnect()

    # Inisialisasi lock & dedup untuk user ini
    if user_id not in dl_locks:
        dl_locks[user_id] = asyncio.Lock()

    client = build_client(api_id, api_hash, string_session)
    await client.start()

    # ── .dl HANDLER ──────────────────────────────────────────────
    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.dl$"))
    async def dl_handler(event):
        # ── DEDUP CHECK: tolak jika event ini sudah pernah diproses ──
        if _dl_dedup_check(user_id, event.id):
            return

        # ── LOCK: pastikan hanya satu proses .dl berjalan per user ──
        lock = dl_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            dl_locks[user_id] = lock

        async with lock:
            await _process_dl(event, client, user_id)

    # ── .copy HANDLER ─────────────────────────────────────────────
    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.copy\s+(https?://t\.me/\S+)$"))
    async def copy_handler(event):
        if not is_subscribed(user_id):
            await event.client.send_message("me", "❌ Akses `.copy` membutuhkan langganan VIP aktif.")
            return
        await event.delete()

        url = event.pattern_match.group(1).strip()
        m = TG_LINK_RE.match(url)
        if not m:
            await client.send_message("me", "❌ Link tidak valid. Gunakan format: `.copy https://t.me/channel/123`")
            return

        channel_id_part = m.group("channel_id")
        msg_id2_part    = m.group("msg_id2")
        username_part   = m.group("username")
        msg_id_part     = m.group("msg_id")

        if channel_id_part and msg_id2_part:
            try:
                channel_entity = await client.get_entity(InputPeerChannel(
                    channel_id=int(channel_id_part),
                    access_hash=0
                ))
            except Exception:
                try:
                    from telethon.tl.types import PeerChannel
                    channel_entity = await client.get_entity(PeerChannel(int(channel_id_part)))
                except Exception as e:
                    await client.send_message(
                        "me",
                        f"❌ Gagal mengakses channel `{channel_id_part}`.\n"
                        f"Pastikan akun kamu sudah *bergabung* ke channel tersebut.\n\nError: `{e}`"
                    )
                    return
            msg_id = int(msg_id2_part)
        elif username_part and msg_id_part:
            channel_entity = username_part
            msg_id = int(msg_id_part)
        else:
            await client.send_message("me", "❌ Format link tidak dikenali.")
            return

        status_msg = await client.send_message("me", "⏳ Sedang mengambil pesan...")

        try:
            msg = await client.get_messages(channel_entity, ids=msg_id)
        except Exception as e:
            await status_msg.edit(
                f"❌ Gagal mengambil pesan: `{e}`\n\n"
                f"Pastikan akun kamu sudah *bergabung* ke channel tersebut."
            )
            return

        if msg is None:
            await status_msg.edit("❌ Pesan tidak ditemukan.")
            return

        if not msg.media:
            text_content = msg.text or msg.message or ""
            if text_content:
                await status_msg.edit(f"📋 **Dari channel:**\n\n{text_content}", parse_mode="markdown")
            else:
                await status_msg.edit("⚠️ Pesan kosong atau tidak ada konten.")
            return

        if not is_no_forward(msg):
            try:
                await client.forward_messages("me", msg)
                await status_msg.delete()
                return
            except Exception:
                pass

        await status_msg.edit("⏳ Sedang mendownload media...")

        try:
            if isinstance(msg.media, MessageMediaPhoto):
                media_bytes = await client.download_media(msg.media, bytes)
                if media_bytes:
                    file_obj = io.BytesIO(media_bytes)
                    file_obj.name = "photo.jpg"
                    caption = msg.text or ""
                    await status_msg.delete()
                    await client.send_file("me", file=file_obj, caption=caption)
                else:
                    await status_msg.edit("❌ Gagal mendownload foto.")

            elif isinstance(msg.media, MessageMediaDocument):
                doc  = msg.media.document
                mime = getattr(doc, "mime_type", "") or ""

                if is_sticker_doc(doc):
                    media_bytes = await client.download_media(msg.media, bytes)
                    if media_bytes:
                        file_obj = io.BytesIO(media_bytes)
                        if "webp" in mime:        file_obj.name = "sticker.webp"
                        elif "tgsticker" in mime: file_obj.name = "sticker.tgs"
                        elif "video" in mime:     file_obj.name = "sticker.webm"
                        else:                      file_obj.name = "sticker.webp"
                        await status_msg.delete()
                        await client.send_file("me", file=file_obj, force_document=False)

                elif "video" in mime or "mp4" in mime:
                    video_attr = get_video_attributes(doc)
                    fname = get_file_name(doc) or "video.mp4"
                    media_bytes = await client.download_media(msg.media, bytes)
                    if media_bytes:
                        file_obj = io.BytesIO(media_bytes)
                        file_obj.name = fname
                        caption = msg.text or ""
                        send_attrs = []
                        if video_attr:
                            send_attrs = [DocumentAttributeVideo(
                                duration=video_attr.duration,
                                w=video_attr.w,
                                h=video_attr.h,
                                supports_streaming=True,
                                round_message=False,
                            )]
                        await status_msg.delete()
                        await client.send_file(
                            "me", file=file_obj,
                            caption=caption, parse_mode="markdown",
                            attributes=send_attrs if send_attrs else None,
                            allow_cache=False,
                        )
                    else:
                        await status_msg.edit("❌ Gagal mendownload video.")

                else:
                    fname = get_file_name(doc) or "document"
                    if "." not in fname:
                        ext_map = {
                            "audio/mpeg": ".mp3",
                            "audio/ogg":  ".ogg",
                            "application/pdf": ".pdf",
                            "video/webm": ".webm",
                            "image/gif":  ".gif",
                        }
                        fname += ext_map.get(mime, "")
                    media_bytes = await client.download_media(msg.media, bytes)
                    if media_bytes:
                        file_obj = io.BytesIO(media_bytes)
                        file_obj.name = fname
                        caption = msg.text or ""
                        await status_msg.delete()
                        await client.send_file(
                            "me", file=file_obj,
                            caption=caption, force_document=False,
                            allow_cache=False,
                        )
                    else:
                        await status_msg.edit("❌ Gagal mendownload file.")
            else:
                try:
                    await client.forward_messages("me", msg)
                    await status_msg.delete()
                except Exception:
                    await status_msg.edit("⚠️ Tipe media ini tidak didukung untuk di-copy.")

        except Exception as e:
            await status_msg.edit(f"❌ Gagal mendownload media: `{e}`")

    active_clients[user_id] = client
    print(f"✅ Client aktif untuk user {user_id}")
    asyncio.ensure_future(client.run_until_disconnected())


async def _process_dl(event, client, user_id):
    """Logika utama .dl, dipanggil setelah dedup & lock."""
    if not is_subscribed(user_id):
        await event.client.send_message("me", "❌ Akses `.dl` membutuhkan langganan VIP aktif.")
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

    status_msg = await client.send_message("me", "⏳ Sedang mendownload media...")

    is_view_once_media = bool(getattr(replied.media, "ttl_seconds", None))

    # Jika BUKAN view-once dan BUKAN restricted → forward saja
    if not is_view_once_media and not is_no_forward(replied):
        try:
            await client.forward_messages("me", replied)
            await status_msg.edit(caption, parse_mode="markdown")
            return  # ✔️ selesai
        except Exception:
            pass  # fallback ke download manual

    # Download manual
    try:
        media_bytes = await client.download_media(replied.media, bytes)
    except Exception as e:
        await status_msg.edit(f"❌ Gagal mendownload: `{e}`")
        return
    if not media_bytes:
        await status_msg.delete()
        return

    file_obj = io.BytesIO(media_bytes)

    if isinstance(replied.media, MessageMediaPhoto):
        file_obj.name = "photo.jpg"
        await status_msg.delete()
        await client.send_file("me", file=file_obj, caption=caption, parse_mode="markdown")

    elif isinstance(replied.media, MessageMediaDocument):
        doc  = replied.media.document
        mime = getattr(doc, "mime_type", "") or ""

        if is_sticker_doc(doc):
            if "webp" in mime:        file_obj.name = "sticker.webp"
            elif "tgsticker" in mime: file_obj.name = "sticker.tgs"
            elif "video" in mime:     file_obj.name = "sticker.webm"
            else:                      file_obj.name = "sticker.webp"
            await status_msg.delete()
            await client.send_file("me", file=file_obj, force_document=False)

        elif "video" in mime or "mp4" in mime:
            video_attr = get_video_attributes(doc)
            fname      = get_file_name(doc) or "video.mp4"
            file_obj.name = fname
            send_attrs = []
            if video_attr:
                send_attrs = [DocumentAttributeVideo(
                    duration=video_attr.duration,
                    w=video_attr.w,
                    h=video_attr.h,
                    supports_streaming=True,
                    round_message=False,
                )]
            await status_msg.delete()
            await client.send_file(
                "me", file=file_obj,
                caption=caption, parse_mode="markdown",
                attributes=send_attrs if send_attrs else None,
                allow_cache=False,
            )

        else:
            fname = get_file_name(doc) or "document"
            if "." not in fname:
                ext_map = {
                    "audio/mpeg": ".mp3",
                    "audio/ogg":  ".ogg",
                    "application/pdf": ".pdf",
                    "video/webm": ".webm",
                    "image/gif":  ".gif",
                }
                fname += ext_map.get(mime, "")
            file_obj.name = fname
            await status_msg.delete()
            await client.send_file(
                "me", file=file_obj,
                caption=caption, parse_mode="markdown",
                force_document=False, allow_cache=False,
            )
    else:
        await status_msg.delete()
        await client.send_file("me", file=file_obj, caption=caption, parse_mode="markdown")


# ── POST INIT ─────────────────────────────────────────────────────
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


# ── ADMIN HELPERS ─────────────────────────────────────────────────
def _resolve_target(target_str: str):
    clean = target_str.lstrip("@")
    if clean.isdigit():
        return int(clean)
    return get_user_by_username(clean)


def _find_subscribed_user(target_str: str):
    clean = target_str.lstrip("@")
    if clean.isdigit():
        return int(clean)
    return get_user_by_username(clean)


async def _do_gift(target_str: str, days: int, context) -> tuple:
    clean = target_str.lstrip("@")

    if clean.isdigit():
        # ✔️ Langsung pakai user_id — tidak perlu user pernah /start
        target_id = int(clean)
    else:
        # Cari by username di DB
        target_id = get_user_by_username(clean)
        if target_id is None:
            return False, (
                f"❌ Username `@{clean}` tidak ditemukan di database.\n\n"
                f"💡 *Tips:* Gunakan *user\_id* (angka) agar bisa gift tanpa user perlu /start dulu.\n"
                f"User\_id bisa didapat dari @userinfobot atau forward pesan user ke @getidsbot."
            )

    # Pastikan ada di tabel users (insert minimal jika belum ada)
    upsert_user(target_id, None, None)
    expired = activate_subscription(target_id, days=days)

    # Coba kirim notif ke user (mungkin gagal jika user belum start bot)
    notif_sent = False
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=(
                f"🎁 *Selamat! VIP kamu telah diaktifkan!*\n\n"
                f"📅 Aktif hingga: *{expired.strftime('%d %b %Y')}*\n"
                f"⏳ Durasi: *{days} hari*\n\n"
                f"Gunakan /start untuk melihat fitur VIP."
            ),
            parse_mode="Markdown"
        )
        notif_sent = True
    except Exception:
        pass

    notif_info = "" if notif_sent else "\n\u26a0\ufe0f _Notifikasi ke user gagal dikirim (user belum pernah start bot)._"
    return True, (
        f"🎁 VIP berhasil diberikan ke `{target_id}` selama *{days} hari*\n"
        f"Aktif hingga: *{expired.strftime('%d %b %Y')}*"
        f"{notif_info}"
    )


async def _do_revoke(target_str: str, context) -> tuple:
    target_id = _find_subscribed_user(target_str)
    if target_id is None:
        return False, (
            f"❌ User `{target_str}` tidak ditemukan.\n\n"
            "Gunakan *user\_id* (angka) jika username tidak terdaftar."
        )
    if not is_subscribed(target_id):
        return False, (
            f"⚠️ User `{target_id}` tidak memiliki langganan VIP aktif.\n\n"
            "Mungkin VIP sudah pernah dicabut sebelumnya."
        )
    revoke_subscription(target_id)
    notif_sent = False
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text="🚫 *VIP kamu telah dicabut oleh admin.* Hubungi admin jika ada pertanyaan.",
            parse_mode="Markdown"
        )
        notif_sent = True
    except Exception:
        pass
    notif_info = "" if notif_sent else "\n⚠️ _Notifikasi ke user gagal dikirim._"
    return True, f"✅ VIP user `{target_id}` berhasil dicabut.{notif_info}"


# ── ADMIN MESSAGE HANDLER (group=0) ───────────────────────────────
async def admin_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != ADMIN_ID:
        return
    if uid not in waiting_gift and uid not in waiting_revoke and uid not in waiting_restore:
        return

    if uid in waiting_restore:
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

    if uid in waiting_gift:
        waiting_gift.discard(uid)
        text  = update.message.text.strip() if update.message.text else ""
        parts = text.split()
        if not parts:
            await update.message.reply_text("❌ Input tidak valid.", reply_markup=admin_keyboard())
            return
        target_str = parts[0]
        days = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 30
        ok, msg = await _do_gift(target_str, days, context)
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=admin_keyboard())
        return

    if uid in waiting_revoke:
        waiting_revoke.discard(uid)
        text = update.message.text.strip() if update.message.text else ""
        if not text:
            await update.message.reply_text("❌ Input tidak valid.", reply_markup=admin_keyboard())
            return
        ok, msg = await _do_revoke(text, context)
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=admin_keyboard())
        return


# ── COMMAND HANDLERS ──────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    user = update.effective_user

    waiting_gift.discard(uid)
    waiting_revoke.discard(uid)
    waiting_restore.discard(uid)
    temp_store.pop(uid, None)

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
    ok, msg = await _do_revoke(args[0].strip(), context)
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=admin_keyboard() if ok else None)


async def cmd_gift(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    ok, msg = await _do_gift(target_str, days, context)
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=admin_keyboard() if ok else None)


# ── SETUP CONVERSATION ────────────────────────────────────────────
async def cmd_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_subscribed(uid):
        await update.message.reply_text(
            "❌ Kamu belum berlangganan VIP.\n\nUntuk berlangganan VIP, silakan hubungi admin.",
            parse_mode="Markdown", reply_markup=main_keyboard(uid)
        )
        return ConversationHandler.END
    temp_store.pop(uid, None)
    await update.message.reply_text(
        "🔧 *Setup Session Telegram*\n\n"
        "Proses ini menghubungkan akun Telegram kamu ke bot.\n"
        "Ketik /cancel kapan saja untuk membatalkan.\n\n"
        "━━━━━━━━━━━━━━━━━\n"
        "*Langkah 1 dari 5 \u2014 API ID*\n\n"
        "API ID adalah kode angka unik milik aplikasi Telegram buatanmu.\n\n"
        "📌 *Cara mendapatkan API ID:*\n"
        "1. Buka https://my.telegram.org di browser\n"
        "2. Login dengan nomor HP Telegram kamu\n"
        "3. Masukkan kode OTP yang dikirim ke Telegram\n"
        "4. Klik *API development tools*\n"
        "5. Isi form (nama & platform bebas), klik *Create application*\n"
        "6. Salin angka di kolom *App api\_id*\n\n"
        "Kirim angka tersebut di sini:",
        parse_mode="Markdown"
    )
    return API_ID_STEP


async def setup_api_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text(
            "❌ API ID harus berupa *angka saja*, bukan huruf.\n\n"
            "Contoh yang benar: `12345678`\n\nCoba kirim ulang:",
            parse_mode="Markdown"
        )
        return API_ID_STEP
    temp_store.setdefault(uid, {})["api_id"] = int(text)
    await update.message.reply_text(
        "✅ API ID tersimpan!\n\n"
        "━━━━━━━━━━━━━━━━━\n"
        "*Langkah 2 dari 5 \u2014 API Hash*\n\n"
        "API Hash adalah kode acak 32 karakter (campuran huruf & angka).\n\n"
        "📌 *Cara mendapatkan API Hash:*\n"
        "Di halaman yang sama (my.telegram.org \u2192 API development tools),\n"
        "salin teks panjang di kolom *App api\_hash*\n\n"
        "Contoh tampilan: `a1b2c3d4e5f6g7h8i9j0...` _(32 karakter)_\n\n"
        "Kirim API Hash kamu di sini:",
        parse_mode="Markdown"
    )
    return API_HASH_STEP


async def setup_api_hash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    text = update.message.text.strip()
    if len(text) < 10:
        await update.message.reply_text(
            "❌ API Hash sepertinya terlalu pendek.\n\n"
            "API Hash seharusnya 32 karakter, campuran huruf dan angka.\n"
            "Pastikan kamu menyalin seluruhnya dari my.telegram.org\n\n"
            "Coba kirim ulang:",
            parse_mode="Markdown"
        )
        return API_HASH_STEP
    temp_store.setdefault(uid, {})["api_hash"] = text
    await update.message.reply_text(
        "✅ API Hash tersimpan!\n\n"
        "━━━━━━━━━━━━━━━━━\n"
        "*Langkah 3 dari 5 \u2014 Nomor HP*\n\n"
        "Masukkan nomor HP yang terdaftar di akun Telegram kamu.\n\n"
        "📌 *Format yang benar:*\n"
        "\u2022 Awali dengan kode negara\n"
        "\u2022 Indonesia: `+628xxxxxxxxxx`\n"
        "\u2022 Contoh: `+6281234567890`\n\n"
        "Kirim nomor HP kamu:",
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
            "📨 Kode OTP berhasil dikirim ke Telegram kamu!\n\n"
            "━━━━━━━━━━━━━━━━━\n"
            "*Langkah 4 dari 5 \u2014 Kode OTP*\n\n"
            "Buka aplikasi Telegram kamu, cari pesan dari *Telegram* "
            "yang berisi 5 digit kode verifikasi.\n\n"
            "📌 *Cara mengirim kode:*\n"
            "Ketik kode dengan *spasi di antara setiap angka*\n\n"
            "Contoh: jika kode kamu `12345`, kirim: `1 2 3 4 5`\n\n"
            "_(Spasi wajib agar Telegram tidak mendeteksi sebagai aktivitas mencurigakan)_",
            parse_mode="Markdown"
        )
        return CODE_STEP
    except Exception as e:
        await client.disconnect()
        temp_store.pop(uid, None)
        await update.message.reply_text(
            f"❌ Gagal mengirim OTP: `{e}`\n\n"
            "Kemungkinan penyebab:\n"
            "\u2022 Nomor HP salah format (harus pakai +62...)\n"
            "\u2022 API ID atau API Hash salah\n\n"
            "Silakan /setup ulang dari awal.",
            parse_mode="Markdown", reply_markup=main_keyboard(uid)
        )
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
            "🔐 *Akun ini mengaktifkan verifikasi 2 langkah (2FA)*\n\n"
            "━━━━━━━━━━━━━━━━━\n"
            "*Langkah 5 dari 5 \u2014 Password 2FA*\n\n"
            "Masukkan password 2FA Telegram kamu.\n\n"
            "📌 Ini adalah password yang kamu buat sendiri di:\n"
            "Telegram \u2192 Pengaturan \u2192 Privasi & Keamanan \u2192 Verifikasi 2 Langkah\n\n"
            "Kirim password kamu:",
            parse_mode="Markdown"
        )
        return PASSWORD_STEP
    except (PhoneCodeInvalidError, PhoneCodeExpiredError):
        await client.disconnect()
        temp_store.pop(uid, None)
        await update.message.reply_text(
            "❌ Kode OTP salah atau sudah kadaluarsa.\n\n"
            "Silakan /setup ulang untuk mendapatkan kode baru.",
            parse_mode="Markdown"
        )
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
        await update.message.reply_text(
            f"❌ Password 2FA salah: `{e}`\nSilakan /setup ulang.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END
    return await _finish_setup(update, uid, data, client)


async def _finish_setup(update, uid, data, client):
    string_session = client.session.save()
    save_user_session(uid, data["api_id"], data["api_hash"], string_session)
    await start_client_for_user(uid, data["api_id"], data["api_hash"], string_session)
    temp_store.pop(uid, None)
    await update.message.reply_text(
        "✅ *Setup berhasil! Session kamu sudah aktif.*\n\n"
        "Gunakan tombol *📖 Cara Penggunaan* di menu utama untuk panduan lengkap fitur VIP.",
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
    elif data == "menu_guide":
        await query.edit_message_text(
            GUIDE_TEXT,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Kembali", callback_data="menu_back")]
            ])
        )
    elif data == "menu_admin":
        if uid != ADMIN_ID:
            return
        await query.edit_message_text(
            "👤 *Menu Admin*\n\nPilih tindakan:",
            parse_mode="Markdown", reply_markup=admin_keyboard()
        )
    elif data == "menu_back":
        waiting_gift.discard(uid)
        waiting_revoke.discard(uid)
        waiting_restore.discard(uid)
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
        await query.edit_message_text(
            f"👋 *Selamat datang di Rams VIP Bot!*\n\nStatus session: {status}{sub_status}\n\nPilih menu di bawah:",
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
            "_(Default 30 hari jika tidak diisi)_\n\n"
            "💡 *Tips:* Gunakan *user\_id* (angka) agar bisa gift tanpa user perlu\n"
            "klik /start dulu. User\_id bisa dari @userinfobot atau @getidsbot.\n\n"
            "_/cancel untuk batal._",
            parse_mode="Markdown"
        )
    elif data == "admin_revoke":
        if uid != ADMIN_ID: return
        waiting_revoke.add(ADMIN_ID)
        await query.edit_message_text(
            "🚫 *Revoke VIP*\n\n"
            "Fitur ini akan *mencabut* langganan VIP milik pengguna.\n"
            "Pengguna tidak akan bisa menggunakan fitur `.dl` dan `.copy` lagi.\n\n"
            "━━━━━━━━━━━━━━━━━\n"
            "*Cara menggunakan:*\n"
            "Kirim user\_id atau @username pengguna yang ingin dicabut VIP-nya.\n\n"
            "Contoh:\n"
            "\u2022 `123456789` _(user\_id, paling disarankan)_\n"
            "\u2022 `@johndoe` _(username, harus sudah pernah /start)_\n\n"
            "💡 *Tips:* Revoke by user\_id selalu berhasil walau user belum /start.\n\n"
            "_/cancel untuk batal._",
            parse_mode="Markdown"
        )


# ── HELPER: SUBSCRIPTION ──────────────────────────────────────────
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


# ── HELPER: BACKUP ────────────────────────────────────────────────
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


# ── MAIN ──────────────────────────────────────────────────────────
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
            CommandHandler("start",  cmd_start),
        ],
        allow_reentry=True,
    )

    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.Document.ALL) & ~filters.COMMAND & filters.User(ADMIN_ID),
            admin_message_handler
        ),
        group=0
    )

    app.add_handler(setup_conv, group=1)
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("revoke", cmd_revoke))
    app.add_handler(CommandHandler("gift",   cmd_gift))
    app.add_handler(CallbackQueryHandler(callback_handler))

    print("🤖 Rams VIP Bot berjalan...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
