"""
박스 주문 제안 Agent 서버 (FastAPI) — Spring Boot 가 내부 HTTP 로 부르는 AI Serving 계층

레퍼런스 아키텍처의 ②③④⑦: Spring 이 작업을 접수(202)한 뒤 여기에 실행을 요청하고, 진행 이벤트를 SSE 로 받아
화면에 중계한다. 브라우저는 이 서버를 직접 부르지 않는다(X-Internal-Token, docker network 안에서만 연다).

  POST /internal/agent/tasks                     작업 실행 요청 → 202 {taskId, traceId}
  GET  /internal/agent/tasks/{taskId}            상태·결과 {status, result, error, traceId, harness}
  GET  /internal/agent/tasks/{taskId}/events     진행 이벤트 SSE (Last-Event-ID 로 이어 받기)
  POST /internal/agent/tasks/{taskId}/approval   승인·반려 {decision: APPROVED|REJECTED, comment} → 202, 같은 thread_id 로 재개
  POST /internal/agent/hq-reports                본부 통합 효율화 리포트 {month, note} → 202 {taskId} (조치가 있으면 승인 대기)
  GET  /internal/agent/health

이벤트: TOOL_START · TOOL_END · TOKEN(제안서 본문) · APPROVAL_REQUIRED(save_proposal 앞에서 멈춤) · DONE · FAILED
상태:   RUNNING → WAITING_APPROVAL → (승인) RUNNING → DONE / (반려) RUNNING → WAITING_APPROVAL … / FAILED
checkpointer(PostgreSQL, thread_id = taskId)에 상태가 남아 서버가 다시 떠도 승인 대기 작업을 재개할 수 있다.

실행: python agent_server.py [--host 0.0.0.0] [--port 8000]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from langgraph.types import Command
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

import box_spec_agent as agent
import hq_report_agent as hq
import harness

TERMINAL = {"DONE", "FAILED"}


class TaskCreate(BaseModel):
    warehouseId: str | None = Field(None, description="생략하면 센터가 1개일 때 자동 선택")
    targetMonth: str | None = Field(None, pattern=r"^\d{4}-\d{2}$", description="기본: 다음 달")
    jobId: str | None = None
    taskId: str | None = Field(None, description="Spring 작업 id 를 그대로 쓰면 같은 id 로 추적된다")


class HQTaskCreate(BaseModel):
    month: str | None = Field(None, pattern=r"^\d{4}-\d{2}$", description="리포트 대상 월. 기본: 지난달")
    note: str = Field("", description="리포트 상단 표시 문구")
    taskId: str | None = None


class Approval(BaseModel):
    decision: Literal["APPROVED", "REJECTED"]
    comment: str = ""


class Task:
    def __init__(self, task_id: str, job: dict, kind: str = "box"):
        self.id, self.job, self.kind = task_id, job, kind        # kind: box(박스 주문 제안) | hq(본부 리포트)
        self.status, self.result, self.error = "RUNNING", None, None
        self.events: list[dict] = []
        self.changed = asyncio.Condition()
        self.trace: dict | None = None
        self.runner: asyncio.Task | None = None

    def push(self, event: str, data: dict) -> None:
        self.events.append({"id": len(self.events) + 1, "event": event, "data": data})
        if event == "APPROVAL_REQUIRED":
            self.status = "WAITING_APPROVAL"
        elif event in TERMINAL:
            self.status = event
        asyncio.get_running_loop().create_task(self._notify())

    async def _notify(self):
        async with self.changed:
            self.changed.notify_all()

    def view(self) -> dict:
        return {"taskId": self.id, "status": self.status, "job": self.job, "result": self.result, "error": self.error,
                "traceId": self.trace and self.trace["traceId"], "events": len(self.events),
                "harness": self.trace and self._summary()}

    def _summary(self) -> dict:
        harness.resume_trace(self.trace)
        return harness.summary()


TASKS: dict[str, Task] = {}
STATE: dict = {}
SLOTS = asyncio.Semaphore(int(os.getenv("BOX_AGENT_MAX_CONCURRENT", "2")))


@asynccontextmanager
async def lifespan(_: FastAPI):
    agent.load_env()
    agent.log("env", agent.env_summary())
    async with agent.open_checkpointer() as saver, agent.MCPHub(agent.MCP_SERVERS) as hub:
        agent.HUB = hub
        STATE["graph"] = agent.build_graph(saver)
        STATE["hq_graph"] = hq.build_graph(saver)
        yield


app = FastAPI(title="box-agent", lifespan=lifespan)


def require_token(x_internal_token: str | None = Header(None)):
    expected = os.getenv("INTERNAL_API_TOKEN", "")
    if not expected or x_internal_token != expected:
        raise HTTPException(401, "X-Internal-Token 이 없거나 다름")


async def _run(task: Task, payload) -> None:
    """그래프 한 구간(다음 승인 대기 또는 끝까지)을 실행한다. 같은 작업은 같은 trace 로 이어 기록한다"""
    async with SLOTS:
        if task.trace is None:
            task.trace = harness.start_trace(task.id, emit=task.push, source="api", jobId=task.job["jobId"])
        else:
            harness.resume_trace(task.trace)
        task.status = "RUNNING"
        try:
            if task.kind == "hq":
                out = await hq.run_until_pause(STATE["hq_graph"], payload, hq.run_config(task.id, task.job))
                if "__interrupt__" not in out:
                    task.result = {"month": task.job["month"], "reportPaths": out["report"]["paths"],
                                   "actions": out.get("actions") or [],
                                   "recommendations": out.get("recommendations") or [], "approval": out.get("approval"),
                                   "applied": out.get("applied") or []}
                    harness.emit("DONE", **task.result, harness=harness.summary())
                return
            out = await agent.run_until_pause(STATE["graph"], payload, agent.run_config(task.id, task.job))
            if "__interrupt__" not in out:
                task.result = {"proposalId": task.job["proposalId"], "targetMonth": task.job["targetMonth"],
                               "totalAmount": out["order_plan"]["totals"]["amount"],
                               "totalQty": out["order_plan"]["totals"]["qty"],
                               "sources": [{"sourceId": k, **v} for k, v in (out.get("sources") or {}).items()],
                               "proposalMarkdown": out["proposal_md"]}
                task.result["langsmith"] = await asyncio.to_thread(harness.langsmith_links)
                harness.emit("DONE", **{k: v for k, v in task.result.items() if k != "proposalMarkdown"},
                             harness=harness.summary())
        except Exception as e:   # Recovery: 실패는 FAILED 이벤트 + trace 경로를 작업 이력에 남긴다
            task.error = f"{type(e).__name__}: {str(e)[:500]}"
            agent.log("agent_server", f"task {task.id} FAILED {task.error}")
            links = await asyncio.to_thread(harness.langsmith_links)   # 실패 작업은 trace 링크를 작업 이력에 남긴다
            harness.emit("FAILED", error=task.error, tracePath=harness.trace_path(), langsmith=links)


@app.get("/internal/agent/health")
async def health():
    return {"status": "UP", "tasks": len(TASKS), "mcp": list(agent.HUB.sessions) if agent.HUB else []}


@app.post("/internal/agent/tasks", status_code=202, dependencies=[Depends(require_token)])
async def create_task(body: TaskCreate):
    task_id = body.taskId or f"task-{uuid.uuid4().hex[:12]}"
    if task_id in TASKS:
        raise HTTPException(409, f"이미 있는 taskId: {task_id}")
    try:
        wh = await agent.pick_warehouse(agent.HUB, body.warehouseId)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    except RuntimeError as e:   # 업무 API(Spring) 장애
        raise HTTPException(503, str(e)[:300]) from None
    nm = datetime.now(agent.KST).date().replace(day=1)
    nm = (nm.replace(day=28) + timedelta(days=4)).replace(day=1)
    job = agent.make_job(wh, body.targetMonth or f"{nm.year:04d}-{nm.month:02d}", body.jobId)
    task = TASKS[task_id] = Task(task_id, job)
    task.runner = asyncio.create_task(_run(task, {"job": job, "messages": []}))
    await asyncio.sleep(0)            # trace 가 열리도록 한 번 양보
    return {"taskId": task_id, "traceId": task.trace and task.trace["traceId"], "job": job}


async def _restore(task_id: str) -> Task | None:
    """프로세스 재시작 뒤: checkpointer 에 승인 대기 상태가 남아 있으면 작업을 되살린다(박스 제안·본부 리포트)"""
    for kind, key in (("hq", "hq_graph"), ("box", "graph")) if task_id.startswith("hq-") else (("box", "graph"),):
        snap = await STATE[key].aget_state({"configurable": {"thread_id": task_id}})
        if snap and snap.values and "human_review" in (snap.next or ()):
            break
    else:
        return None
    task = TASKS[task_id] = Task(task_id, snap.values["job"], kind)
    task.status = "WAITING_APPROVAL"
    return task


@app.post("/internal/agent/hq-reports", status_code=202, dependencies=[Depends(require_token)])
async def create_hq_report(body: HQTaskCreate):
    """본부 통합 효율화 리포트 작업. 조치(센터 규칙 변경)가 있으면 승인 대기 → 같은 approval API 로 재개"""
    last = (datetime.now(agent.KST).date().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    job = hq.make_job(body.month or last, body.note)
    task_id = body.taskId or job["jobId"]
    if task_id in TASKS:
        raise HTTPException(409, f"이미 있는 taskId: {task_id}")
    task = TASKS[task_id] = Task(task_id, job, "hq")
    task.runner = asyncio.create_task(_run(task, {"job": job, "messages": []}))
    await asyncio.sleep(0)
    return {"taskId": task_id, "traceId": task.trace and task.trace["traceId"], "job": job}


@app.get("/internal/agent/tasks/{task_id}", dependencies=[Depends(require_token)])
async def get_task(task_id: str):
    task = TASKS.get(task_id) or await _restore(task_id)
    if not task:
        raise HTTPException(404, f"없는 작업: {task_id}")
    return task.view()


@app.post("/internal/agent/tasks/{task_id}/approval", status_code=202, dependencies=[Depends(require_token)])
async def approve(task_id: str, body: Approval):
    task = TASKS.get(task_id) or await _restore(task_id)
    if not task:
        raise HTTPException(404, f"없는 작업: {task_id}")
    if task.status != "WAITING_APPROVAL":
        raise HTTPException(409, f"승인 대기 상태가 아님: {task.status}")
    task.status = "RUNNING"
    task.runner = asyncio.create_task(_run(task, Command(resume={"status": body.decision, "comment": body.comment})))
    return {"taskId": task_id, "decision": body.decision}


@app.get("/internal/agent/tasks/{task_id}/events", dependencies=[Depends(require_token)])
async def events(task_id: str, request: Request, last_event_id: str | None = Header(None)):
    task = TASKS.get(task_id) or await _restore(task_id)
    if not task:
        raise HTTPException(404, f"없는 작업: {task_id}")
    start = int(last_event_id or request.query_params.get("after", 0))

    async def stream():
        sent = start
        while True:
            while sent < len(task.events):
                ev = task.events[sent]
                sent += 1
                yield {"id": str(ev["id"]), "event": ev["event"], "data": json.dumps(ev["data"], ensure_ascii=False,
                                                                                     default=str)}
                if ev["event"] in TERMINAL:
                    return
            if await request.is_disconnected():
                return
            async with task.changed:
                try:
                    await asyncio.wait_for(task.changed.wait(), 15)
                except asyncio.TimeoutError:
                    pass

    return EventSourceResponse(stream(), ping=15)


@app.exception_handler(HTTPException)
async def http_error(_, e: HTTPException):
    return JSONResponse({"error": {"status": e.status_code, "message": e.detail}}, status_code=e.status_code)


if __name__ == "__main__":
    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.getenv("AGENT_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.getenv("AGENT_PORT", "8000")))
    a = ap.parse_args()
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")
