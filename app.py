import os
import re
import sys
import logging
import threading
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template, request, jsonify, redirect, url_for

# 프로젝트 루트를 path에 추가
sys.path.insert(0, os.path.dirname(__file__))

from config import Config
from models import (
    init_db, add_to_queue, get_all_queue, get_pending,
    get_queue_item, update_status, update_schedule,
    delete_queue_item, reset_item, get_schedule_settings,
    update_schedule_settings, get_queue_stats,
)
from coupang_api import CoupangAPI
from scraper import CoupangScraper
from scheduler import PostScheduler

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
# [FIX #6] 고정 secret_key 사용 (매 재시작 시 변경되지 않음)
app.secret_key = Config.SECRET_KEY or os.urandom(24)

# DB 초기화
init_db()

# 스케줄러 초기화
post_scheduler = PostScheduler()

# 스크래퍼 싱글턴
_scraper = None


def get_scraper():
    global _scraper
    if _scraper is None:
        _scraper = CoupangScraper()
    return _scraper


# ── [FIX #1] 인증 미들웨어 ──

def require_auth(f):
    """Basic Auth 인증 데코레이터."""
    @wraps(f)
    def decorated(*args, **kwargs):
        password = Config.ADMIN_PASSWORD
        if not password:
            # ADMIN_PASSWORD 미설정 시 인증 스킵 (개발 편의)
            return f(*args, **kwargs)
        auth = request.authorization
        if not auth or auth.password != password:
            # 브라우저 요청(HTML)이면 401 페이지 반환, API면 JSON 반환
            if request.accept_mimetypes.accept_html:
                return (
                    '<html><body style="text-align:center;padding:60px;font-family:sans-serif">'
                    '<h1>401 Unauthorized</h1>'
                    '<p>인증이 필요합니다. 비밀번호를 입력해 주세요.</p></body></html>',
                    401,
                    {'WWW-Authenticate': 'Basic realm="Saiso Admin"'},
                )
            return jsonify({'error': '인증이 필요합니다.'}), 401, {
                'WWW-Authenticate': 'Basic realm="Saiso Admin"'
            }
        return f(*args, **kwargs)
    return decorated


# ── [FIX #2] .env 입력 검증 ──

def sanitize_env_value(value):
    """줄바꿈, 캐리지리턴 등 .env에 위험한 문자를 제거."""
    if not isinstance(value, str):
        return ''
    # 줄바꿈, 캐리지리턴, NULL 바이트 제거
    return re.sub(r'[\r\n\x00]', '', value).strip()


# ── [FIX #3] API 키 마스킹 ──

def mask_key(key, visible=4):
    """API 키를 마스킹하여 뒷부분만 노출."""
    if not key or len(key) <= visible:
        return '****'
    return '****' + key[-visible:]


# ── 카테고리 상수 ──

COUPANG_CATEGORIES = [
    {"id": "1003", "name": "패션의류"},
    {"id": "1001", "name": "패션잡화"},
    {"id": "1010", "name": "뷰티"},
    {"id": "1011", "name": "식품"},
    {"id": "1012", "name": "로켓프레시"},
    {"id": "1013", "name": "생활용품"},
    {"id": "1016", "name": "가전디지털"},
    {"id": "1002", "name": "스포츠/레저"},
    {"id": "1018", "name": "자동차용품"},
    {"id": "1019", "name": "도서/음반"},
    {"id": "1022", "name": "반려/애완용품"},
    {"id": "1015", "name": "문구/사무용품"},
]


# ── 메인 페이지 ──

@app.route('/')
@require_auth
def index():
    """메인 페이지: 상품 검색 + 목록."""
    stats = get_queue_stats()
    return render_template('index.html', stats=stats, categories=COUPANG_CATEGORIES)


@app.route('/api/search', methods=['POST'])
@require_auth
def api_search():
    """쿠팡 파트너스 API 상품 검색."""
    keyword = request.json.get('keyword', '').strip()
    if not keyword:
        return jsonify({'error': '키워드를 입력하세요.'}), 400

    if not Config.COUPANG_ACCESS_KEY or not Config.COUPANG_SECRET_KEY:
        return jsonify({'error': '쿠팡 파트너스 API 키가 설정되지 않았습니다.'}), 400

    try:
        api = CoupangAPI()
        products = api.search_products(keyword)
        return jsonify({'products': products})
    except Exception as e:
        logger.error(f"쿠팡 검색 실패: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/goldbox', methods=['POST'])
@require_auth
def api_goldbox():
    """골드박스(오늘의 특가) 상품 조회 — 웹 스크래핑."""
    try:
        scraper = get_scraper()
        products = scraper.scrape_goldbox(limit=20)
        return jsonify({'products': products})
    except Exception as e:
        logger.error(f"골드박스 스크래핑 실패: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/best-categories', methods=['POST'])
@require_auth
def api_best_categories():
    """카테고리별 베스트 상품 조회."""
    category_id = request.json.get('category_id', '').strip()
    if not category_id:
        return jsonify({'error': '카테고리를 선택하세요.'}), 400

    # [FIX #7] category_id 숫자 검증
    if not category_id.isdigit():
        return jsonify({'error': '유효하지 않은 카테고리 ID입니다.'}), 400

    if not Config.COUPANG_ACCESS_KEY or not Config.COUPANG_SECRET_KEY:
        return jsonify({'error': '쿠팡 파트너스 API 키가 설정되지 않았습니다.'}), 400

    try:
        api = CoupangAPI()
        products = api.get_best_categories(category_id)
        return jsonify({'products': products})
    except Exception as e:
        logger.error(f"베스트 카테고리 조회 실패: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/queue/add', methods=['POST'])
@require_auth
def api_queue_add():
    """선택한 상품들을 대기열에 추가. affiliate_url이 없으면 딥링크 변환."""
    products = request.json.get('products', [])
    if not products:
        return jsonify({'error': '상품을 선택하세요.'}), 400

    try:
        # affiliate_url이 비어있는 상품에 대해 딥링크 변환
        needs_deeplink = [
            p for p in products
            if not p.get('affiliate_url') and p.get('product_url')
        ]
        if needs_deeplink and Config.COUPANG_ACCESS_KEY and Config.COUPANG_SECRET_KEY:
            try:
                api = CoupangAPI()
                urls = [p['product_url'] for p in needs_deeplink]
                deeplinks = api.get_deeplink(urls)
                # 딥링크 결과 매핑
                url_map = {}
                for dl in deeplinks:
                    orig = dl.get('originalUrl', '')
                    short = dl.get('shortenUrl', '')
                    if orig and short:
                        url_map[orig] = short
                for p in products:
                    if not p.get('affiliate_url') and p.get('product_url'):
                        p['affiliate_url'] = url_map.get(p['product_url'], p['product_url'])
            except Exception as e:
                logger.warning(f"딥링크 변환 실패 (원본 URL 사용): {e}")
                for p in products:
                    if not p.get('affiliate_url'):
                        p['affiliate_url'] = p.get('product_url', '')

        # affiliate_url이 여전히 비어있으면 product_url로 폴백
        for p in products:
            if not p.get('affiliate_url'):
                p['affiliate_url'] = p.get('product_url', '')

        added_ids = add_to_queue(products)
        return jsonify({
            'message': f'{len(added_ids)}개 상품이 대기열에 추가되었습니다.',
            'ids': added_ids,
        })
    except Exception as e:
        logger.error(f"대기열 추가 실패: {e}")
        return jsonify({'error': str(e)}), 500


# ── 대기열 페이지 ──

@app.route('/queue')
@require_auth
def queue_page():
    """대기열 관리 페이지."""
    items = get_all_queue()
    settings = get_schedule_settings()
    stats = get_queue_stats()
    return render_template('queue.html', items=items, settings=settings, stats=stats)


@app.route('/api/queue/schedule', methods=['POST'])
@require_auth
def api_queue_schedule():
    """선택한 항목들에 스케줄 일괄 지정."""
    data = request.json
    item_ids = data.get('item_ids', [])
    mode = data.get('mode', 'interval')

    if not item_ids:
        return jsonify({'error': '항목을 선택하세요.'}), 400

    try:
        if mode == 'immediate':
            post_scheduler.schedule_items_immediate(item_ids)
            return jsonify({'message': f'{len(item_ids)}개 항목이 즉시 발행 예약되었습니다.'})

        elif mode == 'interval':
            start_str = data.get('start_time')
            interval = int(data.get('interval_hours', 6))
            start_time = datetime.strptime(start_str, '%Y-%m-%dT%H:%M') if start_str else None
            post_scheduler.schedule_items_interval(item_ids, start_time, interval)

        elif mode == 'random':
            posts_per_day = int(data.get('posts_per_day', 3))
            start_hour = int(data.get('start_hour', 8))
            end_hour = int(data.get('end_hour', 23))
            post_scheduler.schedule_items_random(
                item_ids,
                posts_per_day=posts_per_day,
                start_hour=start_hour,
                end_hour=end_hour,
            )
            return jsonify({
                'message': f'{len(item_ids)}개 항목이 하루 {posts_per_day}회 랜덤 발행으로 예약되었습니다.'
            })

        elif mode == 'individual':
            schedules = data.get('schedules', {})
            for item_id_str, time_str in schedules.items():
                update_schedule(int(item_id_str), time_str)

        return jsonify({'message': '스케줄이 설정되었습니다.'})
    except Exception as e:
        logger.error(f"스케줄 설정 실패: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/queue/<int:item_id>', methods=['DELETE'])
@require_auth
def api_queue_delete(item_id):
    """대기열 항목 삭제."""
    try:
        delete_queue_item(item_id)
        return jsonify({'message': '삭제되었습니다.'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/queue/delete-bulk', methods=['POST'])
@require_auth
def api_queue_delete_bulk():
    """선택한 항목 일괄 삭제."""
    item_ids = request.json.get('item_ids', [])
    if not item_ids:
        return jsonify({'error': '항목을 선택하세요.'}), 400

    try:
        for item_id in item_ids:
            delete_queue_item(item_id)
        return jsonify({'message': f'{len(item_ids)}개 항목이 삭제되었습니다.'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/queue/<int:item_id>/retry', methods=['POST'])
@require_auth
def api_queue_retry(item_id):
    """실패한 항목 재시도 (pending으로 리셋)."""
    try:
        reset_item(item_id)
        return jsonify({'message': '재시도 대기 중으로 변경되었습니다.'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/queue/<int:item_id>/run-now', methods=['POST'])
@require_auth
def api_queue_run_now(item_id):
    """대기열 항목 즉시 처리."""
    item = get_queue_item(item_id)
    if not item:
        return jsonify({'error': '항목을 찾을 수 없습니다.'}), 404

    try:
        # [NEW] 즉시 실행 버튼 클릭 시 예정시간을 현재시간으로 강제 덮어쓰기 (화면 즉각 반영)
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        update_schedule(item_id, now_str)

        # 별도 스레드에서 실행 무작위 생성 방지 -> 안전한 순차 큐에 삽입
        post_scheduler.add_to_immediate_queue(item_id)
        return jsonify({'message': '처리가 시작되었습니다. 대기열에서 상태를 확인하세요.'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── 설정 페이지 ──

@app.route('/settings')
@require_auth
def settings_page():
    """설정 페이지 — API 키는 마스킹하여 전달."""
    missing = Config.validate()
    settings = get_schedule_settings()
    # [FIX #3] API 키 마스킹
    masked_config = {
        'COUPANG_ACCESS_KEY': mask_key(Config.COUPANG_ACCESS_KEY, 6),
        'COUPANG_SECRET_KEY': mask_key(Config.COUPANG_SECRET_KEY, 6),
        'OPENAI_API_KEY': mask_key(Config.OPENAI_API_KEY, 4),
        'GEMINI_API_KEY': mask_key(Config.GEMINI_API_KEY, 4),
        'WP_URL': Config.WP_URL,
        'WP_USERNAME': Config.WP_USERNAME,
        'WP_APP_PASSWORD': mask_key(Config.WP_APP_PASSWORD, 4),
    }
    return render_template('settings.html', missing=missing, settings=settings, config=masked_config)


@app.route('/api/settings/save', methods=['POST'])
@require_auth
def api_settings_save():
    """설정 저장 (.env 파일 업데이트)."""
    data = request.json
    env_path = os.path.join(os.path.dirname(__file__), '.env')

    try:
        # 현재 .env 읽기
        env_lines = {}
        if os.path.exists(env_path):
            with open(env_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        key, _, value = line.partition('=')
                        env_lines[key.strip()] = value.strip()

        # [FIX #2] 값 업데이트 (마스킹된 값이면 기존 값 유지)
        key_map = {
            'coupang_access_key': 'COUPANG_ACCESS_KEY',
            'coupang_secret_key': 'COUPANG_SECRET_KEY',
            'openai_api_key': 'OPENAI_API_KEY',
            'gemini_api_key': 'GEMINI_API_KEY',
            'wp_url': 'WP_URL',
            'wp_username': 'WP_USERNAME',
            'wp_app_password': 'WP_APP_PASSWORD',
        }

        for form_key, env_key in key_map.items():
            raw_value = data.get(form_key, '')
            if not raw_value:
                continue
            # 마스킹된 값은 무시 (****로 시작하면 기존 값 유지)
            if raw_value.startswith('****'):
                continue
            # 줄바꿈 인젝션 방지
            env_lines[env_key] = sanitize_env_value(raw_value)

        # .env 파일 쓰기
        with open(env_path, 'w') as f:
            f.write("# 쿠팡 파트너스 API\n")
            f.write(f"COUPANG_ACCESS_KEY={env_lines.get('COUPANG_ACCESS_KEY', '')}\n")
            f.write(f"COUPANG_SECRET_KEY={env_lines.get('COUPANG_SECRET_KEY', '')}\n\n")
            f.write("# OpenAI\n")
            f.write(f"OPENAI_API_KEY={env_lines.get('OPENAI_API_KEY', '')}\n\n")
            f.write("# Google Gemini (Nano Banana 2)\n")
            f.write(f"GEMINI_API_KEY={env_lines.get('GEMINI_API_KEY', '')}\n\n")
            f.write("# WordPress\n")
            f.write(f"WP_URL={env_lines.get('WP_URL', '')}\n")
            f.write(f"WP_USERNAME={env_lines.get('WP_USERNAME', '')}\n")
            f.write(f"WP_APP_PASSWORD={env_lines.get('WP_APP_PASSWORD', '')}\n\n")
            f.write("# 기타\n")
            f.write(f"CHROME_HEADLESS={env_lines.get('CHROME_HEADLESS', 'true')}\n\n")
            f.write("# 보안\n")
            f.write(f"ADMIN_PASSWORD={env_lines.get('ADMIN_PASSWORD', '')}\n")
            f.write(f"SECRET_KEY={env_lines.get('SECRET_KEY', '')}\n")

        # 스케줄 설정 저장
        if 'schedule_mode' in data:
            update_schedule_settings(
                mode=data.get('schedule_mode', 'interval'),
                interval_hours=int(data.get('interval_hours', 6)),
                daily_times=data.get('daily_times', '09:00,15:00,21:00'),
                max_posts_per_day=int(data.get('max_posts_per_day', 5)),
                auto_publish=1 if data.get('auto_publish') else 0,
            )

        # Config 다시 로드
        Config.reload()

        return jsonify({'message': '설정이 저장되었습니다.'})
    except Exception as e:
        logger.error(f"설정 저장 실패: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/settings/test-wp', methods=['POST'])
@require_auth
def api_test_wp():
    """워드프레스 연결 테스트."""
    try:
        from wordpress_publisher import WordPressPublisher
        publisher = WordPressPublisher()
        success, message = publisher.test_connection()
        return jsonify({'success': success, 'message': message})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


@app.route('/api/settings/test-coupang', methods=['POST'])
@require_auth
def api_test_coupang():
    """쿠팡 API 연결 테스트."""
    try:
        api = CoupangAPI()
        success, message = api.test_connection()
        return jsonify({'success': success, 'message': message})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


# ── API: 대기열 상태 조회 ──

@app.route('/api/queue/stats')
@require_auth
def api_queue_stats():
    """대기열 통계 JSON."""
    return jsonify(get_queue_stats())


@app.route('/api/queue/list')
@require_auth
def api_queue_list():
    """대기열 전체 목록 JSON."""
    return jsonify(get_all_queue())


# ── 앱 시작 ──

if __name__ == '__main__':
    logger.info("사이소(Saiso) 시작")
    missing = Config.validate()
    if missing:
        logger.warning(f"누락된 설정: {', '.join(missing)}")
        logger.warning("설정 페이지에서 API 키를 입력하세요: http://localhost:8080/settings")

    if not Config.ADMIN_PASSWORD:
        logger.warning("⚠️  ADMIN_PASSWORD가 설정되지 않았습니다. API가 인증 없이 노출됩니다!")

    # 스케줄러 시작
    post_scheduler.start()

    try:
        # [FIX #1] debug=False로 변경하여 디버거 RCE 방지
        app.run(debug=False, use_reloader=False, port=8080)
    finally:
        post_scheduler.stop()
        if _scraper:
            _scraper.close()
