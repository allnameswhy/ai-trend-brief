"""
작가(Writer) 파인튜닝용 1건 테스트 하네스
------------------------------------------
목적: .claude/agents/writer.md 의 지시문을 고쳐가며 작가의 글쓰기 방식을 조정할 때,
      전체 파이프라인 대신 '기사 1건'만 빠르게 번역·요약시켜 결과를 확인한다.

사용법 (프로젝트 루트에서, 가상환경 활성 상태):
  python tools/test_writer_single.py           # 최신 selected.json 의 1번 기사로 테스트
  python tools/test_writer_single.py 7         # 7번 기사로 테스트
  python tools/test_writer_single.py 3 --source data/runs/20260708_132347/selected.json

동작:
  1. selected.json 에서 지정한 기사 1건을 뽑아 data/tests/<실행시각>/single_item.json 으로 저장
  2. 작가 에이전트를 그 폴더에서 실행 → item.md 생성
     (문체 참고 자료는 docs/ref*_text.txt 를 제자리에서 읽게 함 — run 마다 복사하지 않음)
  → 문체 튜닝은 이 파일이 아니라 .claude/agents/writer.md 를 수정하면 된다.
"""

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from backend.orchestrator import run_agent, PROJECT_ROOT  # noqa: E402

RUNS_DIR = PROJECT_ROOT / "data" / "runs"
# 테스트 산출물은 종류 불문 data/tests/ 아래에 모은다 (writer 테스트는 그 하위 writer/)
TESTS_DIR = PROJECT_ROOT / "data" / "tests" / "writer"

TASK_TEMPLATE = """single_item.json 에 담긴 기사 1건을 번역·요약해 브리프 항목 원고 1건을 작성하고,
item.md 파일에 저장하세요. 원고 완성 후에는 역할 지침의 '카드뉴스 JSON 산출' 규격에 따라
card.json 파일도 작성하세요. (이 기사의 항목 번호는 {article_no} 입니다.)

- 기존 발간물의 톤·문체·구성 참고 자료(읽기 전용): {ref_paths}
- 원문 무료 공개 범위가 좁아 분량을 채우기 어려우면 내용을 지어내지 말고, 항목 끝에
  `> [편집 참고: 분량 부족 — 사유]` 형식의 표시를 남기세요.
"""


def find_latest_selected() -> Path | None:
    """data/runs/ 에서 가장 최근 실행의 selected.json 을 찾는다."""
    candidates = sorted(RUNS_DIR.glob("*/selected.json"), key=lambda p: p.parent.name, reverse=True)
    return candidates[0] if candidates else None


def find_ref_texts() -> list[Path]:
    """문체 참고 자료(ref*_text.txt)는 docs/ 에 상주한다."""
    return sorted((PROJECT_ROOT / "docs").glob("ref*_text.txt"))


def validate_card(card: dict) -> list[str]:
    """card_schema.md 규격 위반 목록을 돌려준다. 빈 목록이면 합격.
    (사람 눈 대신 기계가 판정 — 규격은 tools/cardnews/card_schema.md 참조)"""
    errors = []

    def need(cond: bool, msg: str) -> None:
        if not cond:
            errors.append(msg)

    need(card.get("category") in ("정책", "기술", "윤리"), "category는 정책/기술/윤리 중 하나")
    need(bool(re.fullmatch(r"\d{2}", str(card.get("article_no", "")))), "article_no는 두 자리 숫자")
    need(bool(re.fullmatch(r"\d{1,2}월 \d주", card.get("week_label", ""))), "week_label은 'M월 N주' 형식")
    need(0 < len(card.get("eyebrow", "")) <= 25, "eyebrow는 1~25자")

    headline = card.get("headline")
    need(isinstance(headline, list) and 1 <= len(headline) <= 2, "headline은 1~2줄 배열")
    if isinstance(headline, list):
        for i, line in enumerate(headline, 1):
            need(len(line) <= 12, f"headline {i}줄이 12자 초과({len(line)}자)")

    need(0 < len(card.get("subhead", "")) <= 70, "subhead는 1~70자")

    points = card.get("points", [])
    need(2 <= len(points) <= 5, f"points는 2~5개(현재 {len(points)}개)")
    for i, pt in enumerate(points, 1):
        need(0 < len(pt.get("title", "")) <= 22, f"point {i} title은 1~22자")
        need(0 < len(pt.get("desc", "")) <= 60, f"point {i} desc는 1~60자")

    source = card.get("source", [])
    need(isinstance(source, list) and len(source) == 3, "source는 정확히 3줄")
    if isinstance(source, list) and len(source) == 3:
        need(bool(re.match(r"출처 : .+\((Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?, \d{4}\), .+", source[0])),
             "source 1줄은 '출처 : 매체(Mon., YYYY), 제목' 형식")
        need(source[1].startswith("원문 : "), "source 2줄은 '원문 : '으로 시작")
        need(bool(re.fullmatch(r"발간일 : \d{2}\.\d{2}\.\d{2}\.", source[2])), "source 3줄은 '발간일 : YY.MM.DD.' 형식")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description="작가 에이전트 1건 테스트")
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

    refs = find_ref_texts()
    ref_paths = ", ".join(str(p) for p in refs) if refs else "(없음 — 참고 자료 생략)"
    task = TASK_TEMPLATE.format(ref_paths=ref_paths, article_no=f"{args.item:02d}")

    def logger(stage, message):
        print(f"[{datetime.now():%H:%M:%S}] {stage:8s} | {message}", flush=True)

    title = item.get("title_ko") or item.get("title_en") or "(제목 없음)"
    logger("test", f"기사 {args.item}번 테스트 시작: {title}")
    logger("test", f"입력: {source}")
    asyncio.run(run_agent("writer", task, test_dir, logger))

    out = test_dir / "item.md"
    if out.exists():
        logger("test", f"원고 완료 — {out}")
    else:
        logger("test", "item.md 가 생성되지 않았습니다. 위 로그를 확인하세요.")

    card_out = test_dir / "card.json"
    if not card_out.exists():
        logger("test", "card.json 이 생성되지 않았습니다. 위 로그를 확인하세요.")
        return
    try:
        card = json.loads(card_out.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        logger("test", f"card.json 이 올바른 JSON이 아닙니다: {e}")
        return
    violations = validate_card(card)
    if violations:
        logger("test", f"카드 규격 위반 {len(violations)}건:")
        for v in violations:
            logger("test", f"  - {v}")
    else:
        logger("test", f"카드 규격 검사 통과 ✓ — {card_out}")
        logger("test", f"렌더 명령: python tools/build_cardnews.py {card_out}")


if __name__ == "__main__":
    main()
