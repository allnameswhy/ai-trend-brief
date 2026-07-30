"""
스티비(Stibee) 메일 발송 API 클라이언트
---------------------------------------
발간물을 이메일로 보내기 위한 스티비 v2 API 래퍼. 여기에는 API 호출만 두고,
언제 호출할지(상태 가드·HITL)는 backend/main.py 가 결정한다.

인증: 모든 요청에 AccessToken 헤더 (키는 .env 의 STIBEE_API_KEY —
      backend/orchestrator.py 의 load_dotenv 가 서버 기동 시 로드).
주의: 발송(send)은 자동화하지 않는다 — 자동 발송 금지(HITL, CLAUDE.md 규칙 2).
      이 모듈은 초안 생성 등 발송 이전 단계만 담당한다.
"""

import os

import httpx

BASE_URL = "https://api.stibee.com/v2"
TIMEOUT = 15.0   # 초

# 발신 정보 — 스티비 워크스페이스에 등록된 값 (2026-07-30 사용자 확정, 고정)
SENDER_EMAIL = "nrfplan@nrf.re.kr"
SENDER_NAME = "한국연구재단 AI·정책기획팀"
LIST_ID = 507115   # 발송 대상 주소록 아이디
SUBJECT_TEMPLATE = "[AI·정책기획팀] NRF AI Trend Brief {week_label}"


class StibeeError(Exception):
    """스티비 API 오류. 스티비가 준 오류 code 와 사람이 읽을 message 를 담는다."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code} — {message}")


def _api_key() -> str:
    key = os.environ.get("STIBEE_API_KEY", "").strip()
    if not key:
        raise StibeeError("NoApiKey", "STIBEE_API_KEY 가 .env 에 없습니다")
    return key


def _raise_from_response(resp: httpx.Response) -> None:
    """스티비 오류 응답(JSON: code/message/values.message)을 StibeeError 로 변환한다."""
    try:
        body = resp.json()
        detail = body.get("values", {}).get("message") if isinstance(body.get("values"), dict) else None
        message = body.get("message", "")
        if detail:
            message = f"{message} ({detail})"
        raise StibeeError(body.get("code", f"HTTP{resp.status_code}"), message or resp.text[:200])
    except (ValueError, AttributeError):   # JSON 이 아닌 응답 (5XX 등)
        raise StibeeError(f"HTTP{resp.status_code}", resp.text[:200])


def create_email(subject: str) -> int:
    """일반 이메일(초안)을 생성하고 스티비 이메일 아이디를 반환한다. (POST /emails)
    제목·발신자·주소록만 채운 빈 이메일 — 콘텐츠·발송은 이후 단계. 동기 함수라
    FastAPI 안에서는 asyncio.to_thread 로 감싸 부른다."""
    resp = httpx.post(
        f"{BASE_URL}/emails",
        headers={"AccessToken": _api_key()},
        json={
            "subject": subject,
            "senderEmail": SENDER_EMAIL,
            "senderName": SENDER_NAME,
            "listId": LIST_ID,
        },
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        _raise_from_response(resp)
    email_id = resp.json().get("id")
    if not isinstance(email_id, int):
        raise StibeeError("UnexpectedResponse", f"응답에 이메일 id 가 없습니다: {resp.text[:200]}")
    return email_id
