"""
작가 → 검토 → (반려 시 재작성 1회) → PNG 렌더 : 기사 1건 파이프라인 테스트
--------------------------------------------------------------------------
실제 발간 파이프라인(backend/orchestrator.py)에서 '조사자·편집장'만 제거한 형태.
selected.json 의 기사 1건만 뽑아 뒷단(③작가 → ④검토 → 반려 시 재작성 1회 → ⑤PNG 렌더)을
그대로 돌린다.

핵심: 이 파일은 프롬프트를 새로 만들지 않는다. run_pipeline(selected_from=..., output_base=...)
      을 그대로 호출하므로 작가·검토자·재작성·렌더 지시문이 실제 파이프라인과 100% 동일하다.
      (오케스트레이터 프롬프트가 바뀌면 이 테스트도 자동으로 따라간다 — 복붙본 낡음 없음)

test_writer_single.py 와의 차이:
  - test_writer_single.py : 작가 1회 + 기계 검사만 (검토자 없음, 재작성 없음, 렌더 없음)
  - 이 파일               : 작가 → 검토자 → 반려 시 재작성 → PNG 까지 (뒷단 전체)

사용법 (프로젝트 루트에서, 가상환경 활성 상태):
  python tools/test_writer_review_single.py           # 최신 selected.json 의 1번 기사
  python tools/test_writer_review_single.py 7          # 7번 기사
  python tools/test_writer_review_single.py 3 --source data/runs/20260709_161328/selected.json

산출물: data/tests/<실행시각>/ (실제 run 과 같은 구조)
  - selected.json (뽑은 1건) · item_input_01.json · card_01.json/html/png · review.md · run_*.log
  ※ 카드 번호는 단건 테스트라 원래 기사 번호와 무관하게 항상 '01' 로 매겨진다.
"""

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from backend.orchestrator import run_pipeline, PROJECT_ROOT  # noqa: E402

RUNS_DIR = PROJECT_ROOT / "data" / "runs"
# 테스트 산출물은 종류 불문 data/tests/<타임스탬프>/ 에 모은다 (data/runs/<타임스탬프>/ 와 같은 구조)
TESTS_DIR = PROJECT_ROOT / "data" / "tests"


def find_latest_selected() -> Path | None:
    """data/runs/ 에서 가장 최근 실행의 selected.json 을 찾는다."""
    candidates = sorted(RUNS_DIR.glob("*/selected.json"), key=lambda p: p.parent.name, reverse=True)
    return candidates[0] if candidates else None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="작가→검토→재작성→렌더 1건 테스트 (조사자·편집장 제외 파이프라인)")
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

    # 뽑은 기사 1건만 담은 selected.json 을 임시로 만들어 파이프라인에 넘긴다.
    # (조사자·편집장이 만드는 selected.json 을 그대로 흉내내되 1건짜리) — run_pipeline 이
    # 이 파일을 산출물 폴더로 복사하므로, 임시 파일 자체는 tempfile 로 만들고 끝나면 지운다.
    tmp_dir = Path(tempfile.mkdtemp(prefix="single_selected_"))
    tmp_selected = tmp_dir / "selected.json"
    tmp_selected.write_text(json.dumps([item], ensure_ascii=False, indent=2), encoding="utf-8")

    def logger(stage, message):
        print(f"[{datetime.now():%H:%M:%S}] {stage:12s} | {message}", flush=True)

    title = item.get("title_ko") or item.get("title_en") or "(제목 없음)"
    logger("test", f"기사 {args.item}번 테스트 시작: {title}")
    logger("test", f"입력: {source}")
    logger("test", "흐름: 작가 → 검토 → (반려 시 재작성 1회) → PNG 렌더")

    try:
        result = asyncio.run(run_pipeline(
            on_log=logger,
            selected_from=str(tmp_selected),
            output_base=TESTS_DIR,
        ))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)   # 임시 selected 정리

    run_dir = Path(result["run_dir"])
    png = run_dir / "card_01.png"
    logger("test", f"산출물 폴더: {run_dir}")
    logger("test", f"검토 로그: {result.get('review')}")
    logger("test", f"PNG: {'생성됨 ✓ ' + str(png) if png.exists() else '없음 (렌더 실패 가능 — 로그 확인)'}")


if __name__ == "__main__":
    main()
