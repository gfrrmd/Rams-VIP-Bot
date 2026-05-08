import os
from datetime import datetime, timedelta
import psycopg2

DATABASE_URL = os.environ["DATABASE_URL"]


def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def init_db():
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id    BIGINT PRIMARY KEY,
            username   TEXT,
            full_name  TEXT,
            created_at TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            user_id        BIGINT PRIMARY KEY,
            api_id         BIGINT NOT NULL,
            api_hash       TEXT NOT NULL,
            string_session TEXT NOT NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id    BIGINT PRIMARY KEY,
            plan       TEXT DEFAULT 'vip',
            paid_at    TEXT,
            expired_at TEXT,
            is_active  INTEGER DEFAULT 1
        )
    """)

    conn.commit()
    conn.close()


# ── USER HELPERS ──────────────────────────────────────────────────
def upsert_user(user_id, username, full_name):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO users (user_id, username, full_name, created_at)
        VALUES (%s,%s,%s,%s)
        ON CONFLICT(user_id) DO UPDATE SET
            username=EXCLUDED.username,
            full_name=EXCLUDED.full_name
    """, (user_id, username, full_name, datetime.now().isoformat()))
    conn.commit()
    conn.close()


# ── SESSION HELPERS ──────────────────────────────────────────────
def save_user_session(user_id, api_id, api_hash, string_session):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO sessions (user_id, api_id, api_hash, string_session)
        VALUES (%s,%s,%s,%s)
        ON CONFLICT(user_id) DO UPDATE SET
            api_id=EXCLUDED.api_id,
            api_hash=EXCLUDED.api_hash,
            string_session=EXCLUDED.string_session
    """, (user_id, api_id, api_hash, string_session))
    conn.commit()
    conn.close()


def get_user_session(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "SELECT api_id, api_hash, string_session FROM sessions WHERE user_id=%s",
        (user_id,)
    )
    row = c.fetchone()
    conn.close()
    if row:
        return {"api_id": row[0], "api_hash": row[1], "string_session": row[2]}
    return None


def delete_user_session(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM sessions WHERE user_id=%s", (user_id,))
    conn.commit()
    conn.close()


# ── SUBSCRIPTION HELPERS ─────────────────────────────────────────
def is_subscribed(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT expired_at FROM subscriptions
        WHERE user_id=%s AND is_active=1
    """, (user_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return False
    return datetime.now() < datetime.fromisoformat(row[0])


def get_subscription_info(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "SELECT paid_at, expired_at, is_active FROM subscriptions WHERE user_id=%s",
        (user_id,)
    )
    row = c.fetchone()
    conn.close()
    return row


def activate_subscription(user_id, days=30):
    now     = datetime.now()
    expired = now + timedelta(days=days)
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO subscriptions (user_id, plan, paid_at, expired_at, is_active)
        VALUES (%s,'vip',%s,%s,1)
        ON CONFLICT(user_id) DO UPDATE SET
            paid_at=EXCLUDED.paid_at,
            expired_at=EXCLUDED.expired_at,
            is_active=1
    """, (user_id, now.isoformat(), expired.isoformat()))
    conn.commit()
    conn.close()
    return expired


def revoke_subscription(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE subscriptions SET is_active=0 WHERE user_id=%s", (user_id,))
    conn.commit()
    conn.close()


def get_user_by_username(username):
    """Cari user_id berdasarkan username (tanpa @)."""
    conn = get_conn()
    c = conn.cursor()
    username_clean = username.lstrip("@").lower()
    c.execute(
        "SELECT user_id FROM users WHERE LOWER(username)=%s",
        (username_clean,)
    )
    row = c.fetchone()
    conn.close()
    return row[0] if row else None
