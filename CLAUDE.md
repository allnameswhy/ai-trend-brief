# CLAUDE.md — AI TREND 자동 발간 시스템

이 파일은 Claude Code가 이 프로젝트에서 작업할 때 항상 참조하는 컨텍스트입니다.

## 프로젝트 개요
격주 브리프(월 2회, 2·4주차) 『AI TREND — 해외 AI 정책·기술 동향 조사』를
멀티 에이전트 파이프라인으로 자동 생성하는 시스템.
**기획·구조·설계의 단일 원천(spec)은 `PROJECT_NOTES.md`** — 1장 개요 / 2장 파이프라인 구조 /
3장 에이전트별 현재 동작·남은 과제 / 4장 미해결 설계 과제. 다른 문서와 어긋나면 PROJECT_NOTES가 우선한다.
할 일 목록만 `TODO.md`가 관리한다. 문서와 코드가 어긋나면 문서를 우선하되 사용자에게 확인할 것.

## 현재 상태
- 조사→선별→(선정 대기)→집필→검토→(카드 검토 대기)→발행(카드 PNG·헤더·표지·열람용 PDF)까지
  전 구간 동작. 대시보드 리허설 run 검증 완료(`data/runs/20260803_111102/`). 세부는 PROJECT_NOTES 2장
- 발송은 사람이 스티비 웹에서 PNG 직접 업로드 — 시스템 역할은 렌더에서 끝 (PROJECT_NOTES 1장)
- 미구현: 기록 관리자(발간물 아카이브 DB), 기사 수집 DB, 스케줄링, 아카이브·과거 run 열람 UI (PROJECT_NOTES 3장)
- 다음 단계: 발간물 DB 구축 → 2차 데모 → 테스트 발송

## 구조
```
.claude/agents/*.md      # 에이전트 정의(역할·도구). 행동 수정은 여기서. 코드 수정 불필요
backend/orchestrator.py  # 파이프라인 라이브러리: run_phase1(조사·선별)/run_phase2(집필·검토)/render_cards(발행 렌더) + CLI용 run_pipeline
backend/main.py          # FastAPI: 상태 머신(state.json), 단계 실행·중단·선정 확정·카드 편집·발행 API, SSE
backend/card_service.py  # build_cardnews 인프로세스 래퍼: 프리뷰 HTML 렌더·자수 검사·종합(Edge 실측) 검사
frontend/index.html      # HITL 대시보드: 제어·로그 / 기사 선정 / 카드 검토·편집 3화면 (프레임워크 없는 순수 HTML)
tools/build_cardnews.py  # 카드·헤더·표지 렌더(HTML→PNG)와 규격 기계 검사(--check). 렌더는 오케스트레이터가 직접 실행, 검토자만 --check용 Bash 허용
tools/build_cardnews_pdf.py  # publish/ HTML을 Edge 인쇄로 병합해 열람용 벡터 PDF 생성
tools/cardnews/          # card_schema.md(카드 규격 단일 원천) · card_template.html.j2 · card_v2_sample.json · header_template.html.j2 · cover_template.html.j2 · nrf-symbol.png
tools/test_*.py          # 부분 테스트 하네스 (아래 규칙 8)
scripts/cloud_setup.sh   # Claude Code 클라우드 세션 환경 준비(폰트·의존성·Chromium). 코드 작업은 클라우드, 발간 run은 로컬 (PROJECT_NOTES 2장)
data/runs/<timestamp>/   # 실행마다 생성: candidates.json → selected.json(+dropped.json+screened.json) → item_input_NN.json → card_NN.json → review.md. 발행 산출물은 publish/ 하위. 대시보드 run은 state.json·selected_editor.json·card_NN_orig.json 추가
data/tests/<timestamp>/  # 테스트 산출물 (run과 같은 구조, 규칙 8)
logs/run_<timestamp>.log # 실행별 작업 로그 (PROJECT_NOTES 2장)
docs/                    # gitignore 대상 — 기존 발간물 원본, 핸드오프 문서 (예외: architecture.png는 추적 중)
```

## 실행 방법
- 대시보드(표준): `uvicorn backend.main:app` 후 http://localhost:8000
  (**발간 실행 시 `--reload` 금지** — 파일 저장 시 실행 중 파이프라인이 죽음. 개발 중에만 사용)
- 터미널(보조, 중간 개입 없음): `python backend/orchestrator.py`
  - `--selected <selected.json 경로>` — 조사자·편집장 건너뛰고 작가부터 (새 폴더)
  - `--resume <run 폴더>` — 중단된 run을 그 폴더에서 이어서 (기작성 카드 재사용)

## 프로젝트 규칙 (반드시 준수)
1. **파일명은 영어**, 문서·주석·에이전트 프롬프트는 한국어
   - **예외**: `docs/` 아래 기존 발간물은 원제(한글) 유지. 구조 부분만 통일(`AITrend`/`RnD-Brief`/`NRF-IssueReport`_연도-호수_원제)
2. **자동 발송 절대 금지** — 파이프라인은 카드+검토 로그 제시 후 반드시 멈춘다 (HITL, PROJECT_NOTES 1·2장)
3. 작가 재작성 **피드백 루프는 1회로 고정** (PROJECT_NOTES 2장)
4. **무료 공개 콘텐츠만** 수집, 페이월은 무료 부분까지만. SNS·개인 블로그 소스 금지 (PROJECT_NOTES 1장)
5. 기사 건수는 **최대 10건** — 상한일 뿐 굳이 채우지 않음. 카테고리 비율 정책:기술:윤리 ≈ 2:4:2는
   **참고 기준** — 기사의 질·대주제 다양성이 우선 (PROJECT_NOTES 1장)
6. 사용자는 개발 경험이 적음 → 변경 사항은 쉬운 말로 설명하고, 새 라이브러리 도입 전에 이유를 먼저 설명할 것
7. **핸드오프·세션 인계 문서는 `docs/` 아래에 저장**(gitignore — 저장소 루트 금지). 인계 내용 중
   **계속 유효한 설계 결정은 `PROJECT_NOTES.md`에, 할 일은 `TODO.md`에 옮기고**, 문서에는 세션 한정 경위만 남긴다
8. **테스트 산출물은 종류 불문 `data/tests/<타임스탬프>/` 아래에 저장** (run과 같은 구조).
   아래 도구들이 폴더를 자동 생성하며, 새 테스트 도구도 기본 출력을 여기로 잡는다
   - `build_cardnews.py <카드> --test` — 카드뉴스 렌더 테스트
   - `test_writer_single.py` — 기사 1건 작가 테스트(카드 작성까지)
   - `test_writer_review_single.py` — 기사 1건 전 구간 테스트(작가 → 검토 → 반려 시 재작성 1회 → PNG 렌더)
   - `test_research_edit.py` — 조사자→편집장 구간 테스트
   - `test_dashboard_fixture.py make` / `serve` — 실제 run의 카드 복사본(픽스처)으로 8001 포트에 대시보드를 띄워
     편집·저장·발행 구간을 토큰 0으로 검증 (launch.json `dashboard-test`). 테스트 서버에서 [1단계 실행]·[초기화] 금지

## TODO 관리 방식
`TODO.md`는 시급도(최대한 빨리 / 되도록 이번 주까지 / 차순위 / 차차순위 / 나중에)를 heading으로 하고,
각 heading 아래 `(에이전트) 할 일` 항목을 에이전트 가나다순으로 정렬한다.
사용자가 새 할 일을 말하면 해당 시급도·에이전트 위치에 끼워 넣을 것.
