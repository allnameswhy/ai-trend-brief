# CLAUDE.md — AI TREND 자동 발간 시스템

이 파일은 Claude Code가 이 프로젝트에서 작업할 때 항상 참조하는 컨텍스트입니다.

## 프로젝트 개요
수작업으로 발간하던 격주 브리프(월 2회, 2·4주차) 『AI TREND — 해외 AI 정책·기술 동향 조사』를
멀티 에이전트 파이프라인으로 자동 생성하는 시스템.
**프로젝트 관리·명세의 단일 원천(spec)은 `PROJECT_NOTES.md`이다** — 기획·구조·설계 판단은 모두 여기를 따르고,
다른 문서와 어긋나면 `PROJECT_NOTES.md`가 우선한다. 할 일 목록만 `TODO.md`가 관리한다.
문서와 코드가 어긋나면 문서를 우선하되 사용자에게 확인할 것.

## 현재 상태 (1차 데모 완료)
- 파이프라인 4단계(조사자→편집장→작가→검토자) + 오케스트레이터 + FastAPI 서버 + 대시보드 골격 완성
- **데모 실행 결과 있음**: `data/runs/20260709_161328/` 1회(카드뉴스 JSON·PNG까지 산출). 이전 런·테스트 산출물은 2026-07-13 정리로 삭제
- **카드뉴스 v2 전환 완료 (2026-07-13)**: 카드 디자인을 불릿+완전한 문장형으로 개편(규격: `tools/cardnews/card_schema.md`).
  작가가 원고(item.md/draft.md) 없이 기사에서 **card_NN.json을 직접 작성**. 검토자가 카드마다 규격 기계 검사
  명령(`build_cardnews --check`)을 실행해 결과를 review.md에 기록하고(판정은 기계), 불합격은 반려 사유 —
  반려 시 작가 수정 1회(재검토 없음). 수정까지 끝난 **최종 카드는 오케스트레이터가 `build_cardnews`로 직접 PNG 렌더**
  (카드당 1회. 2026-07-20 변경 — 렌더는 판단이 필요 없는 결정적 작업이라 작가 2차 호출을 폐지하고 코드가 subprocess로
  실행, 작가의 Bash 권한도 제거). 즉 순서는 작성 → 검토(--check) → (반려 시)수정 → 렌더(코드 직접 실행).
  최초 작성 때는 렌더하지 않으므로 어떤 카드도 두 번 렌더되지 않으며, 누락 PNG는 로그로만 경고(자동 재렌더 없음).
  최종 확인은 사람이 PNG로. (반려 자동 수정은 대시보드 구현 전까지의 임시 정책 — TODO 참조)
- **2026-07-21 변경**: 오케스트레이터 `--resume`(중단 run 재개 — selected.json·기작성 카드 재사용),
  편집장이 탈락 최종후보(최대 5건+사유)를 `dropped.json`에 기록하고 오케스트레이터가 run 로그에 남김,
  조사자 저널 게재물 URL 제외(논문·Comment 기고 제외, 뉴스·매거진만), 편집장 선별 규칙 개편
  (AI 중심성 게이트·중복 클러스터링 2단계·글 유형 감점·카테고리 비율 유연화). 세부는 PROJECT_NOTES 2.1·2.2·2.8.2
- **HITL 대시보드 구축 완료 (2026-07-24)**: 파이프라인을 [1단계 조사·선별]→선정 대기(사람이 기사 확정)→
  [2단계 집필·검토]→카드 검토 대기(사람이 카드 수정)→[발행 PNG]로 분리. 오케스트레이터를
  `run_phase1`/`run_phase2`/`render_cards`로 분해(CLI `run_pipeline` 동작 불변), 산출물 누락은 `PipelineError`,
  상태는 run 폴더 `state.json`(서버 기록·재시작 복원), 중단 버튼(서브프로세스 정리 실측 확인), 오류·중단 복구
  3단계(편집장부터/카드부터 재개), 초기화 버튼(run 버리기 — `discarded` 표식으로 복원 제외, 폴더는 보존). 편집장이 `screened.json`(게이트 통과 url 목록) 추가 산출 — 선정 화면 후보 풀.
  사람 수정 백업: `selected_editor.json`·`card_NN_orig.json`.
  세부는 PROJECT_NOTES 0장·2.6·2.8.2. **주의: 발간 실행 시 uvicorn 은 `--reload` 없이**
- **발간 헤더 추가 (2026-07-30)**: 카드 묶음 맨 위 마스트헤드 `header.png`(1080×380, `tools/cardnews/header_template.html.j2`,
  사용자 확정 시안 재현). `render_cards`가 발행 시점 호수를 `month_week_label()`로 재계산해 **호수가 다른 카드 JSON 을
  갱신(호수 통일)**하고 헤더를 렌더(`build_cardnews.py --header "N월 N주"`). 세부는 PROJECT_NOTES 2.8.2·2.9
- **발행 렌더 크기 선택 (2026-08-05)**: 대시보드 [발행] 버튼 옆에서 원본 크기(2160px)/절반 크기(1080px) 선택 —
  `POST /publish` 본문 `{"scale": 2|1}` → `render_cards(scale)` → `build_cardnews.py --scale`. 절반 크기도
  PNG 축소가 아니라 HTML 에서 직접 렌더(선명도). 두 크기 모두 같은 `publish/` 폴더·같은 파일명(마지막 렌더가 남음).
  CLI(run_pipeline)는 원본 크기 고정. 세부는 PROJECT_NOTES 2.8.2
- **열람용 PDF 버튼 (2026-08-05)**: 대시보드 [열람용 PDF 생성] → `POST /publish/pdf` → `tools/build_cardnews_pdf.py`
  (publish/ HTML 을 Edge 인쇄로 병합, 텍스트 검색·복사 가능). PNG 발행 후에만 가능, 파이프라인 상태는 바꾸지 않음
  (끝나면 이전 상태 복귀). 세부는 PROJECT_NOTES 2.8.2
- **발간 표지 추가 (2026-08-05)**: 표지 `cover.png`(1080×1240, `tools/cardnews/cover_template.html.j2`, 사용자 확정
  시안 재현) — 호수 + **목차**(카테고리별 카드 title, 카드가 모두 완성된 뒤 run 폴더의 card_NN.json 을 코드가 직접
  읽어 채움. 에이전트 호출 없음)를 담고 안내문 등 나머지 문구는 하드코딩. 발행(PNG 렌더) 시 `render_cards`가
  **헤더·표지 둘 다 생성**(`--header`/`--cover`)하고, 어느 것을 카드 묶음 맨 위에 쓸지는 사람이 발송 때 고른다.
  세부는 PROJECT_NOTES 2.8.2·2.9
- **메일 발송 방식 확정 (2026-07-30 팀 결정)**: 스티비(Stibee) API 자동 발송 구상 **전면 폐기** — 발송은
  **사람이 스티비 웹에서 렌더된 PNG 를 직접 업로드**해 진행. **대시보드 역할은 PNG 렌더에서 끝난다.**
  (실험 결과: v2 API 에 이미지 업로드 엔드포인트 없음, data URI 내장 본문(약 18MB)은 기관 메일에서 표시 불가.
  관련 코드·템플릿(`backend/stibee.py`, `tools/build_mail_html.py`, 메일 템플릿 2종)·httpx 의존성 제거. 세부는 PROJECT_NOTES 2.9)
- 데모 범위에서 제외(추후 과제): 스케줄링, SQLite DB, 기록 관리자(⑤) 아카이브 참조
  (이메일 발송은 추후 과제가 아니라 **수동으로 확정** — 위 2026-07-30 항목)
- 원본 PDF 저장소: 데모 단계는 로컬(gitignore 폴더), 정식 운영 시 AWS S3 검토(20GB 기준 월 1천 원 미만, PROJECT_NOTES 3장 #3)

## 구조
```
.claude/agents/*.md   # 에이전트 정의(역할·도구). 행동 수정은 여기서. 코드 수정 불필요
backend/orchestrator.py  # 파이프라인 라이브러리: run_phase1(조사·선별)/run_phase2(집필·검토)/render_cards(PNG) + CLI용 run_pipeline
backend/main.py          # FastAPI: 상태 머신(선정 대기·카드 검토 대기 등 state.json), 단계 실행·중단·선정 확정·카드 편집·발행 API, SSE
backend/card_service.py  # build_cardnews 인프로세스 래퍼: 프리뷰 HTML 렌더·자수 검사·종합(Edge 실측) 검사
frontend/index.html      # HITL 대시보드: 제어·로그 / 기사 선정 / 카드 검토·편집 3화면 (프레임워크 없는 순수 HTML)
tools/build_cardnews.py  # 카드 렌더(HTML→PNG)와 규격 기계 검사(--check). 렌더는 오케스트레이터가 직접 실행, 검토자만 --check용 Bash 허용
tools/cardnews/          # card_schema.md(카드 규격 단일 원천) · card_template.html.j2(디자인) · card_v2_sample.json(견본) · header_template.html.j2(발간 헤더) · cover_template.html.j2(발간 표지 — 호수·목차) · nrf-symbol.png(심벌)
tools/test_writer_*.py   # 기사 1건짜리 부분 테스트 하네스 (아래 규칙 8)
data/runs/<timestamp>/   # 실행마다 생성: candidates.json → selected.json(+dropped.json+screened.json) → card_NN.json → review.md. 발행 산출물(header·cover·card_NN 의 HTML/PNG)은 publish/ 하위 폴더에 모음(2026-08-05). 대시보드 run은 state.json(상태)·selected_editor.json(편집장 원안 백업)·card_NN_orig.json(작가 원본 백업) 추가
data/tests/<timestamp>/  # 테스트 산출물 (run 과 같은 구조, 규칙 8)
logs/run_<timestamp>.log # 실행별 누적 작업 로그 (PROJECT_NOTES 2.10)
docs/                    # gitignore 대상 — 기존 발간물 원본, 핸드오프 문서 (예외: architecture.png 는 추적 중)
```

## 실행 방법
- 1차 데모(터미널): `python backend/orchestrator.py`
  - 부분 실행: `--selected <selected.json 경로>` — 조사자·편집장 건너뛰고 작가부터 (새 폴더)
  - 재개 실행: `--resume <run 폴더>` — 중단된 run을 그 폴더에서 이어서 (기작성 카드 재사용)
- 2차 데모(대시보드): `uvicorn backend.main:app` 후 http://localhost:8000
  (**발간 실행 시 `--reload` 금지** — 파일 저장 시 실행 중 파이프라인이 죽음. 개발 중에만 사용)

## 프로젝트 규칙 (반드시 준수)
1. **파일명은 영어**, 문서·주석·에이전트 프롬프트는 한국어 (PROJECT_NOTES 2.0)
   - **예외**: `docs/` 아래 기존 발간물(자동화 이전 수작업 원본)은 원제(한글) 그대로 유지한다. 구조 부분만 통일(`AITrend`/`RnD-Brief`/`NRF-IssueReport`_연도-호수_원제)
2. **자동 발송 절대 금지** — 파이프라인은 카드+검토 로그 제시 후 반드시 멈춘다 (HITL, 2.9)
3. 작가 재작성 **피드백 루프는 1회로 고정** (2.8)
4. 데모 단계: **무료 공개 콘텐츠만** 수집, 페이월은 무료 부분까지만 (2.0). SNS·개인 블로그 소스 금지 (PROJECT_NOTES 3장 #2)
5. 기사 건수는 **최대 10건** — 모니터링 기간이 2주(격주 발간)로 짧아 굳이 채우지 않음(8건 등 가능, 10건 초과 불가). 카테고리 비율 정책:기술:윤리 ≈ 2:4:2는 **참고 기준** — 엄격히 맞추지 않으며 기사의 질·대주제 다양성이 우선 (2026-07-09 변경, 구 2.2 대체; 비율 유연화는 editor.md 반영)
6. 사용자는 개발 경험이 적음 → 변경 사항은 쉬운 말로 설명하고, 새 라이브러리 도입 전에 이유를 먼저 설명할 것
7. **핸드오프·세션 인계 문서는 `docs/` 아래에 저장** — `docs/`는 gitignore 대상이라 저장소를 오염시키지 않는다. 저장소 루트에 두지 말 것. 인계 내용 중 **계속 유효한 기획·설계 결정은 `PROJECT_NOTES.md`에, 할 일은 `TODO.md`에 옮기고**, 핸드오프 문서에는 그 세션 한정의 경위만 남긴다 (2026-07-15 지시)
8. **테스트 산출물은 종류 불문 `data/tests/<타임스탬프>/` 아래에 저장**  (`data/runs/<타임스탬프>/` 와 같은 구조 — 매 테스트 실행이 자기 타임스탬프 폴더를 가진다). 아래 도구들이 그 폴더를 자동 생성한다. 새 테스트 도구도 기본 출력을 여기로 잡는다 (2026-07-09 지시, 2026-07-14 run 과 동일 구조로 통일)
   - `build_cardnews.py <카드> --test` — 카드뉴스 렌더 테스트
   - `test_writer_single.py` — 기사 1건 작가 테스트(카드 작성까지)
   - `test_writer_review_single.py` — 기사 1건 전 구간 테스트(작가 → 검토 → 반려 시 재작성 1회 → PNG 렌더)

## TODO 관리 방식
`TODO.md`는 시급도(최대한 빨리 / 되도록 이번 주까지 / 차순위 / 차차순위 / 나중에)를 heading으로 하고,
각 heading 아래 `(에이전트) 할 일` 항목을 에이전트 가나다순으로 정렬한다.
사용자가 새 할 일을 말하면 해당 시급도·에이전트 위치에 끼워 넣을 것.

## 다음 작업 순서 (Work Plan, PROJECT_NOTES 4장)
1. ✅ 에이전트 정의 → 2. ✅ 파이프라인 1차 데모(카드뉴스 v2 전환 포함 — 개편 후 전체 run 검증은 미실시) → 3. ✅ **대시보드 구축(2026-07-24 완료)** — 중간 개입(선정 확정·카드 편집·발행) 포함. 남은 것: 아카이브 열람 UI(4단계와 함께), 대시보드 전체 리허설 run 1회 → 4. 발간물 DB(SQLite)·아카이브 참조 → 5. 2차 데모 → 6. 테스트 발송
