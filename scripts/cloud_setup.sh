#!/bin/bash
# AI TREND — Claude Code 클라우드 세션용 환경 준비 스크립트 (2026-09-30)
#
# 쓰는 법: claude.ai/code → 환경(Environment) 설정 → "Setup script" 칸에 이 파일 내용을 그대로 붙여 넣는다.
#   - 새 환경에서 처음 한 번 실행되고 결과가 스냅샷으로 저장된다 (스크립트·네트워크 설정을 바꾸거나 약 7일이 지나면 재실행)
#   - 5분 안에 끝나야 스냅샷이 만들어진다. 비필수 단계는 `|| true` 로 감싸 세션이 안 뜨는 일이 없게 한다
#   - 같은 환경의 네트워크 접근(Network access)을 Custom 으로 두고 아래 도메인을 추가해야 한다:
#       cdn.jsdelivr.net                          카드 폰트 Pretendard (없으면 아래서 설치하는 Noto Sans CJK 로 대체 렌더)
#       cdn.playwright.dev                        Chromium 다운로드
#       playwright.download.prss.microsoft.com    Chromium 다운로드 예비 주소
#     기사 수집(조사자)까지 클라우드에서 돌리려면 뉴스 사이트도 열어야 하지만, 현재 운영 방식은
#     "코드 작업은 클라우드, 발간 run 은 로컬" 이라 필요 없다 (PROJECT_NOTES 2장).
#   - 환경변수에 TZ=Asia/Seoul 을 추가한다 (호수 자동 계산이 PC 시각 기준이라 UTC 면 어긋난다)
#
# 하는 일: 한글 폰트 설치 → 파이썬 의존성 설치 → 렌더용 Chromium 설치(Playwright 로 내려받기) → 탐색 확인.
# Chromium 은 tools/build_cardnews.py 의 find_browser() 가 /opt/ms-playwright 에서 찾는다.
# Playwright 는 코드에서 쓰지 않는다 — Ubuntu 24.04 의 apt chromium 이 snap 이라 헤드리스로 못 쓰기 때문에
# 검증된 Chromium 빌드를 내려받는 설치 도구로만 쓴다.
#
# 로컬 Windows PC 에서는 실행할 필요가 없다 (Edge 가 있음). 실수로 실행돼도 첫 검사에서 바로 끝난다.

set -u
if [ "$(uname -s)" != "Linux" ]; then
  echo "[cloud_setup] Linux 가 아니므로 아무것도 하지 않습니다."
  exit 0
fi

export DEBIAN_FRONTEND=noninteractive
export PIP_BREAK_SYSTEM_PACKAGES=1          # Ubuntu 시스템 파이썬에 pip 설치 허용 (PEP 668 경고 우회)
export PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
REPO="${CLAUDE_PROJECT_DIR:-$PWD}"

echo "[cloud_setup] 1/4 한글 폰트"
apt-get update -qq || true
apt-get install -y -qq fonts-noto-cjk || echo "  ! 폰트 설치 실패 — CDN 폰트에만 의존"

echo "[cloud_setup] 2/4 파이썬 의존성"
if [ -f "$REPO/requirements.txt" ]; then
  pip install -q -r "$REPO/requirements.txt" || true
else
  echo "  ! requirements.txt 를 못 찾음 ($REPO) — 패키지 이름을 직접 설치"
  pip install -q claude-agent-sdk fastapi uvicorn python-dotenv jinja2 pypdf || true
fi

echo "[cloud_setup] 3/4 렌더용 Chromium"
pip install -q playwright || true
python3 -m playwright install --with-deps chromium || echo "  ! Chromium 설치 실패 — 네트워크 허용 목록(cdn.playwright.dev) 확인"

echo "[cloud_setup] 4/4 확인"
ls -d /opt/ms-playwright/chromium-*/chrome-linux*/chrome 2>/dev/null || echo "  ! Chromium 실행 파일이 없음"
fc-list 2>/dev/null | grep -qi "Noto Sans CJK" && echo "  폰트 OK: Noto Sans CJK" || echo "  ! Noto Sans CJK 없음"
if [ -f "$REPO/tools/build_cardnews.py" ]; then
  (cd "$REPO" && python3 -c "import sys; sys.path.insert(0, 'tools'); from build_cardnews import find_browser; print('  find_browser():', find_browser())") || true
fi
exit 0
