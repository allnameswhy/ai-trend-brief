"""
조사자→편집장 구간 테스트 하네스 (수집·선정만, 작가 이후 없음)
----------------------------------------------------------------
목적: .claude/agents/researcher.md · editor.md 의 지시문을 고쳐가며 수집·선정 방식을
      조정할 때, 작가·검토자·렌더 없이 '수집 → 선정'까지만 실행해 결과를 확인한다.

사용법 (프로젝트 루트에서, 가상환경 활성 상태):
  python tools/test_research_edit.py                # 조사자부터 실행
  python tools/test_research_edit.py --candidates data/tests/20260721_101010/candidates.json
                                                    # 기존 수집 결과로 편집장만 실행

동작:
  1. data/tests/<실행시각>/ 폴더 생성 (테스트 산출물 규칙)
  2. 조사자 실행 → candidates.json  (--candidates 지정 시 복사해 오고 건너뜀)
  3. 편집장 실행 → selected.json + dropped.json
  4. 건수·카테고리 배분을 집계해 출력하고 멈춤 (작가는 부르지 않음)
  → 이후 이 결과로 작가를 테스트하려면:
     python tools/test_writer_single.py 1 --source data/tests/<실행시각>/selected.json
"""

import argparse
import asyncio
import json
import shutil
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from backend.orchestrator import run_agent, log_dropped, PROJECT_ROOT  # noqa: E402

# 테스트 산출물은 종류 불문 data/tests/<타임스탬프>/ 에 모은다 (data/runs/<타임스탬프>/ 와 같은 구조)
TESTS_DIR = PROJECT_ROOT / "data" / "tests"

# 지시문은 오케스트레이터(run_pipeline)의 조사자·편집장 단계와 동일하게 유지한다
RESEARCHER_TASK = "소스 목록을 돌며 최근 AI 관련 기사를 수집하고, candidates.json 파일에 저장하세요."
EDITOR_TASK = (
    "candidates.json 을 읽고, 최종 10건을 카테고리 배분에 맞춰 확정한 뒤 selected.json 에 저장하세요. "
    "탈락한 최종 후보(최대 5건)는 사유와 함께 dropped.json 에 저장하세요."
)


def count_by_category(path: Path) -> tuple[int, Counter]:
    items = json.loads(path.read_text(encoding="utf-8"))
    return len(items), Counter(it.get("category", "(미분류)") for it in items)


async def run_test(candidates_from: Path | None) -> None:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    test_dir = TESTS_DIR / ts
    test_dir.mkdir(parents=True)
    log_file = test_dir / f"run_{ts}.log"

    def logger(stage, message):
        line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {stage} | {message}"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        print(f"[{datetime.now():%H:%M:%S}] {stage:12s} | {message}", flush=True)

    logger("test", f"수집·선정 구간 테스트 시작 (작가 이후 없음) — {test_dir}")

    # ① 조사자 — 후보 수집 (기존 수집 결과가 있으면 복사해 오고 건너뜀)
    if candidates_from:
        shutil.copy(candidates_from, test_dir / "candidates.json")
        logger("test", f"조사자 건너뜀 — 기존 수집 결과 재사용: {candidates_from}")
    else:
        await run_agent("researcher", RESEARCHER_TASK, test_dir, logger)
        if not (test_dir / "candidates.json").exists():
            logger("test", "candidates.json 이 생성되지 않았습니다. 위 로그를 확인하세요.")
            return
    total, cats = count_by_category(test_dir / "candidates.json")
    logger("test", f"후보 수집: {total}건 — " + ", ".join(f"{k} {v}" for k, v in cats.items()))

    # ② 편집장 — 최종 확정 + 탈락 기록
    await run_agent("editor", EDITOR_TASK, test_dir, logger)
    if not (test_dir / "selected.json").exists():
        logger("test", "selected.json 이 생성되지 않았습니다. 위 로그를 확인하세요.")
        return
    log_dropped(test_dir, logger)

    total, cats = count_by_category(test_dir / "selected.json")
    logger("test", f"최종 확정: {total}건 — " + ", ".join(f"{k} {v}" for k, v in cats.items())
           + " (정책:기술:윤리 ≈ 2:4:2, 최대 10건)")
    logger("test", f"확인할 파일: {test_dir}\\candidates.json · selected.json · dropped.json")
    logger("test", f"이 결과로 작가 테스트: python tools/test_writer_single.py 1 --source {test_dir}\\selected.json")


def main() -> None:
    parser = argparse.ArgumentParser(description="조사자→편집장 구간 테스트 (수집·선정만)")
    parser.add_argument("--candidates", type=Path, default=None, metavar="경로",
                        help="기존 candidates.json 경로 — 지정하면 조사자를 건너뛰고 편집장만 실행")
    args = parser.parse_args()
    if args.candidates and not args.candidates.exists():
        sys.exit(f"candidates.json 을 찾지 못했습니다: {args.candidates}")
    asyncio.run(run_test(args.candidates))


if __name__ == "__main__":
    main()
