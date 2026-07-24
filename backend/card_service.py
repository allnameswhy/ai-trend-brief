"""
카드 서비스 — 대시보드(main.py)가 쓰는 카드 렌더·검사 래퍼
-----------------------------------------------------------
tools/build_cardnews.py 의 모듈 함수를 subprocess 없이 직접(인프로세스) 호출한다.
- 프리뷰: 카드 JSON → HTML 렌더가 밀리초 단위라, 편집할 때마다 실시간으로 다시 그릴 수 있다.
  (HTML 파일을 미리 만들어 두는 방식과 달리 '낡은 프리뷰' 문제가 아예 없다)
- 검사: 자수 규격만 보는 빠른 검사(quick)와 Edge 렌더 실측까지 하는 종합 검사(full)를 나눠 제공.

tools/ 는 파이썬 패키지가 아니라서 import 경로 우회가 필요한데, 그 우회를 이 파일 한 곳에 격리한다.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_TOOLS_DIR = str(PROJECT_ROOT / "tools")
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

import build_cardnews  # noqa: E402  (tools/ 를 sys.path 에 추가한 뒤 import)


def render_card_html(card: dict) -> str:
    """카드 JSON(dict) → 완성 HTML 문자열. 프리뷰 iframe 용 (렌더만, 파일 저장 없음)."""
    return build_cardnews.render_html(card)


def quick_validate(card: dict) -> list[str]:
    """자수·형식 규격만 즉시 검사 (렌더 없음, 0초). 문제 목록 반환 — 빈 목록이면 합격."""
    return build_cardnews.validate_card(card)


def full_check(card_path: Path) -> list[str]:
    """자수 규격 + Edge 렌더 실측(넘침)까지 종합 기계 검사 — build_cardnews --check 와 동일 판정.
    Edge 실행으로 수 초 걸리는 동기(블로킹) 함수 — 서버에서는 asyncio.to_thread 로 감쌀 것."""
    return build_cardnews.card_problems(Path(card_path))
