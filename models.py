import sqlite3
import os
from datetime import datetime
from contextlib import contextmanager
from werkzeug.security import generate_password_hash, check_password_hash

from config import Config

DB_PATH = Config.DB_PATH

@contextmanager
def get_db():
    """DB 연결 반환 (context manager — 예외 시에도 자동 close)."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    """DB 초기화 + 테이블 생성."""
    with get_db() as conn:
        cursor = conn.cursor()

        # users 테이블
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                coupang_access_key TEXT,
                coupang_secret_key TEXT,
                openai_api_key TEXT,
                gemini_api_key TEXT,
                wp_url TEXT,
                wp_username TEXT,
                wp_app_password TEXT,
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS product_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                product_name TEXT NOT NULL,
                product_url TEXT NOT NULL,
                affiliate_url TEXT NOT NULL,
                price TEXT,
                thumbnail_url TEXT,
                scheduled_at TEXT,
                status TEXT DEFAULT 'pending',
                post_url TEXT,
                error_message TEXT,
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                updated_at TEXT DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS schedule_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL UNIQUE,
                mode TEXT DEFAULT 'interval',
                interval_hours INTEGER DEFAULT 6,
                daily_times TEXT DEFAULT '09:00,15:00,21:00',
                max_posts_per_day INTEGER DEFAULT 5,
                auto_publish INTEGER DEFAULT 0,
                updated_at TEXT DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        ''')

        conn.commit()

# ── Users CRUD ──

def create_user(username, password):
    with get_db() as conn:
        try:
            conn.execute(
                'INSERT INTO users (username, password_hash) VALUES (?, ?)',
                (username, generate_password_hash(password))
            )
            user_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            # 기본 스케줄 설정도 함께 생성
            conn.execute('''
                INSERT INTO schedule_settings (user_id, mode, interval_hours, daily_times, max_posts_per_day, auto_publish)
                VALUES (?, 'interval', 6, '09:00,15:00,21:00', 5, 0)
            ''', (user_id,))
            conn.commit()
            return user_id, None
        except sqlite3.IntegrityError:
            return None, "이미 존재하는 아이디입니다."

def get_user_by_username(username):
    with get_db() as conn:
        row = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        return dict(row) if row else None

def get_user_by_id(user_id):
    with get_db() as conn:
        row = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
        return dict(row) if row else None

def update_user_keys(user_id, keys_dict):
    with get_db() as conn:
        updates = []
        values = []
        for col, val in keys_dict.items():
            updates.append(f"{col} = ?")
            values.append(val)
        if not updates:
            return

        values.append(user_id)
        sql = f"UPDATE users SET {', '.join(updates)} WHERE id = ?"
        conn.execute(sql, values)
        conn.commit()

def get_all_users():
    """모든 사용자 ID 반환 (스케줄러용)."""
    with get_db() as conn:
        rows = conn.execute('SELECT id FROM users').fetchall()
        return [r['id'] for r in rows]

# ── product_queue CRUD ──

def add_to_queue(user_id, products):
    """상품 목록을 대기열에 추가. 중복(pending/processing) 시 기존 ID 반환."""
    with get_db() as conn:
        cursor = conn.cursor()
        added = []
        for p in products:
            existing = cursor.execute('''
                SELECT id FROM product_queue
                WHERE user_id = ? AND product_url = ? AND status IN ('pending', 'processing')
                LIMIT 1
            ''', (user_id, p['product_url'])).fetchone()

            if existing:
                added.append(existing[0])
                continue

            cursor.execute('''
                INSERT INTO product_queue (user_id, product_name, product_url, affiliate_url, price, thumbnail_url, scheduled_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                user_id,
                p['product_name'],
                p['product_url'],
                p['affiliate_url'],
                p.get('price', ''),
                p.get('thumbnail_url', ''),
                p.get('scheduled_at'),
            ))
            added.append(cursor.lastrowid)
        conn.commit()
        return added

def get_all_queue(user_id):
    """특정 사용자의 전체 대기열 조회."""
    with get_db() as conn:
        rows = conn.execute(
            'SELECT * FROM product_queue WHERE user_id = ? ORDER BY created_at DESC', (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]

def get_pending(user_id):
    """특정 사용자의 처리 대기 항목."""
    with get_db() as conn:
        rows = conn.execute('''
            SELECT * FROM product_queue
            WHERE user_id = ? AND status = 'pending'
            ORDER BY scheduled_at ASC, created_at ASC
        ''', (user_id,)).fetchall()
        return [dict(r) for r in rows]

def get_due_items(user_id):
    """현재 시각 이전에 예정된 pending 항목."""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with get_db() as conn:
        rows = conn.execute('''
            SELECT * FROM product_queue
            WHERE user_id = ?
              AND status = 'pending'
              AND scheduled_at IS NOT NULL
              AND scheduled_at <= ?
            ORDER BY scheduled_at ASC, created_at ASC
        ''', (user_id, now)).fetchall()
        return [dict(r) for r in rows]

def get_queue_item(item_id, user_id=None):
    """단일 대기열 항목 조회 (user_id가 주어지면 소유권 검증)."""
    with get_db() as conn:
        if user_id:
            row = conn.execute(
                'SELECT * FROM product_queue WHERE id = ? AND user_id = ?', (item_id, user_id)
            ).fetchone()
        else:
            row = conn.execute(
                'SELECT * FROM product_queue WHERE id = ?', (item_id,)
            ).fetchone()
        return dict(row) if row else None

def update_status(item_id, status, post_url=None, error_message=None):
    """대기열 항목 상태 업데이트."""
    with get_db() as conn:
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        conn.execute('''
            UPDATE product_queue
            SET status = ?, post_url = ?, error_message = ?, updated_at = ?
            WHERE id = ?
        ''', (status, post_url, error_message, now, item_id))
        conn.commit()

def update_schedule(item_id, scheduled_at, user_id):
    """대기열 항목의 예정 시각 변경."""
    with get_db() as conn:
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        conn.execute('''
            UPDATE product_queue
            SET scheduled_at = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
        ''', (scheduled_at, now, item_id, user_id))
        conn.commit()

def db_update_schedule_unsafe(item_id, scheduled_at):
    """스케줄러 내부 호출용 (auth 없이 상태 업데이트)."""
    with get_db() as conn:
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        conn.execute('''
            UPDATE product_queue
            SET scheduled_at = ?, updated_at = ?
            WHERE id = ?
        ''', (scheduled_at, now, item_id))
        conn.commit()

def delete_queue_item(item_id, user_id):
    """대기열 항목 삭제."""
    with get_db() as conn:
        conn.execute('DELETE FROM product_queue WHERE id = ? AND user_id = ?', (item_id, user_id))
        conn.commit()

def reset_item(item_id, user_id):
    """실패 항목을 다시 pending으로 리셋."""
    with get_db() as conn:
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        conn.execute('''
            UPDATE product_queue
            SET status = 'pending', error_message = NULL, updated_at = ?
            WHERE id = ? AND user_id = ?
        ''', (now, item_id, user_id))
        conn.commit()

# ── schedule_settings CRUD ──

def get_schedule_settings(user_id):
    """특정 유저 스케줄 설정 조회."""
    with get_db() as conn:
        row = conn.execute('SELECT * FROM schedule_settings WHERE user_id = ?', (user_id,)).fetchone()
        return dict(row) if row else None

def update_schedule_settings(user_id, mode, interval_hours, daily_times, max_posts_per_day, auto_publish):
    """특정 유저 스케줄 설정 업데이트."""
    with get_db() as conn:
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        conn.execute('''
            UPDATE schedule_settings
            SET mode = ?, interval_hours = ?, daily_times = ?,
                max_posts_per_day = ?, auto_publish = ?, updated_at = ?
            WHERE user_id = ?
        ''', (mode, interval_hours, daily_times, max_posts_per_day, auto_publish, now, user_id))
        conn.commit()

# ── 통계 ──

def get_today_completed_count(user_id):
    """특정 유저의 오늘 completed 항목 수."""
    today = datetime.now().strftime('%Y-%m-%d')
    with get_db() as conn:
        row = conn.execute('''
            SELECT COUNT(*) FROM product_queue
            WHERE user_id = ? AND status = 'completed'
              AND updated_at >= ? || ' 00:00:00'
              AND updated_at <= ? || ' 23:59:59'
        ''', (user_id, today, today)).fetchone()
        return row[0] if row else 0

def get_queue_stats(user_id):
    """특정 유저의 대기열 통계."""
    with get_db() as conn:
        stats = {}
        for status in ['pending', 'processing', 'completed', 'failed']:
            row = conn.execute(
                'SELECT COUNT(*) FROM product_queue WHERE user_id = ? AND status = ?', (user_id, status)
            ).fetchone()
            stats[status] = row[0]
        
        row_total = conn.execute('SELECT COUNT(*) FROM product_queue WHERE user_id = ?', (user_id,)).fetchone()
        stats['total'] = row_total[0]
        return stats
