# -*- coding: utf-8 -*-
"""
카드뉴스 빌더 — 『AI 브리프 카드뉴스』 디자인(1080×1240)을 실제 데이터로 렌더링한다.

원 디자인은 Claude Design 캔버스 전용 컴포넌트(.dc.html)라 로컬에서 그대로 실행되지 않는다.
이 스크립트는 같은 시안을 순수 HTML/CSS 템플릿(card_template.html)으로 옮긴 뒤,
콘텐츠 JSON을 끼워 넣어 HTML을 만들고, Edge 헤드리스로 PNG까지 뽑아낸다.
(PDF 파이프라인과 동일한 Edge를 재사용 — 새 라이브러리 없음)

사용법:
    python tools/build_cardnews.py                     # 데모 카드(demo_card.json) 렌더
    python tools/build_cardnews.py 내콘텐츠.json         # 임의 콘텐츠 렌더
    python tools/build_cardnews.py 내콘텐츠.json --out 폴더 --no-png

콘텐츠 JSON 스키마: demo_card.json 참고.
"""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

HERE = Path(__file__).resolve().parent
TEMPLATE_NAME = "card_template.html.j2"
DEMO = HERE / "cardnews" / "demo_card.json"
# 테스트 산출물은 종류 불문 data/tests/ 아래에 모은다 (프로젝트 규칙)
DEFAULT_OUT = HERE.parent / "data" / "tests" / "cardnews"

# autoescape=True: 기사 제목 등에 <, & 같은 문자가 있어도 자동으로 무해하게 처리됨
JINJA_ENV = Environment(loader=FileSystemLoader(HERE / "cardnews"), autoescape=True)

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

# 카테고리별 강조색 (원 디자인 accentMap과 동일)
ACCENT_MAP = {"정책": "#3E6DE8", "기술": "#0EA79E", "윤리": "#7C64EE"}


def hex_to_rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


def as_lines(value) -> list:
    """문자열이면 한 줄짜리 목록으로, 목록이면 그대로 반환. (headline·source 공용)"""
    if isinstance(value, list):
        return value
    return [value] if value else []


def density_class(point_count: int) -> str:
    """포인트 개수에 따른 타이포 단계. 상자 크기는 CSS flex가 알아서 나누지만
    글자 크기는 스스로 줄지 않으므로, 렌더 시점에 아는 개수로 단계를 정해준다."""
    if point_count <= 2:
        return "roomy"
    if point_count == 3:
        return ""        # 기본 (원 디자인 그대로)
    if point_count == 4:
        return "tight"
    return "dense"       # 5개 이상


def render_html(card: dict) -> str:
    category = card.get("category", "정책")
    accent = ACCENT_MAP.get(category, "#3E6DE8")
    points = card.get("points", [])

    context = {
        "category": category,
        "accent": accent,
        "accent_glow": hex_to_rgba(accent, 0.55),
        "accent_soft": hex_to_rgba(accent, 0.10),
        "article_no": str(card.get("article_no", "")),
        "week_label": card.get("week_label", ""),
        "eyebrow": card.get("eyebrow", ""),
        "headline_lines": as_lines(card.get("headline", "")),
        "subhead": card.get("subhead", ""),
        "points": points,
        "density": density_class(len(points)),
        "source_lines": as_lines(card.get("source", "")),
    }
    return JINJA_ENV.get_template(TEMPLATE_NAME).render(context)


def find_edge() -> str | None:
    for p in EDGE_CANDIDATES:
        if Path(p).exists():
            return p
    return None


def export_png(html_path: Path, png_path: Path) -> bool:
    edge = find_edge()
    if not edge:
        print("  ! Edge를 찾지 못해 PNG는 건너뜀. HTML만 생성됨.", file=sys.stderr)
        return False
    with tempfile.TemporaryDirectory() as udd:
        cmd = [
            edge,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--force-device-scale-factor=2",   # 2배 해상도 → 2160×2480
            "--window-size=1080,1240",
            "--default-background-color=00000000",
            "--virtual-time-budget=3000",       # 웹폰트 로드 대기
            f"--user-data-dir={udd}",
            f"--screenshot={png_path.resolve()}",
            html_path.resolve().as_uri(),   # 상대 경로 입력도 안전하게 (as_uri는 절대 경로 필수)
        ]
        subprocess.run(cmd, capture_output=True, timeout=120)
    return png_path.exists()


def build_one(card_path: Path, out_dir: Path, make_png: bool) -> None:
    card = json.loads(card_path.read_text(encoding="utf-8"))
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = card_path.stem

    html_out = out_dir / f"{stem}.html"
    html_out.write_text(render_html(card), encoding="utf-8")
    print(f"  HTML : {html_out}")

    if make_png:
        png_out = out_dir / f"{stem}.png"
        if export_png(html_out, png_out):
            size = png_out.stat().st_size
            print(f"  PNG  : {png_out}  ({size:,} bytes)")
        else:
            print("  PNG  : 실패")


def main() -> None:
    ap = argparse.ArgumentParser(description="AI 브리프 카드뉴스 빌더")
    ap.add_argument("content", nargs="?", default=str(DEMO),
                    help="카드 콘텐츠 JSON (기본: demo_card.json)")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="출력 폴더")
    ap.add_argument("--no-png", action="store_true", help="HTML만 생성(PNG 건너뜀)")
    args = ap.parse_args()

    card_path = Path(args.content)
    if not card_path.exists():
        sys.exit(f"콘텐츠 파일을 찾을 수 없습니다: {card_path}")

    print(f"카드뉴스 빌드: {card_path.name}")
    build_one(card_path, Path(args.out), make_png=not args.no_png)
    print("완료.")


if __name__ == "__main__":
    main()
