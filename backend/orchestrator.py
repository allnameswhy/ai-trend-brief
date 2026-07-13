"""
오케스트레이터 (Orchestrator)
------------------------------
조사자 → 편집장 → 작가(기사별 카드 작성) → 검토자 → PNG 렌더 순서로 에이전트를 지휘하는 '지휘자' 코드입니다.
- 실행 순서 강제 / 단계 산출물(파일) 전달
- 작가는 기사 1건당 카드뉴스 JSON(card_NN.json) 1장을 직접 작성 (2026-07-13 변경 —
  원고(item.md/draft.md)를 만들고 다시 카드로 압축하던 중간 단계 폐지: 비용 절감 + 재압축 손실 제거)
- 검토자가 카드마다 규격 기계 검사 명령(build_cardnews --check: 자수 규격+렌더 실측)을 실행해
  그 결과를 review.md 에 기록 — 판정은 명령(기계)이 하고, 검토자는 실행·기록과 내용 검증을 맡는다
- 반려(REVISE) 시 작가 재작성 루프 1회 — 내용 지적과 규격 불합격을 함께 수정 (PROJECT_NOTES 2.8)
  (※ 대시보드가 생기면 자동 수정 대신 인간이 직접 수정하는 흐름으로 바꿀 예정)
- PNG는 검사 결과와 무관하게 전 카드 생성 — 최종 확인은 사람이 PNG로 한다
- 각 단계를 타임스탬프 로그 파일로 기록 (PROJECT_NOTES 2.10)
- 마지막에 자동 발송하지 않고, 카드 + 검토 로그 경로를 제시하고 멈춤 (Human-in-the-loop)

실행 방법 두 가지:
  1) 터미널에서 직접:      python backend/orchestrator.py   ← 1차 데모
  2) FastAPI 서버가 호출:  backend/main.py 가 run_pipeline() 을 불러 대시보드로 로그 스트리밍

각 에이전트의 '성격(시스템 프롬프트)'은 .claude/agents/*.md 파일에 있습니다.
→ 에이전트 행동을 바꾸고 싶으면 그 마크다운 파일만 고치면 됩니다. (코드 수정 불필요)
"""

import asyncio
import json
import shutil
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

# 카드 도구(자수 검사·렌더 실측·PNG 추출)는 tools/build_cardnews.py 에 모여 있다
sys.path.insert(0, str(PROJECT_ROOT / "tools"))
from build_cardnews import render_html, export_png  # noqa: E402

# 검토자가 실행하는 카드 기계 검사 명령 (자수 규격 + 렌더 실측 — 판정은 이 명령이 한다).
# sys.executable = 지금 파이프라인을 돌리는 파이썬(가상환경) — 검토자도 같은 환경을 쓰게 한다.
CARD_CHECK_CMD = f"{Path(sys.executable).as_posix()} {(PROJECT_ROOT / 'tools' / 'build_cardnews.py').as_posix()}"

# .env 의 ANTHROPIC_API_KEY 를 환경변수로 로드 (파일이 없으면 조용히 넘어감)
load_dotenv(PROJECT_ROOT / ".env")

# 단계별로 각 에이전트에게 허용할 도구 (.claude/agents/*.md 의 frontmatter 와 맞춰 둠)
# 검토자의 Bash 는 카드 기계 검사 명령 딱 하나만 허용 (최소 권한)
ALLOWED_TOOLS = {
    "researcher": ["WebSearch", "WebFetch", "Read", "Write"],
    "editor": ["WebSearch", "Read", "Write"],
    "writer": ["WebSearch", "WebFetch", "Read", "Write"],
    "reviewer": ["WebSearch", "WebFetch", "Read", "Write", f"Bash({CARD_CHECK_CMD}:*)"],
}


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
    """한 에이전트를 실행한다. 에이전트는 run_dir 안에서 파일을 읽고 쓴다."""
    on_log(agent_name, "시작")

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
        permission_mode="acceptEdits",         # 파일 쓰기 자동 승인 (비대화형 실행용)
        cwd=str(run_dir),                      # 에이전트의 작업 폴더 = 이번 run 폴더
        max_turns=50,                          # 무한 루프 방지 안전장치
    )

    async for message in query(prompt=task, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    on_log(agent_name, f"도구 사용: {block.name}")
                elif isinstance(block, TextBlock):
                    snippet = block.text.strip().replace("\n", " ")
                    if snippet:
                        on_log(agent_name, snippet[:160])
        elif isinstance(message, ResultMessage):
            if message.is_error:
                on_log(agent_name, f"⚠️ 오류로 종료 (subtype={message.subtype})")
            cost = message.total_cost_usd
            if cost is not None:
                on_log(agent_name, f"완료 (턴 {message.num_turns}회, 비용 ${cost:.4f})")
            else:
                on_log(agent_name, f"완료 (턴 {message.num_turns}회)")


async def run_pipeline(on_log=None, selected_from: str | None = None) -> dict:
    """전체 파이프라인 1회 실행. on_log(stage, message) 콜백으로 로그를 내보낸다.

    selected_from: 기존 selected.json 경로를 주면 조사자·편집장 단계를 건너뛰고
                   그 확정 기사 목록으로 작가 단계부터 시작한다. (부분 실행용)
    """
    if on_log is None:
        def on_log(stage, message):
            print(f"[{datetime.now():%H:%M:%S}] {stage:12s} | {message}")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = PROJECT_ROOT / "data" / "runs" / ts
    run_dir.mkdir(parents=True, exist_ok=True)

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOGS_DIR / f"run_{ts}.log"

    # 화면(또는 대시보드)으로 보내는 로그를 파일에도 똑같이 남긴다 (PROJECT_NOTES 2.10)
    def logger(stage, message):
        line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {stage} | {message}"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        on_log(stage, message)

    logger("orchestrator", f"발간 파이프라인 시작 (run={ts})")

    result = {"run_dir": str(run_dir), "cards": [], "review": None, "log": str(log_file)}

    def output_missing(filename: str) -> bool:
        """단계 산출물이 안 생겼으면 True. 뒤 단계를 헛돌리지 않도록 파이프라인을 멈추는 용도."""
        if (run_dir / filename).exists():
            return False
        logger("orchestrator", f"기대한 산출물({filename})이 생성되지 않았습니다. 이후 단계를 중단합니다.")
        return True

    if selected_from:
        # 부분 실행: 기존 확정 기사 목록을 복사해 오고 조사자·편집장은 건너뛴다.
        shutil.copy(Path(selected_from), run_dir / "selected.json")
        logger("orchestrator", f"조사자·편집장 단계 건너뜀 — 기존 확정 기사 목록 재사용: {selected_from}")
    else:
        # ① 조사자 — 소스에서 후보 기사 수집 → candidates.json
        await run_agent(
            "researcher",
            "소스 목록을 돌며 최근 AI 관련 기사를 수집하고, candidates.json 파일에 저장하세요.",
            run_dir, logger,
        )
        if output_missing("candidates.json"):
            return result

        # ② 편집장 — 후보 중 최종 10건 확정 → selected.json
        await run_agent(
            "editor",
            "candidates.json 을 읽고, 최종 10건을 카테고리 배분에 맞춰 확정한 뒤 selected.json 에 저장하세요.",
            run_dir, logger,
        )
        if output_missing("selected.json"):
            return result

    # ③ 작가 — 기사별로 카드뉴스 JSON(card_NN.json)을 직접 작성한다 (기사 1건 = 호출 1회).
    #    원고(item.md)→draft.md→카드 변환의 중간 단계를 없앰 (2026-07-13 변경):
    #    원문을 읽고 바로 카드 규격으로 요약하는 편이 싸고, 재압축하며 뜻이 사라지는 문제도 없다.
    #    (기사별 호출 유지 이유: 10건을 한 세션에 맡기면 품질이 얕아지는 문제 확인됨)
    items = json.loads((run_dir / "selected.json").read_text(encoding="utf-8"))
    category_order = {"정책": 0, "기술": 1, "윤리": 2}
    items.sort(key=lambda it: category_order.get(it.get("category"), 9))

    # 카드 규격은 writer.md 에 싣지 않고 지시문에만 첨부한다 (단일 원천: card_schema.md)
    card_spec = (PROJECT_ROOT / "tools" / "cardnews" / "card_schema.md").read_text(encoding="utf-8")
    now = datetime.now()
    week_label = f"{now.month}월 {(now.day - 1) // 7 + 1}주"   # 발행 주차 (그 달의 몇 번째 7일 구간)

    for idx, item in enumerate(items, start=1):
        input_file = f"item_input_{idx:02d}.json"
        (run_dir / input_file).write_text(
            json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        await run_agent(
            "writer",
            f"{input_file} 에 담긴 기사 1건을 읽고, 원문 URL에 접속해 내용을 확인한 뒤 "
            f"아래 카드 규격에 따라 card_{idx:02d}.json 을 작성하세요. "
            f'article_no는 "{idx:02d}", week_label은 "{week_label}" 입니다.\n\n'
            f"--- 카드 규격 (원천: tools/cardnews/card_schema.md) ---\n\n{card_spec}",
            run_dir, logger,
        )
        if output_missing(f"card_{idx:02d}.json"):
            return result
        logger("orchestrator", f"작가 진행: {idx}/{len(items)}건 완료")

    # ④ 검토자 — 카드마다 규격 기계 검사(명령 실행) + 내용의 출처·사실관계 검증 → review.md
    #    규격 판정은 검토자가 직접 하지 않고 아래 명령(자수 규격 + 렌더 실측)이 한다.
    await run_agent(
        "reviewer",
        f"card_01.json ~ card_{len(items):02d}.json ({len(items)}건)의 카드뉴스 콘텐츠를 검증하고 "
        f"결과를 review.md 에 저장하세요.\n"
        f"먼저 카드마다 아래 규격 기계 검사 명령을 반드시 실행하고, 그 출력(합격/불합격 사유)을 "
        f"review.md 에 그대로 기록하세요. 규격 합격 여부를 직접 판단하지 마세요 — 판정은 이 명령이 합니다:\n"
        f"  {CARD_CHECK_CMD} card_01.json --check   (card_01.json 부분을 각 카드 파일명으로 바꿔 실행)\n"
        f"기계 검사 불합격도 반려(REVISE) 사유입니다. "
        f"마지막 줄에 'VERDICT: PASS' 또는 'VERDICT: REVISE' 를 남기세요.",
        run_dir, logger,
    )
    if output_missing("review.md"):
        return result

    # 피드백 루프 1회 — REVISE 인 경우: 작가가 지적된 카드 수정 → 기계 재검사 → 검토자 재검토 → 종료
    verdict = read_verdict(run_dir)
    logger("orchestrator", f"검토 판정: {verdict}")
    if verdict == "REVISE":
        logger("orchestrator", "반려 → 작가 재작성 1회 진행 (지적된 카드 수정)")
        # 재작성 전 카드를 기억해 두었다가, 실제로 바뀐 카드만 재검사·재검토시킨다 (토큰 절약)
        card_files = [f"card_{idx:02d}.json" for idx in range(1, len(items) + 1)]
        before = {name: (run_dir / name).read_text(encoding="utf-8") for name in card_files}
        await run_agent(
            "writer",
            "review.md 의 지적 사항을 반영해 해당 카드 파일(card_XX.json)들을 수정하세요. "
            "카드 규격(필드 구조·자수 한도)은 그대로 유지해야 합니다.",
            run_dir, logger,
        )
        changed = [name for name in card_files
                   if (run_dir / name).read_text(encoding="utf-8") != before[name]]
        logger("orchestrator", f"수정 감지된 카드: {', '.join(changed) if changed else '(없음)'}")
        if changed:
            recheck_task = (
                f"반려 후 작가가 다음 카드만 수정했습니다: {', '.join(changed)}. "
                f"이 카드들만 규격 기계 검사 명령을 다시 실행하고 내용을 재검증한 뒤 review.md 를 갱신하세요:\n"
                f"  {CARD_CHECK_CMD} card_01.json --check   (card_01.json 부분을 수정된 카드 파일명으로 바꿔 실행)\n"
                "수정되지 않은 카드는 재검증하지 않습니다. "
                "(재작성 기회는 더 이상 없습니다. 남은 문제는 로그로만 남기세요.)"
            )
        else:
            recheck_task = (
                "카드들을 다시 검증하고 review.md 를 갱신하세요. "
                "(재작성 기회는 더 이상 없습니다. 남은 문제는 로그로만 남기세요.)"
            )
        await run_agent("reviewer", recheck_task, run_dir, logger)

    # ⑤ PNG 렌더 — 검사 결과와 무관하게 모든 카드를 이미지로 뽑는다 (2026-07-13 지시).
    #    최종 판단은 사람이 PNG를 보고 한다. (JSON이 깨진 카드만 건너뛰고 로그로 알림)
    made = 0
    for idx in range(1, len(items) + 1):
        try:
            card = json.loads((run_dir / f"card_{idx:02d}.json").read_text(encoding="utf-8"))
            html_path = run_dir / f"card_{idx:02d}.html"
            html_path.write_text(render_html(card), encoding="utf-8")
            if export_png(html_path, run_dir / f"card_{idx:02d}.png"):
                made += 1
            else:
                logger("orchestrator", f"card_{idx:02d} PNG 생성 실패 (HTML은 생성됨)")
        except Exception as e:
            logger("orchestrator", f"card_{idx:02d} 렌더 불가 — {e}")
    logger("orchestrator", f"PNG 렌더: {made}/{len(items)}건 생성")

    logger("orchestrator", "파이프라인 종료")

    # Human-in-the-loop: 자동 발송하지 않는다. 결과물 위치만 알려주고 사람의 판단을 기다린다.
    review = run_dir / "review.md"
    logger("orchestrator",
           f"검토 대기: 카드 {len(items)}건(PNG {made}건), 검토 로그={review} "
           f"— 검토 로그의 기계 검사 결과와 PNG를 확인하고 발송 여부를 결정하세요. (자동 발송 없음) "
           f"직접 수정 시 렌더: python tools/build_cardnews.py {run_dir}\\card_XX.json")

    result["cards"] = [str(run_dir / f"card_{idx:02d}.json") for idx in range(1, len(items) + 1)]
    result["review"] = str(review)
    return result


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
    #   python backend/orchestrator.py --selected <경로>     ← 기존 selected.json 으로 작가부터
    import argparse

    parser = argparse.ArgumentParser(description="AI TREND 발간 파이프라인")
    parser.add_argument("--selected", default=None, metavar="경로",
                        help="기존 selected.json 경로 — 지정하면 조사자·편집장을 건너뛰고 작가부터 실행")
    args = parser.parse_args()
    asyncio.run(run_pipeline(selected_from=args.selected))
