import json
import base64
import logging
import time
import re
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities
from webdriver_manager.chrome import ChromeDriverManager

from config import Config

logger = logging.getLogger(__name__)

GOLDBOX_URL = "https://www.coupang.com/np/goldbox"

# [FIX #5] SSRF 방지를 위한 허용 도메인
ALLOWED_SCRAPE_DOMAINS = {'coupang.com', 'www.coupang.com', 'm.coupang.com', 'pages.coupang.com'}
ALLOWED_IMAGE_DOMAINS = {'thumbnail.image.rakuten.co.jp', 'image7.coupangcdn.com',
                         'image6.coupangcdn.com', 'image5.coupangcdn.com',
                         'image4.coupangcdn.com', 'image3.coupangcdn.com',
                         'image2.coupangcdn.com', 'image1.coupangcdn.com',
                         'img1a.coupangcdn.com', 'img1b.coupangcdn.com',
                         'thumbnail6.coupangcdn.com', 'thumbnail7.coupangcdn.com',
                         'thumbnail8.coupangcdn.com', 'thumbnail9.coupangcdn.com',
                         'thumbnail10.coupangcdn.com'}


class CoupangScraper:
    def __init__(self):
        self.headless = Config.CHROME_HEADLESS
        self._driver = None

    # ── 골드박스 스크래핑 ──

    def scrape_goldbox(self, limit=20):
        """골드박스 페이지에서 Performance Log를 통해 상품 JSON을 캡처.

        Returns:
            list of dict: 표준 상품 dict 목록
        """
        driver = self._create_driver(enable_logging=True)
        try:
            logger.info("골드박스 페이지 로딩 시작")
            driver.get(GOLDBOX_URL)
            time.sleep(5)

            # 페이지가 pages.coupang.com으로 리다이렉트될 때까지 대기
            WebDriverWait(driver, 15).until(
                lambda d: 'pages.coupang.com' in d.current_url or 'goldbox' in d.current_url
            )
            time.sleep(3)

            products_json = self._capture_products_json(driver)
            if products_json:
                return self._parse_goldbox_json(products_json, limit)

            logger.warning("Performance Log에서 상품 JSON을 찾지 못함")
            return []
        except Exception as e:
            logger.error(f"골드박스 스크래핑 실패: {e}")
            raise
        finally:
            self._quit_logging_driver(driver)


    def _capture_products_json(self, driver):
        """Performance Log에서 /api/products 응답 JSON을 추출."""
        logs = driver.get_log('performance')
        logger.info(f"Performance Log 항목 수: {len(logs)}")

        for entry in logs:
            try:
                log_msg = json.loads(entry['message'])['message']
                method = log_msg.get('method', '')

                if method != 'Network.responseReceived':
                    continue

                url = log_msg['params']['response']['url']
                if '/api/products' not in url:
                    continue

                request_id = log_msg['params']['requestId']
                logger.info(f"/api/products 응답 발견: {url}")

                # CDP로 응답 본문 가져오기
                body = driver.execute_cdp_cmd(
                    'Network.getResponseBody',
                    {'requestId': request_id}
                )
                response_body = body.get('body', '')
                if body.get('base64Encoded'):
                    response_body = base64.b64decode(response_body).decode('utf-8')

                data = json.loads(response_body)
                return data

            except Exception:
                continue

        return None

    def _parse_goldbox_json(self, data, limit):
        """골드박스 내부 API JSON → 표준 상품 dict 목록 변환."""
        products = []

        # data 구조: 최상위에 'products' 키가 있거나, data 자체가 리스트일 수 있음
        raw_items = []
        if isinstance(data, dict):
            # 여러 가능한 키 탐색
            for key in ('products', 'data', 'items', 'rData'):
                if key in data:
                    candidate = data[key]
                    if isinstance(candidate, list):
                        raw_items = candidate
                        break
                    elif isinstance(candidate, dict):
                        # 중첩된 products 키
                        for sub_key in ('products', 'data', 'items'):
                            if sub_key in candidate and isinstance(candidate[sub_key], list):
                                raw_items = candidate[sub_key]
                                break
                        if raw_items:
                            break
            if not raw_items and isinstance(data, dict):
                # 마지막 시도: dict의 첫 번째 list 값
                for v in data.values():
                    if isinstance(v, list) and len(v) > 0:
                        raw_items = v
                        break
        elif isinstance(data, list):
            raw_items = data

        logger.info(f"골드박스 원시 상품 수: {len(raw_items)}")

        for item in raw_items[:limit]:
            try:
                product = self._parse_goldbox_item(item)
                if product:
                    products.append(product)
            except Exception as e:
                logger.debug(f"상품 파싱 실패: {e}")
                continue

        logger.info(f"골드박스 파싱 완료: {len(products)}개")
        return products

    def _parse_goldbox_item(self, item):
        """개별 골드박스 상품 아이템 파싱."""
        # imageAndTitleArea 구조
        title_area = item.get('imageAndTitleArea', {})
        price_area = item.get('priceArea', {})
        rocket_area = item.get('rocketArea', {})

        product_name = title_area.get('title', '')
        thumbnail_url = title_area.get('defaultUrl', '')
        link = item.get('link', '')
        price_raw = price_area.get('price', '')
        is_rocket = rocket_area.get('show', False)

        if not product_name:
            # 대체 구조 시도
            product_name = item.get('productName', item.get('title', ''))
        if not thumbnail_url:
            thumbnail_url = item.get('productImage', item.get('imageUrl', ''))
        if not link:
            link = item.get('productUrl', item.get('url', ''))
        if not price_raw:
            price_raw = item.get('productPrice', item.get('price', ''))

        if not product_name:
            return None

        # URL 정규화
        product_url = link
        if product_url and not product_url.startswith('http'):
            product_url = f"https://www.coupang.com{product_url}"

        # 이미지 URL 정규화
        if thumbnail_url and not thumbnail_url.startswith('http'):
            thumbnail_url = f"https:{thumbnail_url}"

        # 가격 포맷
        price = self._format_goldbox_price(price_raw)

        return {
            'product_name': product_name,
            'product_url': product_url,
            'affiliate_url': '',  # 딥링크 변환 필요
            'price': price,
            'thumbnail_url': thumbnail_url,
            'category': '',
            'is_rocket': bool(is_rocket),
            'is_free_shipping': False,
        }

    @staticmethod
    def _validate_url(url, allowed_domains):
        """URL이 허용된 도메인인지 검증. [FIX #5]"""
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            raise ValueError(f"허용되지 않는 URL scheme: {parsed.scheme}")
        host = parsed.netloc.lower().split(':')[0]  # 포트 제거
        if not any(host == d or host.endswith('.' + d) for d in allowed_domains):
            raise ValueError(f"허용되지 않는 도메인: {host}")

    @staticmethod
    def _format_goldbox_price(price_raw):
        """골드박스 가격 문자열을 포맷."""
        if not price_raw:
            return ''
        price_str = str(price_raw)
        # 이미 "원"이 포함되어 있으면 그대로
        if '원' in price_str:
            return price_str
        # 숫자만 추출해서 포맷
        digits = re.sub(r'[^\d]', '', price_str)
        if digits:
            return f"{int(digits):,}원"
        return price_str

    def _quit_logging_driver(self, driver):
        """로깅 드라이버 정리."""
        try:
            driver.quit()
        except Exception:
            pass

    def close(self):
        """드라이버 정리."""
        if self._driver:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None

    # ── 기존 상품 상세 스크래핑 ──

    def _create_driver(self, enable_logging=False):
        """봇 감지 우회 설정이 적용된 Chrome 드라이버 생성.

        Args:
            enable_logging: True면 Performance logging 활성화 (골드박스용)
        """
        options = Options()
        if self.headless:
            options.add_argument('--headless=new')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_argument('--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                             'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option('useAutomationExtension', False)

        if enable_logging:
            options.set_capability('goog:loggingPrefs', {'performance': 'ALL'})

        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)

        # navigator.webdriver 속성 제거
        driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
            'source': 'Object.defineProperty(navigator, "webdriver", {get: () => undefined})'
        })

        return driver

    def scrape_product(self, product_url):
        """쿠팡 제품 상세 페이지 스크래핑.

        Returns:
            dict: description, features, specs, image_urls

        Raises:
            ValueError: URL이 허용된 도메인이 아닌 경우
        """
        # [FIX #5] URL 검증
        self._validate_url(product_url, ALLOWED_SCRAPE_DOMAINS)

        driver = self._create_driver()
        result = {
            'description': '',
            'features': [],
            'specs': {},
            'image_urls': [],
        }

        try:
            driver.get(product_url)
            time.sleep(3)

            # 페이지 로드 대기
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )

            html = driver.page_source
            soup = BeautifulSoup(html, 'html.parser')

            # 1. 제품명 (폴백)
            result['title'] = self._extract_title(soup)

            # 2. 제품 설명
            result['description'] = self._extract_description(soup)

            # 3. 특징 목록
            result['features'] = self._extract_features(soup)

            # 4. 스펙 정보
            result['specs'] = self._extract_specs(soup)

            # 5. 이미지 URL
            result['image_urls'] = self._extract_images(soup)

        except Exception as e:
            result['error'] = str(e)
        finally:
            driver.quit()

        return result

    def _extract_title(self, soup):
        """제품명 추출."""
        # 셀렉터 우선순위
        selectors = [
            'h1.prod-buy-header__title',
            'h2.prod-buy-header__title',
            '.prod-buy-header__title',
        ]
        for sel in selectors:
            el = soup.select_one(sel)
            if el:
                return el.get_text(strip=True)

        # OpenGraph 폴백
        og = soup.find('meta', property='og:title')
        if og and og.get('content'):
            return og['content']

        return ''

    def _extract_description(self, soup):
        """제품 설명 추출."""
        selectors = [
            '.prod-description',
            '.product-detail-content-inside',
            '#productDescription',
        ]
        for sel in selectors:
            el = soup.select_one(sel)
            if el:
                return el.get_text(strip=True)[:2000]

        # OpenGraph 폴백
        og = soup.find('meta', property='og:description')
        if og and og.get('content'):
            return og['content']

        return ''

    def _extract_features(self, soup):
        """특징 목록 추출."""
        features = []

        # 상품 속성 테이블
        attr_table = soup.select('.prod-attr-item')
        for item in attr_table:
            dt = item.select_one('dt')
            dd = item.select_one('dd')
            if dt and dd:
                features.append(f"{dt.get_text(strip=True)}: {dd.get_text(strip=True)}")

        # 상품 설명에서 리스트 추출
        if not features:
            ul_elements = soup.select('.prod-description ul li, .product-detail-content-inside ul li')
            for li in ul_elements[:10]:
                text = li.get_text(strip=True)
                if text:
                    features.append(text)

        return features

    def _extract_specs(self, soup):
        """스펙 정보(테이블) 추출."""
        specs = {}
        spec_tables = soup.select('.prod-spec-table tr, .prod-attr table tr')
        for tr in spec_tables:
            th = tr.select_one('th')
            td = tr.select_one('td')
            if th and td:
                key = th.get_text(strip=True)
                value = td.get_text(strip=True)
                if key and value:
                    specs[key] = value
        return specs

    def _extract_images(self, soup):
        """제품 이미지 URL 추출."""
        images = []
        seen = set()

        # 메인 이미지
        selectors = [
            '.prod-image__detail img',
            '.prod-image img',
            '.vendor-item__img img',
        ]
        for sel in selectors:
            for img in soup.select(sel):
                src = img.get('src') or img.get('data-src', '')
                if src and src not in seen and not src.endswith('.gif'):
                    # 고화질로 변환
                    src = re.sub(r'/thumbnails/remote/\d+x\d+(?:ex)?/', '/thumbnails/remote/1000x1000ex/', src)
                    if not src.startswith('http'):
                        src = 'https:' + src
                    images.append(src)
                    seen.add(src)

        # OpenGraph 이미지 폴백
        if not images:
            og = soup.find('meta', property='og:image')
            if og and og.get('content'):
                images.append(og['content'])

        return images[:5]
