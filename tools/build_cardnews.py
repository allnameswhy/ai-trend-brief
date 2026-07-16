# -*- coding: utf-8 -*-
"""
카드뉴스 빌더 — 『AI 브리프 카드뉴스』 디자인(1080×1240)을 실제 데이터로 렌더링한다.

원 디자인은 Claude Design 캔버스 전용 컴포넌트(.dc.html)라 로컬에서 그대로 실행되지 않는다.
이 스크립트는 같은 시안을 순수 HTML/CSS 템플릿(card_template.html.j2)으로 옮긴 뒤,
콘텐츠 JSON을 끼워 넣어 HTML을 만들고, Edge 헤드리스로 PNG까지 뽑아낸다.
(PDF 파이프라인과 동일한 Edge를 재사용 — 새 라이브러리 없음)

사용법:
    python tools/build_cardnews.py 내콘텐츠.json         # 콘텐츠 렌더
    python tools/build_cardnews.py 내콘텐츠.json --out 폴더 --no-png
    python tools/build_cardnews.py 내콘텐츠.json --check  # 기계 검사만 (자수 규격 + 렌더 실측 넘침)

콘텐츠 JSON 스키마: tools/cardnews/card_schema.md 참고.
"""
import argparse
import contextlib
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

HERE = Path(__file__).resolve().parent
TEMPLATE_NAME = "card_template.html.j2"

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
    """문자열이면 한 줄짜리 목록으로, 목록이면 그대로 반환. (source용)"""
    if isinstance(value, list):
        return value
    return [value] if value else []


def render_html(card: dict) -> str:
    category = card.get("category", "정책")
    accent = ACCENT_MAP.get(category, "#3E6DE8")

    context = {
        "category": category,
        "accent": accent,
        "accent_glow": hex_to_rgba(accent, 0.55),
        "accent_hl": hex_to_rgba(accent, 0.20),   # 핵심 구절 형광펜 하이라이트 색
        "article_no": str(card.get("article_no", "")),
        "week_label": card.get("week_label", ""),
        "title": card.get("title", ""),
        "subhead": card.get("subhead", ""),
        # points 항목: {pre, key, post, sub?} — key는 문장 속 하이라이트 구절, sub는 보조 설명(선택)
        "points": card.get("points", []),
        "source_lines": as_lines(card.get("source", "")),
    }
    return JINJA_ENV.get_template(TEMPLATE_NAME).render(context)


def find_edge() -> str | None:
    for p in EDGE_CANDIDATES:
        if Path(p).exists():
            return p
    return None


# ── 넘침 검사 (--check) ────────────────────────────────────────
# 글자 수 규칙은 근사치라, 최종 판정은 실제로 렌더해서 잰다 (기계 검사).
# 카드에 측정 스크립트를 끼워 Edge 헤드리스로 열고 세 가지를 읽어 온다:
#   bottom_overflow — 푸터 하단이 카드 높이(1240px)를 벗어난 픽셀 수. 0보다 크면 넘침(잘림).
#                     (내용이 넘치면 본문·푸터 사이 여백이 음수가 되는 게 아니라
#                      전체가 아래로 밀려 카드 밖에서 잘리므로, 이걸로 판정해야 한다)
#   gap             — 본문 끝~푸터 시작 여백(참고용). 내용이 적어 여백이 커지는 것은 허용된 디자인.
#   title_overflow  — 제목(한 줄 고정)이 잘린 픽셀 수.
CHECK_JS = """
<script>
(async () => {
  try { await document.fonts.ready; } catch (e) {}
  await new Promise(r => setTimeout(r, 300));
  const $ = s => document.querySelector(s);
  const card = $('.card'), points = $('.points'), footer = $('.footer');
  const inner = $('.footer .inner'), title = $('.headline .title');
  const cardTop = card.getBoundingClientRect().top;
  const m = {
    bottom_overflow: Math.max(0, Math.round(footer.getBoundingClientRect().bottom - cardTop - 1240)),
    gap: Math.round(inner.getBoundingClientRect().top - points.getBoundingClientRect().bottom),
    title_overflow: title ? Math.max(0, title.scrollWidth - Math.round(title.getBoundingClientRect().width)) : 0,
  };
  document.title = 'CARDCHECK' + JSON.stringify(m);
})();
</script>
"""


@contextlib.contextmanager
def edge_user_data_dir():
    """Edge headless 용 임시 user-data-dir.
    Windows에서 Edge가 그 안에 Crashpad 폴더를 남겨, tempfile.TemporaryDirectory 자동 정리가
    'WinError 145: 디렉터리가 비어 있지 않습니다' 로 실패한다. 정리 오류는 무시한다(임시 폴더라 무방)."""
    udd = tempfile.mkdtemp()
    try:
        yield udd
    finally:
        shutil.rmtree(udd, ignore_errors=True)


def check_layout(card: dict) -> dict | None:
    """카드를 실제로 렌더해 레이아웃 수치를 잰다. Edge가 없으면 None."""
    edge = find_edge()
    if not edge:
        return None
    html = render_html(card).replace("</body>", CHECK_JS + "</body>")
    with edge_user_data_dir() as udd:
        page = Path(udd) / "check.html"
        page.write_text(html, encoding="utf-8")
        out = subprocess.run(
            [edge, "--headless=new", "--disable-gpu", "--virtual-time-budget=6000",
             f"--user-data-dir={udd}", "--dump-dom", page.as_uri()],
            capture_output=True, timeout=90)
    m = re.search(rb"<title>CARDCHECK(.*?)</title>", out.stdout, re.S)
    return json.loads(m.group(1).decode("utf-8")) if m else None


# ── 자수 규격 검사 ─────────────────────────────────────────────
# (렌더 없이 0초로 판정하는 1차 검사. 실측 검사와 함께 card_problems 로 묶어 쓴다)

def weighted_len(text: str) -> float:
    """환산 자수 — 한글·한자·전각 1자, 공백 0.3자, 영문·숫자·부호 0.6자.
    (실측 자폭 비율: 24px 기준 한글 20.7px, 영문 12.8px≈0.62, 공백 5.9px≈0.29)"""
    total = 0.0
    for c in text:
        if c.isspace():
            total += 0.3
        elif ord(c) > 0x2E7F:   # 한글·한자·전각 문자
            total += 1.0
        else:
            total += 0.6
    return total


def est_lines(weighted: float) -> int:
    """문장 환산 자수 → 예상 줄수. 한 줄 용량 ≈ 44자(폭 920px ÷ 한글 20.7px),
    단어 단위 줄바꿈 여유를 둬 42자로 나눈다. card_schema.md의 환산표와 동일."""
    return max(1, math.ceil(weighted / 42))


def validate_card(card: dict) -> list[str]:
    """card_schema.md(v2) 규격 위반 목록을 돌려준다. 빈 목록이면 합격.
    자수·개수만 검사한다 — 실제 넘침은 check_layout(렌더 실측)이 판정."""
    errors = []

    def need(cond: bool, msg: str) -> None:
        if not cond:
            errors.append(msg)

    need(card.get("category") in ("정책", "기술", "윤리"), "category는 정책/기술/윤리 중 하나")
    need(bool(re.fullmatch(r"\d{2}", str(card.get("article_no", "")))), "article_no는 두 자리 숫자")
    need(bool(re.fullmatch(r"\d{1,2}월 \d주", card.get("week_label", ""))), "week_label은 'M월 N주' 형식")

    title = card.get("title", "")
    need(bool(title) and weighted_len(title) <= 17,
         f"title은 환산 17자 이하(현재 {weighted_len(title):.1f}자) — 한 줄 고정이라 넘치면 잘림")

    # 분량은 최대값만 검사한다 — 내용이 적어 본문·푸터 사이가 비는 것은 허용된 디자인
    subhead_w = weighted_len(card.get("subhead", ""))
    need(subhead_w <= 78, f"subhead는 환산 78자 이하(현재 {subhead_w:.1f}자)")

    points = card.get("points", [])
    need(1 <= len(points) <= 5, f"points는 최대 5개(현재 {len(points)}개)")
    total_lines = 0
    sub_count = 0
    for i, pt in enumerate(points, 1):
        key, post = pt.get("key", ""), pt.get("post", "")
        sentence = pt.get("pre", "") + key + (post if key else "")
        sw = weighted_len(sentence)
        need(0 < sw <= 126,
             f"point {i} 문장(pre+key+post)은 환산 126자 이하(현재 {sw:.1f}자)")
        total_lines += est_lines(sw)
        if key:
            kw = weighted_len(key)
            need(kw <= 25, f"point {i} key는 환산 25자 이하(현재 {kw:.1f}자)")
        else:
            need(not post, f"point {i}: key가 없으면 post도 쓰지 않는다(문장 전체를 pre에)")
        # sub는 문자열 하나 또는 배열 — 보조가 늘면 그만큼 포인트(문장 줄수)를 줄이면 된다
        subs = pt.get("sub", "") or []
        if isinstance(subs, str):
            subs = [subs]
        for j, sub in enumerate(subs, 1):
            sub_count += 1
            total_lines += 1
            need(weighted_len(sub) <= 46,
                 f"point {i} sub {j}는 환산 46자(한 줄) 이하(현재 {weighted_len(sub):.1f}자)")
    # 보조 한 줄(44px)은 문장 한 줄(36px)보다 약간 비싸서, 보조가 많으면 한도를 한 줄 줄인다
    line_budget = 13 if sub_count >= 4 else 14
    need(total_lines <= line_budget,
         f"본문 예상 줄수 합계(문장 줄+보조)는 {line_budget}줄 이하(현재 {total_lines}줄, 보조 {sub_count}개) — 카드에서 넘침")

    source = card.get("source", [])
    need(isinstance(source, list) and len(source) == 3, "source는 정확히 3줄")
    if isinstance(source, list) and len(source) == 3:
        need(source[0].startswith("원문 제목 : "), "source 1줄은 '원문 제목 : '으로 시작")
        need(len(source[0]) <= 100, f"source 1줄은 100자 이하(현재 {len(source[0])}자) — 한 줄 고정")
        need(bool(re.fullmatch(r"발간처\(발간일\) : .+\(\d{2}\.\d{2}\.\d{2}\.\)", source[1])),
             "source 2줄은 '발간처(발간일) : 매체(YY.MM.DD.)' 형식")
        need(bool(re.match(r"URL : (?!https?://).+", source[2])), "source 3줄은 'URL : '로 시작(프로토콜 생략)")
    return errors


def card_problems(card_path: Path) -> list[str]:
    """자수 규격(validate_card) + 렌더 실측(check_layout)을 합친 종합 기계 판정.
    빈 목록이면 합격. 오케스트레이터·테스트 하네스가 공용으로 쓴다."""
    try:
        card = json.loads(Path(card_path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return [f"JSON 형식 오류: {e}"]
    problems = validate_card(card)
    if problems:
        return problems          # 자수부터 맞춰야 실측이 의미 있으므로 여기서 반환
    r = check_layout(card)
    if r is None:
        return []                # Edge 없음 — 자수 검사 통과로 갈음
    if r["bottom_overflow"] > 0:
        px = r["bottom_overflow"]
        # 문장 한 줄 36px(보조 한 줄 44px), 한 줄 ≈ 환산 42자 — card_schema.md 실측값과 동일
        lines_over = math.ceil(px / 36)
        problems.append(
            f"실측 결과 내용이 카드 아래로 {px}px 넘침 — 약 {lines_over}줄(환산 약 {lines_over * 42}자) 분량을 줄일 것"
            f" (보조 설명 한 줄을 빼면 44px 확보)")
    if r["title_overflow"] > 0:
        problems.append(f"실측 결과 제목이 {r['title_overflow']}px 잘림 — 제목을 더 짧게")
    return problems


def run_check(card_path: Path) -> int:
    """기계 검사(자수 규격 + 렌더 실측) 결과를 출력하고 종료 코드를 돌려준다 (0=통과, 2=불합격)."""
    problems = card_problems(card_path)
    if problems:
        for p in problems:
            print(f"  불합격: {p}")
        return 2
    print("  기계 검사 통과")
    return 0


def export_png(html_path: Path, png_path: Path) -> bool:
    edge = find_edge()
    if not edge:
        print("  ! Edge를 찾지 못해 PNG는 건너뜀. HTML만 생성됨.", file=sys.stderr)
        return False
    with edge_user_data_dir() as udd:
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
    # Edge --headless=new 는 스크린샷을 비동기로 저장하는 경우가 있어, 프로세스 종료 직후엔
    # 아직 파일이 없을 수 있다. 최대 2초까지 생성 여부를 확인한다 (허위 '실패' 방지).
    for _ in range(20):
        if png_path.exists():
            return True
        time.sleep(0.1)
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
    ap.add_argument("content", help="카드 콘텐츠 JSON (규격: tools/cardnews/card_schema.md)")
    ap.add_argument("--out", default=None, help="출력 폴더 (기본: 콘텐츠 JSON과 같은 폴더)")
    ap.add_argument("--no-png", action="store_true", help="HTML만 생성(PNG 건너뜀)")
    ap.add_argument("--check", action="store_true", help="렌더 대신 기계 검사만 수행 (자수 규격 + 렌더 실측)")
    ap.add_argument("--test", action="store_true",
                    help="테스트 렌더 — 결과를 data/tests/<타임스탬프>/ 에 저장 (data/runs 와 같은 구조)")
    args = ap.parse_args()

    card_path = Path(args.content)
    if not card_path.exists():
        sys.exit(f"콘텐츠 파일을 찾을 수 없습니다: {card_path}")

    if args.check:
        print(f"카드 기계 검사: {card_path.name}")
        sys.exit(run_check(card_path))

    # 출력 폴더 결정:
    #   --test: 테스트 산출물은 종류 불문 data/tests/<타임스탬프>/ 에 모은다 (data/runs/<타임스탬프>/ 와 같은 구조)
    #   --out : 지정한 폴더
    #   기본  : 콘텐츠 JSON 과 같은 폴더 (파이프라인은 card_NN.json 옆 run 폴더에 그대로 생성)
    if args.test:
        out_dir = HERE.parent / "data" / "tests" / datetime.now().strftime("%Y%m%d_%H%M%S")
    elif args.out:
        out_dir = Path(args.out)
    else:
        out_dir = card_path.parent
    print(f"카드뉴스 빌드: {card_path.name}")
    build_one(card_path, out_dir, make_png=not args.no_png)
    print(f"완료. → {out_dir}")


if __name__ == "__main__":
    main()
