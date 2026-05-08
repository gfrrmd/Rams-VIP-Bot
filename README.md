# ⚡ Rams VIP Bot

> Telegram VIP Bot yang menggabungkan **bot biasa** (python-telegram-bot) dan **userbot** (Telethon) untuk mengunduh konten dari channel restricted, lengkap dengan sistem langganan VIP manual via admin.

---

## ✨ Fitur

### 🤖 Bot (Admin)
- **Manajemen VIP** — Aktifkan, gift, atau cabut akses VIP member
- **Backup & Restore DB** — Kirim dan pulihkan database langsung via bot
- **Sistem Session** — Simpan & kelola session Telethon per user di database

### 👤 Userbot (Member VIP)
- **`.dl`** — Reply pesan → forward/download media ke Saved Messages
- **`.copy <link>`** — Salin konten dari channel restricted (teks, foto, video, dokumen, stiker) ke Saved Messages

### 🗃️ Database (PostgreSQL)
- Menyimpan data member VIP, session Telethon, dan log aktivitas
- Support backup & restore via file `.sql`

---

## 🛠️ Tech Stack

| Teknologi | Versi | Kegunaan |
|---|---|---|
| `python-telegram-bot` | 20.7 | Bot Telegram utama |
| `Telethon` | 1.36.0 | Userbot untuk channel restricted |
| `psycopg2-binary` | 2.9.9 | Koneksi ke PostgreSQL |
| PostgreSQL | — | Database utama |
| Railway | — | Hosting & deployment |

---

## 🚀 Deploy ke Railway

1. **Fork** repo ini ke akun GitHub kamu
2. Buat project baru di [Railway](https://railway.app)
3. Tambahkan **PostgreSQL** sebagai plugin database
4. Set **environment variables** berikut:

```env
BOT_TOKEN=your_bot_token_from_botfather
ADMIN_ID=your_telegram_user_id
DATABASE_URL=postgresql://...  # otomatis dari Railway PostgreSQL
```

5. Railway akan otomatis mendeteksi `Procfile` dan langsung deploy

---

## ⚙️ Environment Variables

| Variable | Deskripsi |
|---|---|
| `BOT_TOKEN` | Token bot dari [@BotFather](https://t.me/BotFather) |
| `ADMIN_ID` | Telegram User ID admin (angka) |
| `DATABASE_URL` | Connection string PostgreSQL (auto dari Railway) |

---

## 📋 Perintah Admin

| Perintah | Fungsi |
|---|---|
| `/gift <id/@username> [days]` | Berikan VIP ke user (default 30 hari) |
| `/revoke <id/@username>` | Cabut akses VIP member |
| Menu → Gift VIP | Gift VIP via inline button |
| Menu → Revoke VIP | Cabut VIP via inline button |
| Menu → Backup DB | Unduh backup database |
| Menu → Restore DB | Pulihkan database dari file |

## 📋 Perintah User VIP (Userbot)

| Perintah | Fungsi |
|---|---|
| `.dl` | Download/forward media yang di-reply ke Saved Messages |
| `.copy <link>` | Salin konten channel restricted ke Saved Messages |

---

## 📝 Alur Berlangganan VIP

```
User → Hubungi Admin → Admin aktifkan VIP → User login session → Fitur aktif
```

Tidak ada sistem pembayaran otomatis. Admin mengonfirmasi dan mengaktifkan VIP secara manual.

---

## ⚠️ Catatan Penting

- Fitur `.copy` menggunakan **akun Telegram asli** (Telethon userbot), bukan bot
- Forward diutamakan untuk kecepatan; download dipakai sebagai fallback untuk konten restricted
- Media yang didukung: **teks, foto, video, dokumen, stiker** (`.webp` / `.tgs` / `.webm`), audio
- Jangan bagikan `session string` ke siapapun — ini setara akses penuh ke akun Telegram

---

## 📁 Struktur File

```
Rams-VIP-Bot/
├── main.py          # Logic utama bot & userbot
├── database.py      # Fungsi database (PostgreSQL)
├── requirements.txt # Dependencies Python
├── Procfile         # Entry point Railway
├── railway.json     # Konfigurasi Railway
└── runtime.txt      # Versi Python
```

---

<div align="center">
  Made with ❤️ by <a href="https://github.com/gfrrmd">gfrrmd</a>
</div>
