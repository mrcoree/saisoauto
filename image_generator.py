import os
import logging
import tempfile
import requests
from PIL import Image, ImageDraw, ImageFont
import io

from google import genai
from google.genai import types

from config import Config

logger = logging.getLogger(__name__)

# [FIX #5] 이미지 다운로드 허용 도메인 (쿠팡 CDN)
ALLOWED_IMAGE_HOSTS = {
    'coupangcdn.com', 'image.coupangcdn.com',
    'thumbnail6.coupangcdn.com', 'thumbnail7.coupangcdn.com',
    'thumbnail8.coupangcdn.com', 'thumbnail9.coupangcdn.com',
    'thumbnail10.coupangcdn.com',
}

# ── 제품 카테고리별 라이프스타일 장면 프롬프트 ──
PRODUCT_CATEGORIES = {
    '뷰티/스킨케어': {
        'keywords': ['클렌징', '파운데이션', '아이라이너', '립스틱', '마스크팩', '선크림', '에센스', '앰플', 
                     '세럼', '토너', '로션', '크림', '오일', '워시', '미스트', '샴푸', '컨디셔너', '바디워시', '핸드크림', '화장품', '스킨'],
        'scenes': [
            "{name} placed elegantly on a clean bathroom counter next to a folded white towel, soft morning light",
            "a styled flat-lay of {name} surrounded by soft floral elements on a marble vanity, high-end aesthetic",
            "a person's hand gently reaching for {name} on an elegant bathroom vanity, creating a warm, personal atmosphere",
        ],
    },
    '반려동물': {
        'keywords': ['배변패드', '훈련패드', '캣타워', '하네스', '사료', '간식', '목줄', '펫패드',
                     '강아지', '고양이', '반려', '애완', '펫'],
        'scenes': [
            "a cute small puppy sitting happily on {name} in a clean, modern living room, warm lighting",
            "a pet owner smiling while placing {name} on the floor, a small dog watching curiously nearby",
        ],
    },
    '체중/건강': {
        'keywords': ['체중계', '혈압계', '체온계', '산소포화도', '혈당', '안마', '마사지', '폼롤러', 
                     '체지방', '스트레칭', '근육', '건강'],
        'scenes': [
            "a person standing barefoot on {name} in a clean modern bathroom, checking their weight",
            "a healthy lifestyle flat-lay with {name}, a water bottle, and fresh fruits on a wooden floor",
        ],
    },
    '주방/식품': {
        'keywords': ['밀키트', '에어프라이어', '전자레인지', '믹서기', '프라이팬', '텀블러', '보온병', '밥솥', 
                     '냄비', '도마', '칼', '두유', '커피', '우유', '음료', '차', '식품', '간편식', '조리'],
        'scenes': [
            "a cozy breakfast table setting with {name} placed naturally next to a plate and a coffee cup, morning light",
            "{name} sitting beautifully on a modern kitchen counter with fresh ingredients subtly in the background",
            "a person's hand interacting with {name} while preparing a meal in a bright, modern kitchen scenery",
        ],
    },
    '가전/디지털': {
        # 일반 명사(기기 종류) 위주로 우선 매칭 유도 (브랜드명 배제)
        'keywords': ['노트북', '태블릿', '모니터', '키보드', '마우스', '이어폰', '헤드폰', '스피커', 
                     '스마트폰', '휴대폰', '보조배터리', '충전기', '케이블', '허브', '블루투스'],
        'scenes': [
            "{name} resting elegantly on a minimalist desk setup next to a closed laptop and a coffee mug",
            "a styled flat-lay of {name} on a clean white desk alongside a modern notepad and a pen",
            "a person's hand pressing or adjusting {name} on a stylish modern desk, capturing a realistic working moment",
        ],
    },
    '가구/인테리어': {
        'keywords': ['매트리스', '서랍장', '테이블', '수납장', '행거', '소파', '침대', '의자', '책상', '선반', 
                     '커튼', '조명', '쿠션', '러그', '화분', '수납'],
        'scenes': [
            "{name} beautifully arranged in a modern Scandinavian-style living room, natural daylight",
            "{name} positioned perfectly in a cozy, warm-toned home interior setup",
        ],
    },
    '생활용품': {
        'keywords': ['옷걸이', '물티슈', '방향제', '탈취제', '쓰레기통', '정리함', '바구니', 
                     '세제', '수건', '빨래', '걸레', '세탁', '청소', '휴지'],
        'scenes': [
            "{name} placed naturally in a neatly organized, bright closet or laundry room",
            "{name} arranged on a clean shelf in a well-lit, organized home setting",
            "a person's hand comfortably holding or arranging {name} in a sunlit, tidy living space",
        ],
    },
    '유아/아동': {
        'keywords': ['실내화', '기저귀', '놀이매트', '유모차', '카시트', '이유식', '젖병', '장난감', 
                     '유아', '아동', '아기', '어린이'],
        'scenes': [
            "{name} placed on a soft, colorful play mat in a bright and safe children's room",
            "{name} sitting next to cute wooden toys on a clean nursery floor, warm lighting",
        ],
    },
    '패션': {
        'keywords': ['원피스', '선글라스', '스카프', '넥타이', '운동화', '시계', '지갑', '벨트', '가방',
                     '모자', '양말', '코트', '자켓', '티셔츠', '구두', '의류', '신발'],
        'scenes': [
            "{name} styled elegantly on a wooden chair in a sunlit boutique setting",
            "a styled outfit flat-lay featuring {name} alongside complementary accessories on a marble surface",
        ],
    },
    '운동/레저': {
        'keywords': ['요가매트', '자전거', '운동화', '침낭', '텐트', '덤벨', '러닝', '등산', '캠핑', 
                     '수영', '헬스', '스포츠', '트레이닝', '운동복'],
        'scenes': [
            "{name} placed on the floor of a modern home gym with a water bottle and towel nearby",
            "{name} resting naturally on a wooden bench in an outdoor park setting, motivational mood",
        ],
    },
    '정수/필터': {
        'keywords': ['샤워필터', '브리타', '연수기', '정수기', '필터', '정수'],
        'scenes': [
            "{name} sitting on a pristine kitchen counter next to a glass of crystal clear water",
            "a bright, clean modern kitchen setting featuring {name} prominently on the countertop",
        ],
    },
    '자동차': {
        'keywords': ['블랙박스', '대시보드', '방향제', '트렁크', '시트', '핸들', '차량용', '자동차', 
                     '세차', '코팅'],
        'scenes': [
            "{name} placed perfectly inside a modern car interior, clean and premium look",
            "{name} resting on a premium leather car seat with natural sunlight coming through the window",
            "a driver's hand adjusting {name} inside a premium vehicle interior, realistic commuting scene",
        ],
    },
}


class ImageGenerator:
    """쿠팡 원본 이미지를 참조하여 Gemini로 제품 이미지 생성. 실패 시 원본 폴백."""

    def __init__(self):
        self.client = None
        if Config.GEMINI_API_KEY:
            self.client = genai.Client(api_key=Config.GEMINI_API_KEY)

    def generate_images(self, product_info, count=3):
        """제품 이미지 생성.

        1. 쿠팡 원본 이미지 다운로드
        2. 원본을 참조 이미지로 Gemini에 전달하여 새 이미지 생성
        3. Gemini 실패 시 원본 이미지 사용

        1번 이미지(특성이미지): 제품명 텍스트 하단에 추가

        Returns:
            list of str: 이미지 파일 경로들
        """
        product_name = product_info.get('product_name', '')
        image_urls = product_info.get('image_urls', [])

        # thumbnail_url 폴백: image_urls가 비어있으면 thumbnail_url 사용
        if not image_urls and product_info.get('thumbnail_url'):
            image_urls = [product_info['thumbnail_url']]

        # 1단계: 쿠팡 원본 이미지 다운로드
        original_images = []
        for url in image_urls[:count]:
            try:
                img = self._download_image(url)
                if img:
                    original_images.append(img)
            except Exception as e:
                logger.warning(f"  원본 이미지 다운로드 실패: {e}")

        if not original_images:
            logger.warning("  사용 가능한 제품 이미지 없음")
            # Gemini 텍스트 전용 생성 시도
            if self.client:
                return self._generate_text_only(product_info, count)
            return []

        # 2단계: 각 이미지에 대해 Gemini로 향상 시도, 실패 시 원본 사용
        image_paths = []
        prompts = self._build_prompts(product_info, count)
        # 참조 이미지는 첫 번째 원본 사용
        ref_image = original_images[0]

        for i in range(count):
            img = None

            # Gemini 시도
            if self.client and i < len(prompts):
                try:
                    img = self._generate_with_gemini(ref_image, prompts[i])
                    if img:
                        logger.info(f"  Gemini 이미지 {i+1}/{count} 생성 완료")
                except Exception as e:
                    logger.warning(f"  Gemini 이미지 {i+1}/{count} 실패: {e}")

            # Gemini 실패 시 원본 사용
            if not img:
                if i < len(original_images):
                    img = original_images[i].copy()
                    logger.info(f"  원본 이미지 {i+1}/{count} 사용")
                else:
                    continue

            # 첫 번째 이미지: 특성이미지 → 제품명 추가
            if i == 0 and product_name:
                img = self._add_title_bar(img, product_name)

            path = self._save_image(img, i)
            image_paths.append(path)

        return image_paths

    def _detect_category(self, product_name):
        """제품명 키워드로 카테고리 및 핵심 키워드 감지.

        가장 많이 매칭된 카테고리를 찾고, 
        그 카테고리의 키워드 배열 중 가장 처음으로 매칭된 (즉, 가장 구체적인 지정) 
        단어를 핵심 키워드(core keyword)로 반환.
        """
        name_lower = product_name.lower()
        best_category = None
        best_score = 0
        best_keyword = None

        for category, info in PRODUCT_CATEGORIES.items():
            matches = [kw for kw in info['keywords'] if kw.lower() in name_lower]
            if matches:
                score = len(matches)
                if score > best_score:
                    best_score = score
                    best_category = category
                    # 리스트에서 우선순위가 가장 높은 첫 번째 매칭 단어
                    best_keyword = matches[0]

        return best_category, best_keyword

    def _build_prompts(self, product_info, count):
        """제품 카테고리에 맞는 맞춤형 프롬프트 생성.

        긴 전체 제품명을 그대로 쓰지 않고 핵심 명사(예: '에어팟')를 추출하여 
        Gemini가 형태를 헷갈리지 않게 유도.
        """
        full_name = product_info.get('product_name', '제품')
        category, core_kw = self._detect_category(full_name)
        
        # 전체 길이의 텍스트가 아니라 추출된 핵심 키워드만 사용 (없으면 원래 값 유지)
        prompt_subject = core_kw if core_kw else full_name

        # 카테고리별 라이프스타일 장면 선택
        if category and category in PRODUCT_CATEGORIES:
            import random
            scenes = PRODUCT_CATEGORIES[category]['scenes']
            scene_desc = random.choice(scenes).format(name=prompt_subject)
            logger.info(f"  카테고리 감지: '{category}' → 핵심 키워드: '{prompt_subject}'")
        else:
            scene_desc = (
                f"{prompt_subject} being used or displayed in a realistic home environment, "
                "warm natural lighting, lifestyle concept"
            )
            logger.info(f"  카테고리 미감지 → 핵심 키워드: '{prompt_subject}' (일반)")

        prompts = [
            # 1번: 메인 제품 사진 (깔끔한 배경)
            (
                f"Based on this product image, create a professional product photo of {prompt_subject}. "
                "Keep the exact same product appearance, shape, color, and design. "
                "Clean white background, studio lighting, centered composition. "
                "NO text, NO labels, NO watermarks anywhere in the image."
            ),
            # 2번: 디테일 클로즈업
            (
                f"Based on this product image, create a close-up detail shot of {prompt_subject}. "
                "Show the same product from a slightly different angle, focusing on texture and quality details. "
                "Keep the exact same product appearance and design. "
                "Soft gradient background. NO text, NO labels, NO watermarks."
            ),
            # 3번: 카테고리별 맞춤 라이프스타일
            (
                f"Based on this product image, create a realistic lifestyle photograph showing "
                f"{scene_desc}. "
                f"The product {prompt_subject} must look exactly like the reference image. "
                "Photorealistic, high quality, natural lighting. "
                "NO text, NO labels, NO watermarks."
            ),
        ]

        return prompts[:count]

    def _generate_with_gemini(self, ref_image, prompt):
        """원본 이미지를 참조로 Gemini API에서 이미지 생성."""
        # PIL Image → bytes 변환
        buf = io.BytesIO()
        ref_image.save(buf, format='PNG')
        image_bytes = buf.getvalue()

        contents = [
            types.Content(
                role="user",
                parts=[
                    types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
                    types.Part.from_text(text=prompt),
                ],
            ),
        ]

        response = self.client.models.generate_content(
            model="gemini-3.1-flash-image-preview",
            contents=contents,
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE", "TEXT"],
                image_config=types.ImageConfig(
                    image_size="1K",
                    aspect_ratio="16:9",
                ),
            ),
        )

        if not response.parts:
            return None

        for part in response.parts:
            if part.inline_data and part.inline_data.data:
                img = Image.open(io.BytesIO(part.inline_data.data)).convert('RGB')
                return img

        return None

    def _generate_text_only(self, product_info, count):
        """참조 이미지 없이 텍스트만으로 Gemini 이미지 생성 (최후 폴백)."""
        name = product_info.get('product_name', '제품')
        product_name = product_info.get('product_name', '')
        image_paths = []

        prompts = [
            f"A professional product photo of {name}. Clean white background, studio lighting. NO text, NO watermarks.",
            f"A close-up detail shot of {name}. Soft gradient background. NO text, NO watermarks.",
            f"A lifestyle photo of {name} in a home setting. Warm lighting. NO text, NO watermarks.",
        ]

        for i, prompt in enumerate(prompts[:count]):
            try:
                response = self.client.models.generate_content(
                    model="gemini-3.1-flash-image-preview",
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_modalities=["IMAGE", "TEXT"],
                        image_config=types.ImageConfig(
                            image_size="1K",
                            aspect_ratio="16:9",
                        ),
                    ),
                )
                if response.parts:
                    for part in response.parts:
                        if part.inline_data and part.inline_data.data:
                            img = Image.open(io.BytesIO(part.inline_data.data)).convert('RGB')
                            if i == 0 and product_name:
                                img = self._add_title_bar(img, product_name)
                            path = self._save_image(img, i)
                            image_paths.append(path)
                            logger.info(f"  Gemini 텍스트 전용 이미지 {i+1}/{count} 생성 완료")
                            break
            except Exception as e:
                logger.warning(f"  Gemini 텍스트 전용 이미지 {i+1}/{count} 실패: {e}")

        return image_paths

    def _download_image(self, url):
        """URL에서 이미지 다운로드."""
        # [FIX #5] URL 검증
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            logger.warning(f"  허용되지 않는 이미지 URL scheme: {parsed.scheme}")
            return None
        host = parsed.netloc.lower().split(':')[0]
        if not any(host == d or host.endswith('.' + d) for d in ALLOWED_IMAGE_HOSTS):
            logger.warning(f"  허용되지 않는 이미지 호스트: {host}")
            return None

        resp = requests.get(url, timeout=15, headers={
            'User-Agent': 'Mozilla/5.0',
            'Referer': 'https://www.coupang.com/',
        })
        if resp.status_code == 200:
            return Image.open(io.BytesIO(resp.content)).convert('RGB')
        return None

    def _add_title_bar(self, img, text):
        """이미지 하단에 반투명 배경 + 제품명 텍스트(멀티라인 자동 줄바꿈) 추가."""
        width, height = img.size
        # 기본 폰트 크기 및 높이 설정
        base_bar_height = max(60, int(height * 0.13))
        font = self._get_font(base_bar_height)
        max_text_width = width * 0.9  # 좌우 여백 확보를 위해 90% 제한

        draw_measure = ImageDraw.Draw(img)

        # 1. 텍스트 줄바꿈 계산
        lines = []
        words = text.split()
        current_line = []

        for word in words:
            test_line = " ".join(current_line + [word])
            bbox = draw_measure.textbbox((0, 0), test_line, font=font)
            if bbox[2] - bbox[0] <= max_text_width:
                current_line.append(word)
            else:
                if not current_line:  # 단어 하나가 전체 너비보다 긴 경우 (거의 없지만 방어)
                    lines.append(word)
                else:
                    lines.append(" ".join(current_line))
                    current_line = [word]
        if current_line:
            lines.append(" ".join(current_line))

        # 2. 필요한 총 텍스트 높이 계산
        bbox = draw_measure.textbbox((0, 0), "A", font=font)
        line_height = bbox[3] - bbox[1]
        line_spacing = int(line_height * 0.3)
        total_text_height = (line_height * len(lines)) + (line_spacing * (len(lines) - 1))

        # 텍스트 높이에 비례한 새로운 배경 바 높이 (기본 패딩 포함)
        bar_height = total_text_height + int(base_bar_height * 0.5)

        # 3. 반투명 배경 박스 그리기
        img = img.convert('RGBA')
        overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
        draw_bg = ImageDraw.Draw(overlay)
        draw_bg.rectangle(
            [(0, height - bar_height), (width, height)],
            fill=(0, 0, 0, 160),
        )
        img = Image.alpha_composite(img, overlay)

        # 4. 여러 줄의 텍스트 그리기 (가운데 정렬)
        draw = ImageDraw.Draw(img)
        # 전체 텍스트 블록의 시작 Y 좌표 계산
        start_y = height - bar_height + (bar_height - total_text_height) // 2

        for i, line in enumerate(lines):
            bbox = draw.textbbox((0, 0), line, font=font)
            text_w = bbox[2] - bbox[0]
            # 각 줄을 가로 중앙에 배치
            text_x = (width - text_w) // 2
            text_y = start_y + i * (line_height + line_spacing)
            draw.text((text_x, text_y), line, fill=(255, 255, 255, 255), font=font)

        return img.convert('RGB')

    def _get_font(self, bar_height):
        """한국어 지원 폰트 로드."""
        font_size = max(20, int(bar_height * 0.45))
        
        # 프로젝트 로컬 폰트 최우선 적용 (리눅스 클라우드 서버 등 OS 배포 호환성)
        local_font_path = os.path.join(os.path.dirname(__file__), 'fonts', 'NanumGothic-Bold.ttf')
        
        font_paths = [
            local_font_path,
            '/System/Library/Fonts/AppleSDGothicNeo.ttc',
            '/System/Library/Fonts/Supplemental/AppleGothic.ttf',
            '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
            '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
        ]
        for path in font_paths:
            if os.path.exists(path):
                try:
                    return ImageFont.truetype(path, font_size)
                except Exception:
                    continue
        try:
            return ImageFont.truetype("arial.ttf", font_size)
        except Exception:
            return ImageFont.load_default()

    def _save_image(self, img, index):
        """이미지를 임시 파일로 저장."""
        temp_dir = os.path.join(tempfile.gettempdir(), 'saiso_images')
        os.makedirs(temp_dir, exist_ok=True)
        file_path = os.path.join(temp_dir, f'product_image_{index}_{os.getpid()}.png')
        img.save(file_path, 'PNG')
        return file_path

    @staticmethod
    def cleanup_images(image_paths):
        """[FIX #12] 임시 이미지 파일 삭제."""
        for path in image_paths:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass
