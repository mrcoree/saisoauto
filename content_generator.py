import json
import time
import logging
from openai import OpenAI

from config import Config

logger = logging.getLogger(__name__)


class ContentGenerator:
    def __init__(self, api_key):
        self.client = OpenAI(api_key=api_key) if api_key else None

    def generate_review(self, product_info, max_retries=3):
        """제품 정보를 바탕으로 리뷰 글 생성.

        Args:
            product_info: dict with keys:
                - product_name, price, affiliate_url
                - description, features, specs (from scraper)
            max_retries: 최대 재시도 횟수

        Returns:
            dict: title, content (HTML), excerpt, tags
        """
        prompt = self._build_prompt(product_info)

        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                response = self.client.chat.completions.create(
                    model="gpt-5.2",
                    messages=[
                        {"role": "system", "content": self._system_prompt()},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.7,
                    response_format={"type": "json_object"},
                )

                if not response.choices:
                    raise ValueError("OpenAI 응답에 choices가 없습니다")

                raw = response.choices[0].message.content
                if not raw:
                    raise ValueError("OpenAI 응답 content가 비어있습니다")

                try:
                    result = json.loads(raw)
                except json.JSONDecodeError as e:
                    logger.error(f"JSON 파싱 실패 (시도 {attempt}/{max_retries}): {raw[:200]}")
                    raise ValueError(f"JSON 파싱 실패: {e}") from e

                # 필수 키 검증
                if 'content' not in result:
                    raise ValueError(f"응답에 'content' 키 없음: {list(result.keys())}")

                return result

            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    wait = 2 ** attempt  # 2, 4, 8초
                    logger.warning(
                        f"글 생성 실패 (시도 {attempt}/{max_retries}), "
                        f"{wait}초 후 재시도: {e}"
                    )
                    time.sleep(wait)

        raise last_error

    def _system_prompt(self):
        return """당신은 한국어 제품 리뷰 전문 블로거입니다.
주어진 제품 정보를 바탕으로 SEO에 최적화된 자연스러운 한국어 리뷰 글을 작성합니다.

반드시 아래 JSON 형식으로 응답하세요:
{
    "title": "SEO 최적화된 글 제목",
    "content": "HTML 형식의 본문",
    "excerpt": "요약문 (2-3문장)",
    "tags": ["태그1", "태그2", "태그3"]
}

글 본문(content)은 HTML 형식이며 다음 구조를 따릅니다:
1. 도입부 (자연스러운 시작) + {{IMAGE_1}} 플레이스홀더
2. {{CTA_BUTTON}} 플레이스홀더 (첫 번째 구매 유도 버튼)
3. 장점 분석 + {{IMAGE_2}} 플레이스홀더
4. 단점 또는 아쉬운 점
5. 추천 대상 + {{IMAGE_3}} 플레이스홀더
6. 제품 주요 스펙/특징을 <table> 태그로 정리 (요약 정리)
7. {{CTA_BUTTON}} 플레이스홀더 (두 번째 구매 유도 버튼)
8. 쿠팡 파트너스 필수 고지문

절대 지켜야 할 규칙:
- {{IMAGE_1}}, {{IMAGE_2}}, {{IMAGE_3}} 플레이스홀더를 반드시 본문에 배치할 것. 정확히 이 문자열을 사용할 것.
- {{CTA_BUTTON}} 플레이스홀더를 글 하단에 반드시 배치할 것. 정확히 이 문자열을 사용할 것.
- <a> 태그, href 속성, URL 링크를 본문에 절대 포함하지 말 것. 모든 링크는 시스템이 플레이스홀더를 통해 자동 생성합니다.
- <img> 태그를 본문에 직접 넣지 말 것. 이미지도 시스템이 플레이스홀더를 통해 자동 삽입합니다.
- <table>은 깔끔한 HTML 테이블로 작성
- 마지막에 쿠팡 파트너스 고지문 추가: "이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다."
- 자연스럽고 신뢰감 있는 톤"""

    def _build_prompt(self, info):
        parts = [f"제품명: {info.get('product_name', '')}"]

        if info.get('price'):
            parts.append(f"가격: {info['price']}")

        if info.get('description'):
            parts.append(f"제품 설명: {info['description'][:1000]}")

        if info.get('features'):
            features_text = '\n'.join(f"- {f}" for f in info['features'][:10])
            parts.append(f"주요 특징:\n{features_text}")

        if info.get('specs'):
            specs_text = '\n'.join(f"- {k}: {v}" for k, v in list(info['specs'].items())[:15])
            parts.append(f"스펙 정보:\n{specs_text}")

        parts.append("\n위 정보를 바탕으로 상세한 제품 리뷰 글을 작성해주세요.")

        return '\n\n'.join(parts)
