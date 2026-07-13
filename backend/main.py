"""
FastAPI 서버 (관리 인터페이스 백엔드 — PROJECT_NOTES 2.6)
---------------------------------------------------------
역할: 대시보드(프론트)의 "발간" 트리거를 받아 파이프라인을 실행하고,
      진행 로그를 SSE(Server-Sent Events)로 브라우저에 실시간으로 흘려보낸다.

실행:  uvicorn backend.main:app --reload
접속:  http://localhost:8000

이 파일은 오케스트레이터를 '감싸는 껍데기'일 뿐, 실제 파이프라인 로직은
backend/orchestrator.py 에 있습니다. (같은 로직을 터미널/웹 두 입구에서 공유)
"""

import asyncio
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse

from backend.orchestrator import run_pipeline

app = FastAPI(title="AI TREND — 관리 대시보드")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = PROJECT_ROOT / "frontend" / "index.html"

# 데모용: 한 번에 하나의 run 만. 로그는 큐에 담아 SSE 로 흘려보낸다.
log_queue: "asyncio.Queue[str]" = asyncio.Queue()
running = False


def push_log(stage: str, message: str):
    """오케스트레이터가 부르는 로그 콜백 → 큐에 넣는다."""
    log_queue.put_nowait(f"{stage} | {message}")


async def _run():
    global running
    running = True
    try:
        result = await run_pipeline(on_log=push_log)
        log_queue.put_nowait(f"orchestrator | ✅ 완료 — 카드 {len(result['cards'])}건: {result['run_dir']}")
    except Exception as e:
        log_queue.put_nowait(f"orchestrator | ❌ 오류: {e}")
    finally:
        running = False
        log_queue.put_nowait("__END__")  # 스트림 종료 신호


@app.get("/")
def index():
    return FileResponse(INDEX_HTML)


@app.post("/run")
async def run():
    """발간 버튼 → 파이프라인을 백그라운드로 시작."""
    global running
    if running:
        return JSONResponse({"status": "already_running"}, status_code=409)
    asyncio.create_task(_run())
    return {"status": "started"}


@app.get("/events")
async def events():
    """브라우저가 EventSource 로 연결해 로그를 실시간 수신하는 SSE 엔드포인트."""
    async def stream():
        while True:
            line = await log_queue.get()
            if line == "__END__":
                yield "event: end\ndata: done\n\n"
                break
            yield f"data: {line}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")
