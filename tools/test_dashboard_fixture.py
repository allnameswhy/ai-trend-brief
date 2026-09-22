"""
대시보드 발행 구간 검증용 픽스처 서버 (토큰 0)
------------------------------------------------
목적: 카드 편집·저장·발행(PNG 렌더)처럼 에이전트를 부르지 않는 대시보드 기능을 고칠 때,
      실제 발간 run(data/runs/)을 건드리지 않고 복사본(픽스처)으로 서버를 띄워 확인한다.

사용법 (프로젝트 루트에서, 가상환경 활성 상태):
  python tools/test_dashboard_fixture.py make                      # 최신 run 의 카드로 픽스처 생성
  python tools/test_dashboard_fixture.py make --run data/runs/20260921_100041 --week "9월 3주"
  python tools/test_dashboard_fixture.py serve                     # 최신 픽스처로 8001 포트에 서버
  python tools/test_dashboard_fixture.py serve --port 8002

동작:
  make  — data/tests/<실행시각>/ 에 card_NN.json(+review.md)을 복사하고 state.json 을
          '카드 검토 대기(waiting_final_review)'로 써 둔다. --week 를 주면 카드의 호수를 그 값으로 바꿔
          집필 주와 발행 주가 다른 상황을 재현한다. (프로젝트 규칙 8: 테스트 산출물은 data/tests/<타임스탬프>/)
  serve — 서버가 'data/tests 에서 state.json 이 있는 최신 폴더'를 현재 run 으로 복원하도록 RUNS_DIR 을 바꿔 끼우고,
          로그도 픽스처 폴더 안에 남긴다(logs/ 의 실제 run 로그와 섞이지 않음). .claude/launch.json 의
          "dashboard-test" 구성이 이 명령을 실행한다.
  ※ 테스트 서버에서 [1단계 실행]·[초기화]는 누르지 말 것 — 1단계는 실제 data/runs 에 새 run 을 만들고 에이전트를 부른다.
"""

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = PROJECT_ROOT / "data" / "runs"
TESTS_DIR = PROJECT_ROOT / "data" / "tests"


def latest_run() -> Path | None:
    runs = sorted((p for p in RUNS_DIR.glob("*/") if (p / "card_01.json").exists()), key=lambda p: p.name)
    return runs[-1] if runs else None


def latest_fixture() -> Path | None:
    fx = sorted((p for p in TESTS_DIR.glob("*/") if (p / "state.json").exists()), key=lambda p: p.name)
    return fx[-1] if fx else None


def make(run: Path | None, week: str | None) -> Path:
    src = run or latest_run()
    if src is None or not src.exists():
        sys.exit("카드가 있는 run 을 찾지 못했습니다. --run 으로 경로를 지정하세요.")
    if week and not re.fullmatch(r"(1[0-2]|[1-9])월 [1-6]주", week):
        sys.exit(f'--week 형식이 아닙니다: "{week}" — "9월 3주" 처럼 지정하세요')
    dst = TESTS_DIR / datetime.now().strftime("%Y%m%d_%H%M%S")
    dst.mkdir(parents=True)
    cards = sorted(p for p in src.glob("card_??.json") if p.stem[5:7].isdigit())
    for p in cards:
        card = json.loads(p.read_text(encoding="utf-8"))
        if week:
            card["week_label"] = week
        (dst / p.name).write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
    if (src / "review.md").exists():
        shutil.copy(src / "review.md", dst / "review.md")
    (dst / "state.json").write_text(json.dumps(
        {"state": "waiting_final_review", "updated_at": datetime.now().isoformat(timespec="seconds"),
         "error": None, "cards": len(cards)}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"픽스처 생성: {dst}  (카드 {len(cards)}건, 원본 {src.name}" + (f", 호수 '{week}'" if week else "") + ")")
    return dst


def serve(port: int) -> None:
    fx = latest_fixture()
    if fx is None:
        sys.exit("픽스처가 없습니다. 먼저 `python tools/test_dashboard_fixture.py make` 를 실행하세요.")
    import uvicorn
    import backend.main as main
    import backend.orchestrator as orch
    # 서버는 lifespan 에서 RUNS_DIR 의 최신 state.json 폴더를 복원한다 — data/tests 로 바꿔 끼우면 픽스처가 잡힌다.
    main.RUNS_DIR = TESTS_DIR
    orch.LOGS_DIR = fx      # make_logger 는 호출 시점에 모듈 전역을 읽는다 → 로그가 픽스처 폴더 안에 남는다
    print(f"테스트 서버: http://localhost:{port}  (픽스처 {fx.name} — 실제 run 은 건드리지 않음)")
    uvicorn.run(main.app, host="127.0.0.1", port=port)


def main() -> None:
    parser = argparse.ArgumentParser(description="대시보드 발행 구간 검증용 픽스처 서버")
    sub = parser.add_subparsers(dest="cmd", required=True)
    mk = sub.add_parser("make", help="최신 run(또는 --run)의 카드로 픽스처 생성")
    mk.add_argument("--run", type=Path, default=None, help="원본 run 폴더 (기본: 카드가 있는 최신 run)")
    mk.add_argument("--week", default=None, help="카드 호수를 이 값으로 바꿔 복사 (예: '9월 3주')")
    sv = sub.add_parser("serve", help="최신 픽스처로 테스트 서버 실행")
    sv.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    if args.cmd == "make":
        make(args.run, args.week)
    else:
        serve(args.port)


if __name__ == "__main__":
    main()
