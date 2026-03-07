import os
import re
import sys
import logging
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, jsonify, redirect, url_for, session, flash
from werkzeug.security import check_password_hash

# 프로젝트 루트를 path에 추가
sys.path.insert(0, os.path.dirname(__file__))

from config import Config
from models import (
    init_db, add_to_queue, get_all_queue, get_pending,
    get_queue_item, update_status, update_schedule,
    delete_queue_item, reset_item, get_schedule_settings,
    update_schedule_settings, get_queue_stats,
    create_user, get_user_by_username, get_user_by_id, update_user_keys,
    get_user_count, get_admin_users, delete_user
)
from coupang_api import CoupangAPI
from scraper import CoupangScraper
from scheduler import PostScheduler
from werkzeug.security import check_password_hash

# 로깅 설정
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# [SaaS] SECRET_KEY: .env에 없으면 instance/에 자동 생성 및 영구 보존
def _load_or_create_secret_key():
    instance_dir = os.path.join(os.path.dirname(__file__), 'instance')
    os.makedirs(instance_dir, exist_ok=True)
    key_file = os.path.join(instance_dir, 'secret_key.txt')
    # .env에 명시된 값 우선 사용
    if Config.SECRET_KEY:
        return Config.SECRET_KEY
    # 파일에 저장된 키 슬라오기
    if os.path.exists(key_file):
        with open(key_file, 'r') as f:
            return f.read().strip()
    # 최초 실행: 자동 생성 + 저장
    new_key = os.urandom(32).hex()
    with open(key_file, 'w') as f:
        f.write(new_key)
    logger.info("[SaaS] Flask SECRET_KEY를 instance/secret_key.txt에 자동 생성했습니다.")
    return new_key

app.secret_key = _load_or_create_secret_key()

# DB 초기화
init_db()

# 스케줄러 초기화
post_scheduler = PostScheduler()

# 스크래퍼 싱글턴 (브라우저 메모리 관리를 위해 공통 1개만 띄움)
_scraper = None
def get_scraper():
    global _scraper
    if _scraper is None:
        _scraper = CoupangScraper()
    return _scraper

# ── [SaaS] 인증 미들웨어 (Session 기반) ──

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            # API 요청이면 JSON 401 반환, 아니면 로그인 페이지 리다이렉트
            if request.accept_mimetypes.accept_json and not request.accept_mimetypes.accept_html:
                return jsonify({'error': '로그인이 필요합니다.'}), 401
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

# ── API 키 마스킹 ──
def mask_key(key, visible=4):
    """API 키를 마스킹하여 뒷부분만 노출."""
    if not key or len(key) <= visible:
        return '' if not key else '****'
    return '****' + key[-visible:]

# ── 카테고리 상수 ──
COUPANG_CATEGORIES = [
    {"id": "1003", "name": "패션의류"}, {"id": "1001", "name": "패션잡화"},
    {"id": "1010", "name": "뷰티"}, {"id": "1011", "name": "식품"},
    {"id": "1012", "name": "로켓프레시"}, {"id": "1013", "name": "생활용품"},
    {"id": "1016", "name": "가전디지털"}, {"id": "1002", "name": "스포츠/레저"},
    {"id": "1018", "name": "자동차용품"}, {"id": "1019", "name": "도서/음반"},
    {"id": "1022", "name": "반려/애완용품"}, {"id": "1015", "name": "문구/사무용품"},
]

# ── 템플릿 Context Inject ──
@app.context_processor
def inject_user():
    user = None
    if 'user_id' in session:
        user = get_user_by_id(session['user_id'])
    return dict(current_user=user)

# ── 인증 라우트 (로그인/회원가입) ──

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = get_user_by_username(username)
        if user and check_password_hash(user['password_hash'], password):
            session['user_id'] = user['id']
            next_page = request.args.get('next')
            return redirect(next_page or url_for('index'))
        else:
            flash('아이디 또는 비밀번호가 올바르지 않습니다.')
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        nickname = request.form.get('nickname', '').strip()
        email = request.form.get('email', '').strip()

        if not username or not password or not nickname or not email:
            flash('모든 필수 항목을 입력해 주세요.')
            user_count = get_user_count()
            return render_template('register.html', need_invite=(user_count > 0))

        user_count = get_user_count()

        if user_count == 0:
            # 첨 번째 사용자는 자동으로 최고 관리자(is_admin=1)가 됩니다
            user_id, err = create_user(username, password, is_admin=1, nickname=nickname, email=email)
        else:
            # 2번째부터는 기존 관리자의 승인 코드 확인
            invite_code = request.form.get('invite_code', '')
            admins = get_admin_users()
            from werkzeug.security import check_password_hash
            approved = any(check_password_hash(a['password_hash'], invite_code) for a in admins)
            if not approved and Config.ADMIN_PASSWORD and invite_code == Config.ADMIN_PASSWORD:
                approved = True
            if not approved:
                flash('관리자 승인 코드가 올바르지 않습니다.')
                return render_template('register.html', need_invite=True)
            user_id, err = create_user(username, password, is_admin=0, nickname=nickname, email=email)

        if err:
            flash(err)
        else:
            session['user_id'] = user_id
            return redirect(url_for('settings_page'))

    user_count = get_user_count()
    return render_template('register.html', need_invite=(user_count > 0))

@app.route('/logout')
def logout():
    session.pop('user_id', None)
    return redirect(url_for('login'))

@app.route('/delete-account', methods=['POST'])
@login_required
def delete_account():
    """현재 로그인된 사용자 계정과 모든 데이터를 영구 삭제 (Hard Delete)."""
    user_id = session['user_id']
    confirm_password = request.form.get('confirm_password', '')

    user = get_user_by_id(user_id)
    if not user or not check_password_hash(user['password_hash'], confirm_password):
        flash('비밀번호가 일치하지 않습니다. 탈퇴가 취소되었습니다.')
        return redirect(url_for('settings_page'))

    delete_user(user_id)
    session.pop('user_id', None)
    flash('계정과 모든 데이터가 영구 삭제되었습니다.')
    return redirect(url_for('login'))

# ── 메인 페이지 ──

@app.route('/')
@login_required
def index():
    user_id = session['user_id']
    stats = get_queue_stats(user_id)
    return render_template('index.html', stats=stats, categories=COUPANG_CATEGORIES)

@app.route('/api/search', methods=['POST'])
@login_required
def api_search():
    keyword = request.json.get('keyword', '').strip()
    if not keyword:
        return jsonify({'error': '키워드를 입력하세요.'}), 400

    user = get_user_by_id(session['user_id'])
    if not user.get('coupang_access_key') or not user.get('coupang_secret_key'):
        return jsonify({'error': '쿠팡 파트너스 API 키가 설정되지 않았습니다.'}), 400

    try:
        api = CoupangAPI(user['coupang_access_key'], user['coupang_secret_key'])
        products = api.search_products(keyword)
        return jsonify({'products': products})
    except Exception as e:
        logger.error(f"쿠팡 검색 실패: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/goldbox', methods=['POST'])
@login_required
def api_goldbox():
    # 골드박스는 스크래퍼이므로 공통 기능 사용
    try:
        scraper = get_scraper()
        products = scraper.scrape_goldbox(limit=20)
        return jsonify({'products': products})
    except Exception as e:
        logger.error(f"골드박스 스크래핑 실패: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/best-categories', methods=['POST'])
@login_required
def api_best_categories():
    category_id = request.json.get('category_id', '').strip()
    if not category_id or not category_id.isdigit():
        return jsonify({'error': '유효하지 않은 카테고리 ID입니다.'}), 400

    user = get_user_by_id(session['user_id'])
    if not user.get('coupang_access_key') or not user.get('coupang_secret_key'):
        return jsonify({'error': '쿠팡 파트너스 API 키가 설정되지 않았습니다.'}), 400

    try:
        api = CoupangAPI(user['coupang_access_key'], user['coupang_secret_key'])
        products = api.get_best_categories(category_id)
        return jsonify({'products': products})
    except Exception as e:
        logger.error(f"베스트 카테고리 조회 실패: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/queue/add', methods=['POST'])
@login_required
def api_queue_add():
    user_id = session['user_id']
    user = get_user_by_id(user_id)
    products = request.json.get('products', [])
    if not products:
        return jsonify({'error': '상품을 선택하세요.'}), 400

    try:
        needs_deeplink = [p for p in products if not p.get('affiliate_url') and p.get('product_url')]
        if needs_deeplink and user.get('coupang_access_key') and user.get('coupang_secret_key'):
            try:
                api = CoupangAPI(user['coupang_access_key'], user['coupang_secret_key'])
                urls = [p['product_url'] for p in needs_deeplink]
                deeplinks = api.get_deeplink(urls)
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
                logger.warning(f"딥링크 변환 실패: {e}")

        for p in products:
            if not p.get('affiliate_url'):
                p['affiliate_url'] = p.get('product_url', '')

        added_ids = add_to_queue(user_id, products)
        return jsonify({
            'message': f'{len(added_ids)}개 상품이 대기열에 추가되었습니다.',
            'ids': added_ids,
        })
    except Exception as e:
        logger.error(f"대기열 추가 실패: {e}")
        return jsonify({'error': str(e)}), 500

# ── 대기열 관리 ──

@app.route('/queue')
@login_required
def queue_page():
    user_id = session['user_id']
    items = get_all_queue(user_id)
    settings = get_schedule_settings(user_id)
    stats = get_queue_stats(user_id)
    return render_template('queue.html', items=items, settings=settings, stats=stats)

@app.route('/api/queue/schedule', methods=['POST'])
@login_required
def api_queue_schedule():
    user_id = session['user_id']
    data = request.json
    item_ids = data.get('item_ids', [])
    mode = data.get('mode', 'interval')

    if not item_ids: return jsonify({'error': '항목을 선택하세요.'}), 400

    try:
        if mode == 'immediate':
            post_scheduler.schedule_items_immediate(item_ids)
        elif mode == 'interval':
            start_str = data.get('start_time')
            interval = int(data.get('interval_hours', 6))
            start_time = datetime.strptime(start_str, '%Y-%m-%dT%H:%M') if start_str else None
            post_scheduler.schedule_items_interval(item_ids, start_time, interval)
        elif mode == 'random':
            post_scheduler.schedule_items_random(
                item_ids,
                posts_per_day=int(data.get('posts_per_day', 3)),
                start_hour=int(data.get('start_hour', 8)),
                end_hour=int(data.get('end_hour', 23)),
            )
        elif mode == 'individual':
            schedules = data.get('schedules', {})
            for item_id_str, time_str in schedules.items():
                update_schedule(int(item_id_str), time_str, user_id)

        return jsonify({'message': '스케줄이 설정되었습니다.'})
    except Exception as e:
        logger.error(f"스케줄 설정 실패: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/queue/<int:item_id>', methods=['DELETE'])
@login_required
def api_queue_delete(item_id):
    try:
        delete_queue_item(item_id, session['user_id'])
        return jsonify({'message': '삭제되었습니다.'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/queue/delete-bulk', methods=['POST'])
@login_required
def api_queue_delete_bulk():
    item_ids = request.json.get('item_ids', [])
    try:
        for item_id in item_ids:
            delete_queue_item(item_id, session['user_id'])
        return jsonify({'message': f'{len(item_ids)}개 삭제됨'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/queue/<int:item_id>/retry', methods=['POST'])
@login_required
def api_queue_retry(item_id):
    try:
        reset_item(item_id, session['user_id'])
        return jsonify({'message': '재시도 대기 중으로 변경됨'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/queue/<int:item_id>/run-now', methods=['POST'])
@login_required
def api_queue_run_now(item_id):
    user_id = session['user_id']
    item = get_queue_item(item_id, user_id)
    if not item: return jsonify({'error': '항목이 없거나 권한이 없습니다.'}), 404

    try:
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        update_schedule(item_id, now_str, user_id)
        post_scheduler.add_to_immediate_queue(item_id)
        return jsonify({'message': '처리가 시작되었습니다.'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── 설정 페이지 ──

@app.route('/settings')
@login_required
def settings_page():
    user_id = session['user_id']
    user = get_user_by_id(user_id)
    settings = get_schedule_settings(user_id)
    
    masked_config = {
        'COUPANG_ACCESS_KEY': mask_key(user.get('coupang_access_key'), 6),
        'COUPANG_SECRET_KEY': mask_key(user.get('coupang_secret_key'), 6),
        'OPENAI_API_KEY': mask_key(user.get('openai_api_key'), 4),
        'GEMINI_API_KEY': mask_key(user.get('gemini_api_key'), 4),
        'WP_URL': user.get('wp_url', ''),
        'WP_USERNAME': user.get('wp_username', ''),
        'WP_APP_PASSWORD': mask_key(user.get('wp_app_password'), 4),
    }
    
    # 필수 키 검증
    missing = []
    for k in ('coupang_access_key', 'coupang_secret_key', 'openai_api_key', 'gemini_api_key', 'wp_url', 'wp_username', 'wp_app_password'):
        if not user.get(k): missing.append(k)

    return render_template('settings.html', missing=missing, settings=settings, config=masked_config)

@app.route('/api/settings/save', methods=['POST'])
@login_required
def api_settings_save():
    user_id = session['user_id']
    data = request.json

    try:
        keys_dict = {}
        key_map = {
            'coupang_access_key': 'coupang_access_key',
            'coupang_secret_key': 'coupang_secret_key',
            'openai_api_key': 'openai_api_key',
            'gemini_api_key': 'gemini_api_key',
            'wp_url': 'wp_url',
            'wp_username': 'wp_username',
            'wp_app_password': 'wp_app_password',
        }

        for form_key, db_col in key_map.items():
            raw_value = data.get(form_key, '')
            if not raw_value or raw_value.startswith('****'):
                continue
            # sanitize
            clean_value = re.sub(r'[\r\n\x00]', '', raw_value).strip()
            keys_dict[db_col] = clean_value

        if keys_dict:
            update_user_keys(user_id, keys_dict)

        if 'schedule_mode' in data:
            update_schedule_settings(
                user_id=user_id,
                mode=data.get('schedule_mode', 'interval'),
                interval_hours=int(data.get('interval_hours', 6)),
                daily_times=data.get('daily_times', '09:00,15:00,21:00'),
                max_posts_per_day=int(data.get('max_posts_per_day', 5)),
                auto_publish=1 if data.get('auto_publish') else 0,
            )

        return jsonify({'message': '설정이 저장되었습니다.'})
    except Exception as e:
        logger.error(f"설정 저장 실패: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/settings/test-wp', methods=['POST'])
@login_required
def api_test_wp():
    user = get_user_by_id(session['user_id'])
    try:
        from wordpress_publisher import WordPressPublisher
        publisher = WordPressPublisher(user['wp_url'], user['wp_username'], user['wp_app_password'])
        success, message = publisher.test_connection()
        return jsonify({'success': success, 'message': message})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/api/settings/test-coupang', methods=['POST'])
@login_required
def api_test_coupang():
    user = get_user_by_id(session['user_id'])
    try:
        api = CoupangAPI(user['coupang_access_key'], user['coupang_secret_key'])
        success, message = api.test_connection()
        return jsonify({'success': success, 'message': message})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/api/queue/stats')
@login_required
def api_queue_stats():
    return jsonify(get_queue_stats(session['user_id']))

@app.route('/api/queue/list')
@login_required
def api_queue_list():
    return jsonify(get_all_queue(session['user_id']))

if __name__ == '__main__':
    logger.info("SaaS Saiso App 시작")
    if not Config.SECRET_KEY:
        logger.warning("⚠️ SECRET_KEY가 환경변수에 없습니다. 임시 토큰을 사용하므로 재시작 시 로그아웃됩니다.")

    post_scheduler.start()

    try:
        app.run(debug=False, use_reloader=False, port=8080)
    finally:
        post_scheduler.stop()
        if _scraper:
            _scraper.close()
