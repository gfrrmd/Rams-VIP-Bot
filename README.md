# ⚡ Rams VIP Bot

Telegram VIP Bot dengan fitur download media dari channel restricted, manajemen session Telethon, dan sistem langganan VIP manual via admin.

## ✨ Fitur Utama

- **`.dl`** — Reply pesan lalu ketik `.dl` untuk forward/download media ke Saved Messages
- **`.copy <link>`** — Download/copy konten dari channel restricted (teks, foto, video, dokumen, stiker) ke Saved Messages
- **Sistem VIP Manual** — Member berlangganan dengan menghubungi admin, admin yang mengaktifkan VIP
- **Gift VIP** — Admin bisa gift VIP ke user via ID atau @username
- **Revoke VIP** — Admin bisa mencabut VIP member kapan saja
- **Backup & Restore DB** — Admin bisa backup dan restore database via bot

## 🚀 Cara Deploy (Railway)

1. Fork/clone repo ini
2. Buat project baru di [Railway](https://railway.app)
3. Tambahkan PostgreSQL database
4. Set environment variables:
   - `BOT_TOKEN` — Token bot dari @BotFather
   - `ADMIN_ID` — Telegram user ID admin
   - `DATABASE_URL` — Otomatis dari Railway PostgreSQL
5. Deploy!

## 📋 Perintah Admin

| Perintah | Fungsi |
|---|---|
| `/gift <id/@username> [days]` | Berikan VIP ke user |
| `/revoke <id/@username>` | Cabut VIP member |
| Menu Admin → Gift VIP | Berikan VIP via tombol |
| Menu Admin → Revoke VIP | Cabut VIP via tombol |
| Menu Admin → Backup DB | Backup database |
| Menu Admin → Restore DB | Restore database |

## 📋 Perintah User (Userbot via Telethon)

| Perintah | Fungsi |
|---|---|
| `.dl` | Download/forward media yang di-reply ke Saved Messages |
| `.copy <link>` | Copy konten channel restricted ke Saved Messages |

## ⚙️ Environment Variables

```
BOT_TOKEN=your_bot_token
ADMIN_ID=your_telegram_id
DATABASE_URL=postgresql://...
```

## 📝 Cara Berlangganan VIP

User harus menghubungi admin secara manual. Admin akan mengaktifkan VIP setelah konfirmasi. Tidak ada sistem pembayaran otomatis.

## ⚠️ Catatan

- Fitur `.copy` menggunakan akun Telegram user (Telethon userbot), bukan bot biasa
- Forward diutamakan untuk kecepatan, download dipakai sebagai fallback untuk konten restricted
- Media yang didukung: teks, foto, video, dokumen, stiker (webp/tgs/webm), audio
