"""구글 Gemini 직접 호출 키 묶음 — 키를 우선순위대로 쓰고, 한 키가 막히면(한도·거부) 다음 키로.

정책(2026-09-22): 구글 키 1순위→2순위, 전부 막히면 호출부가 kie → EvoLink 로(체인은 gemini_chain.py).
설정: GOOGLE_AI_API_KEYS="키1,키2"(쉼표, 앞이 1순위). 없으면 종전 단일 키 GEMINI_API_KEY. 둘 다 있으면 단일 키는 뒤에.
봉인 범위: 429(한도) → 그 키의 그 모델(scope)만 15분(GOOGLE_AI_KEY_COOLDOWN_S). 401/403·API_KEY_INVALID(키 거부) → 키 전체.
404(모델 없음)는 이 키의 프로젝트에만 없을 수 있어(새 프로젝트 키는 gemini-2.5 가 404) 봉인 없이 다음 키.
5xx·타임아웃은 키 탓이 아니므로 그대로 던진다 → 호출부가 다음 경로로.
ERP(yt-erp lib/google-ai.ts)와 같은 규약. 표준 라이브러리만 쓴다.
정본은 kslang apps/worker/pipeline/google_keys.py — 규칙은 그대로 두고, 이 리포의 종전 키(DB·사용자 필드)를
env 목록 **뒤에** 붙이는 `extra` 인자만 더했다.
"""
import os
import re
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")
ANY = "*"
_blocked: dict[str, float] = {}


class GoogleKeysExhausted(RuntimeError):
    """모든 키가 막혀 구글 직접 경로를 쓸 수 없다. 호출부는 다음 경로로 넘어간다."""


def google_keys(extra: list[str] | str | None = None) -> list[str]:
    """env GOOGLE_AI_API_KEYS(앞이 1순위) → env GEMINI_API_KEY → extra(리포 종전 키, 쉼표 목록 허용) 순. 중복 제거."""
    out: list[str] = []
    raws = [os.environ.get("GOOGLE_AI_API_KEYS", ""), os.environ.get("GEMINI_API_KEY", "")]
    if isinstance(extra, str):
        raws.append(extra)
    elif extra:
        raws.extend(x for x in extra if x)
    for raw in raws:
        for k in re.split(r"[,;\n]", raw or ""):
            k = k.strip()
            if k and k not in out:
                out.append(k)
    return out


def google_configured(extra: list[str] | str | None = None) -> bool:
    return bool(google_keys(extra))


def _cooldown_s() -> float:
    try:
        v = float(os.environ.get("GOOGLE_AI_KEY_COOLDOWN_S", "0"))
    except ValueError:
        v = 0
    return v if v > 0 else 15 * 60


def _status(e: BaseException) -> int | None:
    for attr in ("status", "code", "status_code"):
        v = getattr(e, attr, None)
        if isinstance(v, int) and 100 <= v < 600:
            return v
    m = re.search(r"\b(400|401|403|404|429)\b", str(e))
    return int(m.group(1)) if m else None


def is_model_missing(e: BaseException) -> bool:
    return _status(e) == 404 or bool(re.search(r"NOT_FOUND|no longer available to new users", str(e), re.I))


def is_key_rejected(e: BaseException) -> bool:
    st, msg = _status(e), str(e)
    if st in (401, 403):
        return True
    if st == 400 and re.search(r"API_KEY_INVALID|API key not valid|API key expired", msg, re.I):
        return True
    return bool(re.search(r"PERMISSION_DENIED|API_KEY_INVALID|API key not valid|API key expired|CONSUMER_SUSPENDED", msg, re.I))


def is_quota_hit(e: BaseException) -> bool:
    return _status(e) == 429 or bool(re.search(r"RESOURCE_EXHAUSTED|quota exceeded", str(e), re.I))


def is_key_unusable(e: BaseException) -> bool:
    return is_key_rejected(e) or is_quota_hit(e)


def _blocked_until(key: str, scope: str) -> float:
    return max(_blocked.get(f"{key}|{ANY}", 0.0), _blocked.get(f"{key}|{scope}", 0.0))


def _label(keys: list[str], key: str) -> str:
    return f"키#{keys.index(key) + 1}/{len(keys)}(…{key[-4:]})"


def with_google_key(fn: Callable[[str], T], scope: str = ANY, extra: list[str] | str | None = None) -> T:
    """키를 순서대로 써서 fn(key) 를 돌린다. 막힌 키는 봉인하고 다음 키, 키 탓이 아닌 오류는 그대로 던진다."""
    keys = google_keys(extra)
    if not keys:
        raise GoogleKeysExhausted("GOOGLE_AI_API_KEYS(또는 GEMINI_API_KEY) 미설정")
    now = time.time()
    live = [k for k in keys if _blocked_until(k, scope) <= now]
    if not live:
        nxt = min(_blocked_until(k, scope) for k in keys)
        raise GoogleKeysExhausted(f"구글 키 {len(keys)}개 전부 막힘({scope}) — {int((nxt - now) / 60) + 1}분 뒤 재시도")
    last: BaseException | None = None
    missing: BaseException | None = None
    for key in live:
        try:
            return fn(key)
        except GoogleKeysExhausted:
            raise
        except Exception as e:  # noqa: BLE001 — 분류해서 키만 바꾼다
            if is_model_missing(e) and not is_key_unusable(e):
                missing = e
                print(f"[google-ai] {_label(keys, key)} 모델 없음(404) → 다음 키: {str(e)[:120]}", flush=True)
                continue
            if not is_key_unusable(e):
                raise
            last = e
            wide = is_key_rejected(e)
            _blocked[f"{key}|{ANY if wide else scope}"] = time.time() + _cooldown_s()
            print(f"[google-ai] {_label(keys, key)} {'거부(키 전체 봉인)' if wide else f'한도({scope} 봉인)'}"
                  f"({_status(e) or '?'}: {str(e)[:120]}) → 다음 키", flush=True)
    if missing is not None and last is None:
        raise missing
    raise GoogleKeysExhausted(f"구글 키 {len(keys)}개 전부 막힘({scope}, 마지막: {str(last)[:160]})")
