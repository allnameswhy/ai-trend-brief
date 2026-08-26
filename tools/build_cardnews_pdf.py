#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
카드뉴스 HTML → 병합 PDF (열람용)

발행 산출물 폴더(run/publish)의 cover.html + card_NN.html 을 Edge 헤드리스로
각각 PDF 로 인쇄한 뒤 한 권으로 병합한다.

PNG 를 이어 붙인 PDF 와 달리 글자가 벡터로 들어가므로
 - 확대해도 선명하고, 텍스트 검색·복사가 되며,
 - 용량이 훨씬 작다 (PNG 병합 16MB → 1MB 안팎).
메일 첨부용 최종 발간물은 여전히 PNG 다. 이건 보관·열람용이다.

왜 HTML 을 하나로 합치지 않고 PDF 를 병합하는가:
  표지와 카드 템플릿이 dot·grid·week·glow-tr·glow-bl 다섯 클래스명을 공유하는데
  값이 서로 다르다. 한 문서에 합치면 뒤 스타일이 앞을 덮어써 배경·여백이 틀어진다.
  문서마다 따로 인쇄하면 CSS 가 격리되어 이 문제가 생기지 않는다.

사용법:
    python tools/build_cardnews_pdf.py                      # data/runs 의 최신 run
    python tools/build_cardnews_pdf.py data/runs/20260803_111102
    python tools/build_cardnews_pdf.py <run> --header       # 표지 대신 헤더를 맨 앞에
    python tools/build_cardnews_pdf.py <run> -o out.pdf

필요 패키지: pypdf (PDF 병합용. 순수 파이썬이라 별도 시스템 설치 불필요)
    pip install pypdf
"""

from __future__ import annotations

import argparse
import base64
import io
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_cardnews import find_edge, edge_user_data_dir  # noqa: E402  (Edge 탐색 로직 재사용)

# 카드·표지는 1080×1240 CSS px. 96dpi 기준 인치로 환산해 페이지 크기로 준다.
CARD_PAGE_IN = (1080 / 96, 1240 / 96)   # 11.25 × 12.9167 in
HEADER_PAGE_IN = (1080 / 96, 380 / 96)  # 헤더는 세로가 짧다

# 가변(Variable) 폰트는 Edge 인쇄 시 PDF 에 Type3(도형 글리프)로 들어가는데, Adobe Acrobat 이
# 이를 백지로 렌더링한다 (2026-08-05 실측 — pdfium 계열 뷰어에서만 보였음). 인쇄용 임시 복사본에서
# 폰트 CSS 를 정적(static) Pretendard 로 바꿔 진짜 폰트가 내장되게 한다. 템플릿의 font-family 는
# 'Pretendard Variable', Pretendard 순서라 가변 폰트가 없으면 자동으로 정적 폰트로 넘어간다.
# ※ 서브셋 조각(dynamic-subset)판이 아니라 통짜(pretendard.min.css)판을 쓴다 — 조각판은 쓰인
#   유니코드 조각마다 별도 폰트로 내장돼 폰트가 페이지당 수십 개(11쪽 300개 실측)가 되고,
#   Edge 의 Adobe 계열 PDF 뷰어가 그 개수 탓에 로드를 굉장히 오래 끈다 (2026-08-05 실측).
VARIABLE_FONT_CSS = "dist/web/variable/pretendardvariable-dynamic-subset.min.css"
STATIC_FONT_CSS = "dist/web/static/pretendard.min.css"

# ── 장식 배경 래스터화 (2026-08-05 — Edge/Acrobat 로드 속도) ──────────────
# 전면 그라데이션·글로우(알파 그라데이션→투명 마스크)·모눈 격자(타일 패턴)를 쪽마다 벡터로 그리면
# Adobe 계열 뷰어가 페이지 그리기를 오래 끈다. 장식 배경만 한 번 이미지로 구워 모든 쪽이 공유하게
# 한다(내용 글자는 벡터 유지 — 검색·복사·선명도 불변. 병합 뒤 중복 제거로 PDF 안에서도 한 장).
# 아래 값들은 card_template/cover_template 의 배경 시스템을 복사한 것 — 템플릿 변경 시 함께 갱신.
BACKDROP_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><style>
* { margin:0; padding:0; box-sizing:border-box; }
.card { width:1080px; height:1240px; position:relative; overflow:hidden;
  background: linear-gradient(158deg, #F7F8FC 0%, #EDF1FA 46%, #E6EDFB 72%, #F1F4FA 100%); }
.glow-tr { position:absolute; top:-220px; right:-180px; width:640px; height:640px; border-radius:50%;
  background: radial-gradient(circle, rgba(62,109,232,0.14) 0%, rgba(62,109,232,0) 68%); }
.glow-bl { position:absolute; bottom:-260px; left:-200px; width:700px; height:700px; border-radius:50%;
  background: radial-gradient(circle, rgba(38,178,255,0.10) 0%, rgba(38,178,255,0) 70%); }
.grid { position:absolute; inset:0;
  background-image: linear-gradient(rgba(43,68,128,0.05) 1px, transparent 1px),
                    linear-gradient(90deg, rgba(43,68,128,0.05) 1px, transparent 1px);
  background-size: 72px 72px; }
</style></head><body><div class="card"><div class="glow-tr"></div><div class="glow-bl"></div><div class="grid"></div></div></body></html>"""

# 인쇄 복사본에 끼워 넣어 벡터 장식을 끄고 래스터 배경으로 바꾸는 CSS (표지 .cover, 카드 .card 공용)
BACKDROP_CSS = """
<style id="pdf-backdrop">
  .glow-tr, .glow-bl, .grid {{ display: none !important; }}
  .card, .cover {{ background: url({uri}) 0 0 / 1080px 1240px no-repeat !important; }}
</style>
"""


def render_backdrop(edge: str, udd: str) -> str | None:
    """장식 배경을 1080×1240 이미지 한 장(data URI)으로 굽는다. 실패하면 None(벡터 그대로)."""
    with tempfile.TemporaryDirectory() as td:
        html = Path(td) / "backdrop.html"
        png = Path(td) / "backdrop.png"
        html.write_text(BACKDROP_HTML, encoding="utf-8")
        cmd = [edge, "--headless=new", "--disable-gpu", "--hide-scrollbars",
               "--force-device-scale-factor=1", "--window-size=1080,1240",
               f"--user-data-dir={udd}", f"--screenshot={png.resolve()}", html.resolve().as_uri()]
        subprocess.run(cmd, capture_output=True, timeout=120)
        for _ in range(20):
            if png.exists() and png.stat().st_size > 0:
                break
            time.sleep(0.1)
        if not png.exists() or png.stat().st_size == 0:
            return None
        data, mime = png.read_bytes(), "image/png"
        try:                     # Pillow 가 있으면 JPEG 로 줄인다 (그라데이션은 JPEG 압축 효율이 좋다)
            from PIL import Image
            buf = io.BytesIO()
            Image.open(io.BytesIO(data)).convert("RGB").save(buf, "JPEG", quality=90)
            data, mime = buf.getvalue(), "image/jpeg"
        except ImportError:
            pass
        return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


# ── 형광펜 강조 인쇄 호환 (2026-08-25) ──────────────────────────────────
# 본문 강조 구절(.pt-main .text strong)의 linear-gradient(transparent 68%, rgba 68%) 배경을
# Edge 인쇄가 '타일링 패턴 + 투명 마스크(SMask Luminosity)'로 내보내는데, Adobe 계열 뷰어
# (Acrobat·Edge 내장)가 이를 그리지 못해 형광펜이 사라진다 (pdfium 계열 뷰어에서만 보임 —
# 가변 폰트 백지와 같은 유형, 2026-08-25 실측). 인쇄 복사본에서만 같은 모양의 단순 채우기
# (box-shadow, 높이 0.4em = 실측 9.5px)로 바꾼다 — 상수 알파 사각형이라 모든 뷰어가 그린다.
# PNG 렌더(원본 HTML)는 그라데이션을 그대로 쓴다.
HL_GRADIENT_RE = re.compile(r"linear-gradient\(transparent 68%,\s*(rgba\([^()]+\))\s+68%\)")
HL_PRINT_CSS = """
<style id="pdf-highlight-fix">
  .pt-main .text strong {{ background: none !important;
    box-shadow: inset 0 -0.4em 0 0 {color} !important; }}
</style>
"""

# 원본 HTML 은 건드리지 않고, 임시 복사본에만 끼워 넣는 인쇄용 CSS.
#   @page       — 페이지를 카드 크기에 정확히 맞추고 여백을 없앤다
#   color-adjust— 배경 그라데이션이 인쇄에서 빠지지 않게 강제한다
PRINT_CSS = """
<style id="pdf-page-setup">
  @page {{ size: {w}in {h}in; margin: 0; }}
  html, body {{
    margin: 0; padding: 0;
    -webkit-print-color-adjust: exact; print-color-adjust: exact;
  }}
</style>
"""


def find_latest_run(root: Path) -> Path | None:
    runs = sorted((root / "data" / "runs").glob("*/"), key=lambda p: p.name)
    return runs[-1] if runs else None


def collect_pages(pub: Path, use_header: bool) -> list[Path]:
    """맨 앞 장(표지 또는 헤더) + card_01..NN 순서로 HTML 경로를 모은다."""
    pages: list[Path] = []
    first = pub / ("header.html" if use_header else "cover.html")
    if first.exists():
        pages.append(first)
    else:
        print(f"  ! {first.name} 없음 — 건너뜀", file=sys.stderr)
    pages += sorted(pub.glob("card_*.html"), key=lambda p: p.name)
    return pages


def html_to_pdf(edge: str, html: Path, pdf_out: Path, page_in: tuple[float, float],
                udd: str, backdrop: str | None = None) -> bool:
    """HTML 한 장을 PDF 한 쪽으로 인쇄한다.
    udd(user-data-dir)는 호출자가 전체 빌드 동안 하나를 공유한다 — 통짜 폰트(수백 KB×웨이트)를
    쪽마다 다시 받지 않고 Edge HTTP 캐시를 재사용하기 위함.
    backdrop 이 주어지면 벡터 장식 배경을 끄고 그 이미지로 대체한다(로드 속도 — 위 주석 참조)."""
    src = html.read_text(encoding="utf-8")
    src = src.replace(VARIABLE_FONT_CSS, STATIC_FONT_CSS)   # Acrobat 백지 방지 — 위 주석 참조
    css = PRINT_CSS.format(w=round(page_in[0], 4), h=round(page_in[1], 4))
    if backdrop:
        css += BACKDROP_CSS.format(uri=backdrop)
    hl = HL_GRADIENT_RE.search(src)         # 카드 본문의 형광펜 그라데이션 → 인쇄 호환 채우기
    if hl:
        css += HL_PRINT_CSS.format(color=hl.group(1))
    # </head> 앞에 넣어야 문서 자체 스타일보다 뒤에 와서 확실히 적용된다.
    if "</head>" in src:
        src = src.replace("</head>", css + "</head>", 1)
    else:
        src = css + src

    # 임시 복사본은 원본과 같은 폴더에 둔다 — 상대 경로 자원(있을 경우)이 깨지지 않게.
    tmp_html = html.with_name(f"~pdftmp_{html.stem}.html")
    tmp_html.write_text(src, encoding="utf-8")
    try:
        cmd = [
            edge,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--no-pdf-header-footer",        # 머리말(제목)·꼬리말(URL·쪽번호) 제거
            "--virtual-time-budget=6000",    # 웹폰트(Pretendard) 로드 대기
            f"--user-data-dir={udd}",
            f"--print-to-pdf={pdf_out.resolve()}",
            tmp_html.resolve().as_uri(),
        ]
        subprocess.run(cmd, capture_output=True, timeout=180)
        # Edge 는 파일을 비동기로 떨구는 경우가 있어 잠깐 기다린다 (허위 '실패' 방지).
        for _ in range(30):
            if pdf_out.exists() and pdf_out.stat().st_size > 0:
                return True
            time.sleep(0.1)
        return False
    finally:
        tmp_html.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="카드뉴스 HTML을 병합 PDF로 인쇄한다 (열람용)")
    ap.add_argument("run", nargs="?", help="run 폴더 (생략하면 data/runs 의 최신 run)")
    ap.add_argument("--header", action="store_true",
                    help="표지(cover) 대신 헤더(header)를 맨 앞 장으로 쓴다")
    ap.add_argument("-o", "--out", help="출력 PDF 경로 (기본: publish/AITrend_<run>_vector.pdf)")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    run = Path(args.run) if args.run else find_latest_run(root)
    if not run or not run.exists():
        print(f"run 폴더를 찾지 못했습니다: {args.run or '(data/runs 비어 있음)'}", file=sys.stderr)
        return 1

    # 발행 산출물은 publish/ 아래에 모인다 (2026-08-05). 옛 run 은 루트에 있을 수 있다.
    pub = run / "publish"
    if not pub.exists():
        pub = run
    pages = collect_pages(pub, args.header)
    if not pages:
        print(f"인쇄할 HTML이 없습니다: {pub}", file=sys.stderr)
        return 1

    edge = find_edge()
    if not edge:
        print("Edge를 찾지 못했습니다. PDF를 만들 수 없습니다.", file=sys.stderr)
        return 1

    try:
        from pypdf import PdfWriter
    except ImportError:
        print("pypdf 가 필요합니다:  pip install pypdf", file=sys.stderr)
        return 1

    out = Path(args.out) if args.out else pub / f"AITrend_{run.name}_vector.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)

    print(f"run    : {run}")
    print(f"입력   : {len(pages)}장 ({', '.join(p.stem for p in pages)})")

    with tempfile.TemporaryDirectory() as td, edge_user_data_dir() as udd:
        backdrop = render_backdrop(edge, udd)
        if backdrop:
            print(f"  배경 래스터화: 약 {len(backdrop) * 3 // 4 // 1024} KB — 전 쪽 공유")
        else:
            print("  ! 배경 래스터화 실패 — 벡터 배경 그대로 진행 (느릴 수 있음)", file=sys.stderr)

        parts: list[Path] = []
        for i, html in enumerate(pages, 1):
            size = HEADER_PAGE_IN if html.stem == "header" else CARD_PAGE_IN
            part = Path(td) / f"{i:02d}_{html.stem}.pdf"
            # 헤더(1080×380)는 배경 치수가 달라 래스터 배경을 적용하지 않는다 (한 장뿐이라 영향 미미)
            ok = html_to_pdf(edge, html, part, size, udd,
                             backdrop=None if html.stem == "header" else backdrop)
            print(f"  [{i:2d}/{len(pages)}] {html.name:<16} {'OK' if ok else '실패'}")
            if ok:
                parts.append(part)

        if not parts:
            print("모든 장이 실패했습니다.", file=sys.stderr)
            return 1

        writer = PdfWriter()
        for part in parts:
            writer.append(str(part))
        # 쪽마다 따로 인쇄해 합치므로 배경 패턴·음영 등이 쪽 수만큼 중복된다 — 동일 객체를 하나로 합쳐
        # 파일 크기와 뷰어의 해석 부담을 줄인다 (폰트 서브셋은 쪽마다 글자가 달라 대부분 남는다).
        writer.compress_identical_objects(remove_duplicates=True, remove_unreferenced=True)
        with open(out, "wb") as f:
            writer.write(f)
        writer.close()

    print(f"\n완성 : {out}  ({out.stat().st_size:,} bytes, {len(parts)}쪽)")
    if out.stat().st_size > 5 * 1024 * 1024:
        print("  ! 5MB를 넘습니다. 표지 심벌 이미지가 큰지 확인하세요.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
