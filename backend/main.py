"""
FastAPI 서버 (HITL 대시보드 백엔드 — PROJECT_NOTES 2.6)
--------------------------------------------------------
역할: 파이프라인을 두 단계로 나눠 실행하고, 단계 사이에 사람의 확인을 끼워 넣는다.
  [1단계] 조사자→편집장 → '선정 대기' → 사람이 기사 선정 확정
  [2단계] 작가→검토자(+반려 수정 1회) → '카드 검토 대기' → 사람이 카드 확인·수정
  [발행]  PNG 렌더까지만 — 메일 발송은 구현하지 않는다 (자동 발송 금지, HITL)

진행 상황은 SSE(Server-Sent Events)로 브라우저에 실시간 스트리밍한다.
이벤트는 JSON 구조: {"id", "type", "agent", "message", ...}
  type: log(진행 로그) / agent_text(에이전트 발화 전문) / tool_use(도구 사용)
        / state(상태 전이) / error(오류 — 화면에서 강조)

상태 머신 (state.json 으로 run 폴더에도 기록 — 서버 재시작 시 복원):
  idle → phase1_running → waiting_selection → phase2_running
       → waiting_final_review → rendering → done
  어느 실행 상태에서든 → error(진행 불가) / cancelled(중단 버튼)
복구: candidates.json 만 있으면 1단계 재개(편집장부터), selected.json 이 있으면
      2단계 재개(없거나 깨진 카드부터) — 기존 산출물을 재사용해 토큰 낭비를 막는다.

실행:  uvicorn backend.main:app        ← 실제 발간 run
       (--reload 는 개발 중에만 — 실행 중 파일 저장 시 파이프라인이 죽는다)
접속:  http://localhost:8000

파이프라인 로직은 backend/orchestrator.py 에 있다. (터미널/웹 두 입구에서 같은 함수 공유)
"""

import asyncio
import json
import os
import shutil
import subprocess
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

from backend.orchestrator import (
    PROJECT_ROOT,
    PipelineError,
    make_logger,
    new_run_dir,
    png_status,
    render_cards,
    run_phase1,
    run_phase2,
)
from backend import card_service

INDEX_HTML = PROJECT_ROOT / "frontend" / "index.html"
RUNS_DIR = PROJECT_ROOT / "data" / "runs"

RUNNING_STATES = ("phase1_running", "phase2_running", "rendering")

# ── 전역 상태 (데모용: 한 번에 하나의 run 만) ─────────────────────────────
current = {"run_dir": None, "state": "idle", "error": None, "cards": 0, "task": None}

# SSE: 접속별 큐 팬아웃 + 최근 이벤트 링버퍼(새로고침·재접속 시 재전송)
history: deque = deque(maxlen=500)
subscribers: set = set()
event_seq = 0
MAIN_LOOP = None   # push_event 를 다른 스레드(to_thread 안의 렌더)에서도 안전하게 부르기 위함


# ── 이벤트 발행 ───────────────────────────────────────────────────────────
def _deliver(ev: dict) -> None:
    """이벤트에 id를 붙여 링버퍼에 쌓고 모든 구독자 큐로 팬아웃한다. (이벤트 루프 안에서만 호출)"""
    global event_seq
    event_seq += 1
    ev["id"] = event_seq
    history.append(ev)
    dead = []
    for q in subscribers:
        try:
            q.put_nowait(ev)
        except asyncio.QueueFull:      # 소비가 멎은(죽은) 연결 — 그 구독자만 버린다
            dead.append(q)
    for q in dead:
        subscribers.discard(q)


def push_event(type_: str, agent: str | None = None, message: str | None = None, **extra) -> None:
    """어디서 불러도 안전한 이벤트 발행. 렌더(to_thread) 등 다른 스레드에서 오면
    call_soon_threadsafe 로 이벤트 루프에 넘긴다 (asyncio.Queue 는 스레드 안전이 아님)."""
    ev = {"type": type_, "ts": datetime.now().strftime("%H:%M:%S")}
    if agent is not None:
        ev["agent"] = agent
    if message is not None:
        ev["message"] = message
    ev.update(extra)
    try:
        in_loop = asyncio.get_running_loop() is MAIN_LOOP
    except RuntimeError:
        in_loop = False
    if in_loop or MAIN_LOOP is None:
        _deliver(ev)
    else:
        MAIN_LOOP.call_soon_threadsafe(_deliver, ev)


_KIND_TO_TYPE = {"log": "log", "text": "agent_text", "tool": "tool_use"}


def on_log(stage: str, message: str, kind: str = "log") -> None:
    """오케스트레이터 로그 콜백 → SSE 이벤트. (파일 기록은 make_logger 가 별도로 한다)"""
    push_event(_KIND_TO_TYPE.get(kind, "log"), agent=stage, message=message)


# ── 상태 관리 ─────────────────────────────────────────────────────────────
def run_dir_path() -> Path | None:
    return Path(current["run_dir"]) if current["run_dir"] else None


def load_json(rd: Path, name: str, default=None):
    """run 폴더의 JSON 파일을 읽는다. 없거나 깨졌으면 default."""
    p = rd / name
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return default


def resume_available() -> str | None:
    """오류/중단 상태에서 가능한 재개 경로: "phase2"(카드부터) / "phase1"(편집장부터) / None."""
    rd = run_dir_path()
    if not rd or current["state"] not in ("error", "cancelled"):
        return None
    if (rd / "selected.json").exists():
        return "phase2"
    if (rd / "candidates.json").exists():
        return "phase1"
    return None


def write_state_file() -> None:
    """현재 상태를 run 폴더의 state.json 에 기록한다 (서버 재시작 시 복원용).
    상태 기록은 서버의 책임 — 오케스트레이터는 순수 파이프라인 라이브러리로 유지한다."""
    rd = run_dir_path()
    if not rd or not rd.exists():
        return
    data = {
        "state": current["state"],
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "error": current["error"],
        "cards": current["cards"],
    }
    (rd / "state.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def set_state(new_state: str, error: str | None = None) -> None:
    current["state"] = new_state
    current["error"] = error
    write_state_file()
    push_event("state", state=new_state, error=error,
               run_dir=current["run_dir"], cards=current["cards"], resume=resume_available())


# ── 파이프라인 백그라운드 태스크 ──────────────────────────────────────────
async def _phase_task(phase: str, resume: bool) -> None:
    """1·2단계 실행 래퍼 — 성공하면 대기 상태로, 오류는 error, 중단은 cancelled 로 전이.
    CancelledError 는 여기(태스크 최상위)에서만 흡수한다 — 아래 계층에서 잡으면
    SDK 서브프로세스 정리가 안 돌 수 있다."""
    rd = run_dir_path()
    logger = make_logger(rd, on_log)
    try:
        if phase == "phase1":
            set_state("phase1_running")
            note = " [편집장부터 재개 — 기존 candidates 재사용]" if resume else ""
            logger("orchestrator", f"1단계 시작 — 조사·선별 (run={rd.name}){note}")
            await run_phase1(rd, logger, resume=resume)
            set_state("waiting_selection")
            logger("orchestrator", "◆ 선정 대기 — 대시보드에서 기사 선정을 확정해 주세요")
        else:
            set_state("phase2_running")
            note = " [재개 — 이미 작성된 카드 재사용]" if resume else ""
            logger("orchestrator", f"2단계 시작 — 집필·검토{note}")
            n = await run_phase2(rd, logger, resume=resume)
            current["cards"] = n
            set_state("waiting_final_review")
            logger("orchestrator", f"◆ 카드 검토 대기 — 카드 {n}건 준비됨. 내용을 확인·수정한 뒤 [발행]을 누르세요")
    except asyncio.CancelledError:
        logger("orchestrator", "⛔ 사용자 요청으로 중단됨 — 만들어진 산출물은 재개 시 재사용됩니다")
        set_state("cancelled", error="사용자가 중단함")
    except PipelineError as e:
        push_event("error", agent="orchestrator", message=str(e))
        set_state("error", error=str(e))
    except Exception as e:   # 예상 못 한 오류도 화면에 강조 표시되도록
        logger("orchestrator", f"예상치 못한 오류: {e!r}")
        push_event("error", agent="orchestrator", message=f"예상치 못한 오류: {e}")
        set_state("error", error=str(e))
    finally:
        current["task"] = None


async def _publish_task() -> None:
    """발행: 전 카드 PNG 렌더 → done. (메일 발송 없음 — 자동 발송 금지, HITL)
    렌더는 동기 subprocess 라 to_thread 로 감싼다 — 렌더 중에도 SSE 가 멎지 않게."""
    rd = run_dir_path()
    logger = make_logger(rd, on_log)
    try:
        set_state("rendering")
        n = current["cards"] or len(list_card_numbers(rd))
        current["cards"] = n
        logger("orchestrator", f"발행 시작 — 카드 {n}건 PNG 렌더")
        await asyncio.to_thread(render_cards, rd, n, logger)
        made, missing = png_status(rd, n)
        if missing:
            logger("orchestrator",
                   f"PNG 누락 {len(missing)}건: {', '.join(missing)} — 렌더 실패, 위 로그의 오류 확인")
        logger("orchestrator", f"PNG 상태: {made}/{n}건 존재")
        logger("orchestrator",
               f"발행 완료 — PNG 를 확인하세요: {rd} (메일 발송은 미구현 — 확인 후 수동 발송)")
        set_state("done")
    except Exception as e:
        logger("orchestrator", f"발행 중 오류: {e!r}")
        push_event("error", agent="orchestrator", message=f"발행 중 오류: {e}")
        set_state("error", error=str(e))
    finally:
        current["task"] = None


# ── 헬퍼 ─────────────────────────────────────────────────────────────────
def list_card_numbers(rd: Path) -> list[str]:
    """run 폴더의 카드 번호 목록 ("01", "02", ...). card_NN_orig.json 백업은 제외된다."""
    nums = []
    for p in sorted(rd.glob("card_??.json")):
        nn = p.stem[5:7]
        if nn.isdigit():
            nums.append(nn)
    return nums


def selection_pool(rd: Path):
    """기사 선정 화면의 후보 풀을 만든다.
    반환: (pool {url: 기사 dict}, sel_by_url, drop_reason {url: 사유}, screened_missing)
    - screened.json(편집장의 게이트 통과 기록)이 있으면 그 안의 기사만 —
      게이트 배제가 확정된 기사는 화면에 아예 올리지 않는다.
    - 없는 구식 run 은 selected ∪ dropped 로 폴백한다.
    - 기사 내용은 편집장이 category 를 확정한 selected 항목을 우선 사용한다."""
    candidates = load_json(rd, "candidates.json", []) or []
    selected = load_json(rd, "selected.json", []) or []
    dropped = load_json(rd, "dropped.json", []) or []
    screened = load_json(rd, "screened.json", None)

    sel_by_url = {it.get("url"): it for it in selected if isinstance(it, dict) and it.get("url")}
    drop_reason = {it.get("url"): it.get("reason") or "(사유 없음)"
                   for it in dropped if isinstance(it, dict) and it.get("url")}

    if isinstance(screened, list):
        allowed = {u for u in screened if isinstance(u, str)}
        allowed |= set(sel_by_url) | set(drop_reason)   # 방어: 선정·탈락 기사는 항상 포함
        screened_missing = False
    else:
        allowed = set(sel_by_url) | set(drop_reason)
        screened_missing = True

    pool: dict = {}
    for c in candidates:
        url = c.get("url")
        if url in allowed and url not in pool:
            pool[url] = dict(sel_by_url.get(url, c))
    for url, it in sel_by_url.items():   # 방어: candidates 에 없는 선정 기사도 표시
        pool.setdefault(url, dict(it))
    return pool, sel_by_url, drop_reason, screened_missing


def auth_status() -> dict:
    """Claude 인증의 가벼운 추정 체크 — API 키 환경변수 또는 Claude Code 로그인 자격증명 파일.
    (진짜 검증은 파이프라인 실행 시 SDK 가 한다 — 실패하면 로그에 오류로 나타난다)"""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return {"logged_in": True, "method": "api_key"}
    if (Path.home() / ".claude" / ".credentials.json").exists():
        return {"logged_in": True, "method": "credentials"}
    return {"logged_in": False, "method": None}


def restore_last_run() -> None:
    """서버 시작 시 가장 최근 run 의 state.json 을 찾아 상태를 복원한다.
    실행 중(phase*_running/rendering)이었다면 프로세스가 죽은 것이므로 error 로 바꾼다."""
    if not RUNS_DIR.exists():
        return
    for d in sorted((p for p in RUNS_DIR.iterdir() if p.is_dir()),
                    key=lambda p: p.name, reverse=True):
        st = load_json(d, "state.json", None)
        if not isinstance(st, dict):
            continue      # state.json 없는 구식 run 은 무시
        current["run_dir"] = str(d)
        current["cards"] = st.get("cards") or 0
        state = st.get("state", "idle")
        if state in RUNNING_STATES:
            current["state"] = "error"
            current["error"] = "서버 재시작으로 실행이 중단됨 — 재개 버튼으로 이어서 실행하세요"
            write_state_file()
        else:
            current["state"] = state
            current["error"] = st.get("error")
        push_event("log", agent="orchestrator",
                   message=f"서버 시작 — 기존 run 복원: {d.name} (상태: {current['state']})")
        return


@asynccontextmanager
async def lifespan(app: FastAPI):
    global MAIN_LOOP
    MAIN_LOOP = asyncio.get_running_loop()
    restore_last_run()
    yield


app = FastAPI(title="AI TREND — 관리 대시보드", lifespan=lifespan)


# ── 기본 페이지·상태 ─────────────────────────────────────────────────────
@app.get("/")
def index():
    return FileResponse(INDEX_HTML)


@app.get("/state")
async def get_state():
    """페이지 로드·재접속 시 화면 라우팅의 근거가 되는 현재 상태."""
    return {
        "state": current["state"],
        "run_dir": current["run_dir"],
        "cards": current["cards"],
        "error": current["error"],
        "resume": resume_available(),
        "auth": auth_status(),
        "last_event_id": event_seq,
    }


# ── 실행 제어 ─────────────────────────────────────────────────────────────
@app.post("/run/phase1")
async def start_phase1():
    """1단계(조사·선별) 시작. 오류/중단 run 에 candidates 만 있으면 같은 폴더에서
    조사자를 건너뛰고 편집장부터 재개한다. 그 외에는 새 run 폴더로 처음부터."""
    if current["task"] is not None or current["state"] in RUNNING_STATES:
        return JSONResponse({"status": "busy", "error": "이미 실행 중입니다"}, status_code=409)
    resume = resume_available() == "phase1"
    if not resume:
        rd = new_run_dir()
        current["run_dir"] = str(rd)
        current["cards"] = 0
        current["error"] = None
    current["task"] = asyncio.create_task(_phase_task("phase1", resume))
    return {"status": "started", "run_dir": current["run_dir"], "resume": resume}


@app.post("/run/phase2")
async def start_phase2():
    """2단계(집필·검토) 시작. 선정 대기 상태에서의 정상 진행, 또는
    오류/중단 run 에 selected.json 이 있으면 재개(없거나 깨진 카드부터)."""
    if current["task"] is not None or current["state"] in RUNNING_STATES:
        return JSONResponse({"status": "busy", "error": "이미 실행 중입니다"}, status_code=409)
    rd = run_dir_path()
    if current["state"] == "waiting_selection":
        resume = False
    elif current["state"] in ("error", "cancelled") and rd and (rd / "selected.json").exists():
        resume = True
    else:
        return JSONResponse(
            {"status": "invalid_state",
             "error": "2단계는 선정 대기 상태, 또는 selected.json 이 있는 오류/중단 run 에서만 시작할 수 있습니다"},
            status_code=409)
    current["task"] = asyncio.create_task(_phase_task("phase2", resume))
    return {"status": "started", "resume": resume}


@app.post("/cancel")
async def cancel():
    """실행 중 파이프라인 중단. 렌더 중(rendering)에는 불가 — 스레드 안이라 끊을 수 없고 금방 끝난다."""
    task = current["task"]
    if not task or current["state"] not in ("phase1_running", "phase2_running"):
        return JSONResponse({"status": "not_running", "error": "중단할 실행이 없습니다 (렌더 중에는 중단 불가)"},
                            status_code=409)
    task.cancel()
    return {"status": "cancelling"}


@app.post("/publish")
async def publish():
    """발행 — 전 카드 PNG 렌더까지만. 메일 발송은 미구현(자동 발송 금지, HITL)."""
    if current["task"] is not None or current["state"] in RUNNING_STATES:
        return JSONResponse({"status": "busy", "error": "이미 실행 중입니다"}, status_code=409)
    if current["state"] not in ("waiting_final_review", "done"):
        return JSONResponse({"status": "invalid_state", "error": "발행은 카드 검토 대기(또는 완료) 상태에서만 가능합니다"},
                            status_code=409)
    current["task"] = asyncio.create_task(_publish_task())
    return {"status": "started"}


# ── 기사 선정 (1단계 후 HITL) ─────────────────────────────────────────────
@app.get("/run/candidates")
async def get_candidates():
    """스크리닝(필수 게이트) 통과 기사 목록 + 편집장 선정/탈락 표시.
    게이트 배제가 확정된 기사는 목록에 오르지 않는다 (사용자 지시)."""
    rd = run_dir_path()
    if not rd or not (rd / "candidates.json").exists():
        return JSONResponse({"error": "candidates.json 이 없습니다 — 1단계를 먼저 실행하세요"}, status_code=404)
    pool, sel_by_url, drop_reason, screened_missing = selection_pool(rd)
    articles = []
    for url, item in pool.items():
        row = dict(item)
        row["selected"] = url in sel_by_url
        row["dropped_reason"] = drop_reason.get(url)
        articles.append(row)
    # 편집장 선정 → 탈락 최종후보 → 나머지 순으로 정렬해 보여준다
    articles.sort(key=lambda a: (not a["selected"], a["dropped_reason"] is None, a.get("category", "")))
    return {"articles": articles, "editor_count": len(sel_by_url), "screened_missing": screened_missing}


@app.post("/run/selection")
async def post_selection(request: Request):
    """사람이 확정한 기사 선정을 selected.json 에 반영한다 (편집장 원안은 selected_editor.json 백업).
    선정 대기 상태에서만 허용 — 2단계 이후에는 카드 번호-기사 대응이 깨지므로 막는다."""
    if current["state"] != "waiting_selection":
        return JSONResponse({"error": "지금은 기사 선정 단계가 아닙니다"}, status_code=409)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "JSON 본문이 필요합니다"}, status_code=400)
    urls = payload.get("urls") if isinstance(payload, dict) else None
    if not isinstance(urls, list) or not urls:
        return JSONResponse({"error": "urls 배열이 필요합니다"}, status_code=400)
    # 중복 제거(순서 유지)
    seen: set = set()
    urls = [u for u in urls if not (u in seen or seen.add(u))]
    if not (1 <= len(urls) <= 10):
        return JSONResponse({"error": f"선정 기사는 1~10건이어야 합니다 (현재 {len(urls)}건)"}, status_code=400)

    rd = run_dir_path()
    pool, _, _, _ = selection_pool(rd)
    bad = [u for u in urls if u not in pool]
    if bad:
        return JSONResponse({"error": f"스크리닝 통과 목록에 없는 기사입니다: {bad[0]}"
                                      + (f" 외 {len(bad) - 1}건" if len(bad) > 1 else "")},
                            status_code=400)

    final = [pool[u] for u in urls]
    if (rd / "selected.json").exists() and not (rd / "selected_editor.json").exists():
        shutil.copy(rd / "selected.json", rd / "selected_editor.json")   # 편집장 원안 보존 (최초 1회)
    (rd / "selected.json").write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    logger = make_logger(rd, on_log)
    logger("orchestrator", f"사람이 기사 선정 확정: {len(final)}건 (편집장 원안은 selected_editor.json 에 보관)")
    return {"count": len(final)}


# ── 카드 검토·편집 (2단계 후 HITL) ───────────────────────────────────────
@app.get("/run/cards")
async def get_cards():
    """카드 목록 + 검토 로그(review.md) 본문."""
    rd = run_dir_path()
    if not rd:
        return {"cards": [], "review": None}
    cards = []
    for nn in list_card_numbers(rd):
        card = load_json(rd, f"card_{nn}.json", {}) or {}
        cards.append({
            "no": nn,
            "category": card.get("category", ""),
            "title": card.get("title", ""),
            "subhead": card.get("subhead", ""),
            "has_png": (rd / f"card_{nn}.png").exists(),
        })
    review = rd / "review.md"
    review_text = review.read_text(encoding="utf-8") if review.exists() else None
    return {"cards": cards, "review": review_text}


def _card_path(no: str) -> Path | None:
    rd = run_dir_path()
    if not rd or len(no) != 2 or not no.isdigit():
        return None
    return rd / f"card_{no}.json"


@app.get("/run/cards/{no}")
async def get_card(no: str):
    p = _card_path(no)
    if not p or not p.exists():
        return JSONResponse({"error": f"card_{no}.json 이 없습니다"}, status_code=404)
    card = load_json(p.parent, p.name, None)
    if card is None:
        return JSONResponse({"error": f"card_{no}.json 을 읽을 수 없습니다 (JSON 오류)"}, status_code=500)
    return card


@app.get("/run/cards/{no}/preview")
async def preview_card(no: str):
    """현재 card_NN.json 기준의 HTML 프리뷰 (iframe 용) — 항상 최신 내용으로 즉석 렌더."""
    p = _card_path(no)
    if not p or not p.exists():
        return HTMLResponse("<p>카드가 없습니다</p>", status_code=404)
    card = load_json(p.parent, p.name, None)
    if card is None:
        return HTMLResponse("<p>카드 JSON 을 읽을 수 없습니다</p>", status_code=500)
    return HTMLResponse(card_service.render_card_html(card))


@app.put("/run/cards/{no}")
async def put_card(no: str, request: Request):
    """사람이 수정한 카드 저장 (+ 즉시 자수 검사). 작가 원본은 card_NN_orig.json 으로 최초 1회 백업."""
    if current["state"] not in ("waiting_final_review", "done"):
        return JSONResponse({"error": "카드 수정은 카드 검토 대기(또는 완료) 상태에서만 가능합니다"}, status_code=409)
    p = _card_path(no)
    if not p or not p.exists():
        return JSONResponse({"error": f"card_{no}.json 이 없습니다"}, status_code=404)
    try:
        card = await request.json()
    except Exception:
        return JSONResponse({"error": "JSON 본문이 필요합니다"}, status_code=400)
    if not isinstance(card, dict) or not card.get("title") or not isinstance(card.get("points"), list):
        return JSONResponse({"error": "카드 형식이 아닙니다 (title, points 필수)"}, status_code=400)

    orig = p.parent / f"card_{no}_orig.json"
    if not orig.exists():
        shutil.copy(p, orig)   # 작가 원본 보존 (최초 사람 수정 시 1회)
    p.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
    problems = card_service.quick_validate(card)
    logger = make_logger(run_dir_path(), on_log)
    logger("orchestrator", f"사람이 card_{no} 수정 저장 (자수 검사 {'통과' if not problems else f'불합격 {len(problems)}건'})")
    return {"ok": not problems, "quick_problems": problems}


@app.post("/run/cards/{no}/check")
async def check_card(no: str):
    """종합 기계 검사 (자수 규격 + Edge 렌더 실측) — build_cardnews --check 와 동일 판정."""
    p = _card_path(no)
    if not p or not p.exists():
        return JSONResponse({"error": f"card_{no}.json 이 없습니다"}, status_code=404)
    problems = await asyncio.to_thread(card_service.full_check, p)
    return {"ok": not problems, "problems": problems}


# ── 인증 ─────────────────────────────────────────────────────────────────
@app.get("/auth/status")
async def get_auth_status():
    return auth_status()


@app.post("/auth/open-terminal")
async def open_login_terminal():
    """새 콘솔 창에서 claude CLI 를 띄운다 — 사용자가 그 창에서 /login 으로 로그인."""
    try:
        subprocess.Popen(["cmd.exe", "/k", "claude"],
                         creationflags=subprocess.CREATE_NEW_CONSOLE)
        return {"status": "opened",
                "message": "새 터미널이 열렸습니다. 그 창에서 /login 을 입력해 로그인하세요."}
    except OSError as e:
        return JSONResponse({"status": "error", "message": f"터미널을 열지 못했습니다: {e}"}, status_code=500)


# ── SSE ──────────────────────────────────────────────────────────────────
def _sse_format(ev: dict) -> str:
    return f"id: {ev['id']}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"


@app.get("/events")
async def events(request: Request, since: int | None = None):
    """실시간 이벤트 스트림. ?since=<id> 로 그 이후 이벤트를 링버퍼에서 재생한 뒤 라이브로 전환.
    브라우저 자동 재접속의 Last-Event-ID 헤더가 있으면 그것을 우선한다 (끊긴 지점부터)."""
    header = request.headers.get("last-event-id")
    last_id = int(header) if header and header.isdigit() else since

    async def stream():
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        subscribers.add(q)
        try:
            replayed = 0
            if last_id is not None:
                for ev in list(history):
                    if ev["id"] > last_id:
                        replayed = ev["id"]
                        yield _sse_format(ev)
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"    # 에이전트 침묵 구간의 연결 유지
                    continue
                if ev["id"] <= replayed:  # 재생 직후 큐에 겹쳐 들어온 이벤트 중복 방지
                    continue
                yield _sse_format(ev)
        finally:
            subscribers.discard(q)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})
