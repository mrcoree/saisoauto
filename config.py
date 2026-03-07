import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))


class Config:
    # 쿠팡 파트너스 API
    COUPANG_ACCESS_KEY = os.getenv('COUPANG_ACCESS_KEY', '')
    COUPANG_SECRET_KEY = os.getenv('COUPANG_SECRET_KEY', '')

    # OpenAI
    OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '')

    # Google Gemini
    GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '')

    # WordPress
    WP_URL = os.getenv('WP_URL', '')
    WP_USERNAME = os.getenv('WP_USERNAME', '')
    WP_APP_PASSWORD = os.getenv('WP_APP_PASSWORD', '')

    # 기타
    CHROME_HEADLESS = os.getenv('CHROME_HEADLESS', 'true').lower() == 'true'

    # 보안
    ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', '')
    SECRET_KEY = os.getenv('SECRET_KEY', '')
    FERNET_KEY = os.getenv('FERNET_KEY', '')  # API 키 암호화용 마스터 키

    # SQLite
    DB_PATH = os.path.join(os.path.dirname(__file__), 'saiso.db')

    @classmethod
    def validate(cls):
        """필수 설정 누락 확인. 누락된 키 목록 반환."""
        missing = []
        required = {
            'COUPANG_ACCESS_KEY': cls.COUPANG_ACCESS_KEY,
            'COUPANG_SECRET_KEY': cls.COUPANG_SECRET_KEY,
            'OPENAI_API_KEY': cls.OPENAI_API_KEY,
            'GEMINI_API_KEY': cls.GEMINI_API_KEY,
            'WP_URL': cls.WP_URL,
            'WP_USERNAME': cls.WP_USERNAME,
            'WP_APP_PASSWORD': cls.WP_APP_PASSWORD,
        }
        for key, value in required.items():
            if not value:
                missing.append(key)
        return missing

    @classmethod
    def reload(cls):
        """환경변수 다시 로드 (설정 변경 후 호출)."""
        load_dotenv(os.path.join(os.path.dirname(__file__), '.env'), override=True)
        cls.COUPANG_ACCESS_KEY = os.getenv('COUPANG_ACCESS_KEY', '')
        cls.COUPANG_SECRET_KEY = os.getenv('COUPANG_SECRET_KEY', '')
        cls.OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '')
        cls.GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '')
        cls.WP_URL = os.getenv('WP_URL', '')
        cls.WP_USERNAME = os.getenv('WP_USERNAME', '')
        cls.WP_APP_PASSWORD = os.getenv('WP_APP_PASSWORD', '')
        cls.CHROME_HEADLESS = os.getenv('CHROME_HEADLESS', 'true').lower() == 'true'
        cls.ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', '')
        cls.SECRET_KEY = os.getenv('SECRET_KEY', '')
        cls.FERNET_KEY = os.getenv('FERNET_KEY', '')
