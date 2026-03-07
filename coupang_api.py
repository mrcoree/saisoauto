import hmac
import hashlib
import time
import datetime
import requests
from urllib.parse import urlencode

from config import Config

DOMAIN = "https://api-gateway.coupang.com"


class CoupangAPI:
    def __init__(self, access_key, secret_key):
        self.access_key = access_key
        self.secret_key = secret_key

    def _generate_hmac(self, method, url_path, query_string=""):
        """HMAC-SHA256 인증 헤더 생성."""
        datetime_now = datetime.datetime.utcnow().strftime('%y%m%dT%H%M%SZ')

        message = datetime_now + method + url_path + query_string
        signature = hmac.HMAC(
            self.secret_key.encode('utf-8'),
            message.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

        return f"CEA algorithm=HmacSHA256, access-key={self.access_key}, signed-date={datetime_now}, signature={signature}"

    def search_products(self, keyword, limit=10):
        """쿠팡 파트너스 API로 상품 검색.

        Args:
            keyword: 검색 키워드
            limit: 결과 수 (최대 10)

        Returns:
            list of dict: 상품 목록
        """
        method = "GET"
        path = "/v2/providers/affiliate_open_api/apis/openapi/v1/products/search"
        params = {
            "keyword": keyword,
            "limit": min(limit, 10),
        }
        query_string = urlencode(params)
        url = f"{DOMAIN}{path}?{query_string}"

        authorization = self._generate_hmac(method, path, query_string)
        headers = {
            "Authorization": authorization,
            "Content-Type": "application/json;charset=UTF-8",
        }

        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()

        if data.get('rCode') != '0':
            raise Exception(f"쿠팡 API 오류: {data.get('rMessage', '알 수 없는 오류')}")

        return self._parse_product_items(data.get('data', {}).get('productData', []))

    def _parse_product_items(self, items):
        """공통 상품 파싱 헬퍼."""
        products = []
        for item in items:
            products.append({
                'product_name': item.get('productName', ''),
                'product_url': item.get('productUrl', ''),
                'affiliate_url': item.get('productUrl', ''),
                'price': self._format_price(item.get('productPrice', 0)),
                'thumbnail_url': item.get('productImage', ''),
                'category': item.get('categoryName', ''),
                'is_rocket': item.get('isRocket', False),
                'is_free_shipping': item.get('isFreeShipping', False),
            })
        return products

    def get_goldbox(self, limit=10):
        """골드박스(오늘의 특가) 상품 조회."""
        method = "GET"
        path = "/v2/providers/affiliate_open_api/apis/openapi/v1/products/goldbox"
        params = {"limit": min(limit, 10)}
        query_string = urlencode(params)
        url = f"{DOMAIN}{path}?{query_string}"

        authorization = self._generate_hmac(method, path, query_string)
        headers = {
            "Authorization": authorization,
            "Content-Type": "application/json;charset=UTF-8",
        }

        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()

        if data.get('rCode') != '0':
            raise Exception(f"골드박스 API 오류: {data.get('rMessage', '알 수 없는 오류')}")

        raw = data.get('data', [])
        items = raw.get('productData', []) if isinstance(raw, dict) else raw
        return self._parse_product_items(items)

    def get_best_categories(self, category_id, limit=10):
        """카테고리별 베스트 상품 조회."""
        method = "GET"
        path = f"/v2/providers/affiliate_open_api/apis/openapi/v1/products/bestcategories/{category_id}"
        params = {"limit": min(limit, 10)}
        query_string = urlencode(params)
        url = f"{DOMAIN}{path}?{query_string}"

        authorization = self._generate_hmac(method, path, query_string)
        headers = {
            "Authorization": authorization,
            "Content-Type": "application/json;charset=UTF-8",
        }

        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()

        if data.get('rCode') != '0':
            raise Exception(f"베스트 카테고리 API 오류: {data.get('rMessage', '알 수 없는 오류')}")

        raw = data.get('data', [])
        items = raw.get('productData', []) if isinstance(raw, dict) else raw
        return self._parse_product_items(items)

    def get_deeplink(self, original_urls):
        """딥링크 생성 API.

        Args:
            original_urls: URL 목록 (list of str)

        Returns:
            list of dict: 딥링크 정보
        """
        method = "POST"
        path = "/v2/providers/affiliate_open_api/apis/openapi/v1/deeplink"
        url = f"{DOMAIN}{path}"

        authorization = self._generate_hmac(method, path)
        headers = {
            "Authorization": authorization,
            "Content-Type": "application/json;charset=UTF-8",
        }

        body = {
            "coupangUrls": original_urls
        }

        response = requests.post(url, headers=headers, json=body, timeout=10)
        response.raise_for_status()
        data = response.json()

        if data.get('rCode') != '0':
            raise Exception(f"딥링크 생성 오류: {data.get('rMessage', '알 수 없는 오류')}")

        return data.get('data', [])

    def test_connection(self):
        """API 연결 테스트 (간단한 검색으로 확인)."""
        try:
            results = self.search_products("테스트", limit=1)
            return True, f"연결 성공 (검색 결과 {len(results)}건)"
        except Exception as e:
            return False, str(e)

    @staticmethod
    def _format_price(price):
        """가격을 한국 원화 형식으로 변환."""
        try:
            return f"{int(price):,}원"
        except (ValueError, TypeError):
            return str(price)
