"""
작가(Writer) 파인튜닝용 1건 테스트 하네스
------------------------------------------
목적: .claude/agents/writer.md 의 지시문을 고쳐가며 작가의 카드 작성 방식을 조정할 때,
      전체 파이프라인 대신 '기사 1건 → 카드 1장'만 빠르게 시켜 결과를 확인한다.

사용법 (프로젝트 루트에서, 가상환경 활성 상태):
  python tools/test_writer_single.py           # 최신 selected.json 의 1번 기사로 테스트
  python tools/test_writer_single.py 7         # 7번 기사로 테스트
  python tools/test_writer_single.py 3 --source data/runs/20260708_132347/selected.json

동작 (2026-07-13 파이프라인 변경: 원고 없이 기사에서 카드 직접 작성):
  1. selected.json 에서 지정한 기사 1건을 뽑아 data/tests/writer/<실행시각>/single_item.json 으로 저장
  2. 작가 에이전트를 그 폴더에서 실행 → card.json 생성
  3. 기계 검사(자수 규격 + 렌더 실측)로 합격 여부 판정
  → 작가 튜닝은 이 파일이 아니라 .claude/agents/writer.md 를 수정하면 된다.
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from backend.orchestrator import run_agent, PROJECT_ROOT  # noqa: E402
from build_cardnews import card_problems  # noqa: E402  (tools/ 가 sys.path 에 있음)

RUNS_DIR = PROJECT_ROOT / "data" / "runs"
# 테스트 산출물은 종류 불문 data/tests/ 아래에 모은다 (writer 테스트는 그 하위 writer/)
TESTS_DIR = PROJECT_ROOT / "data" / "tests" / "writer"
# 카드 규격의 단일 원천 — writer.md 에 없으므로 지시문에 전문을 첨부한다 (오케스트레이터와 동일 방식)
CARD_SCHEMA_PATH = PROJECT_ROOT / "tools" / "cardnews" / "card_schema.md"

TASK_TEMPLATE = """single_item.json 에 담긴 기사 1건을 읽고, 원문 URL에 접속해 내용을 확인한 뒤
아래 카드 규격에 따라 card.json 을 작성하세요.
article_no는 "{article_no}", week_label은 "{week_label}" 입니다.

- 원문 무료 공개 범위가 좁아 카드를 채우기 어려우면 내용을 지어내지 말고 짧게 쓰고,
  그 사유를 응답 텍스트로 보고하세요. (카드 분량 하한은 없습니다)

--- 카드 규격 (원천: tools/cardnews/card_schema.md) ---

{card_spec}
"""


def find_latest_selected() -> Path | None:
    """data/runs/ 에서 가장 최근 실행의 selected.json 을 찾는다."""
    candidates = sorted(RUNS_DIR.glob("*/selected.json"), key=lambda p: p.parent.name, reverse=True)
    return candidates[0] if candidates else None


def main() -> None:
    parser = argparse.ArgumentParser(description="작가 에이전트 1건 테스트 (기사 → 카드)")
    parser.add_argument("item", nargs="?", type=int, default=1, help="기사 번호 (1부터, 기본 1)")
    parser.add_argument("--source", type=Path, default=None, help="selected.json 경로 (기본: 최신 run)")
    args = parser.parse_args()

    source = args.source or find_latest_selected()
    if source is None or not source.exists():
        sys.exit("selected.json 을 찾지 못했습니다. --source 로 경로를 지정하세요.")

    items = json.loads(source.read_text(encoding="utf-8"))
    if not (1 <= args.item <= len(items)):
        sys.exit(f"기사 번호는 1~{len(items)} 사이여야 합니다. (입력: {args.item})")
    item = items[args.item - 1]

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    test_dir = TESTS_DIR / ts
    test_dir.mkdir(parents=True)
    (test_dir / "single_item.json").write_text(
        json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    now = datetime.now()
    week_label = f"{now.month}월 {(now.day - 1) // 7 + 1}주"
    task = TASK_TEMPLATE.format(article_no=f"{args.item:02d}", week_label=week_label,
                                card_spec=CARD_SCHEMA_PATH.read_text(encoding="utf-8"))

    def logger(stage, message):
        print(f"[{datetime.now():%H:%M:%S}] {stage:8s} | {message}", flush=True)

    title = item.get("title_ko") or item.get("title_en") or "(제목 없음)"
    logger("test", f"기사 {args.item}번 테스트 시작: {title}")
    logger("test", f"입력: {source}")
    asyncio.run(run_agent("writer", task, test_dir, logger))

    card_out = test_dir / "card.json"
    if not card_out.exists():
        logger("test", "card.json 이 생성되지 않았습니다. 위 로그를 확인하세요.")
        return
    problems = card_problems(card_out)
    if problems:
        logger("test", f"카드 기계 검사 불합격 {len(problems)}건:")
        for p in problems:
            logger("test", f"  - {p}")
    else:
        logger("test", f"카드 기계 검사 통과 ✓ — {card_out}")
        logger("test", f"렌더 명령: python tools/build_cardnews.py {card_out}")


if __name__ == "__main__":
    main()
