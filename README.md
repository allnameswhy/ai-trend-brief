# AI TREND 자동 발간 시스템 (파일럿)

『AI TREND — 해외 AI 정책·기술 동향 조사』 격주 브리프(월 2회)를
조사자 → 편집장 → 작가 → 검토자 4개 에이전트 파이프라인으로 자동 생성하는 프로젝트입니다.

- 전체 기획·명세(단일 원천): `PROJECT_NOTES.md` — 파이프라인 구조는 2.7 「에이전트 파이프라인 요약」 참조
- 아키텍처 그림: `docs/architecture.png`
- Claude Code 작업 컨텍스트: `CLAUDE.md` / 할 일: `TODO.md`

---

## 처음 한 번만 하는 준비

> 아래 명령은 모두 **터미널**(macOS: Terminal / Windows: PowerShell)에서,
> 이 프로젝트 폴더 안에서 실행합니다. (`cd ai-trend-brief` 로 폴더에 들어간 상태)

**1. 파이썬 확인** — 3.10 이상이 필요합니다.

```bash
python3 --version
```

**2. 가상환경 만들기** — 이 프로젝트만의 독립된 파이썬 공간입니다.
(다른 프로그램과 라이브러리가 섞이지 않게 해줍니다.)

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows PowerShell: .venv\Scripts\Activate.ps1
```

터미널 줄 앞에 `(.venv)` 가 붙으면 성공. **이후 모든 명령은 이 상태에서 실행합니다.**

**3. 라이브러리 설치**

```bash
pip install -r requirements.txt
```

**4. API 키 설정**

`.env.example` 파일을 복사해 `.env` 라는 이름으로 저장하고, 안에 본인의 API 키를 넣습니다.
(Claude Code에 이미 로그인돼 있다면 이 단계 없이도 동작할 수 있습니다 — 안 되면 그때 넣으세요.)

---

## 실행

**1차 데모 — 터미널에서 파이프라인만 (완료)**

```bash
python backend/orchestrator.py
```

조사자→편집장→작가→검토자가 순서대로 돌고 로그가 화면에 흐릅니다.
결과물은 `data/runs/<실행시각>/` 폴더에 생깁니다:
`candidates.json`(후보) → `selected.json`(확정 기사, 최대 10건) → `card_NN.json`(카드 콘텐츠)
→ `review.md`(검토 로그) → `card_NN.png`(배포용 카드 이미지)

마지막에 자동 발송하지 않고 멈춥니다 — `review.md`와 카드 PNG를 직접 확인하세요.

**2차 데모 — 대시보드 (현재 목표, 골격까지 동작)**

```bash
uvicorn backend.main:app --reload
```

브라우저에서 http://localhost:8000 을 열고 [발간 실행] 버튼을 누르면
같은 파이프라인이 돌면서 로그가 화면에 실시간으로 표시됩니다.
(데모 한계: 대시보드 탭은 하나만 열어두세요.)

---

## 자주 겪는 문제

- **`claude` 실행 파일을 찾을 수 없다는 오류** → Claude Code CLI가 필요합니다.
  Node.js 18+ 설치 후 `npm install -g @anthropic-ai/claude-code` 실행.
  (설치 안내: https://code.claude.com/docs)
- **인증 오류** → `.env` 의 `ANTHROPIC_API_KEY` 확인, 또는 터미널에서 `claude` 를 한 번 실행해 로그인.
- **에이전트 행동을 바꾸고 싶다** → 코드가 아니라 `.claude/agents/*.md` 파일의 프롬프트를 수정하세요.
