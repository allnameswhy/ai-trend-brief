"""
오케스트레이터 (Orchestrator)
------------------------------
조사자 → 편집장 → 작가(기사별) → 검토자 → 카드뉴스 생성 순서로 에이전트를 지휘하는 '지휘자' 코드입니다.
- 실행 순서 강제 / 단계 산출물(파일) 전달
- 검토자 반려 시 작가 재작성 루프 1회 (항목 파일 수정 → 자동 재조립, PROJECT_NOTES 2.8)
- 카드뉴스 JSON(최종 배포 산출물)은 검토가 끝난 최종 원고에서 기사별로 생성
- 각 단계를 타임스탬프 로그 파일로 기록 (PROJECT_NOTES 2.10)
- 마지막에 자동 발송하지 않고, 최종 원고 + 검토 로그 경로를 제시하고 멈춤 (Human-in-the-loop)

실행 방법 두 가지:
  1) 터미널에서 직접:      python backend/orchestrator.py   ← 1차 데모
  2) FastAPI 서버가 호출:  backend/main.py 가 run_pipeline() 을 불러 대시보드로 로그 스트리밍

각 에이전트의 '성격(시스템 프롬프트)'은 .claude/agents/*.md 파일에 있습니다.
→ 에이전트 행동을 바꾸고 싶으면 그 마크다운 파일만 고치면 됩니다. (코드 수정 불필요)
"""

import asyncio
import json
import re
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

# .env 의 ANTHROPIC_API_KEY 를 환경변수로 로드 (파일이 없으면 조용히 넘어감)
load_dotenv(PROJECT_ROOT / ".env")

# 단계별로 각 에이전트에게 허용할 도구 (.claude/agents/*.md 의 frontmatter 와 맞춰 둠)
ALLOWED_TOOLS = {
    "researcher": ["WebSearch", "WebFetch", "Read", "Write"],
    "editor": ["WebSearch", "Read", "Write"],
    "writer": ["WebSearch", "WebFetch", "Read", "Write"],
    "reviewer": ["WebSearch", "WebFetch", "Read", "Write"],
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

    result = {"run_dir": str(run_dir), "draft": None, "review": None, "log": str(log_file)}

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

    # ③ 작가 — 번역·요약 품질을 일정하게 유지하기 위해 기사별로 1건씩 따로 호출한다.
    #    (10건을 한 세션에 맡기면 항목당 조사·서술 노력이 분산되어 품질이 얕아지는 문제 확인됨)
    items = json.loads((run_dir / "selected.json").read_text(encoding="utf-8"))
    category_order = {"정책": 0, "기술": 1, "윤리": 2}
    items.sort(key=lambda it: category_order.get(it.get("category"), 9))

    for idx, item in enumerate(items, start=1):
        input_file = f"item_input_{idx:02d}.json"
        (run_dir / input_file).write_text(
            json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        await run_agent(
            "writer",
            f"{input_file} 에 담긴 기사 1건을 번역·요약해 브리프 항목 원고를 작성하고 "
            f"item_{idx:02d}.md 파일에 저장하세요. 이 항목의 번호는 {idx:02d} 입니다.",
            run_dir, logger,
        )
        if output_missing(f"item_{idx:02d}.md"):
            return result
        logger("orchestrator", f"작가 진행: {idx}/{len(items)}건 완료")

    # 항목 원고들을 목차·파트 구분과 함께 draft.md 하나로 조립 (코드가 수행 — 내용 변형 없음)
    assemble_draft(run_dir, items)
    if output_missing("draft.md"):
        return result

    # ④ 검토자 — 검증 → review.md (마지막 줄에 VERDICT)
    await run_agent(
        "reviewer",
        "draft.md 를 검증하고 결과를 review.md 에 저장하세요. 마지막 줄에 'VERDICT: PASS' 또는 'VERDICT: REVISE' 를 남기세요.",
        run_dir, logger,
    )
    if output_missing("review.md"):
        return result

    # 피드백 루프 1회 — REVISE 인 경우: 작가가 항목 파일 수정 → 재조립 → 검토자 재검토 → 종료
    verdict = read_verdict(run_dir)
    logger("orchestrator", f"검토 판정: {verdict}")
    if verdict == "REVISE":
        logger("orchestrator", "반려 → 작가 재작성 1회 진행 (항목 파일 수정 후 재조립)")
        await run_agent(
            "writer",
            "review.md 의 지적 사항을 반영해 해당 항목 원고 파일(item_XX.md)들을 수정하세요. "
            "draft.md 는 직접 고치지 마세요 — 수정된 항목 파일로 자동 재조립됩니다.",
            run_dir, logger,
        )
        assemble_draft(run_dir, items)
        logger("orchestrator", "수정된 항목 파일로 draft.md 재조립 완료")
        await run_agent(
            "reviewer",
            "수정된 draft.md 를 다시 검증하고 review.md 를 갱신하세요. (재작성 기회는 더 이상 없습니다. 남은 문제는 로그로만 남기세요.)",
            run_dir, logger,
        )

    # ⑤ 카드뉴스 — 검토가 끝난 '최종' 항목 원고를 기반으로 기사별 카드 JSON 생성 (최종 배포 산출물)
    #    (검토 이전에 만들면 반려·수정 시 카드가 낡아지므로, 반드시 검토 루프가 끝난 뒤에 생성한다)
    for idx in range(1, len(items) + 1):
        await run_agent(
            "writer",
            f"item_{idx:02d}.md 는 검토를 마친 최종 원고입니다. 이 원고에 있는 내용만으로, "
            f"역할 지침의 '카드뉴스 JSON 산출' 규격에 따라 card_{idx:02d}.json 을 작성하세요. "
            f"이 항목의 번호는 {idx:02d} 입니다.",
            run_dir, logger,
        )
        if output_missing(f"card_{idx:02d}.json"):
            return result
        logger("orchestrator", f"카드 진행: {idx}/{len(items)}건 완료")

    logger("orchestrator", "파이프라인 종료")

    # Human-in-the-loop: 자동 발송하지 않는다. 결과물 위치만 알려주고 사람의 판단을 기다린다.
    draft = run_dir / "draft.md"
    review = run_dir / "review.md"
    logger("orchestrator",
           f"검토 대기: 최종 원고={draft}, 검토 로그={review}, 카드 JSON={len(items)}건(card_NN.json) "
           f"— 내용을 확인하고 발송 여부를 결정하세요. (자동 발송 없음) "
           f"카드 렌더: python tools/build_cardnews.py {run_dir}\\card_01.json")

    result["draft"] = str(draft)
    result["review"] = str(review)
    return result


def assemble_draft(run_dir: Path, items: list) -> None:
    """기사별 항목 원고(item_NN.md)를 카테고리 파트·목차와 함께 draft.md 하나로 조립한다."""
    part_heads = {"정책": "Ⅰ. 정책 동향", "기술": "Ⅱ. 기술 동향", "윤리": "Ⅲ. 윤리 동향"}
    parts = {name: [] for name in part_heads}

    for idx, item in enumerate(items, start=1):
        path = run_dir / f"item_{idx:02d}.md"
        text = path.read_text(encoding="utf-8").strip()
        # 항목 번호를 전체 순번으로 통일 (작가가 다른 번호를 썼어도 여기서 교정)
        text = re.sub(r"^##\s*\d*\s*", f"## {idx:02d} ", text, count=1)
        category = item.get("category") if item.get("category") in parts else "기술"
        parts[category].append(text)

    lines = ["# 해외 AI 정책·기술 동향 리포트 (AI TREND)", "", "## CONTENTS", ""]
    for name, head in part_heads.items():
        if not parts[name]:
            continue
        roman = head.split(".")[0]
        lines.append(f"**{roman} {name} 동향**")
        for text in parts[name]:
            m = re.match(r"^## (\d{2}) (.+)$", text.splitlines()[0])
            if m:
                lines.append(f"- {m.group(1)} {m.group(2)}")
        lines.append("")

    for name, head in part_heads.items():
        if not parts[name]:
            continue
        lines += ["---", "", f"# {head}", ""]
        for text in parts[name]:
            lines += [text, "", "---", ""]
        lines.pop()  # 파트 마지막 항목 뒤의 빈 줄 정리
        lines.pop()  # 파트 마지막 항목 뒤의 --- 제거 (다음 파트 앞에서 다시 붙음)

    (run_dir / "draft.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


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
