import sqlite3
import os
from datetime import datetime
from contextlib import contextmanager

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

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS product_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
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
                updated_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS schedule_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mode TEXT DEFAULT 'interval',
                interval_hours INTEGER DEFAULT 6,
                daily_times TEXT DEFAULT '09:00,15:00,21:00',
                max_posts_per_day INTEGER DEFAULT 5,
                auto_publish INTEGER DEFAULT 0,
                updated_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        ''')

        # 기본 스케줄 설정이 없으면 생성
        cursor.execute('SELECT COUNT(*) FROM schedule_settings')
        if cursor.fetchone()[0] == 0:
            cursor.execute('''
                INSERT INTO schedule_settings (mode, interval_hours, daily_times, max_posts_per_day, auto_publish)
                VALUES ('interval', 6, '09:00,15:00,21:00', 5, 0)
            ''')

        conn.commit()


# ── product_queue CRUD ──

def add_to_queue(products):
    """상품 목록을 대기열에 추가. 중복(pending/processing) 시 기존 ID 반환."""
    with get_db() as conn:
        cursor = conn.cursor()
        added = []
        for p in products:
            # 중복 체크: 같은 product_url이 pending/processing 상태로 존재하면 기존 ID 반환
            existing = cursor.execute('''
                SELECT id FROM product_queue
                WHERE product_url = ? AND status IN ('pending', 'processing')
                LIMIT 1
            ''', (p['product_url'],)).fetchone()

            if existing:
                added.append(existing[0])
                continue

            cursor.execute('''
                INSERT INTO product_queue (product_name, product_url, affiliate_url, price, thumbnail_url, scheduled_at)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
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


def get_all_queue():
    """전체 대기열 조회 (최신순)."""
    with get_db() as conn:
        rows = conn.execute(
            'SELECT * FROM product_queue ORDER BY created_at DESC'
        ).fetchall()
        return [dict(r) for r in rows]


def get_pending():
    """처리 대기 중인 항목 (scheduled_at 순)."""
    with get_db() as conn:
        rows = conn.execute('''
            SELECT * FROM product_queue
            WHERE status = 'pending'
            ORDER BY scheduled_at ASC, created_at ASC
        ''').fetchall()
        return [dict(r) for r in rows]


def get_due_items():
    """현재 시각 이전에 예정된 pending 항목. scheduled_at이 없으면 자동 발행 대상 아님."""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with get_db() as conn:
        rows = conn.execute('''
            SELECT * FROM product_queue
            WHERE status = 'pending'
              AND scheduled_at IS NOT NULL
              AND scheduled_at <= ?
            ORDER BY scheduled_at ASC, created_at ASC
        ''', (now,)).fetchall()
        return [dict(r) for r in rows]


def get_queue_item(item_id):
    """단일 대기열 항목 조회."""
    with get_db() as conn:
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


def update_schedule(item_id, scheduled_at):
    """대기열 항목의 예정 시각 변경."""
    with get_db() as conn:
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        conn.execute('''
            UPDATE product_queue
            SET scheduled_at = ?, updated_at = ?
            WHERE id = ?
        ''', (scheduled_at, now, item_id))
        conn.commit()


def delete_queue_item(item_id):
    """대기열 항목 삭제."""
    with get_db() as conn:
        conn.execute('DELETE FROM product_queue WHERE id = ?', (item_id,))
        conn.commit()


def reset_item(item_id):
    """실패 항목을 다시 pending으로 리셋."""
    with get_db() as conn:
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        conn.execute('''
            UPDATE product_queue
            SET status = 'pending', error_message = NULL, updated_at = ?
            WHERE id = ?
        ''', (now, item_id))
        conn.commit()


# ── schedule_settings CRUD ──

def get_schedule_settings():
    """스케줄 설정 조회."""
    with get_db() as conn:
        row = conn.execute('SELECT * FROM schedule_settings ORDER BY id LIMIT 1').fetchone()
        return dict(row) if row else None


def update_schedule_settings(mode, interval_hours, daily_times, max_posts_per_day, auto_publish):
    """스케줄 설정 업데이트."""
    with get_db() as conn:
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        conn.execute('''
            UPDATE schedule_settings
            SET mode = ?, interval_hours = ?, daily_times = ?,
                max_posts_per_day = ?, auto_publish = ?, updated_at = ?
            WHERE id = 1
        ''', (mode, interval_hours, daily_times, max_posts_per_day, auto_publish, now))
        conn.commit()


# ── 통계 ──

def get_today_completed_count():
    """오늘 completed 상태로 변경된 항목 수."""
    today = datetime.now().strftime('%Y-%m-%d')
    with get_db() as conn:
        row = conn.execute('''
            SELECT COUNT(*) FROM product_queue
            WHERE status = 'completed'
              AND updated_at >= ? || ' 00:00:00'
              AND updated_at <= ? || ' 23:59:59'
        ''', (today, today)).fetchone()
        return row[0] if row else 0


def get_queue_stats():
    """대기열 통계."""
    with get_db() as conn:
        stats = {}
        for status in ['pending', 'processing', 'completed', 'failed']:
            row = conn.execute(
                'SELECT COUNT(*) FROM product_queue WHERE status = ?', (status,)
            ).fetchone()
            stats[status] = row[0]
        stats['total'] = sum(stats.values())
        return stats
