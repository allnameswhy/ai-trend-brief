# CLAUDE.md — AI TREND 자동 발간 시스템

이 파일은 Claude Code가 이 프로젝트에서 작업할 때 항상 참조하는 컨텍스트입니다.

## 프로젝트 개요
수작업으로 발간하던 주간 브리프 『AI TREND — 해외 AI 정책·기술 동향 조사』를
멀티 에이전트 파이프라인으로 자동 생성하는 시스템. **전체 기획은 `PROJECT_NOTES.md`가 단일 원천(spec)**이며,
아키텍처 그림은 `docs/architecture.png` 참조. 이 두 문서와 코드가 어긋나면 문서를 우선하되 사용자에게 확인할 것.

## 현재 상태 (스캐폴드 단계)
- 파이프라인 4단계(조사자→편집장→작가→검토자) + 오케스트레이터 + FastAPI 서버 + 대시보드 골격 완성
- **아직 한 번도 실제 실행된 적 없음** → 최우선 과제는 1차 데모(터미널 실행) 성공
- 데모 범위에서 제외(추후 과제): 스케줄링, 이메일 발송, 이미지 산출물, SQLite DB, 기록 관리자(⑤) 아카이브 참조

## 구조
```
.claude/agents/*.md   # 에이전트 정의(역할·도구). 행동 수정은 여기서. 코드 수정 불필요
backend/orchestrator.py  # 파이프라인 지휘: 순서 강제, 반려 시 1회 재작성 루프, 로그, HITL 정지
backend/main.py          # FastAPI: POST /run(트리거), GET /events(SSE 로그), GET /(대시보드)
frontend/index.html      # 대시보드: 발간 버튼 + 실시간 로그 (프레임워크 없는 순수 HTML)
data/runs/<timestamp>/   # 실행마다 생성: candidates.json → selected.json → draft.md → review.md
logs/run_<timestamp>.log # 실행별 누적 작업 로그 (PROJECT_NOTES 2.10)
```

## 실행 방법
- 1차 데모(터미널): `python backend/orchestrator.py`
- 2차 데모(대시보드): `uvicorn backend.main:app --reload` 후 http://localhost:8000

## 프로젝트 규칙 (반드시 준수)
1. **파일명은 영어**, 문서·주석·에이전트 프롬프트는 한국어 (PROJECT_NOTES 2.0)
2. **자동 발송 절대 금지** — 파이프라인은 원고+검토 로그 제시 후 반드시 멈춘다 (HITL, 2.9)
3. 작가 재작성 **피드백 루프는 1회로 고정** (2.8)
4. 데모 단계: **무료 공개 콘텐츠만** 수집, 페이월은 무료 부분까지만 (이슈 #6). SNS·개인 블로그 소스 금지 (이슈 #2)
5. 카테고리 배분: 정책 2~3 / 기술 4~6 / 윤리 2~3, 합계 10건 (2.2)
6. 사용자는 개발 경험이 적음 → 변경 사항은 쉬운 말로 설명하고, 새 라이브러리 도입 전에 이유를 먼저 설명할 것

## 다음 작업 순서 (Work Plan, PROJECT_NOTES 4장)
1. ✅ 에이전트 정의 → 2. **파이프라인 1차 데모(현재 목표)** → 3. 대시보드 데모 → 4. 발간물 DB(SQLite)·아카이브 참조 → 5. 2차 데모 → 6. 테스트 발송
