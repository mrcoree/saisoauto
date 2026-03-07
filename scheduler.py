import queue
import random
import threading
import logging
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler

from models import (
    get_due_items, get_queue_item, update_status,
    get_schedule_settings, get_today_completed_count,
    update_schedule as db_update_schedule,
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
        # 파이프라인 컴포넌트 (lazy 초기화)
        self._scraper = None
        self._generator = None
        self._img_gen = None
        self._publisher = None

        # [NEW] 즉시 실행 전용 순차 처리 큐 & 워커 스레드
        self.immediate_queue = queue.Queue()
        self.immediate_worker = threading.Thread(target=self._process_immediate_queue, daemon=True)
        self.immediate_worker.start()

    # [NEW] 즉시 실행 큐에 아이템 밀어넣기
    def add_to_immediate_queue(self, item_id):
        self.immediate_queue.put(item_id)

    # [NEW] 큐에서 하나씩 꺼내 순서대로(직렬) 처리하는 전담 노동자
    def _process_immediate_queue(self):
        while True:
            item_id = self.immediate_queue.get()
            try:
                self.process_queue_item(item_id)
            except Exception as e:
                logger.error(f"Error processing immediate item {item_id}: {e}")
            finally:
                self.immediate_queue.task_done()

    # ── lazy 프로퍼티 ──

    @property
    def scraper(self):
        if self._scraper is None:
            self._scraper = CoupangScraper()
        return self._scraper

    @property
    def generator(self):
        if self._generator is None:
            self._generator = ContentGenerator()
        return self._generator

    @property
    def img_gen(self):
        if self._img_gen is None:
            self._img_gen = ImageGenerator()
        return self._img_gen

    @property
    def publisher(self):
        if self._publisher is None:
            self._publisher = WordPressPublisher()
        return self._publisher

    # ── 스케줄러 제어 ──

    def start(self):
        """스케줄러 시작."""
        # 매분 큐 확인
        self.scheduler.add_job(
            self.check_queue,
            'interval',
            minutes=1,
            id=self._job_id,
            replace_existing=True,
        )
        self.scheduler.start()
        logger.info("PostScheduler 시작됨")

    def stop(self):
        """스케줄러 중지."""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("PostScheduler 중지됨")

    # ── 큐 처리 ──

    def check_queue(self):
        """예정 시간이 지난 pending 항목을 처리."""
        settings = get_schedule_settings()
        if not settings or not settings.get('auto_publish'):
            return

        due_items = get_due_items()
        max_per_day = settings.get('max_posts_per_day', 5)

        # 오늘 이미 발행된 건수를 차감하여 남은 쿼터 계산
        today_count = get_today_completed_count()
        remaining = max(0, max_per_day - today_count)

        if remaining <= 0:
            logger.debug("오늘 일일 발행 한도 도달, 스킵")
            return

        for item in due_items[:remaining]:
            try:
                self.process_queue_item(item['id'], settings=settings)
            except Exception as e:
                logger.error(f"큐 항목 #{item['id']} 처리 실패: {e}")

    def process_queue_item(self, item_id, settings=None):
        """단일 상품 전체 파이프라인 실행.

        1. 스크래핑 → 2. 글 생성 → 3. 이미지 생성 → 4. WP 발행

        Args:
            item_id: 큐 항목 ID
            settings: 스케줄 설정 (None이면 내부에서 조회).
                      check_queue에서 호출 시 전달받아 중복 DB 조회 방지.

        Raises:
            Exception: 파이프라인 실패 시 예외 재전파.
                       check_queue 내에서는 catch되어 다음 항목으로 넘어감.
                       외부에서 직접 호출 시 호출자가 처리해야 함.
        """
        item = get_queue_item(item_id)
        if not item:
            logger.error(f"큐 항목 #{item_id} 없음")
            return

        update_status(item_id, 'processing')
        logger.info(f"처리 시작: #{item_id} - {item['product_name']}")

        image_paths = []  # finally에서 정리할 수 있도록 미리 선언
        try:
            # 1. 쿠팡 제품 페이지 스크래핑
            logger.info(f"  스크래핑 중: {item['product_url']}")
            scraped = self.scraper.scrape_product(item['product_url'])

            # 스크래핑 데이터 + 기본 정보 합치기
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

            # 2. OpenAI로 리뷰 글 생성
            logger.info("  글 생성 중...")
            article = self.generator.generate_review(product_info)

            # 3. 이미지 생성
            logger.info("  이미지 생성 중...")
            image_paths = self.img_gen.generate_images(product_info, count=3)
            logger.info(f"  이미지 {len(image_paths)}장 생성 완료")

            # 4. 워드프레스 발행
            logger.info("  워드프레스 발행 중...")
            if settings is None:
                settings = get_schedule_settings()
            publish_status = 'publish' if settings and settings.get('auto_publish') else 'draft'

            result = self.publisher.publish_article(
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
            db_update_schedule(item_id, now_str)
            update_status(item_id, 'completed', post_url=result['post_url'])
            logger.info(f"  완료: {result['post_url']}")

        except Exception as e:
            error_msg = str(e)[:500]
            update_status(item_id, 'failed', error_message=error_msg)
            logger.error(f"  실패: {error_msg}")
            raise
        finally:
            # [FIX #12] 임시 이미지 파일 정리
            if image_paths:
                ImageGenerator.cleanup_images(image_paths)

    # ── 스케줄 배정 ──

    def schedule_items_immediate(self, item_ids):
        """항목들을 즉시 발행 (scheduled_at을 현재 시각으로 설정)."""
        now = datetime.now()
        for i, item_id in enumerate(item_ids):
            # 여러 개일 때 1분 간격으로 순차 배정 (동시 처리 방지)
            scheduled = now + timedelta(minutes=i)
            db_update_schedule(item_id, scheduled.strftime('%Y-%m-%d %H:%M:%S'))

        logger.info(f"즉시발행 스케줄: {len(item_ids)}개 항목")

    def schedule_items_interval(self, item_ids, start_time=None, interval_hours=6):
        """항목들에 고정 간격으로 스케줄 배정.

        Args:
            item_ids: 대기열 항목 ID 목록
            start_time: 시작 시각 (None이면 현재+1시간)
            interval_hours: 항목 간 간격 (시간)
        """
        if start_time is None:
            start_time = datetime.now() + timedelta(hours=1)

        for i, item_id in enumerate(item_ids):
            scheduled = start_time + timedelta(hours=interval_hours * i)
            db_update_schedule(item_id, scheduled.strftime('%Y-%m-%d %H:%M:%S'))

    def schedule_items_random(self, item_ids, posts_per_day=3,
                              start_hour=8, end_hour=23):
        """항목들을 하루 단위로 랜덤 시각에 배정.

        자연스러운 간격으로 배분:
        - 하루 활동 시간을 posts_per_day 구간으로 나눔
        - 각 구간 내에서 랜덤 시각 선택
        - 최소 30분 간격 보장
        - 구간 내 위치에 +-20% 지터 추가

        Args:
            item_ids: 대기열 항목 ID 목록
            posts_per_day: 하루 발행 횟수
            start_hour: 발행 시작 시각 (기본 08시)
            end_hour: 발행 종료 시각 (기본 23시)

        Raises:
            ValueError: start_hour >= end_hour인 경우
        """
        if not item_ids:
            return

        if start_hour >= end_hour:
            raise ValueError(
                f"start_hour({start_hour})는 end_hour({end_hour})보다 작아야 합니다"
            )

        posts_per_day = max(1, min(posts_per_day, 24))
        total_minutes = (end_hour - start_hour) * 60  # 활동 시간 (분)
        min_gap = 30  # 최소 간격 (분)

        today = datetime.now().date()
        # 오늘 남은 시간이 부족하면 내일부터 시작
        now = datetime.now()
        if now.hour >= end_hour - 1:
            current_date = today + timedelta(days=1)
        else:
            current_date = today

        scheduled_times = []
        items_remaining = list(item_ids)

        while items_remaining:
            day_times = self._generate_random_times(
                current_date, posts_per_day, start_hour, end_hour,
                total_minutes, min_gap,
            )

            # 오늘 날짜면 현재 시각 이전 슬롯 제거
            if current_date == today:
                day_times = [t for t in day_times if t > now + timedelta(minutes=2)]

            # 이번 날에 배정할 항목 수
            slots = min(len(items_remaining), len(day_times))
            for i in range(slots):
                scheduled_times.append((items_remaining[i], day_times[i]))

            items_remaining = items_remaining[slots:]
            current_date += timedelta(days=1)

        for item_id, scheduled_at in scheduled_times:
            db_update_schedule(item_id, scheduled_at.strftime('%Y-%m-%d %H:%M:%S'))

        logger.info(
            f"랜덤발행 스케줄: {len(item_ids)}개 항목, "
            f"{posts_per_day}개/일, "
            f"{start_hour}시~{end_hour}시"
        )

    def _generate_random_times(self, date, count, start_hour, end_hour,
                               total_minutes, min_gap):
        """하루 내 자연스러운 랜덤 시각 목록 생성.

        균등 구간 분할 + 지터 방식으로 간격이 너무 균일하지도,
        너무 몰리지도 않게 배분.
        """
        if count <= 0:
            return []

        base = datetime(date.year, date.month, date.day, start_hour, 0)
        end_limit = datetime(date.year, date.month, date.day, end_hour, 0)

        # 구간 크기 (분)
        slot_size = total_minutes / count

        # 최소 간격을 확보할 수 없으면 구간 크기 조정
        if slot_size < min_gap:
            count = max(1, total_minutes // min_gap)
            slot_size = total_minutes / count

        times = []
        for i in range(count):
            slot_start = slot_size * i
            slot_end = slot_size * (i + 1)

            # 구간 내 마진 (앞뒤 15% 여유)
            margin = slot_size * 0.15
            pick_start = slot_start + margin
            pick_end = slot_end - margin

            if pick_start >= pick_end:
                pick_start = slot_start
                pick_end = slot_end

            # 구간 내 랜덤 분 선택
            random_minute = random.uniform(pick_start, pick_end)
            random_minute = max(0, min(total_minutes - 1, random_minute))

            t = base + timedelta(minutes=round(random_minute))
            times.append(t)

        # 최소 간격 보정 (end_hour 초과 방지)
        for i in range(1, len(times)):
            diff = (times[i] - times[i - 1]).total_seconds() / 60
            if diff < min_gap:
                adjusted = times[i - 1] + timedelta(minutes=min_gap)
                times[i] = min(adjusted, end_limit)

        # 동일 시각 중복 제거 (end_limit에 닿았을 때)
        seen = set()
        unique_times = []
        for t in times:
            key = t.strftime('%Y-%m-%d %H:%M')
            if key not in seen:
                seen.add(key)
                unique_times.append(t)

        return unique_times
