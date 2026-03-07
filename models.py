import sqlite3
import os
from datetime import datetime
from contextlib import contextmanager
from werkzeug.security import generate_password_hash, check_password_hash
from cryptography.fernet import Fernet, InvalidToken

from config import Config

DB_PATH = Config.DB_PATH

# ── Fernet AES-256 암호화 헬퍼 ──

_fernet = None

def _get_fernet():
    """Fernet 인스턴스 반환 (지연 로딩)."""
    global _fernet
    if _fernet is None and Config.FERNET_KEY:
        _fernet = Fernet(Config.FERNET_KEY.encode())
    return _fernet

# API 키 컨럼명 목록 (이 컨럼만 암호화 대상)
_API_KEY_COLUMNS = {
    'email', 'nickname',   # 개인정보 비식별 처리
    'coupang_access_key', 'coupang_secret_key',
    'openai_api_key', 'gemini_api_key',
    'wp_url', 'wp_username', 'wp_app_password',
}

def _encrypt(value: str) -> str:
    """API 키 등 민감 정보를 암호화 (Fernet 콌 없으면 평문 그대로)."""
    fernet = _get_fernet()
    if not fernet or not value:
        return value
    return fernet.encrypt(value.encode()).decode()

def _decrypt(value: str) -> str:
    """DB에서 라은 암호문을 복호화 (평문이난 눁비 프리피켜시도 허용)."""
    fernet = _get_fernet()
    if not fernet or not value:
        return value
    try:
        return fernet.decrypt(value.encode()).decode()
    except (InvalidToken, Exception):
        # 이미 평문으로 저장된 값은 그대로 반환 (Migration Safe)
        return value

def _decrypt_user_row(user: dict) -> dict:
    """user dict에서 API 키 컨럼들만 교체 복호화."""
    if not user:
        return user
    result = dict(user)
    for col in _API_KEY_COLUMNS:
        if col in result and result[col]:
            result[col] = _decrypt(result[col])
    return result

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
                nickname TEXT,
                email TEXT,
                is_admin INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending',
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

def create_user(username, password, is_admin=0, nickname=None, email=None, status='pending'):
    with get_db() as conn:
        try:
            conn.execute(
                'INSERT INTO users (username, password_hash, is_admin, nickname, email, status) VALUES (?, ?, ?, ?, ?, ?)',
                (username, generate_password_hash(password), is_admin, nickname, email, status)
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

def get_pending_users():
    """status='pending'인 가입 신청 목록."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, username, nickname, email, created_at FROM users WHERE status = 'pending' ORDER BY created_at ASC"
        ).fetchall()
        return [_decrypt_user_row(dict(r)) for r in rows]

def set_user_status(user_id, status):
    """user_id의 status를 'active' 또는 'rejected'로 변경."""
    with get_db() as conn:
        conn.execute('UPDATE users SET status = ? WHERE id = ?', (status, user_id))
        conn.commit()

def delete_user(user_id):
    """user_id에 해당하는 사용자와 모든 관련 데이터를 DB에서 완전히 삭제 (Hard Delete)."""
    with get_db() as conn:
        conn.execute('DELETE FROM product_queue WHERE user_id = ?', (user_id,))
        conn.execute('DELETE FROM schedule_settings WHERE user_id = ?', (user_id,))
        conn.execute('DELETE FROM users WHERE id = ?', (user_id,))
        conn.commit()

def get_user_count():
    """DB에 저장된 전체 사용자 수."""
    with get_db() as conn:
        return conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]

def get_admin_users():
    """is_admin=1인 관리자 목록."""
    with get_db() as conn:
        rows = conn.execute('SELECT * FROM users WHERE is_admin = 1').fetchall()
        return [_decrypt_user_row(dict(r)) for r in rows]

def get_user_by_username(username):
    with get_db() as conn:
        row = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        return _decrypt_user_row(dict(row)) if row else None

def get_user_by_id(user_id):
    with get_db() as conn:
        row = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
        return _decrypt_user_row(dict(row)) if row else None

def update_user_keys(user_id, keys_dict):
    with get_db() as conn:
        updates = []
        values = []
        for col, val in keys_dict.items():
            updates.append(f"{col} = ?")
            # API 키 컨럼은 저장 전 암호화
            if col in _API_KEY_COLUMNS:
                values.append(_encrypt(val))
            else:
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
