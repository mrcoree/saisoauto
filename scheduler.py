import queue
import random
import threading
import logging
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler

from models import (
    get_due_items, get_queue_item, update_status,
    get_schedule_settings, get_today_completed_count,
    db_update_schedule_unsafe, get_user_by_id, get_all_users
)
from scraper import CoupangScraper
from content_generator import ContentGenerator
from image_generator import ImageGenerator
from wordpress_publisher import WordPressPublisher

logger = logging.getLogger(__name__)


class PostScheduler:
    def __init__(self):
        self.scheduler = BackgroundScheduler()
        self._job_id = 'queue_processor'

        # [NEW] 공통 스크래퍼 (브라우저는 봇이 하나만 띄워서 공용으로 씀)
        self._scraper = None

        # 즉시 실행 전용 순차 처리 큐 & 워커 스레드
        self.immediate_queue = queue.Queue()
        self.immediate_worker = threading.Thread(target=self._process_immediate_queue, daemon=True)
        self.immediate_worker.start()

    def add_to_immediate_queue(self, item_id):
        self.immediate_queue.put(item_id)

    def _process_immediate_queue(self):
        """큐에서 하나씩 꺼내 순서대로(직렬) 처리하는 전담 노동자"""
        while True:
            item_id = self.immediate_queue.get()
            try:
                self.process_queue_item(item_id)
            except Exception as e:
                logger.error(f"Error processing immediate item {item_id}: {e}")
            finally:
                self.immediate_queue.task_done()

    @property
    def scraper(self):
        if self._scraper is None:
            self._scraper = CoupangScraper()
        return self._scraper

    # ── 스케줄러 제어 ──

    def start(self):
        self.scheduler.add_job(
            self.check_queue,
            'interval',
            minutes=1,
            id=self._job_id,
            replace_existing=True,
        )
        self.scheduler.start()
        logger.info("PostScheduler 시작됨 (Multi-tenant mode)")

    def stop(self):
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("PostScheduler 중지됨")

    # ── 큐 처리 ──

    def check_queue(self):
        """다중 사용자 전체 순회하며 예정 시간이 지난 항목 처리."""
        user_ids = get_all_users()
        for user_id in user_ids:
            settings = get_schedule_settings(user_id)
            if not settings:
                continue

            due_items = get_due_items(user_id)
            if not due_items:
                continue

            max_per_day = settings.get('max_posts_per_day', 5)
            today_count = get_today_completed_count(user_id)
            remaining = max(0, max_per_day - today_count)

            if remaining <= 0:
                continue

            for item in due_items[:remaining]:
                try:
                    self.process_queue_item(item['id'], user_id, settings=settings)
                except Exception as e:
                    logger.error(f"[User {user_id}] 큐 항목 #{item['id']} 처리 실패: {e}")

    def process_queue_item(self, item_id, user_id=None, settings=None):
        """특정 사용자의 항목 1개 파이프라인 처리."""
        # user_id가 없으면 DB에서 조회
        if not user_id:
            item = get_queue_item(item_id)
            if not item: return
            user_id = item['user_id']
        else:
            item = get_queue_item(item_id, user_id)
            
        if not item:
            logger.error(f"큐 항목 #{item_id} 없음")
            return

        user = get_user_by_id(user_id)
        if not user:
            logger.error(f"User {user_id} 찾을 수 없음")
            return

        update_status(item_id, 'processing')
        logger.info(f"[User {user_id}] 처리 시작: #{item_id} - {item['product_name']}")

        # 사용자별 서비스 객체 초기화
        generator = ContentGenerator(api_key=user['openai_api_key'])
        img_gen = ImageGenerator(api_key=user['gemini_api_key'])
        publisher = WordPressPublisher(
            wp_url=user['wp_url'], 
            wp_username=user['wp_username'], 
            wp_app_password=user['wp_app_password']
        )

        image_paths = []
        try:
            logger.info(f"  스크래핑 중: {item['product_url']}")
            scraped = self.scraper.scrape_product(item['product_url'])

            product_info = {
                'product_name': item['product_name'],
                'price': item['price'],
                'affiliate_url': item['affiliate_url'],
                'product_url': item['product_url'],
                'thumbnail_url': item.get('thumbnail_url', ''),
                'description': scraped.get('description', ''),
                'features': scraped.get('features', []),
                'specs': scraped.get('specs', {}),
                'image_urls': scraped.get('image_urls', []),
            }

            logger.info("  글 생성 중...")
            article = generator.generate_review(product_info)

            logger.info("  이미지 생성 중...")
            image_paths = img_gen.generate_images(product_info, count=3)
            logger.info(f"  이미지 {len(image_paths)}장 생성 완료")

            logger.info("  워드프레스 발행 중...")
            if settings is None:
                settings = get_schedule_settings(user_id)
            publish_status = 'publish' if settings and settings.get('auto_publish') else 'draft'

            # 쿠팡 파트너스 링크 없으면 딥링크 변환 방어 (여긴 URL 그대로 사용)
            result = publisher.publish_article(
                title=article.get('title', item['product_name']),
                content=article.get('content', ''),
                excerpt=article.get('excerpt', ''),
                tags=article.get('tags', []),
                image_paths=image_paths,
                affiliate_url=item.get('affiliate_url', ''),
                status=publish_status,
            )

            # 성공
            now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            db_update_schedule_unsafe(item_id, now_str)
            update_status(item_id, 'completed', post_url=result['post_url'])
            logger.info(f"  완료: {result['post_url']}")

        except Exception as e:
            error_msg = str(e)[:500]
            update_status(item_id, 'failed', error_message=error_msg)
            logger.error(f"  실패: {error_msg}")
            raise
        finally:
            if image_paths:
                ImageGenerator.cleanup_images(image_paths)

    # ── 스케줄 배정 (내부적으로 권한 검증은 app.py에서 마쳤다고 가정) ──

    def schedule_items_immediate(self, item_ids):
        now = datetime.now()
        for i, item_id in enumerate(item_ids):
            scheduled = now + timedelta(minutes=i)
            db_update_schedule_unsafe(item_id, scheduled.strftime('%Y-%m-%d %H:%M:%S'))

    def schedule_items_interval(self, item_ids, start_time=None, interval_hours=6):
        if start_time is None:
            start_time = datetime.now() + timedelta(hours=1)
        for i, item_id in enumerate(item_ids):
            scheduled = start_time + timedelta(hours=interval_hours * i)
            db_update_schedule_unsafe(item_id, scheduled.strftime('%Y-%m-%d %H:%M:%S'))

    def schedule_items_random(self, item_ids, posts_per_day=3, start_hour=8, end_hour=23):
        if not item_ids: return
        if start_hour >= end_hour:
            raise ValueError("start_hour는 end_hour보다 작아야 합니다")

        posts_per_day = max(1, min(posts_per_day, 24))
        total_minutes = (end_hour - start_hour) * 60
        min_gap = 30

        today = datetime.now().date()
        now = datetime.now()
        if now.hour >= end_hour - 1:
            current_date = today + timedelta(days=1)
        else:
            current_date = today

        scheduled_times = []
        items_remaining = list(item_ids)

        while items_remaining:
            day_times = self._generate_random_times(
                current_date, posts_per_day, start_hour, end_hour, total_minutes, min_gap,
            )
            if current_date == today:
                day_times = [t for t in day_times if t > now + timedelta(minutes=2)]

            slots = min(len(items_remaining), len(day_times))
            for i in range(slots):
                scheduled_times.append((items_remaining[i], day_times[i]))

            items_remaining = items_remaining[slots:]
            current_date += timedelta(days=1)

        for item_id, scheduled_at in scheduled_times:
            db_update_schedule_unsafe(item_id, scheduled_at.strftime('%Y-%m-%d %H:%M:%S'))

    def _generate_random_times(self, date, count, start_hour, end_hour, total_minutes, min_gap):
        if count <= 0: return []
        base = datetime(date.year, date.month, date.day, start_hour, 0)
        end_limit = datetime(date.year, date.month, date.day, end_hour, 0)

        slot_size = total_minutes / count
        if slot_size < min_gap:
            count = max(1, total_minutes // min_gap)
            slot_size = total_minutes / count

        times = []
        for i in range(count):
            slot_start = slot_size * i
            slot_end = slot_size * (i + 1)
            margin = slot_size * 0.15
            pick_start = slot_start + margin
            pick_end = slot_end - margin

            if pick_start >= pick_end:
                pick_start = slot_start
                pick_end = slot_end

            random_minute = random.uniform(pick_start, pick_end)
            random_minute = max(0, min(total_minutes - 1, random_minute))
            t = base + timedelta(minutes=round(random_minute))
            times.append(t)

        for i in range(1, len(times)):
            diff = (times[i] - times[i - 1]).total_seconds() / 60
            if diff < min_gap:
                adjusted = times[i - 1] + timedelta(minutes=min_gap)
                times[i] = min(adjusted, end_limit)

        seen = set()
        unique_times = []
        for t in times:
            key = t.strftime('%Y-%m-%d %H:%M')
            if key not in seen:
                seen.add(key)
                unique_times.append(t)

        return unique_times
