"""
오케스트레이터 (Orchestrator)
------------------------------
조사자 → 편집장 → 작가(기사별 카드 작성) → 검토자 → PNG 렌더(직접 실행) 순서를 지휘하는 '지휘자' 코드입니다.
- 실행 순서 강제 / 단계 산출물(파일) 전달
- 작가는 기사 1건당 카드뉴스 JSON(card_NN.json) 1장을 직접 작성 (2026-07-13 변경 —
  원고(item.md/draft.md)를 만들고 다시 카드로 압축하던 중간 단계 폐지: 비용 절감 + 재압축 손실 제거)
- 검토자가 카드마다 규격 기계 검사 명령(build_cardnews --check: 자수 규격+렌더 실측)을 실행해
  그 결과를 review.md 에 기록 — 판정은 명령(기계)이 하고, 검토자는 실행·기록과 내용 검증을 맡는다
- 반려(REVISE) 시 작가 재작성 루프 1회 — 내용 지적과 규격 불합격을 함께 수정 (PROJECT_NOTES 2.8)
- PNG는 검사 결과와 무관하게 전 카드 생성 — 렌더는 오케스트레이터가 직접 실행하고(판단이 필요 없는
  결정적 작업이라 에이전트를 쓰지 않는다, 2026-07-20 변경), 최종 확인은 사람이 PNG로 한다
- 각 단계를 타임스탬프 로그 파일로 기록 (PROJECT_NOTES 2.10)
- 마지막에 자동 발송하지 않고, 카드 + 검토 로그 경로를 제시하고 멈춤 (Human-in-the-loop)

구조 (2026-07-24 대시보드 대응 분리):
  run_phase1()  ① 조사자 → ② 편집장            (선정 대기 지점까지)
  run_phase2()  ③ 작가 → ④ 검토자 → 반려 수정   (카드 검토 대기 지점까지, PNG 없음)
  render_cards() ⑤ PNG 렌더                      (발행 단계)
  run_pipeline() 위 셋을 쉬지 않고 이어 실행하는 CLI 용 조립 — 동작은 종전과 동일
대시보드 서버(backend/main.py)는 run_pipeline 대신 단계 함수를 따로 불러,
단계 사이에 사람의 확인(기사 선정 확정 / 카드 최종 검토)을 끼워 넣는다.

실행 방법 두 가지:
  1) 터미널에서 직접:      python backend/orchestrator.py   ← 1차 데모
  2) FastAPI 서버가 호출:  backend/main.py 가 단계 함수를 불러 대시보드로 로그 스트리밍

각 에이전트의 '성격(시스템 프롬프트)'은 .claude/agents/*.md 파일에 있습니다.
→ 에이전트 행동을 바꾸고 싶으면 그 마크다운 파일만 고치면 됩니다. (코드 수정 불필요)
"""

import asyncio
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Windows(한국어)에서 출력이 파이프·파일로 연결되면 cp949 인코딩이 적용되어
# 이모지(⚠️)나 특수문자(—) 출력 시 UnicodeEncodeError 로 프로그램이 죽는다.
# → 출력을 UTF-8 로 강제한다. (터미널 직접 실행·파이프 모두 안전)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

# 프로젝트 최상위 폴더 (이 파일의 상위의 상위)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = PROJECT_ROOT / ".claude" / "agents"
LOGS_DIR = PROJECT_ROOT / "logs"
PUBLISH_SUBDIR = "publish"   # 발행(render_cards) 산출물 하위 폴더명 — run 루트의 중간 산출물과 분리 (2026-08-05)

# 검토자가 실행하는 카드 도구 명령 (--check: 자수 검사·렌더 실측). tools/build_cardnews.py.
# PNG 렌더도 같은 도구를 쓰지만 그것은 오케스트레이터가 직접 실행한다 (아래 render_cards).
# sys.executable = 지금 파이프라인을 돌리는 파이썬(가상환경) — 에이전트도 같은 환경을 쓰게 한다.
CARD_TOOL_CMD = f"{Path(sys.executable).as_posix()} {(PROJECT_ROOT / 'tools' / 'build_cardnews.py').as_posix()}"

# .env 의 ANTHROPIC_API_KEY 를 환경변수로 로드 (파일이 없으면 조용히 넘어감)
load_dotenv(PROJECT_ROOT / ".env")

# 단계별로 각 에이전트에게 허용할 도구 (.claude/agents/*.md 의 frontmatter 와 맞춰 둠)
# 검토자의 Bash 는 카드 도구 명령(build_cardnews --check) 하나만 허용 (최소 권한).
# 작가는 Bash 없음 — PNG 렌더는 오케스트레이터가 직접 실행한다 (2026-07-20 변경).
# 편집장의 WebFetch 는 숏리스트 원문 검토용 (editor.md 작업 순서 2단계. 2026-07-21 추가 —
# 빠져 있던 탓에 편집장이 권한 거부를 서브에이전트·ToolSearch 로 우회하다 토큰을 크게 낭비함)
ALLOWED_TOOLS = {
    "researcher": ["WebSearch", "WebFetch", "Read", "Write"],
    "editor": ["WebSearch", "WebFetch", "Read", "Write"],
    "writer": ["WebSearch", "WebFetch", "Read", "Write"],
    "reviewer": ["WebSearch", "WebFetch", "Read", "Write", f"Bash({CARD_TOOL_CMD}:*)"],
}

# 허용 목록 밖 도구는 차단한다 (2026-07-21 추가). allowed_tools 는 '자동 승인' 목록일 뿐
# 나머지 도구가 사라지는 게 아니어서, 에이전트가 권한 없는 도구를 시도하다 거부당하고
# 우회(서브에이전트 생성 등)하며 턴을 낭비하는 문제가 실측됨 — 아예 못 보게 막는다.
# 검토자만 Bash 예외 (카드 기계 검사 명령용).
_BLOCKED = ["Agent", "Task", "ToolSearch", "Bash", "PowerShell", "Glob", "Grep"]
DISALLOWED_TOOLS = {
    "researcher": _BLOCKED,
    "editor": _BLOCKED,
    "writer": _BLOCKED,
    "reviewer": [t for t in _BLOCKED if t != "Bash"],
}

# 에이전트별 모델 지정 (2026-07-13 확정 — 미지정 시 SDK 기본 모델을 따라가 비용을 예측할 수 없음)
# 가격(1M 토큰당 입력/출력): Opus 4.8 $5/$25, Sonnet 5 $3/$15 (2026-08까지 $2/$10)
# 글쓰기 품질이 중요한 작가·검토자는 Opus, 수집·선별 위주인 조사자·편집장은 Sonnet
MODELS = {
    "researcher": "claude-sonnet-5",
    "editor": "claude-sonnet-5",
    "writer": "claude-opus-4-8",
    "reviewer": "claude-opus-4-8",
}

# 에이전트 지시문 — 테스트 하네스(tools/test_research_edit.py)도 이 상수를 import 해
# 실제 파이프라인과 지시문이 어긋나지 않게 한다 (복붙본 낡음 방지).
RESEARCHER_TASK = "소스 목록을 돌며 최근 AI 관련 기사를 수집하고, candidates.json 파일에 저장하세요."
EDITOR_TASK = (
    "candidates.json 을 읽고, 최종 10건을 카테고리 배분에 맞춰 확정한 뒤 selected.json 에 저장하세요. "
    "탈락한 최종 후보(최대 5건)는 사유와 함께 dropped.json 에 저장하세요. "
    "그리고 1차 압축에서 필수 게이트를 통과한 후보 전체의 url 목록을 screened.json 에 저장하세요 "
    "(숏리스트 20건이 아니라 게이트 통과 전체입니다 — 형식은 시스템 프롬프트의 산출물 절 참조)."
)


class PipelineError(Exception):
    """진행 불가 오류 — 단계 산출물 누락 등, 뒤 단계를 계속할 수 없는 상태.
    던지기 전에 원인을 logger 로 이미 기록해 둔다. CLI(run_pipeline)는 이 예외를 잡아
    종전처럼 조용히 종료하고, 대시보드 서버(main.py)는 잡아서 오류 상태로 전환한다."""


def load_role(agent_name: str) -> str:
    """.claude/agents/{name}.md 에서 YAML frontmatter(--- ... ---)를 걷어내고
    본문(시스템 프롬프트)만 반환한다."""
    text = (AGENTS_DIR / f"{agent_name}.md").read_text(encoding="utf-8")
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            return parts[2].strip()
    return text.strip()


async def run_agent(agent_name: str, task: str, run_dir: Path, on_log) -> None:
    """한 에이전트를 실행한다. 에이전트는 run_dir 안에서 파일을 읽고 쓴다.

    on_log(stage, message, kind) 의 kind 는 화면 표시용 분류:
      "log"(진행 로그, 기본) / "text"(에이전트 발화 — 전문을 그대로 넘김) / "tool"(도구 사용).
    취소(CancelledError)는 여기서 잡지 않는다 — SDK 제너레이터의 정리 경로(서브프로세스 종료)가
    돌도록 그대로 통과시키고, 최상위(서버의 태스크 래퍼)에서만 흡수한다.
    """
    on_log(agent_name, "시작", "log")

    # 에이전트가 산출물 저장 위치를 임의로 정하지 않도록, 작업 폴더를 지시문에 명시한다.
    # (위치를 안 알려주면 에이전트가 프로젝트를 둘러보고 그럴듯한 곳을 골라버리는 문제가 실제로 발생)
    task = (
        f"작업 폴더: {run_dir}\n"
        f"산출물(파일 쓰기)은 반드시 이 작업 폴더 안에 저장하세요. 다른 위치에 저장하지 마세요.\n"
        f"(단, 지시문에 다른 경로의 참고 자료가 명시된 경우 그 파일은 읽어도 됩니다.)\n\n"
        f"{task}"
    )

    options = ClaudeAgentOptions(
        system_prompt=load_role(agent_name),   # 에이전트의 역할 = .md 본문
        allowed_tools=ALLOWED_TOOLS[agent_name],
        disallowed_tools=DISALLOWED_TOOLS[agent_name],  # 허용 밖 도구 차단 (위 참조)
        model=MODELS[agent_name],              # 에이전트별 모델 고정 (위 MODELS 참조)
        permission_mode="acceptEdits",         # 파일 쓰기 자동 승인 (비대화형 실행용)
        cwd=str(run_dir),                      # 에이전트의 작업 폴더 = 이번 run 폴더
        max_turns=50,                          # 무한 루프 방지 안전장치
    )

    async for message in query(prompt=task, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    # 도구 입력 요약을 함께 남긴다 (2026-07-21 추가 — 편집장의 WebFetch 횟수처럼
                    # '무엇에' 도구를 썼는지가 로그만으로 판별되도록. 대표 입력 하나만 100자까지)
                    detail = ""
                    if isinstance(block.input, dict):
                        val = next((block.input[k] for k in ("url", "query", "file_path", "command")
                                    if block.input.get(k)), "")
                        if val:
                            detail = f" — {str(val)[:100]}"
                    on_log(agent_name, f"도구 사용: {block.name}{detail}", "tool")
                elif isinstance(block, TextBlock):
                    text = block.text.strip()
                    if text:
                        # 전문을 그대로 넘긴다 — 파일 기록·터미널 출력의 160자 축약은
                        # 로거(make_logger)와 기본 print 콜백이 각자 처리한다.
                        on_log(agent_name, text, "text")
        elif isinstance(message, ResultMessage):
            if message.is_error:
                on_log(agent_name, f"⚠️ 오류로 종료 (subtype={message.subtype})", "log")
            cost = message.total_cost_usd
            if cost is not None:
                on_log(agent_name, f"완료 (턴 {message.num_turns}회, 비용 ${cost:.4f})", "log")
            else:
                on_log(agent_name, f"완료 (턴 {message.num_turns}회)", "log")


def month_week_label(now: datetime) -> str:
    """발행 주차 표기 — 그 달의 몇 번째 7일 구간 (예: '8월 1주'). 표지·카드 호수의 단일 원천."""
    return f"{now.month}월 {(now.day - 1) // 7 + 1}주"


def new_run_dir(output_base: Path | None = None) -> Path:
    """새 run 폴더(data/runs/<타임스탬프>/)를 만들어 반환한다.
    테스트는 output_base 로 data/tests 를 넘겨 실제 발간 run 과 섞이지 않게 한다."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_dir = output_base or (PROJECT_ROOT / "data" / "runs")
    run_dir = base_dir / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def make_logger(run_dir: Path, on_log=None, output_base: Path | None = None):
    """파일 기록 + on_log 콜백을 병행하는 로거를 만든다 (PROJECT_NOTES 2.10).

    logger(stage, message, kind="log") 형태로 호출한다.
    - 파일에는 종전 형식 그대로 남긴다. 에이전트 발화(kind="text")만 종전처럼
      한 줄 160자로 줄여 기록하고, on_log 콜백에는 전문을 그대로 넘긴다
      (대시보드가 사고과정을 온전히 표시하기 위함 — 추가 토큰 비용 없음).
    - 실제 run 은 logs/ 에, 테스트(output_base 지정)는 산출물 폴더 안에 로그를 남긴다.
    """
    if on_log is None:
        def on_log(stage, message, kind="log"):
            if kind == "text":
                message = message.replace("\n", " ")[:160]
            print(f"[{datetime.now():%H:%M:%S}] {stage:12s} | {message}")

    ts = run_dir.name
    if output_base is not None:
        log_file = run_dir / f"run_{ts}.log"
    else:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        log_file = LOGS_DIR / f"run_{ts}.log"

    def logger(stage, message, kind="log"):
        file_msg = message.replace("\n", " ")[:160] if kind == "text" else message
        line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {stage} | {file_msg}"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        on_log(stage, message, kind)

    logger.log_file = log_file   # 호출측(result dict 등)에서 로그 경로를 참조할 수 있게
    return logger


def require_output(run_dir: Path, filename: str, logger) -> None:
    """단계 산출물이 안 생겼으면 로그를 남기고 PipelineError 를 던진다.
    뒤 단계를 헛돌리지 않도록 파이프라인을 멈추는 용도."""
    if (run_dir / filename).exists():
        return
    logger("orchestrator", f"기대한 산출물({filename})이 생성되지 않았습니다. 이후 단계를 중단합니다.")
    raise PipelineError(f"산출물 누락: {filename}")


async def run_phase1(run_dir: Path, logger, resume: bool = False) -> None:
    """1단계: ① 조사자(수집) → ② 편집장(선별). 끝나면 selected.json + dropped.json + screened.json.

    resume=True 이고 candidates.json 이 이미 있으면 조사자를 건너뛰고 편집장부터
    다시 실행한다 (오류·중단된 run 의 수집 결과 재사용 — 비용 절약).
    """
    if resume and (run_dir / "candidates.json").exists():
        logger("orchestrator", "조사자 건너뜀 — 기존 candidates.json 재사용 (편집장부터 재개)")
    else:
        # ① 조사자 — 소스에서 후보 기사 수집 → candidates.json
        await run_agent("researcher", RESEARCHER_TASK, run_dir, logger)
        require_output(run_dir, "candidates.json", logger)

    # ② 편집장 — 후보 중 최종 10건 확정 → selected.json (+ dropped.json, screened.json)
    await run_agent("editor", EDITOR_TASK, run_dir, logger)
    require_output(run_dir, "selected.json", logger)

    # 편집장이 남긴 탈락 최종후보 기록(dropped.json)을 run 로그에도 남긴다 (2026-07-21 추가 —
    # 에이전트의 화면 출력(TextBlock)은 160자에서 잘리므로, 파일로 받아 오케스트레이터가 온전히 기록한다)
    log_dropped(run_dir, logger)


async def run_phase2(run_dir: Path, logger, resume: bool = False) -> int:
    """2단계: ③ 작가(기사별 카드 작성) → ④ 검토자 → 반려(REVISE) 시 작가 수정 1회.
    PNG 렌더는 하지 않는다 — 발행 단계(render_cards)에서 별도 실행한다.
    resume=True 면 이미 정상 작성된 card_NN.json 은 건너뛴다 (없거나 깨진 카드만 재작성).
    반환값: 카드(기사) 수.
    """
    # ③ 작가 — 기사별로 카드뉴스 JSON(card_NN.json)을 직접 작성한다 (기사 1건 = 호출 1회).
    #    원고(item.md)→draft.md→카드 변환의 중간 단계를 없앰 (2026-07-13 변경):
    #    원문을 읽고 바로 카드 규격으로 요약하는 편이 싸고, 재압축하며 뜻이 사라지는 문제도 없다.
    #    (기사별 호출 유지 이유: 10건을 한 세션에 맡기면 품질이 얕아지는 문제 확인됨)
    require_output(run_dir, "selected.json", logger)   # 함수 단독 호출 시에도 명확한 오류가 나도록
    items = json.loads((run_dir / "selected.json").read_text(encoding="utf-8"))
    category_order = {"정책": 0, "기술": 1, "윤리": 2}
    items.sort(key=lambda it: category_order.get(it.get("category"), 9))

    # 카드 규격은 writer.md 에 싣지 않고 지시문에만 첨부한다 (단일 원천: card_schema.md)
    card_spec = (PROJECT_ROOT / "tools" / "cardnews" / "card_schema.md").read_text(encoding="utf-8")
    week_label = month_week_label(datetime.now())   # 집필 시점 주차 — 발행 때 render_cards 가 다시 통일함

    for idx, item in enumerate(items, start=1):
        # 재개 실행이면 이미 작성된 카드는 작가를 부르지 않는다 (비용 절약).
        # selected.json 이 같으면 정렬(stable sort)도 같아 번호-기사 대응이 유지된다.
        # 단, 파일이 있어도 JSON 이 깨져 있으면(작성 도중 중단 등) 다시 작성한다.
        card_file = run_dir / f"card_{idx:02d}.json"
        if resume and card_file.exists():
            try:
                json.loads(card_file.read_text(encoding="utf-8"))
                logger("orchestrator", f"작가 건너뜀: card_{idx:02d}.json 이미 있음 (재사용, {idx}/{len(items)}건)")
                continue
            except (json.JSONDecodeError, UnicodeDecodeError):
                logger("orchestrator", f"card_{idx:02d}.json 이 깨져 있어 다시 작성합니다")
        input_file = f"item_input_{idx:02d}.json"
        (run_dir / input_file).write_text(
            json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        await run_agent(
            "writer",
            f"{input_file} 에 담긴 기사 1건을 읽고, 원문 URL에 접속해 내용을 확인한 뒤 "
            f"아래 카드 규격에 따라 card_{idx:02d}.json 을 작성하세요. "
            f'article_no는 "{idx:02d}", week_label은 "{week_label}" 입니다.\n'
            f"(PNG 렌더는 검토가 끝난 뒤 시스템이 자동 실행합니다 — JSON 작성만 하세요.)\n\n"
            f"--- 카드 규격 (원천: tools/cardnews/card_schema.md) ---\n\n{card_spec}",
            run_dir, logger,
        )
        require_output(run_dir, f"card_{idx:02d}.json", logger)
        logger("orchestrator", f"작가 진행: {idx}/{len(items)}건 완료")

    # ④ 검토자 — 카드마다 규격 기계 검사(명령 실행) + 내용의 출처·사실관계·요약 품질 검증 → review.md
    #    규격 판정은 검토자가 직접 하지 않고 아래 명령(자수 규격 + 렌더 실측)이 한다.
    await run_agent(
        "reviewer",
        f"card_01.json ~ card_{len(items):02d}.json ({len(items)}건)의 카드뉴스 콘텐츠를 검증하고 "
        f"결과를 review.md 에 저장하세요.\n"
        f"먼저 카드마다 아래 규격 기계 검사 명령을 반드시 실행하고, 그 출력(합격/불합격 사유)을 "
        f"review.md 에 그대로 기록하세요. 규격 합격 여부를 직접 판단하지 마세요 — 판정은 이 명령이 합니다:\n"
        f"  {CARD_TOOL_CMD} card_01.json --check   (card_01.json 부분을 각 카드 파일명으로 바꿔 실행)\n"
        f"기계 검사 불합격도 반려(REVISE) 사유입니다. "
        f"마지막 줄에 'VERDICT: PASS' 또는 'VERDICT: REVISE' 를 남기세요.",
        run_dir, logger,
    )
    require_output(run_dir, "review.md", logger)

    # 피드백 루프 1회 — REVISE 면 작가가 지적된 카드만 수정한다 (2차 검토는 하지 않음, 2026-07-14 지시).
    verdict = read_verdict(run_dir)
    logger("orchestrator", f"검토 판정: {verdict}")
    if verdict == "REVISE":
        logger("orchestrator", "반려 → 작가 수정 1회 진행 (지적된 카드 수정, 재검토 없음)")
        await run_agent(
            "writer",
            "review.md 의 지적 사항을 반영해 해당 카드 파일(card_XX.json)들을 수정하세요. "
            "필드 구조(스키마)는 그대로 유지하되, 자수·넘침 등 규격 불합격 지적은 기계 검사 "
            "출력에 적힌 수치(현재/한도, 넘침 분량)만큼만 최소한으로 줄이세요 — 지적되지 않은 "
            "카드·문장을 손대거나 필요 이상으로 줄이지 마세요. "
            "(재작성 기회는 더 이상 없습니다. PNG 렌더는 수정이 끝나면 시스템이 자동 실행합니다.)",
            run_dir, logger,
        )
    return len(items)


async def run_pipeline(on_log=None, selected_from: str | None = None,
                       output_base: Path | None = None,
                       resume_dir: str | None = None) -> dict:
    """전체 파이프라인 1회 실행 — 1단계 + 2단계 + PNG 렌더를 쉬지 않고 이어 실행하는 CLI 용 조립.
    on_log(stage, message, kind) 콜백으로 로그를 내보낸다.
    대시보드 서버(main.py)는 이 함수 대신 run_phase1/run_phase2/render_cards 를 단계별로 부른다.

    selected_from: 기존 selected.json 경로를 주면 조사자·편집장 단계를 건너뛰고
                   그 확정 기사 목록으로 작가 단계부터 시작한다. (부분 실행용)
    output_base:   산출물 폴더의 상위 위치(기본 data/runs). 테스트는 data/tests 를 넘겨
                   실제 발간 run 과 섞이지 않게 한다. 이 경우 로그도 산출물 폴더 안에 둔다
                   (테스트 산출물은 data/tests/<타임스탬프>/ 로 모으는 규칙).
    resume_dir:    중단된 기존 run 폴더 경로. 새 폴더를 만들지 않고 그 폴더에서 이어서
                   실행한다 — selected.json 재사용(조사자·편집장 건너뜀), 이미 작성된
                   card_NN.json 은 작가를 부르지 않고 재사용. 검토 이후 단계는 원래대로.
                   로그도 그 run 의 기존 로그 파일에 이어 쓴다. (2026-07-21 추가 —
                   작가 단계까지 끝나고 중단된 run 을 낭비 없이 재개하는 용도)
    """
    if resume_dir:
        # 재개 실행: 새 타임스탬프 폴더를 만들지 않고 기존 run 폴더를 그대로 쓴다.
        run_dir = Path(resume_dir).resolve()
    else:
        run_dir = new_run_dir(output_base)
    ts = run_dir.name          # 폴더명이 곧 타임스탬프 → 재개 시 로그도 같은 파일에 이어 씀

    logger = make_logger(run_dir, on_log, output_base)
    logger("orchestrator", f"발간 파이프라인 시작 (run={ts})")

    result = {"run_dir": str(run_dir), "cards": [], "review": None, "log": str(logger.log_file)}

    try:
        if resume_dir:
            # 재개 실행: 폴더 안의 기존 selected.json 을 그대로 쓴다 (없으면 재개 불가).
            if not (run_dir / "selected.json").exists():
                logger("orchestrator",
                       "재개 실패: 폴더에 selected.json 이 없습니다. 조사자·편집장부터 처음 실행하세요.")
                raise PipelineError("재개 실패: selected.json 없음")
            logger("orchestrator", f"재개 실행 — selected.json 재사용, 이미 작성된 카드는 건너뜀: {run_dir}")
        elif selected_from:
            # 부분 실행: 기존 확정 기사 목록을 복사해 오고 조사자·편집장은 건너뛴다.
            shutil.copy(Path(selected_from), run_dir / "selected.json")
            logger("orchestrator", f"조사자·편집장 단계 건너뜀 — 기존 확정 기사 목록 재사용: {selected_from}")
        else:
            await run_phase1(run_dir, logger)

        n = await run_phase2(run_dir, logger, resume=bool(resume_dir))

        # ⑤ 렌더 — 검토(및 반려 수정)가 끝난 최종 카드를 오케스트레이터가 직접 PNG로 뽑는다 (카드당 1회).
        #    렌더는 판단이 필요 없는 결정적 작업이라 에이전트를 부르지 않는다 (2026-07-20 변경).
        #    최초 작성 때 렌더하지 않으므로, 반려로 바뀐 카드도 두 번 렌더되지 않는다.
        render_cards(run_dir, n, logger)

        # 렌더 결과 집계 — 누락 PNG가 있으면 로그로 알린다 (자동 재렌더는 하지 않음).
        made, missing = png_status(run_dir, n)
        if missing:
            logger("orchestrator",
                   f"PNG 누락 {len(missing)}건: {', '.join(missing)} — 렌더 실패, 위 로그의 오류 확인. "
                   f"직접 렌더: python tools/build_cardnews.py {run_dir}\\card_XX.json --out {run_dir}\\{PUBLISH_SUBDIR}")
        logger("orchestrator", f"PNG 상태: {made}/{n}건 존재")

        logger("orchestrator", "파이프라인 종료")

        # Human-in-the-loop: 자동 발송하지 않는다. 결과물 위치만 알려주고 사람의 판단을 기다린다.
        review = run_dir / "review.md"
        logger("orchestrator",
               f"검토 대기: 카드 {n}건(PNG {made}건, 위치={run_dir / PUBLISH_SUBDIR}), 검토 로그={review} "
               f"— 검토 로그의 기계 검사 결과와 PNG를 확인하고 발송 여부를 결정하세요. (자동 발송 없음) "
               f"직접 수정 시 렌더: python tools/build_cardnews.py {run_dir}\\card_XX.json --out {run_dir}\\{PUBLISH_SUBDIR}")

        result["cards"] = [str(run_dir / f"card_{idx:02d}.json") for idx in range(1, n + 1)]
        result["review"] = str(review)
    except PipelineError:
        # 원인은 이미 로그에 남았다 — CLI 는 종전처럼 그 시점의 result 로 조용히 종료한다.
        return result
    return result


def render_cards(run_dir: Path, count: int, on_log) -> None:
    """확정된 카드(card_01 ~ card_NN)와 발간 헤더·표지를 PNG로 렌더한다 — 오케스트레이터가 직접 실행.
    렌더는 판단이 필요 없는 결정적 작업이라 에이전트를 쓰지 않는다 (2026-07-20 변경).
    호수는 발행(렌더) 시점에 다시 계산해 헤더·표지와 전 카드에 통일한다 (2026-07-30 변경 —
    집필과 발행이 주가 다르면 카드 알약·헤더/표지 표시가 어긋나므로, 발행일 기준으로 맞춘다).
    표지 cover.png(1080×1240)는 호수와 목차(카테고리별 카드 title)를 담고, build_cardnews --cover 가
    run 폴더의 card_NN.json 을 직접 읽어 목차를 채운다 (2026-08-05 추가 — 에이전트 호출 없음).
    헤더(1080×380)·표지 둘 다 만들어 두고, 어느 것을 카드 묶음 맨 위에 쓸지는 사람이 발송 때 고른다.
    발행 산출물(헤더·표지·카드 HTML/PNG)은 run 루트가 아니라 publish/ 하위 폴더에 모은다
    (2026-08-05 — JSON 등 중간 산출물과 분리해 스티비 업로드용 파일만 한곳에).
    한 카드가 실패해도 나머지는 계속 시도한다 (PNG는 전 카드 생성 원칙, 최종 확인은 사람).
    ※ 동기 함수 — 서버(main.py)에서는 asyncio.to_thread 로 감싸 이벤트 루프를 막지 않는다."""
    tool = PROJECT_ROOT / "tools" / "build_cardnews.py"

    # ① 발행 시점 호수 계산 + 카드 JSON 통일 (card_NN_orig.json 등 백업은 건드리지 않음)
    label = month_week_label(datetime.now())
    updated = 0
    for idx in range(1, count + 1):
        card_file = run_dir / f"card_{idx:02d}.json"
        try:
            card = json.loads(card_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue   # 파일 문제는 아래 렌더 단계가 로그로 알린다
        if card.get("week_label") != label:
            card["week_label"] = label
            card_file.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
            updated += 1
    if updated:
        on_log("orchestrator", f"호수 통일: '{label}' — 카드 {updated}건 갱신", "log")

    # 발행 산출물은 publish/ 하위 폴더로 — 렌더 명령마다 --out 으로 지정 (cwd=run 폴더 기준 상대 경로)
    (run_dir / PUBLISH_SUBDIR).mkdir(exist_ok=True)
    on_log("orchestrator", f"발행 산출물 폴더: {run_dir / PUBLISH_SUBDIR}", "log")

    # ② 발간 헤더·표지 렌더 (실패해도 카드 렌더는 계속) — 둘 다 만들어 두고 사람이 발송 때 골라 쓴다.
    #    표지 목차가 카드 title을 읽으므로 ① 호수 통일 뒤에 실행
    for flag, name in (("--header", "header(발간 헤더)"), ("--cover", "cover(표지)")):
        proc = subprocess.run(
            [sys.executable, str(tool), flag, label, "--out", PUBLISH_SUBDIR],
            cwd=str(run_dir), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        if proc.returncode == 0:
            on_log("orchestrator", f"렌더 완료: {name}", "log")
        else:
            lines = (proc.stderr or proc.stdout or "").strip().splitlines()
            detail = lines[-1] if lines else "출력 없음"
            on_log("orchestrator", f"렌더 실패: {name} (exit={proc.returncode}) — {detail}", "log")

    # ③ 카드 렌더
    for idx in range(1, count + 1):
        card = f"card_{idx:02d}.json"
        proc = subprocess.run(
            [sys.executable, str(tool), card, "--out", PUBLISH_SUBDIR],
            cwd=str(run_dir), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        if proc.returncode == 0:
            on_log("orchestrator", f"렌더 완료: {card}", "log")
        else:
            lines = (proc.stderr or proc.stdout or "").strip().splitlines()
            detail = lines[-1] if lines else "출력 없음"
            on_log("orchestrator", f"렌더 실패: {card} (exit={proc.returncode}) — {detail}", "log")


def png_status(run_dir: Path, count: int) -> tuple[int, list[str]]:
    """렌더 결과 집계 — (생성된 PNG 수, 누락 카드 이름 목록)을 돌려준다.
    PNG 는 발행 산출물 폴더(publish/)에서 찾는다 (2026-08-05 변경)."""
    missing = [f"card_{idx:02d}" for idx in range(1, count + 1)
               if not (run_dir / PUBLISH_SUBDIR / f"card_{idx:02d}.png").exists()]
    return count - len(missing), missing


def log_dropped(run_dir: Path, on_log) -> None:
    """편집장이 기록한 탈락 최종후보(dropped.json)를 run 로그에 한 줄씩 남긴다 (2026-07-21 추가).
    기록용 정보라 파일이 없거나 형식이 틀려도 파이프라인은 계속 진행한다."""
    path = run_dir / "dropped.json"
    if not path.exists():
        on_log("orchestrator", "dropped.json 없음 — 편집장이 탈락 최종후보를 기록하지 않았습니다.", "log")
        return
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        on_log("orchestrator", f"dropped.json 읽기 실패 — 기록 생략 ({e})", "log")
        return
    if not isinstance(entries, list) or not entries:
        on_log("orchestrator", "탈락 최종후보 없음 (숏리스트가 전원 선정됨)", "log")
        return
    shown = entries[:5]   # 편집장 지시가 최대 5건이지만, 초과 기록돼도 5건까지만 로그에 남긴다
    note = f" (기록 {len(entries)}건 중 5건만 표시)" if len(entries) > 5 else ""
    on_log("orchestrator", f"탈락 최종후보 {len(shown)}건 (편집장 기록){note}:", "log")
    for i, e in enumerate(shown, 1):
        title = e.get("title_ko") or e.get("title_en") or e.get("title") or "(제목 없음)"
        reason = e.get("reason") or "(사유 없음)"
        on_log("orchestrator", f"  탈락 {i}. {title} — {reason}", "log")


def read_verdict(run_dir: Path) -> str:
    """review.md 마지막 줄 부근의 VERDICT 를 읽는다. 파일이 없으면 PASS 로 간주."""
    review = run_dir / "review.md"
    if not review.exists():
        return "PASS"
    for line in reversed(review.read_text(encoding="utf-8").splitlines()):
        if "VERDICT:" in line.upper():
            return "REVISE" if "REVISE" in line.upper() else "PASS"
    return "PASS"


if __name__ == "__main__":
    # 터미널 실행:
    #   python backend/orchestrator.py                      ← 전체 파이프라인
    #   python backend/orchestrator.py --selected <경로>     ← 기존 selected.json 으로 작가부터 (새 폴더)
    #   python backend/orchestrator.py --resume <run 폴더>   ← 중단된 run 을 그 폴더에서 이어서 실행
    import argparse

    parser = argparse.ArgumentParser(description="AI TREND 발간 파이프라인")
    parser.add_argument("--selected", default=None, metavar="경로",
                        help="기존 selected.json 경로 — 지정하면 조사자·편집장을 건너뛰고 작가부터 실행")
    parser.add_argument("--resume", default=None, metavar="폴더",
                        help="중단된 run 폴더 경로 — 이미 작성된 카드는 건너뛰고 그 폴더에서 이어서 실행")
    args = parser.parse_args()
    asyncio.run(run_pipeline(selected_from=args.selected, resume_dir=args.resume))
