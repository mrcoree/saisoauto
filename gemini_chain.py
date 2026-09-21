"""Gemini 텍스트 호출 체인 — **구글 직접(키 회전) → kie → EvoLink** (2026-09-22 정책).

정본은 kslang apps/api/app/services/gemini_chain.py(REST, 표준 라이브러리만). 규칙은 그대로 두고 이것만 더했다.
  ① 내용 거절(SAFETY·RECITATION·MAX_TOKENS·promptFeedback.blockReason)은 ContentRefused 로 즉시 중단 — 키·경로를 바꿔도 같으니 폴백 금지.
  ② post_native_json: 네이티브 응답 JSON 을 그대로 돌려준다(EvoLink 로 이미지 출력 모델을 부를 때).
  ③ run_chain(google=False): SDK 가 구글 직접 경로를 이미 돌렸을 때 kie → EvoLink 만 이어 간다.
  ④ model_ladder 는 텍스트 모델에만 폴백을 붙인다(이미지·영상 모델은 폴백 없음).
세 경로 모두 구글 네이티브 `:generateContent` 형식이라 본문은 한 번 만들고 주소·인증만 바꾼다.
  · 구글 직접: generativelanguage.googleapis.com/v1beta/models/{점표기}:generateContent · x-goog-api-key(google_keys 가 키를 순서대로)
  · kie      : api.kie.ai/gemini/v1/models/{대시표기}:generateContent · Bearer KIE_API_KEY (HTTP 200 에 {code,msg} 로 오는 실패도 잡는다)
  · EvoLink  : api.evolink.ai/v1beta/models/{점표기}:generateContent · x-goog-api-key EVOLINK_API_KEY
각 경로 안에서 요청 모델 실패 시 폴백 모델(GEMINI_FALLBACK_MODEL, 기본 gemini-3.5-flash)로 한 번 더(오빠 지시 2026-09-22).
"""
import json
import os
import re
import time
import urllib.error
import urllib.request

try:  # 패키지 안(ytauto app.services)
    from .google_keys import google_configured, with_google_key
except ImportError:  # 평면 모듈(saisoauto)
    from google_keys import google_configured, with_google_key

GOOGLE_BASE = "https://generativelanguage.googleapis.com"
KIE_BASE = "https://api.kie.ai"
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
# EvoLink 앞단 Cloudflare 가 urllib 기본 UA(Python-urllib/3.x)를 1010 으로 차단한다(2026-09-22 실측: 다른 UA 는 200).
USER_AGENT = os.environ.get("GEMINI_CHAIN_USER_AGENT", "gemini-chain/1.0")

# 후보가 텍스트를 못 낸 이유 가운데 "다시 보내도 같은" 것들 — 폴백하지 않는다.
_REFUSAL_REASONS = {"SAFETY", "RECITATION", "MAX_TOKENS", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII",
                    "IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT", "IMAGE_RECITATION", "IMAGE_OTHER"}


class ContentRefused(RuntimeError):
    """모델이 내용을 거절했거나 잘라냈다(SAFETY·RECITATION·MAX_TOKENS·blockReason). 다른 키·경로로 넘어가지 않는다."""


def _dot(m: str) -> str:
    """대시 → 점(구글·EvoLink): gemini-3-5-flash → gemini-3.5-flash"""
    return re.sub(r"^gemini-(\d+)-(\d+)(?=-|$)", r"gemini-\1.\2", m)


def _dash(m: str) -> str:
    """점 → 대시(kie): gemini-3.5-flash → gemini-3-5-flash"""
    return re.sub(r"^gemini-(\d+)\.(\d+)(?=-|$)", r"gemini-\1-\2", m)


def kie_enabled() -> bool:
    return bool(os.environ.get("KIE_API_KEY"))


def evolink_key() -> str:
    return os.environ.get("EVOLINK_API_KEY", "").strip()


def evolink_base() -> str:
    return os.environ.get("EVOLINK_GEMINI_BASE_URL", "https://api.evolink.ai").rstrip("/")


def is_text_model(model: str) -> bool:
    """이미지·영상·음성 모델이 아니면 텍스트 모델. 모델 폴백(3.5-flash)은 텍스트 모델에만 붙는다."""
    return not re.search(r"image|imagen|veo|tts|audio|embedding|banana", model, re.I)


def model_ladder(model: str) -> list[str]:
    """[요청 모델, 폴백 모델](점표기). 같거나 텍스트 모델이 아니면 하나. 오빠 지시 2026-09-22."""
    fb = _dot(os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash"))
    m = _dot(model)
    return [m] if m == fb or not is_text_model(m) else [m, fb]


def chain_configured(extra_keys=None) -> bool:
    return google_configured(extra_keys) or kie_enabled() or bool(evolink_key())


class HttpError(RuntimeError):
    def __init__(self, label: str, status: int, body: str):
        msg = body[:300]
        try:
            j = json.loads(body)
            msg = str((j.get("error") or {}).get("message") or j.get("msg") or msg)
        except ValueError:
            pass
        super().__init__(f"{label} {status}: {msg}")
        self.status = status


def post_native_json(label: str, url: str, headers: dict, body: bytes, timeout: float, retries: int = 2) -> dict:
    """네이티브 :generateContent 를 POST 하고 응답 JSON 을 돌려준다. candidates 가 없으면 예외(거절이면 ContentRefused)."""
    last: Exception | None = None
    for attempt in range(retries):
        if attempt:
            time.sleep(2 * attempt)
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": USER_AGENT, **headers})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err = HttpError(label, e.code, e.read().decode("utf-8", "replace"))
            if e.code in (408, 500, 502, 503, 504) and attempt < retries - 1:
                last = err
                continue
            raise err from e
        if not isinstance(data, dict) or not data.get("candidates"):
            block = ((data.get("promptFeedback") or {}).get("blockReason") if isinstance(data, dict) else None)
            if block:
                raise ContentRefused(f"{label} 프롬프트 차단({block}): {str(data)[:200]}")
            # kie 는 잔액 소진·인증 실패를 HTTP 200 에 {code,msg} 로 담아 보낸다.
            raise RuntimeError(f"{label} 응답에 candidates 없음: {str(data)[:200]}")
        return data
    raise last or RuntimeError(f"{label} 실패")


def _post_native(label: str, url: str, headers: dict, body: bytes, timeout: float, retries: int = 2) -> str:
    data = post_native_json(label, url, headers, body, timeout, retries)
    cand = data["candidates"][0]
    parts = (cand.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        reason = str(cand.get("finishReason") or "").upper()
        if reason in _REFUSAL_REASONS:
            raise ContentRefused(f"{label} 내용 거절({reason}): {str(data)[:200]}")
        raise RuntimeError(f"{label} 빈 응답: {str(data)[:200]}")
    return text


def run_chain(body: dict, *, model: str | None = None, google: bool = True, extra_keys=None) -> str:
    """네이티브 요청 본문(dict) → 텍스트. 구글 직접(키 회전) → kie → EvoLink, 경로마다 요청 모델 → 폴백 모델.

    google=False 면 SDK 가 구글 경로를 이미 돌렸다는 뜻 — kie → EvoLink 만. extra_keys 는 이 리포의 종전 키(뒤에 붙는다).
    """
    return run_chain_detail(body, model=model, google=google, extra_keys=extra_keys)[0]


def run_chain_detail(body: dict, *, model: str | None = None, google: bool = True, extra_keys=None) -> tuple:
    """run_chain 과 같되 (텍스트, "경로:모델") 을 돌려준다 — 어느 경로·모델이 답했는지 호출부가 기록할 수 있게."""
    raw = json.dumps(body).encode("utf-8")
    base = model or GEMINI_MODEL
    timeout = float(os.environ.get("GEMINI_DIRECT_TIMEOUT_S", "180"))

    def kie_model(m: str) -> str:
        return (os.environ.get("KIE_GEMINI_MODEL") if m == _dot(base) else "") or _dash(m)

    steps = [
        ("구글 직접", google and google_configured(extra_keys), lambda m: with_google_key(
            lambda key: _post_native("google", f"{GOOGLE_BASE}/v1beta/models/{m}:generateContent",
                                     {"x-goog-api-key": key}, raw, timeout), scope=m, extra=extra_keys)),
        ("kie", kie_enabled(), lambda m: _post_native(
            "kie", f"{KIE_BASE}/gemini/v1/models/{kie_model(m)}:generateContent",
            {"Authorization": f"Bearer {os.environ['KIE_API_KEY']}"}, raw, timeout)),
        ("EvoLink", bool(evolink_key()), lambda m: _post_native(
            "EvoLink", f"{evolink_base()}/v1beta/models/{m}:generateContent",
            {"x-goog-api-key": evolink_key()}, raw, timeout)),
    ]
    tried: list[str] = []
    last: Exception | None = None
    for name, on, run in steps:
        if not on:
            continue
        for m in model_ladder(base):
            try:
                return run(m), f"{name}:{m}"
            except ContentRefused:
                raise
            except Exception as e:  # noqa: BLE001 — 다음 모델·경로로
                last = e
                tried.append(f"{name}:{m}")
                print(f"[gemini] {name} {m} 실패 → 다음: {str(e)[:200]}", flush=True)
    if not tried:
        raise RuntimeError("Gemini 키 미설정(GOOGLE_AI_API_KEYS · KIE_API_KEY · EVOLINK_API_KEY 중 하나 필요)")
    raise RuntimeError(f"Gemini 경로 전부 실패({' → '.join(tried)}): {last}")


def generate_text_chain(contents: list[str], *, json_mode: bool = False, temperature: float | None = None,
                        model: str | None = None, extra_keys=None) -> str:
    """텍스트 프롬프트(들) → 텍스트. 구글 직접(키 회전) → kie → EvoLink."""
    gc: dict = {"maxOutputTokens": int(os.environ.get("KIE_GEMINI_MAX_OUTPUT", "32768"))}
    if temperature is not None:
        gc["temperature"] = temperature
    if json_mode:
        gc["responseMimeType"] = "application/json"
    body = {
        "contents": [{"role": "user", "parts": [{"text": t} for t in contents]}],
        "generationConfig": gc,
    }
    return run_chain(body, model=model, extra_keys=extra_keys)
