"""
박스 주문 제안 Agent 서버 (FastAPI) — Spring Boot 가 내부 HTTP 로 부르는 AI Serving 계층

레퍼런스 아키텍처의 ②③④⑦: Spring 이 작업을 접수(202)한 뒤 여기에 실행을 요청하고, 진행 이벤트를 SSE 로 받아
화면에 중계한다. 브라우저는 이 서버를 직접 부르지 않는다(X-Internal-Token, docker network 안에서만 연다).

  POST /internal/agent/tasks                     작업 실행 요청 → 202 {taskId, traceId}
  GET  /internal/agent/tasks/{taskId}            상태·결과 {status, result, error, traceId, harness}
  GET  /internal/agent/tasks/{taskId}/events     진행 이벤트 SSE (Last-Event-ID 로 이어 받기)
  POST /internal/agent/tasks/{taskId}/approval   승인·반려 {decision: APPROVED|REJECTED, comment} → 202, 같은 thread_id 로 재개
  POST /internal/agent/hq-reports                본부 통합 효율화 리포트 {month, note} → 202 {taskId} (조치가 있으면 승인 대기)
  GET  /internal/agent/hq-reports                만들어 둔 본부 리포트 월 목록
  GET  /internal/agent/hq-reports/{month}        그 월의 본부 리포트 {month, html}
  GET  /internal/agent/hq-reports/readiness?month=YYYY-MM   리포트 만들기 전 센터별 제안서 현황
  GET  /internal/agent/policies/{docId}          규정 문서 원문 틀(고치기용, {규칙 키} 자리 포함)
  PUT  /internal/agent/policies/{docId}          규정 문서 수정 {markdown} (전 센터 공통) → 검색 색인도 다시 만든다
  GET  /internal/agent/centers                   센터 목록 (지침서 화면)
  GET  /internal/agent/centers/{id}/guide        센터 지침서: 센터별로 조정할 수 있는 규칙(기본값·적용값) + 그 값으로 채운 규정 문서
  PATCH /internal/agent/centers/{id}/guide       규칙 조정 저장 {changes: {키: 값 | null(기본값으로)}} → 다음 제안부터 적용
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


class GuideUpdate(BaseModel):
    changes: dict = Field(..., description="{규칙 키: 값}. null 이면 센터 조정을 지우고 기본값으로")
    actor: str | None = Field(None, max_length=100, description="바꾼 사람(변경 이력)")


class PolicyUpdate(BaseModel):
    markdown: str = Field(..., min_length=10, max_length=50000)
    actor: str | None = Field(None, max_length=100, description="고친 사람(저장 이력)")


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
        self.loop = asyncio.get_running_loop()        # 동기 노드(evaluate 등)는 다른 스레드에서 돌며 이벤트를 보낸다

    def push(self, event: str, data: dict) -> None:
        self.events.append({"id": len(self.events) + 1, "event": event, "data": data})
        if event == "APPROVAL_REQUIRED":
            self.status = "WAITING_APPROVAL"
        elif event in TERMINAL:
            self.status = event
        self.loop.call_soon_threadsafe(lambda: self.loop.create_task(self._notify()))

    async def _notify(self):
        async with self.changed:
            self.changed.notify_all()

    def view(self) -> dict:
        out = {"taskId": self.id, "kind": self.kind, "status": self.status, "job": self.job, "result": self.result,
               "error": self.error, "traceId": self.trace and self.trace["traceId"], "events": len(self.events),
               "harness": self.trace and self._summary()}
        if self.kind == "hq" and self.status == "WAITING_APPROVAL":   # 본부 리포트: 승인할 조치(화면 승인 버튼용)
            out["pending"] = next((e["data"] for e in reversed(self.events) if e["event"] == "APPROVAL_REQUIRED"), None)
        return out

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
    # 진행 이벤트는 메모리에만 있어 사라졌다 → 화면이 초안을 다시 받도록 승인 대기 이벤트를 다시 넣는다
    task.push("APPROVAL_REQUIRED", (hq if kind == "hq" else agent).approval_payload(snap.values))
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


@app.get("/internal/agent/hq-reports", dependencies=[Depends(require_token)])
async def list_hq_reports():
    """저장한 월별 본부 리포트(DB box_agent.hq_report, 없으면 파일)"""
    return await agent.mcp_call("api", "business", "list_hq_reports")


@app.get("/internal/agent/hq-reports/readiness", dependencies=[Depends(require_token)])
async def hq_readiness(month: str):
    """본부 리포트는 승인된 박스 주문 제안의 효과를 모은다 → 센터마다 그 월까지 저장된 제안서가 있는지(대상 월 기준)"""
    if not (len(month) == 7 and month[4] == "-"):
        raise HTTPException(400, "month 는 YYYY-MM")
    whs = (await agent.mcp_call("api", "business", "list_warehouses"))["items"]
    centers = []
    for w in whs:
        past = (await agent.mcp_call("api", "business", "list_past_proposals", warehouse_id=w["warehouseId"], limit=24))["items"]
        upto = sorted({p["targetMonth"] for p in past if p["targetMonth"] <= month})
        centers.append({"warehouseId": w["warehouseId"], "name": w["name"], "proposalMonths": len(upto),
                        "thisMonth": month in upto, "latestMonth": upto[-1] if upto else None})
    ready = [c for c in centers if c["proposalMonths"]]
    return {"month": month, "total": len(centers), "readyCount": len(ready),
            "thisMonthCount": sum(c["thisMonth"] for c in centers), "centers": centers}


@app.get("/internal/agent/policies/{doc_id}", dependencies=[Depends(require_token)])
async def get_policy(doc_id: str):
    try:
        return await agent.mcp_call("api", "rag", "get_policy_template", doc_id=doc_id)
    except Exception as e:
        raise HTTPException(404, str(e).rsplit("get_policy_template: ", 1)[-1]) from e


@app.put("/internal/agent/policies/{doc_id}", dependencies=[Depends(require_token)])
async def update_policy(doc_id: str, body: PolicyUpdate):
    """규정 문서(전 센터 공통) 수정. 형식·{규칙 키} 검증은 rag 의 update_policy_document"""
    try:
        r = await agent.mcp_call("api", "rag", "update_policy_document", doc_id=doc_id, markdown=body.markdown,
                                 actor=body.actor)
    except Exception as e:
        raise HTTPException(400, str(e).rsplit("update_policy_document: ", 1)[-1]) from e
    return {**r, **await agent.mcp_call("api", "rag", "get_policy_template", doc_id=doc_id)}


@app.get("/internal/agent/hq-reports/{month}", dependencies=[Depends(require_token)])
async def get_hq_report(month: str):
    """그 월의 본부 리포트 {month, html, data, summary, recommendations}"""
    try:
        return await agent.mcp_call("api", "business", "get_hq_report", month=month)
    except Exception as e:
        raise HTTPException(404, f"{month} 본부 리포트가 없음") from e


@app.get("/internal/agent/centers", dependencies=[Depends(require_token)])
async def list_centers():
    return await agent.mcp_call("api", "business", "list_warehouses")


async def _guide(warehouse_id: str) -> dict:
    """센터 지침서 = 센터별로 조정할 수 있는 규칙 + 그 센터 값으로 채운 규정 문서(rag policy://{docId}/center/{id})"""
    r = await agent.mcp_call("api", "business", "get_policy_rules", warehouse_id=warehouse_id)
    idx = json.loads(await agent.HUB.read_resource("rag", "policy://index"))
    docs = [{"docId": d["docId"], "title": d["title"],
             "markdown": await agent.HUB.read_resource("rag", f"policy://{d['docId']}/center/{warehouse_id}")}
            for d in idx["items"]]
    hist = (await agent.mcp_call("api", "business", "center_rule_history", warehouse_id=warehouse_id, limit=30))["items"]
    return {"warehouseId": warehouse_id, "version": r["version"], "updatedAt": r["updatedAt"],
            "rules": [x for x in r["rows"] if x["scope"] == "center"], "rejected": r["rejected"], "documents": docs,
            "history": hist}


@app.get("/internal/agent/centers/{warehouse_id}/guide", dependencies=[Depends(require_token)])
async def get_guide(warehouse_id: str):
    return await _guide(warehouse_id)


@app.patch("/internal/agent/centers/{warehouse_id}/guide", dependencies=[Depends(require_token)])
async def update_guide(warehouse_id: str, body: GuideUpdate):
    """센터 규칙 조정 저장(형·범위·조정 가능 여부는 규칙 파일로 검증). 다음 박스 주문 제안부터 적용된다"""
    try:
        await agent.mcp_call("api", "business", "set_center_overrides", warehouse_id=warehouse_id, changes=body.changes,
                             actor=body.actor, reason="지침서 화면에서 조정")
    except Exception as e:                  # 규칙 파일 검증 실패: 'MCP 도구 실패: … set_center_overrides: <이유>' 에서 이유만
        raise HTTPException(400, str(e).rsplit("set_center_overrides: ", 1)[-1]) from e
    return await _guide(warehouse_id)


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
