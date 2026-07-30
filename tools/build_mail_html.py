# -*- coding: utf-8 -*-
"""
메일 본문 HTML 빌더 — 발간 헤더 + 카드뉴스 PNG 를 세로로 병합한 스티비 메일 본문을 만든다.

실제 발송물(스티비 에디터 산출 HTML)을 그대로 옮긴 템플릿(mail_template.html.j2)에
이미지 src 목록만 끼워 넣는다. 이미지 1장 = 독립 블록이라 카드가 8·9·10건이어도
블록이 그 수만큼 생성된다 (고정 슬롯 없음 — 빈 공간 없음).

src 는 주입식이다: 스티비 v2 API 에 이미지 업로드 엔드포인트가 없어서(2026-07-30 문서 확인),
업로드 방법이 정해지기 전까지는 로컬 경로로 미리보기용을 만들고, 정해지면 --srcs 로
업로드된 URL 목록을 넣어 발송용을 다시 만든다.

사용법:
    python tools/build_mail_html.py --run data/runs/<타임스탬프>
        → <run>/mail.html 생성. src=상대 파일명(header.png 등) — run 폴더에서 열면 바로 보임
    python tools/build_mail_html.py --run <run> --srcs urls.json
        → urls.json(문자열 배열, 순서: 헤더→카드 번호순)의 URL 로 src 치환 — 발송용
    python tools/build_mail_html.py --run <run> --test
        → data/tests/<타임스탬프>/mail.html 생성 (규칙 8). src=원본 PNG 의 file:/// 절대 경로
"""
import argparse
import base64
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
sys.path.insert(0, str(PROJECT_ROOT))   # 스크립트로 직접 실행해도 tools.* import 가 되도록

from tools.build_cardnews import ACCENT_MAP, as_lines, hex_to_rgba  # noqa: E402

MAIL_TEMPLATE_NAME = "mail_template.html.j2"
MAIL_BODY_TEMPLATE_NAME = "mail_body_template.html.j2"
MAIL_SYMBOL_PATH = HERE / "cardnews" / "nrf-symbol-mail.png"   # 메일용 축소 심벌 (원본은 162KB 라 과대)

JINJA_ENV = Environment(loader=FileSystemLoader(HERE / "cardnews"), autoescape=True)


def render_mail_html(srcs: list) -> str:
    """이미지 src 목록(헤더→카드 순)으로 메일 본문 HTML 문자열을 만든다.
    backend 가 콘텐츠 업로드 단계에서 그대로 import 해 쓸 수 있는 순수 함수."""
    return JINJA_ENV.get_template(MAIL_TEMPLATE_NAME).render(images=srcs)


def load_cards(run_dir: Path) -> list:
    """run 폴더의 card_NN.json 을 번호순으로 읽는다 (card_NN_orig.json 백업 제외)."""
    files = sorted(p for p in run_dir.glob("card_*.json")
                   if re.fullmatch(r"card_\d{2}\.json", p.name))
    if not files:
        sys.exit(f"card_NN.json 이 한 건도 없습니다: {run_dir}")
    return [json.loads(p.read_text(encoding="utf-8-sig")) for p in files]


def render_mail_body(cards: list, week_label: str) -> str:
    """카드 JSON 목록으로 메일 본문(HTML로 직접 그린 마스트헤드+카드 전체)을 만든다.
    이미지 내장(PNG data URI, 약 18MB)이 기관 메일에서 표시되지 않아 도입한 방식 —
    본문이 수십 KB 텍스트라 용량 문제가 없다. 디자인 수치는 템플릿 주석 참고."""
    if not MAIL_SYMBOL_PATH.exists():
        sys.exit(f"메일용 NRF 심벌이 없습니다: {MAIL_SYMBOL_PATH}")
    symbol = "data:image/png;base64," + base64.b64encode(MAIL_SYMBOL_PATH.read_bytes()).decode("ascii")
    ctx_cards = []
    for card in cards:
        accent = ACCENT_MAP.get(card.get("category", "정책"), "#3E6DE8")
        ctx_cards.append({
            "category": card.get("category", "정책"),
            "article_no": str(card.get("article_no", "")),
            "week_label": card.get("week_label", ""),
            "title": card.get("title", ""),
            "subhead": card.get("subhead", ""),
            "points": card.get("points", []),
            "source_lines": as_lines(card.get("source", "")),
            "accent": accent,
            "accent_glow": hex_to_rgba(accent, 0.55),
            "accent_hl": hex_to_rgba(accent, 0.20),
        })
    return JINJA_ENV.get_template(MAIL_BODY_TEMPLATE_NAME).render(
        week_label=week_label, nrf_symbol=symbol, cards=ctx_cards)


def png_data_uri_srcs(files: list) -> list:
    """PNG 파일들을 본문 내장용 data URI 문자열로 바꾼다. 외부 호스팅 없이 이미지가 보이는
    대신 본문이 매우 커진다(base64 는 원본의 약 1.37배) — 용량 확인 후 쓸 것."""
    return ["data:image/png;base64," + base64.b64encode(p.read_bytes()).decode("ascii")
            for p in files]


def collect_pngs(run_dir: Path) -> list:
    """run 폴더에서 헤더+카드 PNG 를 번호순으로 모은다. 누락은 명확한 오류로 멈춘다."""
    header = run_dir / "header.png"
    if not header.exists():
        sys.exit(f"header.png 가 없습니다: {run_dir} — 발행(렌더)을 먼저 실행하세요")
    cards = sorted(p for p in run_dir.glob("card_*.png")
                   if re.fullmatch(r"card_\d{2}\.png", p.name))
    if not cards:
        sys.exit(f"card_NN.png 가 한 장도 없습니다: {run_dir} — 발행(렌더)을 먼저 실행하세요")
    return [header] + cards


def main() -> None:
    ap = argparse.ArgumentParser(description="헤더+카드뉴스 PNG 병합 메일 HTML 생성")
    ap.add_argument("--run", required=True, help="run 폴더 경로 (data/runs/<타임스탬프>)")
    ap.add_argument("--srcs", help="업로드된 이미지 URL 배열 JSON 파일 (순서: 헤더→카드 번호순)")
    ap.add_argument("--embed", action="store_true",
                    help="이미지를 data URI 로 본문에 내장 (호스팅 불필요, 대신 본문이 매우 커짐)")
    ap.add_argument("--native", action="store_true",
                    help="카드를 이미지가 아니라 HTML 로 직접 그린다 (card_NN.json 사용, PNG 불필요)")
    ap.add_argument("--test", action="store_true",
                    help="data/tests/<타임스탬프>/ 에 출력 (src 는 file:/// 절대 경로)")
    args = ap.parse_args()
    if sum(map(bool, (args.srcs, args.embed, args.native))) > 1:
        sys.exit("--srcs / --embed / --native 는 하나만 쓸 수 있습니다 (본문 구성 방식이 서로 다름)")

    run_dir = Path(args.run)
    if not run_dir.is_dir():
        sys.exit(f"run 폴더가 없습니다: {run_dir}")

    if args.native:
        cards = load_cards(run_dir)
        html = render_mail_body(cards, cards[0].get("week_label", ""))
        summary = f"카드 {len(cards)}건 HTML 직접 그리기, {len(html.encode('utf-8')) / 1024:.0f} KB"
    else:
        files = collect_pngs(run_dir)
        if args.embed:
            srcs = png_data_uri_srcs(files)
        elif args.srcs:
            urls = json.loads(Path(args.srcs).read_text(encoding="utf-8-sig"))   # BOM 있어도 허용
            if not isinstance(urls, list) or not all(isinstance(u, str) for u in urls):
                sys.exit(f"--srcs 는 문자열 배열 JSON 이어야 합니다: {args.srcs}")
            if len(urls) != len(files):
                sys.exit(f"URL 수({len(urls)})가 이미지 수({len(files)} — 헤더1+카드{len(files)-1})와 다릅니다")
            srcs = urls
        elif args.test:
            srcs = [p.resolve().as_uri() for p in files]
        else:
            srcs = [p.name for p in files]   # mail.html 이 PNG 옆에 놓이므로 상대 파일명으로 충분
        html = render_mail_html(srcs)
        summary = f"이미지 {len(files)}장 = 헤더 1 + 카드 {len(files) - 1}"

    if args.test:
        out_dir = PROJECT_ROOT / "data" / "tests" / datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir.mkdir(parents=True, exist_ok=True)
    else:
        out_dir = run_dir
    out = out_dir / "mail.html"
    out.write_text(html, encoding="utf-8")
    print(f"생성 완료: {out} ({summary})")


if __name__ == "__main__":
    main()
