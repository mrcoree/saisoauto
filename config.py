import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))


class Config:
    # 앱 공통 설정
    CHROME_HEADLESS = os.getenv('CHROME_HEADLESS', 'true').lower() == 'true'

    # 보안 (하위 호환성 지원 용 - .env에 있는 경우 활용)
    ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', '')  # 선택사항 (hides this from logs clealy)
    SECRET_KEY = os.getenv('SECRET_KEY', '')           # 없으면 instance/secret_key.txt 자동 생성
    FERNET_KEY = os.getenv('FERNET_KEY', '')           # API 키 AES-256 암호화용 마스터 키

    # SQLite DB 경로
    DB_PATH = os.path.join(os.path.dirname(__file__), 'saiso.db')

    # ── 아래 항목들은 SaaS 전환 후 각 사용자가 직접 [설정] 탭에서 입력합니다 ──
    # 이제 .env가 아닌 DB의 users 테이블에서 사용자별로 조회합니다.
    # 하위 호환성을 위해 잠시 남겨두지만 앱 코드에서 직접 사용하지 않습니다.
    # COUPANG_ACCESS_KEY, COUPANG_SECRET_KEY, OPENAI_API_KEY
    # GEMINI_API_KEY, WP_URL, WP_USERNAME, WP_APP_PASSWORD
