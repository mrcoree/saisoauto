import os
import re
import logging
import random
import requests
from requests.auth import HTTPBasicAuth

from config import Config

logger = logging.getLogger(__name__)


class WordPressPublisher:
    def __init__(self, wp_url, wp_username, wp_app_password):
        self.base_url = wp_url.rstrip('/') if wp_url else ''
        self.auth = HTTPBasicAuth(wp_username, wp_app_password) if wp_username and wp_app_password else None
        self.api_url = f"{self.base_url}/wp-json/wp/v2" if self.base_url else ""
        # [FIX #9] HTTPS 미사용 시 경고
        if self.base_url and not self.base_url.startswith('https://'):
            logger.warning(
                "⚠️  WP_URL이 HTTPS가 아닙니다. 자격증명이 평문으로 전송됩니다: %s",
                self.base_url,
            )

    def test_connection(self):
        """워드프레스 REST API 연결 테스트."""
        try:
            resp = requests.get(
                f"{self.api_url}/users/me",
                auth=self.auth,
                timeout=10,
            )
            if resp.status_code == 200:
                user = resp.json()
                return True, f"연결 성공 ({user.get('name', 'Unknown')})"
            else:
                return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
        except Exception as e:
            return False, str(e)

    def upload_image(self, file_path, alt_text=""):
        """이미지를 워드프레스 미디어 라이브러리에 업로드.

        Returns:
            dict: id, source_url (업로드된 이미지 정보)
        """
        filename = os.path.basename(file_path)
        mime_type = 'image/png' if filename.endswith('.png') else 'image/jpeg'

        with open(file_path, 'rb') as f:
            resp = requests.post(
                f"{self.api_url}/media",
                auth=self.auth,
                headers={
                    'Content-Disposition': f'attachment; filename="{filename}"',
                    'Content-Type': mime_type,
                },
                data=f.read(),
                timeout=30,
            )

        resp.raise_for_status()
        media = resp.json()

        # alt 텍스트 설정
        if alt_text:
            requests.post(
                f"{self.api_url}/media/{media['id']}",
                auth=self.auth,
                json={'alt_text': alt_text},
                timeout=10,
            )

        return {
            'id': media['id'],
            'source_url': media.get('source_url', ''),
        }

    def create_post(self, title, content, excerpt="", tags=None,
                    category_name=None, featured_image_id=None, status="draft"):
        """워드프레스 포스트 생성.

        Args:
            title: 글 제목
            content: HTML 본문
            excerpt: 요약
            tags: 태그 이름 목록
            category_name: 카테고리 이름 (없으면 카테고리 미설정)
            featured_image_id: 대표 이미지 미디어 ID
            status: draft 또는 publish

        Returns:
            dict: id, link (생성된 포스트 정보)
        """
        post_data = {
            'title': title,
            'content': content,
            'excerpt': excerpt,
            'status': status,
        }

        if featured_image_id:
            post_data['featured_media'] = featured_image_id

        # 카테고리 처리
        if category_name:
            cat_id = self._get_or_create_category(category_name)
            if cat_id:
                post_data['categories'] = [cat_id]

        # 태그 처리 (이름 → ID)
        if tags:
            tag_ids = self._get_or_create_tags(tags)
            if tag_ids:
                post_data['tags'] = tag_ids

        resp = requests.post(
            f"{self.api_url}/posts",
            auth=self.auth,
            json=post_data,
            timeout=30,
        )
        resp.raise_for_status()
        post = resp.json()

        return {
            'id': post['id'],
            'link': post.get('link', ''),
        }

    def _get_or_create_category(self, name):
        """카테고리 이름으로 ID 조회, 없으면 생성."""
        resp = requests.get(
            f"{self.api_url}/categories",
            auth=self.auth,
            params={'search': name},
            timeout=10,
        )
        if resp.status_code == 200:
            for cat in resp.json():
                if cat['name'] == name:
                    return cat['id']

        # 없으면 생성
        resp = requests.post(
            f"{self.api_url}/categories",
            auth=self.auth,
            json={'name': name},
            timeout=10,
        )
        if resp.status_code == 201:
            return resp.json()['id']

        logger.warning(f"카테고리 '{name}' 생성 실패: {resp.status_code}")
        return None

    def _get_or_create_tags(self, tag_names):
        """태그 이름으로 ID 조회, 없으면 생성. (일괄 조회 최적화)"""
        tag_names = tag_names[:10]
        if not tag_names:
            return []

        # 1단계: 한 번에 모든 태그 검색 (콤마 구분으로 일괄 조회)
        existing_tags = {}
        try:
            resp = requests.get(
                f"{self.api_url}/tags",
                auth=self.auth,
                params={'per_page': 100, 'search': ','.join(tag_names)},
                timeout=10,
            )
            if resp.status_code == 200:
                for tag in resp.json():
                    existing_tags[tag['name'].lower()] = tag['id']
        except Exception:
            pass

        # 2단계: 매칭되지 않은 태그만 생성
        tag_ids = []
        for name in tag_names:
            existing_id = existing_tags.get(name.lower())
            if existing_id:
                tag_ids.append(existing_id)
            else:
                try:
                    resp = requests.post(
                        f"{self.api_url}/tags",
                        auth=self.auth,
                        json={'name': name},
                        timeout=10,
                    )
                    if resp.status_code == 201:
                        tag_ids.append(resp.json()['id'])
                except Exception:
                    pass

        return tag_ids

    def publish_article(self, title, content, excerpt="", tags=None,
                        image_paths=None, affiliate_url="", status="draft"):
        """이미지 업로드 → 플레이스홀더 교체 → 글 발행 (전체 파이프라인).

        Args:
            image_paths: 이미지 파일 경로 리스트
            affiliate_url: 어필리에이트 링크 (CTA 버튼 및 이미지 링크에 사용)

        Returns:
            dict: post_id, post_url
        """
        uploaded_images = []
        featured_image_id = None

        # 1. 이미지 업로드
        if image_paths:
            for i, path in enumerate(image_paths):
                if os.path.exists(path):
                    alt = f"{title} 이미지 {i+1}"
                    media = self.upload_image(path, alt_text=alt)
                    uploaded_images.append(media)

                    # 첫 번째 이미지를 대표 이미지로
                    if i == 0:
                        featured_image_id = media['id']

        # 2. GPT가 직접 넣은 <a>, <img> 태그 먼저 제거
        content = re.sub(r'<a\s[^>]*href=["\'][^"\']*["\'][^>]*>(.*?)</a>', r'\1', content)
        content = re.sub(r'<img\s[^>]*/?\s*>', '', content)

        # 3. 이미지 플레이스홀더 교체 (어필리에이트 링크 포함)
        for i, media in enumerate(uploaded_images):
            placeholder = f"{{{{IMAGE_{i+1}}}}}"
            img_html = f'<img src="{media["source_url"]}" alt="{title} 이미지 {i+1}" />'
            if affiliate_url:
                img_tag = (
                    f'<figure class="wp-block-image size-large">'
                    f'<a href="{affiliate_url}" target="_blank" rel="nofollow sponsored">'
                    f'{img_html}</a></figure>'
                )
            else:
                img_tag = f'<figure class="wp-block-image size-large">{img_html}</figure>'
            content = content.replace(placeholder, img_tag)

        # 남은 이미지 플레이스홀더 제거
        content = re.sub(r'\{\{IMAGE_\d+\}\}', '', content)

        # 4. CTA 버튼 생성 (인라인 스타일 — 워드프레스에 CSS 없으므로)
        if affiliate_url:
            cta_texts = [
                '최저가 확인하기', '지금 바로 확인하기', '할인가 보러가기',
                '쿠팡에서 확인하기', '상품 자세히 보기',
            ]
            cta_text = random.choice(cta_texts)
            cta_button = (
                f'<div style="text-align:center;margin:30px 0;">'
                f'<a href="{affiliate_url}" target="_blank" rel="nofollow sponsored" '
                f'style="display:inline-block;padding:16px 48px;background-color:#e94560;'
                f'color:#ffffff;font-size:18px;font-weight:700;border-radius:8px;'
                f'text-decoration:none;">'
                f'{cta_text}</a></div>'
            )

            # 플레이스홀더가 있으면 교체, 없으면 고지문 앞에 삽입
            if '{{CTA_BUTTON}}' in content:
                content = content.replace('{{CTA_BUTTON}}', cta_button)
            else:
                notice = '이 포스팅은 쿠팡 파트너스'
                idx = content.find(notice)
                if idx > 0:
                    content = content[:idx] + cta_button + '\n' + content[idx:]
                else:
                    content += '\n' + cta_button

        # 남은 CTA 플레이스홀더 제거
        content = content.replace('{{CTA_BUTTON}}', '')

        # 4. 글 발행 (카테고리: 제품비교)
        post = self.create_post(
            title=title,
            content=content,
            excerpt=excerpt,
            tags=tags,
            category_name='제품Tip',
            featured_image_id=featured_image_id,
            status=status,
        )

        return {
            'post_id': post['id'],
            'post_url': post['link'],
        }
