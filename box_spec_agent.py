"""
박스 주문 제안 에이전트 (LangGraph + MCP)

임무: 다음 달 박스 주문 제안서를 회사 양식대로 작성한다.
      이때 상품 치수 대비 박스가 안 맞는 구간을 찾아, 효용 기준을 넘는 신규 규격은 주문에 포함하고
      그 규격이 대체하는 기존 박스의 소모 예측은 줄여서 반영한다.

MCP 서버 (BUSINESS_MCP_URL·RAG_MCP_URL 이 있으면 Streamable HTTP, 없으면 stdio 하위 프로세스)
    business : 업무 데이터 조회(Spring /internal/v1/**)·박스왕 카탈로그·충진/시뮬레이션/주문 계산·수요 예측·
               제안서 양식·제안 저장(save_proposal, destructiveHint — 승인 뒤에만)
    rag      : search_policies, search_feedback_cases, add_feedback_case + Resource policy://, feedback://

계산 단계(③~⑦)는 순서가 고정이다(숫자는 MCP 계산 도구만 만든다). 모델이 도구를 고르는 곳은 두 군데:
    ⑧ write_proposal   cite_policies — '확인 필요' 항목마다 관련 규정 조항을 rag 도구로 찾아 인용
    ⑩ interpret_feedback — 반려 의견을 rag 도구(과거 반려 기록·규정)로 해석해 기준값 변경으로 바꿈
문장 다듬기는 llm_writer. 모델 호출의 한도·재시도·기록·마스킹은 harness.py, 지시문은 prompts/*.md.

실행: python box_spec_agent.py [--warehouse-id UUID] [--target-month 2026-11] [--auto-approve] [--no-llm]
      (서버로 띄우려면 agent_server.py)
"""
from __future__ import annotations

import argparse
import asyncio
import calendar
import json
import operator
import os
import re
import sys
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

import harness
import llm_writer

KST = timezone(timedelta(hours=9))
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "mcp_servers"))
import policy_rules  # noqa: E402  반려로 바꿀 수 있는 규칙 목록·검증(실제 적용값은 get_policy_rules 로 받는다)
MCP_SERVERS = {   # 이름: (stdio 스크립트, HTTP 주소 환경변수)
    "business": (HERE / "mcp_servers" / "business_mcp.py", "BUSINESS_MCP_URL"),
    "rag": (HERE / "mcp_servers" / "rag_mcp.py", "RAG_MCP_URL"),
}


def log(node: str, msg: str) -> None:
    print(f"[{node:<14}] {msg}", flush=True)
    harness.record("LOG", node=node, msg=msg)


# ---------------------------------------------------------------------------
# MCP 클라이언트
# ---------------------------------------------------------------------------
class MCPHub:
    def __init__(self, servers: dict[str, tuple[Path, str]]):
        self.servers = servers
        self.sessions: dict[str, ClientSession] = {}
        self.tools: dict[str, dict] = {}            # server → {tool name: Tool 정의(설명·스키마·annotations)}
        self.calls = 0

    async def __aenter__(self):
        self._stack = AsyncExitStack()
        for name, (script, url_env) in self.servers.items():
            if url := os.getenv(url_env):           # Streamable HTTP (compose: http://business-mcp:9101/mcp)
                read, write, _ = await self._stack.enter_async_context(streamablehttp_client(url))
                how = url
            else:
                # stdio 는 부모 환경변수를 물려받지 않는다(mcp 기본은 HOME/PATH 등만) → 명시적으로 넘긴다
                params = StdioServerParameters(command=sys.executable, args=[str(script)], env=dict(os.environ))
                read, write = await self._stack.enter_async_context(stdio_client(params))
                how = "stdio"
            session = await self._stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            tools = (await session.list_tools()).tools
            self.sessions[name] = session
            self.tools[name] = {t.name: t for t in tools}
            log("mcp", f"connected {name} ({how}): {len(tools)} tools")
        return self

    async def __aexit__(self, *exc):
        await self._stack.aclose()

    async def call(self, server: str, tool: str, **args) -> dict:
        self.calls += 1
        harness.count_tool_call()
        res = await self.sessions[server].call_tool(tool, args)
        if res.isError:
            raise RuntimeError(f"{server}.{tool} 실패: {res.content[0].text}")
        return json.loads(res.content[0].text)

    async def read_resource(self, server: str, uri: str) -> str:
        res = await self.sessions[server].read_resource(uri)
        return "\n".join(getattr(c, "text", "") for c in res.contents)


HUB: MCPHub | None = None


async def mcp_call(node: str, server: str, tool: str, **args) -> dict:
    log(node, f"→ MCP {server}.{tool}")
    harness.emit("TOOL_START", node=node, server=server, tool=tool)
    t0 = datetime.now()
    try:
        if harness.langsmith_enabled():   # LangSmith 에 tool run 으로 (입력·출력은 잘라서)
            from langsmith.run_helpers import trace
            with trace(f"{server}.{tool}", run_type="tool", inputs={"args": harness.clip(args, 2000)}) as rt:
                out = await HUB.call(server, tool, **args)
                rt.end(outputs={"result": harness.clip(out, 2000)})
        else:
            out = await HUB.call(server, tool, **args)
    except Exception as e:
        harness.emit("TOOL_END", node=node, server=server, tool=tool, ok=False, error=str(e)[:300])
        raise
    harness.emit("TOOL_END", node=node, server=server, tool=tool, ok=True,
                 ms=int((datetime.now() - t0).total_seconds() * 1000))
    return out


def rag_tools(names: list[str]) -> list[dict]:
    """모델에게 줄 도구 목록: rag 의 조회 도구 + read_resource(가상 도구). 변경 도구(destructiveHint)는 넣지 않는다.
    BOX_AGENT_RAG=0 이면 도구 없이(평가에서 RAG 효과 비교용)"""
    if os.getenv("BOX_AGENT_RAG") == "0":
        return []
    specs = [harness.mcp_tool_spec(t) for n_, t in HUB.tools["rag"].items()
             if n_ in names and not (t.annotations and t.annotations.destructiveHint)]
    return specs + [harness.READ_RESOURCE_SPEC]


def rag_executor(node: str, names: list[str], warehouse_id: str, found: dict):
    """모델이 고른 도구를 실행한다. 검색 결과의 sourceId 를 found 에 모아 인용 대조(Guardrail)에 쓴다"""
    async def run(name: str, args: dict):
        if name == "read_resource":
            uri = str(args.get("uri", ""))
            if not uri.startswith(("policy://", "feedback://")):
                raise ValueError("policy:// 또는 feedback:// 주소만 읽을 수 있음")
            harness.emit("TOOL_START", node=node, server="rag", tool="read_resource", uri=uri)
            if uri.startswith("policy://") and "/" not in uri.removeprefix("policy://"):
                uri += f"/center/{warehouse_id}"          # 규정 원문도 이 센터 규칙값으로 채운 것을 읽는다
            text = await HUB.read_resource("rag", uri)
            harness.emit("TOOL_END", node=node, server="rag", tool="read_resource", ok=True)
            return text
        if name not in names:
            raise ValueError(f"쓸 수 없는 도구: {name}. 쓸 수 있는 도구: {names}")
        if name in ("search_feedback_cases", "search_policies"):      # 반려 기록은 이 센터 것, 규정 숫자는 이 센터 규칙값
            args = {**args, "warehouse_id": warehouse_id}
        res = await mcp_call(node, "rag", name, **args)
        for it in res["items"]:
            doc = it["sourceId"].split(" ")[0]
            found[it["sourceId"]] = {"title": it["title"], "kind": "policy" if doc.startswith("POL-") else "feedback_case",
                                     "uri": f"policy://{doc}" if doc.startswith("POL-") else f"feedback://{doc}"}
        return {"items": [{k: it[k] for k in ("sourceId", "title", "content", "score")} for it in res["items"]]}
    return run


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
class AgentState(TypedDict, total=False):
    messages: Annotated[list, operator.add]   # 노드 실행 이력
    job: dict                                 # warehouseId, targetMonth, jobId
    config: dict                              # 게이트 기준, 안전재고 일수, 반올림 단위, 최대 루프
    data_meta: dict                           # inputAsOf, pendingEventCount, 분석 기간
    box_specs: list                           # 보유 규격
    inventory: list                           # 규격별 현재 재고
    shipment_history: list                    # 월별 출고 건수
    purchase_orders: list                     # 전월·전년 동월 발주
    inbound_orders: list                      # 아직 입고되지 않은 발주(승인·배송 중) 규격별 수량
    forecast: dict                            # 다음 달 출고 예측·정확도
    product_fits: list                        # 상품별 치수·출고량·배정박스·충진율·원인
    unfit_products: list                      # 보유 규격 어디에도 안 들어가 포장 대기한 상품(서버 NO_AVAILABLE_BOX)
    tilted_products: list                     # 기울여야 들어가는 상품(검토 대상)
    stockout_products: list                   # 맞는 박스가 품절이라 포장 대기한 상품(서버 NO_AVAILABLE_BOX)
    custom_unfit: dict | None                 # 안 들어가는 상품용 맞춤 제작 규격(유팩 참고 견적)
    custom_notes: list                        # 맞춤 견적 실패·불가 메모('확인 필요'에 적음)
    spec_limit: dict                          # 활성 규격 상한 점검(POL-02 §3): 상한 때문에 뺀 신규 후보, 규격별 사용 비중
    order_quotes: dict                        # 맞춤 규격 2차 견적(제안 수량 기준) {code: quote}
    diagnosis: dict                           # 원인별 출고 비중
    clusters: list                            # '애매한 WDH' 군집
    candidates: list                          # 신규 규격 후보
    sim_results: list                         # 후보별 효과·게이트 판정·선정
    sim_combined: dict | None                 # 선정 규격 모두 도입 시 전체 충진율·월 절감·적재량 전/후
    recent_effect: dict | None                # 새 선정이 없을 때: 지난 제안으로 등록된 규격의 효과(없었을 때 대비)
    custom_effect: dict | None                # 안 들어가는 상품용 맞춤 규격으로 담을 때의 월 적재량
    order_plan: dict                          # 규격별 제안 수량·금액
    template: dict                            # 회사 양식
    proposal_md: str                          # 제안서 본문 (회사 양식 markdown, 검토·평가 대상)
    proposal_html: str                        # 보기용 HTML 제안서 (templates/box_order_proposal.html)
    proposal_html_data: dict                  # 그 HTML 의 입력(양식만 바뀌면 render_proposal_html 로 다시 그린다)
    client_view: dict                         # client BoxOrderView(mocks/orders.js proposal) 모양
    review: dict                              # score, feedback, loop_count
    approval: dict                            # status, comment
    past_proposals: list                      # 같은 센터 과거 제안(이번 jobId 제외)
    rules: dict                               # 적용 규칙 버전·센터 덮어쓰기·규칙별 값(제안 summary 에 남김)
    llm: dict                                 # 문장 작성에 LLM을 썼는지, facts 숫자
    sources: dict                             # 이번 작업에서 rag 도구로 실제 찾은 근거 {sourceId: {title, uri}}
    citations: dict                           # ⑧ 규정 인용 캐시 {key, checks}
    rejections: list                          # ⑩ 반려 의견과 해석(기준값 변경·미지원·근거)


# ---------------------------------------------------------------------------
# 유틸
# ---------------------------------------------------------------------------
def n(v) -> str:
    return f"{v:,}"


def kr(v: float) -> str:
    """화면 문구용 큰 수: 1.2억 / 41만 / 3,500"""
    return f"{v / 1e8:,.1f}억" if abs(v) >= 1e8 else f"{v / 1e4:,.0f}만" if abs(v) >= 1e4 else n(round(v))


STEP_ORDER = ["load_context", "collect_data", "forecast_demand", "analyze_fit", "diagnose", "find_clusters",
              "gen_candidates", "simulate", "plan_order", "write_proposal", "evaluate"]


def progress(node: str, text: str) -> None:
    """화면 진행 문구(PROGRESS): 단계가 끝날 때 결과를 한 문장으로. 내부 용어·코드 대신 담당자가 읽는 말로 쓴다"""
    step = STEP_ORDER.index(node) + 1 if node in STEP_ORDER else None
    harness.emit("PROGRESS", node=node, text=text, step=step, total=len(STEP_ORDER))


def ym_label(ym: str) -> str:
    y, m = ym.split("-")
    return f"{y}년 {int(m)}월"


def prev_ym(ym: str, k: int) -> str:
    y, m = map(int, ym.split("-"))
    m -= k
    while m <= 0:
        y, m = y - 1, m + 12
    return f"{y:04d}-{m:02d}"


def dims_s(d) -> str:
    return "×".join(map(str, d))


def month_end(ym: str) -> str:
    y, m = map(int, ym.split("-"))
    return f"{ym}-{calendar.monthrange(y, m)[1]:02d}"


def won(v: int) -> str:
    return f"₩{v:,}"


def load_env_files(*paths: Path) -> list[str]:
    """KEY=VALUE 형식 .env 를 읽어 아직 없는 환경변수만 채운다(이미 설정된 값이 우선)."""
    loaded = []
    for path in paths:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        loaded.append(str(path))
    return loaded


def _vol(d):
    return d[0] * d[1] * d[2]


def cats(c: dict, k: int = 2) -> str:
    """군집 분류 표시: 출고량 상위 k개 + 나머지 개수"""
    top = c["categories"][:k]
    return ", ".join(top) + (f" 외 {len(c['categories']) - k}개 분류" if len(c["categories"]) > k else "")


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------
def default_config(job: dict, rule_values: dict) -> dict:
    """판단 숫자는 규칙(get_policy_rules)에서만 온다. 여기 남은 값은 실행 제어용(규정 아님)"""
    cfg = {"allow_tilt": False, "tilt_clearance_mm": 0, "max_loops": 3, "eval_pass": 80,
           "demo_eval_loop": job.get("demoEvalLoop", False)}
    for k, v in rule_values.items():
        cfg = _cfg_set(cfg, k, v)
    return cfg


async def load_context(state: AgentState):
    job = state["job"]
    rules = await mcp_call("load_context", "business", "get_policy_rules", warehouse_id=job["warehouseId"])
    cfg = default_config(job, rules["values"])
    log("load_context", f"판단 규칙 v{rules['version']} ({rules['updatedAt']})"
                        + (f", 센터 덮어쓰기 {rules['overrides']}" if rules["overrides"] else "")
                        + (f", 버린 덮어쓰기 {rules['rejected']}" if rules["rejected"] else ""))
    log("load_context", f"job={job['jobId']} warehouse={job['warehouseId']} ({job.get('warehouseName', '')}) "
                        f"targetMonth={job['targetMonth']}")
    pol = await mcp_call("load_context", "business", "get_matching_policy")   # 서버 상품 매칭과 같은 판정 기준
    cfg.update(allow_tilt=pol["tiltedEnabled"], tilt_clearance_mm=pol["tiltedClearanceMm"])
    log("load_context", f"박스 수용 판정: 서버 정책 기울임 {'켜기' if cfg['allow_tilt'] else '끄기'}, "
                        f"여유 {cfg['margin_mm']}mm / 기울임 여유 {cfg['tilt_clearance_mm']}mm [{pol['source']}]")
    past = await mcp_call("load_context", "business", "list_past_proposals", warehouse_id=job["warehouseId"], limit=12)
    past = [x for x in past["items"] if x.get("jobId") != job["jobId"]]
    log("load_context", f"과거 제안 {len(past)}건 (에이전트 {sum(x['source'] == 'box-agent' for x in past)}건, "
                        f"신규 규격 제안 {sum(len(x['newSpecs']) for x in past)}건)")
    progress("load_context", f"{job.get('warehouseName') or '센터'}의 판단 규칙과 지난 제안 {len(past)}건을 확인했어요")
    return {"config": cfg, "rules": {k: rules[k] for k in ("version", "updatedAt", "overrides", "rows")},
            "review": {"score": 0, "feedback": [], "loop_count": 0}, "past_proposals": past,
            "approval": {"status": "PENDING"}, "messages": ["load_context"]}


async def collect_data(state: AgentState):
    job, cfg = state["job"], state["config"]
    wh, tm = job["warehouseId"], job["targetMonth"]
    last_m = prev_ym(tm, 1)
    to_d = datetime.now(KST).date() - timedelta(days=1)                 # 어제까지
    from_d, basis = to_d - timedelta(days=cfg["lookback_days"] - 1), f"최근 {cfg['lookback_days']}일"
    if cfg["lookback_since_change"]:     # 박스·충진율 분석은 마지막 신규 규격 입고(지난 제안 대상 월 1일) 이후만
        changed = [x["targetMonth"] for x in state.get("past_proposals", [])
                   if x.get("newSpecs") and f"{x['targetMonth']}-01" <= to_d.isoformat()]
        since = date.fromisoformat(max(changed) + "-01") if changed else None
        if since and since > from_d and (to_d - since).days >= 6:
            from_d, basis = since, f"박스 구성 변경({since}) 이후"
    specs = await mcp_call("collect_data", "business", "list_box_specs", warehouse_id=wh)
    prods = await mcp_call("collect_data", "business", "list_product_fits", warehouse_id=wh,
                           from_date=from_d.isoformat(), to_date=to_d.isoformat())
    inv = await mcp_call("collect_data", "business", "get_box_inventory", warehouse_id=wh)
    hist = await mcp_call("collect_data", "business", "get_shipment_history", warehouse_id=wh, end_month=last_m)
    pos = await mcp_call("collect_data", "business", "list_purchase_orders", warehouse_id=wh,
                         from_date=f"{prev_ym(tm, 14)}-01", to_date=month_end(last_m))
    m = prods["meta"]
    meta = {"inputAsOf": m["dataAsOf"], "pendingEventCount": m.get("pendingEventCount", 0),
            "source": m.get("source"), "from": from_d.isoformat(), "to": to_d.isoformat()}
    log("collect_data", f"[{meta['source']}] 규격 {len(specs['items'])} 상품 {len(prods['items'])} "
                        f"재고 {len(inv['items'])}규격 출고이력 {len(hist['items'])}개월 발주이력 {len(pos['items'])}건 "
                        f"(박스·충진율 분석 {basis}: {meta['from']}~{meta['to']}, 출고량 예측은 월별 이력)")
    if specs.get("skipped"):
        log("collect_data", f"치수·단가 미입력으로 제외한 규격: {specs['skipped']}")
    if any(prods.get("excluded", {}).values()):
        log("collect_data", f"제외 상품: 치수 없음 {prods['excluded']['noDims']}, 기간 내 출고 없음 {prods['excluded']['noShipments']}")
    for h in hist["items"]:
        if h.get("partial"):
            log("collect_data", f"{h['month']}은 진행 중({h['daysCovered']}일 경과): 실제 {n(h['actual'])}건 → "
                                f"한 달 환산 {n(h['shipments'])}건으로 예측에 사용")
    if m.get("eventStatuses"):
        log("collect_data", f"item_event status별 건수 {m['eventStatuses']} (집계 대상: {m.get('countedStatuses')})")
    if not specs["items"]:
        raise RuntimeError("사용 가능한 박스 규격이 없음 (box_spec 치수·단가·is_active 확인)")
    if not prods["items"]:
        raise RuntimeError(f"분석 기간 {meta['from']}~{meta['to']} 출고가 있는 상품이 없음 (product 치수, item_event 확인)")
    progress("collect_data", f"{basis} 출고 이력에서 상품 {len(prods['items'])}종, 박스 규격 {len(specs['items'])}종을 불러왔어요")
    return {"box_specs": specs["items"], "product_fits": prods["items"], "inventory": inv["items"],
            "shipment_history": hist["items"], "purchase_orders": pos["items"], "data_meta": meta,
            "inbound_orders": (await mcp_call("collect_data", "business", "list_inbound_orders", warehouse_id=wh))["items"],
            "messages": ["collect_data"]}


async def forecast_demand(state: AgentState):
    r = await mcp_call("forecast_demand", "business", "forecast_shipments",
                       history=state["shipment_history"], target_month=state["job"]["targetMonth"],
                       seasonal=state["config"]["forecast_seasonal"])
    if not r["forecast"]:
        raise RuntimeError("월별 출고 이력이 없어 수요 예측 불가 (item_event 확인)")
    ly = n(r["sameMonthLastYear"]) + "건" if r["sameMonthLastYear"] else "없음"
    log("forecast_demand", f"{r['targetMonth']} 예상 출고 {n(r['forecast'])}건 (전년 동월 {ly}) "
                           f"정확도 {str(r['accuracy_pct']) + '%' if r['accuracy_pct'] is not None else '미산출'} [{r['method']}]")
    yoy = (f" (전년 동월 대비 {(r['forecast'] / r['sameMonthLastYear'] - 1) * 100:+.0f}%)"
           if r["sameMonthLastYear"] else "")
    progress("forecast_demand", f"{int(r['targetMonth'][5:])}월 출고는 약 {kr(r['forecast'])}건으로 예상돼요{yoy}")
    return {"forecast": r, "messages": ["forecast_demand"]}


async def analyze_fit(state: AgentState):
    cfg = state["config"]
    r = await mcp_call("analyze_fit", "business", "analyze_fill_rate",
                       products=state["product_fits"], box_specs=state["box_specs"],
                       margin_mm=cfg["margin_mm"], low_fill_pct=cfg["low_fill_pct"], allow_tilt=cfg["allow_tilt"],
                       tilt_clearance_mm=cfg["tilt_clearance_mm"])
    log("analyze_fit", f"전체 충진율 {r['overall_fill']}% | 저충진 SKU {r['low_fill_skus']}개, "
                       f"출고 비중 {r['low_fill_qty_share']}%")
    progress("analyze_fit", f"지금 전체 충진율은 {r['overall_fill']}%예요. 상자에 빈 공간이 많은 상품이 {r['low_fill_skus']}종 있어요")
    if r["unfit"]:
        progress("analyze_fit", f"맞는 박스가 없어 포장을 기다린 상품이 {len(r['unfit'])}종 있어요")
    if r["unfit"]:
        log("analyze_fit", f"보유 규격에 안 들어가 포장 대기한 상품 {len(r['unfit'])}개 제외: "
                           f"{[u['code'] or u['productId'] for u in r['unfit']][:10]}")
    if r["stockout"]:
        log("analyze_fit", f"맞는 박스가 품절이라 포장 대기한 상품 {len(r['stockout'])}개: "
                           f"{[(u['code'] or u['productId'], u['box']) for u in r['stockout']][:10]}")
    if r["inactive"]:
        log("analyze_fit", f"비활성 규격으로 나간 출고 {sum(u['qty90d'] for u in r['inactive'])}건 제외: "
                           f"{sorted({u['box'] for u in r['inactive']})}")
    if r["tilted"]:
        log("analyze_fit", f"기울여야 들어가는 상품 {len(r['tilted'])}개(검토 대상): "
                           f"{[(t['code'] or t['productId'], t['box']) for t in r['tilted']][:10]}")
    return {"product_fits": r["products"], "unfit_products": r["unfit"], "tilted_products": r["tilted"],
            "stockout_products": r["stockout"], "messages": ["analyze_fit"]}


async def diagnose(state: AgentState):
    cfg = state["config"]
    r = await mcp_call("diagnose", "business", "diagnose_inefficiency",
                       products=state["product_fits"], box_specs=state["box_specs"],
                       margin_mm=cfg["margin_mm"], low_fill_pct=cfg["low_fill_pct"])
    log("diagnose", f"원인별 출고 비중 {r['share_pct']}")
    causes = {"SPEC_GAP": "맞는 크기 규격이 없어 큰 박스를 쓴", "SHAPE_MISMATCH": "모양이 맞지 않는 박스를 쓴"}
    bad = {k: v for k, v in r["share_pct"].items() if k in causes}
    if bad and sum(bad.values()):
        top = max(bad, key=bad.get)
        progress("diagnose", f"빈 공간 출고의 {100 * bad[top] / sum(bad.values()):.0f}%가 {causes[top]} 경우예요")
    return {"product_fits": r["products"], "diagnosis": r["share_pct"], "messages": ["diagnose"]}


async def find_clusters(state: AgentState):
    """군집(형태가 비슷한 저충진 묶음, 2~max_clusters 개) + 단일 품목(혼자 튀는 저충진 고출고 상품)을 함께 후보 구간으로 본다"""
    cfg = state["config"]
    r = await mcp_call("find_clusters", "business", "find_misfit_clusters",
                       products=state["product_fits"], max_clusters=cfg["max_clusters"])
    sku = await mcp_call("find_clusters", "business", "find_sku_candidates", products=state["product_fits"],
                         max_fill_pct=cfg["sku_max_fill_pct"], min_share_pct=cfg["sku_min_share_pct"], limit=cfg["sku_limit"])
    fill = {"clusters": []}
    reg = [b["code"] for b in state["box_specs"] if sum(b["dims"]) > 500]
    if cfg["fill_mode"] and cfg["fill_sku_limit"]:   # ESG 우선 모드: 규제 대상 박스의 저충진 상품 전용 후보(POL-02 §7)
        seen = {pid for c in sku["clusters"] for pid in c["productIds"]}
        fill = await mcp_call("find_clusters", "business", "find_sku_candidates", products=state["product_fits"],
                              max_fill_pct=cfg["fill_target_pct"], min_share_pct=0, limit=cfg["fill_sku_limit"] + len(seen),
                              regulated_boxes=reg, id_prefix="F")
        fill["clusters"] = [c for c in fill["clusters"] if c["productIds"][0] not in seen][:cfg["fill_sku_limit"]]
    sku["clusters"] = [{**c, "fill_target": cfg["fill_mode"] and c["from_box"] in reg} for c in sku["clusters"]] + [
        {**c, "fill_target": True} for c in fill["clusters"]]
    log("find_clusters", f"군집 {r['k']}개 (가중 실루엣 {r.get('silhouette')}) + 단일 품목 {len(sku['clusters'])}개"
                         + (f" (충진율 목표 상품 {len(fill['clusters'])}개)" if fill["clusters"] else ""))
    for c in r["clusters"] + sku["clusters"]:
        log("find_clusters", f"{c['clusterId']} {c.get('name') or cats(c)} SKU {c['skus']}개 비중 {c['share_pct']}% "
                             f"P50={c['p50']} 현재 {c['from_box']} 충진율 {c['avg_fill']}%")
    progress("find_clusters", f"크기가 비슷한 상품을 {r['k']}개 묶음으로 나눴어요"
                              + (f" (따로 볼 상품 {len(sku['clusters'])}종)" if sku["clusters"] else ""))
    return {"clusters": r["clusters"] + sku["clusters"], "messages": ["find_clusters"]}


async def gen_candidates(state: AgentState):
    cfg, m = state["config"], state["config"]["margin_mm"]
    specs = {b["code"]: b for b in state["box_specs"]}
    cands = []
    notes: list[str] = []
    own = {tuple(sorted(b["dims"], reverse=True)) for b in state["box_specs"]}
    if cfg["no_new_specs"]:
        log("gen_candidates", "운영자 피드백 반영: 신규 규격 없이 기존 규격만 보충")
        return {"candidates": [], "custom_unfit": None, "custom_notes": [], "messages": ["gen_candidates"]}
    for c in state["clusters"]:
        prods = [p for p in state["product_fits"] if (p.get("pid") or p["productId"]) in c["productIds"]]
        ideal = (await mcp_call("gen_candidates", "business", "ideal_box_dims",
                                p90_dims=c["p90"], margin_mm=m))["dims"]
        hits = await mcp_call(
            "gen_candidates", "business", "search_catalog",
            min_inner_dims=[v + m for v in c["p50"]],
            # 단일 품목은 납작·길쭉한 상품이 많아 이상 치수의 2배 상한이 너무 좁다 → 지금 박스보다 작기만 하면 본다
            max_volume_mm3=_vol(specs[c["from_box"]]["dims"]) - 1 if c.get("scope") == "SKU"
            else min(_vol(specs[c["from_box"]]["dims"]) - 1, cfg["cluster_vol_cap_x"] * _vol(ideal)), limit=50,
            exclude_refs=[b["catalogRef"] for b in state["box_specs"] if b.get("catalogRef")])
        if hits.get("note"):
            log("gen_candidates", f"{c['clusterId']} 기성품: {hits['note']}")
        for it in hits["items"]:
            if tuple(sorted(it["dims"], reverse=True)) in own:
                continue
            cov = (await mcp_call("gen_candidates", "business", "coverage",
                                  products=prods, box_dims=it["dims"], margin_mm=m,
                                  allow_tilt=cfg["allow_tilt"],
                                  tilt_clearance_mm=cfg["tilt_clearance_mm"]))["coverage_pct"]
            if cov >= cfg["coverage_pct"]:
                cands.append({"candId": f"{c['clusterId']}-CAT", "cluster": c["clusterId"], "type": "CATALOG",
                              "scope": c.get("scope", "CLUSTER"), "productIds": c["productIds"],
                              "code": f"N-{it['name']}", "name": it["name"], "dims": it["dims"],
                              "price": it["price"], "moq": it["moq"], "ref": f"{it['supplier']}:{it['goodsNo']}",
                              "coverage": cov, "init_cost": 0})
                break
        # 충진율 목표 상품은 기성품이 있어도 상품 치수에 맞춘 맞춤 후보를 함께 본다(가는·작은 상품은 규제 대상에서 빠짐)
        if not cfg["no_custom"] and (c.get("fill_target") or not any(x["cluster"] == c["clusterId"] for x in cands)):
            q = await _quote_custom("gen_candidates", ideal, sum(p["qty90d"] for p in prods))
            if not q["ok"]:
                notes.append(f"{c['clusterId']} {c.get('name') or cats(c)} 구간: 기성품 후보 없음, {q['note']}")
                continue
            ideal = q["dims"]                    # 최소 제작 크기로 올린 치수
            cov = (await mcp_call("gen_candidates", "business", "coverage", products=prods, box_dims=ideal,
                                  margin_mm=m, allow_tilt=cfg["allow_tilt"],
                                  tilt_clearance_mm=cfg["tilt_clearance_mm"]))["coverage_pct"]
            cands.append({"candId": f"{c['clusterId']}-CUS", "cluster": c["clusterId"], "type": "CUSTOM",
                          "scope": c.get("scope", "CLUSTER"), "productIds": c["productIds"],
                          "code": f"N-맞춤 {dims_s(ideal)}", "name": f"맞춤 {dims_s(ideal)}", "dims": ideal,
                          "price": q["unitPrice"], "moq": 1, "ref": f"upackage:{q['spec']}", "coverage": cov,
                          "init_cost": q["initCost"], "quote": q})
    dup = [x["code"] for x in cands if tuple(sorted(x["dims"], reverse=True)) in own]
    if dup:                              # 이미 보유한 규격과 치수가 같은 후보(지난 제안으로 등록된 맞춤 규격 등)는 신규가 아니다
        log("gen_candidates", f"보유 규격과 치수가 같아 제외: {dup}")
        cands = [x for x in cands if tuple(sorted(x["dims"], reverse=True)) not in own]
    past = {tuple(sorted(s["dims"], reverse=True)): x["targetMonth"]
            for x in reversed(state.get("past_proposals", [])) for s in x["newSpecs"] or []}
    for x in cands:                      # 같은 규격을 이전 제안에서도 냈다면 표시(규격 등록이 안 된 상태)
        x["proposedBefore"] = past.get(tuple(sorted(x["dims"], reverse=True)))
        log("gen_candidates", f"{x['candId']:<6} {x['type']:<7} {x['dims']} {x['price']}원 "
                              f"커버율 {x['coverage']}% ref={x['ref']}"
                              + (f" (지난 {x['proposedBefore']} 제안에도 포함)" if x["proposedBefore"] else ""))
    unfit = None
    if state.get("unfit_products") and not cfg["no_custom"]:   # 보유 규격 어디에도 안 들어가는 상품 → 맞춤 제작 1종
        u = state["unfit_products"]
        need = [max(sorted(p["dims"], reverse=True)[i] for p in u) for i in range(3)]
        dims = (await mcp_call("gen_candidates", "business", "ideal_box_dims", p90_dims=need, margin_mm=m))["dims"]
        q = await _quote_custom("gen_candidates", dims, sum(p["qty90d"] for p in u))
        if q["ok"]:
            dims = q["dims"]
            unfit = {"code": f"N-맞춤 {dims_s(dims)}", "name": f"맞춤 {dims_s(dims)}", "dims": dims,
                     "price": q["unitPrice"], "init_cost": q["initCost"], "quote": q,
                     "products": [p["code"] or p["productId"] for p in u], "qty90d": sum(p["qty90d"] for p in u)}
            log("gen_candidates", f"안 들어가는 상품 {len(u)}개 → 맞춤 {dims_s(dims)} {n(q['unitPrice'])}원 "
                                  f"(견적 {n(q['qty'])}개, 납기 {q['leadDate']})")
        else:
            notes.append(f"안 들어가는 상품 {len(u)}개용 맞춤 규격 {dims_s(dims)}: {q['note']}")
    nc = sum(x["type"] == "CATALOG" for x in cands)
    progress("gen_candidates", f"기성품 박스 카탈로그에서 후보 {nc}개, 맞춤 제작 견적 {len(cands) - nc}개를 찾았어요")
    return {"candidates": cands, "custom_unfit": unfit, "custom_notes": notes, "messages": ["gen_candidates"]}


UPACKAGE_MIN_MM = (0, 80, 50)                # 유팩 견적 계산기 거절 메시지 기준(세로 80mm, 높이 50mm 미만 제작 불가)


async def _quote_custom(node: str, dims: list[int], qty90d: int) -> dict:
    """월 출고량(90일 ÷ 3, 100개 단위 올림)으로 유팩 맞춤 제작 참고 견적.
    유팩 A형 최소 제작 크기(세로 80mm, 높이 50mm)보다 작으면 그 크기까지 올려서 묻는다 → 후보 치수는 q["dims"]"""
    qty = max(100, -(-qty90d // 3 // 100) * 100)
    d = sorted(dims, reverse=True)
    dims = [d[0], max(d[1], UPACKAGE_MIN_MM[1]), max(d[2], UPACKAGE_MIN_MM[2])]
    q = await mcp_call(node, "business", "quote_custom_box", dims=dims, qty=qty)
    log(node, f"맞춤 견적 {dims_s(dims)} × {n(qty)}개: " + (f"{n(q['unitPrice'])}원/개, 초기비 {n(q['initCost'])}원, "
                                                          f"납기 {q['leadDate']}" if q["ok"] else q["note"]))
    return q


def _fill_mode(cfg: dict) -> dict | None:
    """ESG 우선 모드(ISO 18602 기준, POL-02 §7): 본부가 켠 센터만. 절감 대신 규제 대상 충진율 개선으로 선정, 월 추가 비용 한도 안에서"""
    return {"min_gain": cfg["fill_mode_min_gain"], "max_extra_cost": cfg["fill_mode_max_extra_cost"]} if cfg["fill_mode"] else None


async def simulate(state: AgentState):
    r = await mcp_call("simulate", "business", "simulate_boxset",
                       products=state["product_fits"], box_specs=state["box_specs"],
                       candidates=state["candidates"], gate=state["config"]["gate"],
                       margin_mm=state["config"]["margin_mm"], allow_tilt=state["config"]["allow_tilt"],
                       tilt_clearance_mm=state["config"]["tilt_clearance_mm"], fill_mode=_fill_mode(state["config"]))
    if r["combined"]:
        b, a = r["combined"]["load"]["before"], r["combined"]["load"]["after"]
        log("simulate", f"도입 효과(월 {n(b['shipments'])}건): 충진율 {b['fill']}→{a['fill']}% "
                        f"적재 부피 {b['box_m3']}→{a['box_m3']}m³")
    for x in r["results"]:
        log("simulate", f"{x['candId']:<6} 대상 {x['target_fill_before']}→{x['target_fill_after']}% "
                        f"월절감 {n(x['monthly_saving'])} pass={x['pass']} selected={x['selected']}")
    sel = [x["candId"] for x in r["results"] if x["selected"]]
    log("simulate", f"게이트 통과·선정: {sel or '없음 → 기존 규격만 보충'}"
                    + (f" (ESG 우선 모드: 규제 대상 충진율 {r['combined']['reg_fill_before']}→{r['combined']['reg_fill_after']}%)"
                       if state["config"]["fill_mode"] and r["combined"] else ""))
    cfg, specs = state["config"], state["box_specs"]
    opts = {"margin_mm": cfg["margin_mm"], "allow_tilt": cfg["allow_tilt"], "tilt_clearance_mm": cfg["tilt_clearance_mm"]}
    # 활성 규격 상한(POL-02 §3): 기존 규격은 비활성화하지 않고, 넘으면 절감 작은 신규 후보부터 제외
    cu = state.get("custom_unfit")
    lim_products = state["product_fits"] + (state["unfit_products"] if cu else [])
    new = [{"code": x["code"], "dims": x["dims"], "price": x["price"], "priority": x.get("marginal_saving", x["monthly_saving"])}
           for x in r["results"] if x["selected"]]
    if cu:                               # 포장 불가 상품용 맞춤 규격은 가장 늦게 뺀다
        new.append({"code": cu["code"], "dims": cu["dims"], "price": cu["price"], "priority": 10 ** 12})
    lim = await mcp_call("simulate", "business", "check_spec_limit", products=lim_products, box_specs=specs,
                         new_specs=new, max_active=cfg["spec_max_active"], **opts)
    if lim["drop"]:
        picked = {x["candId"] for x in r["results"] if x["selected"]}
        keep = [c for c in state["candidates"] if c["candId"] in picked and c["code"] not in lim["drop"]]
        r = await mcp_call("simulate", "business", "simulate_boxset", products=state["product_fits"], box_specs=specs,
                           candidates=keep, gate=cfg["gate"], fill_mode=_fill_mode(cfg), **opts)
        sel = [x["candId"] for x in r["results"] if x["selected"]]
    log("simulate", f"활성 규격 {lim['count_before']}종 + 신규 {len(new)}종 = {lim['count_proposed']}종 / 상한 {lim['max_active']}종"
                    + (f" → 신규 제외 {lim['drop']}" if lim["drop"] else ""))
    best = max((x for x in r["results"] if x["selected"]), key=lambda x: x["target_fill_after"] - x["target_fill_before"], default=None)
    if best:
        progress("simulate", f"후보 {dims_s(best['dims'])}로 바꿔 보면 충진율이 {best['target_fill_before']:.0f}% → "
                             f"{best['target_fill_after']:.0f}%로 올라가요")
    saving = (r["combined"] or {}).get("monthly_saving") or 0
    progress("simulate", (f"기준을 통과한 신규 규격 {len(sel)}종을 골랐어요" + (f" (월 약 {kr(saving)}원 절감)" if saving > 0 else ""))
             if sel else "기준을 통과한 신규 규격이 없어 기존 규격만 보충해요")
    recent = None                        # 승인된 AI 제안으로 등록한 규격은 description 이 "catalog:boxking:<품번>" 또는 "custom:…"
    intro = [b for b in specs if str(b.get("description") or "").startswith(("catalog:", "custom:"))]
    if not sel and intro:                # 새 선정이 없으면 지난 제안으로 등록된 규격이 지금 내는 효과를 보여 준다
        recent = await mcp_call("simulate", "business", "compare_boxsets", products=state["product_fits"],
                                before_specs=[b for b in specs if b not in intro], after_specs=specs, **opts)
        recent["codes"] = [b["code"] for b in intro]
        if recent["load"]:
            b, a = recent["load"]["before"], recent["load"]["after"]
            log("simulate", f"최근 도입 {recent['codes']} 효과(월 {n(b['shipments'])}건): 충진율 {b['fill']}→{a['fill']}% "
                            f"박스비+운임 {n(b['box_krw'] + b['freight_krw'])}→{n(a['box_krw'] + a['freight_krw'])}원")
        else:
            recent = None
    custom = None
    if (cu := state.get("custom_unfit")):
        custom = (await mcp_call("simulate", "business", "compare_boxsets",
                                 products=state["product_fits"] + state["unfit_products"], before_specs=specs,
                                 after_specs=specs + [{"code": cu["code"], "dims": cu["dims"], "price": cu["price"]}],
                                 **opts))["newly_fit"]
    return {"sim_results": r["results"], "sim_combined": r["combined"], "recent_effect": recent, "spec_limit": lim,
            "custom_effect": custom, "messages": ["simulate"]}


async def plan_order(state: AgentState):
    cfg, tm = state["config"], state["job"]["targetMonth"]
    cd = {c["candId"]: c for c in state["candidates"]}
    new_specs = []
    for r in state["sim_results"]:
        if r["selected"]:
            c = cd[r["candId"]]
            new_specs.append({"code": c["code"], "dims": c["dims"], "price": c["price"],
                              "label": f"{c['name']} (신규·맞춤)" if c["type"] == "CUSTOM"
                              else f"{c['name']} {dims_s(c['dims'])} (신규)"})
    cu = state.get("custom_unfit")
    products = state["product_fits"] + (state.get("stockout_products") or [])   # 품절로 대기한 출고도 보충되면 그 박스를 쓴다
    if cu:                               # 안 들어가던 상품도 맞춤 규격으로 소모량을 잡는다
        new_specs.append({"code": cu["code"], "dims": cu["dims"], "price": cu["price"], "label": f"{cu['name']} (신규·맞춤)"})
        products = products + state["unfit_products"]
    specs = [{**b, "label": f"{b['code']} {dims_s(b['dims'])}"} for b in state["box_specs"]]
    y, m = map(int, tm.split("-"))
    peak = bool(cfg["peak_auto"] and m in cfg["peak_months"])        # 성수기 안전 재고(POL-03 §1)
    safety_days = cfg["peak_safety_days"] if peak else cfg["safety_days"]
    # 월초 예상 재고: 데이터 기준 시각부터 대상 월 1일까지 남은 소모를 빼고, 미입고 발주를 더한다.
    # 같은 대상 월 제안으로 만든 발주는 이번 제안이 대신하므로 빼지 않는다(다시 만들 때 중복 방지)
    as_of = datetime.fromisoformat(state["data_meta"]["inputAsOf"]) if state["data_meta"].get("inputAsOf") else datetime.now(KST)
    start_at = datetime(y, m, 1, tzinfo=KST)
    remaining = round(max(0.0, (start_at - as_of.astimezone(KST)).total_seconds() / 86400), 2)
    inbound: dict[str, int] = {}
    for o in state.get("inbound_orders") or []:
        if o.get("proposalTargetMonth") != tm:
            inbound[o["code"]] = inbound.get(o["code"], 0) + int(o["qty"])
    r = await mcp_call("plan_order", "business", "plan_order",
                       products=products, box_specs=specs, new_specs=new_specs,
                       inventory=state["inventory"], forecast_shipments=state["forecast"]["forecast"],
                       days_in_month=calendar.monthrange(y, m)[1], safety_days=safety_days,
                       rounding_unit=cfg["rounding_unit"], margin_mm=cfg["margin_mm"], allow_tilt=cfg["allow_tilt"],
                       tilt_clearance_mm=cfg["tilt_clearance_mm"], max_stock_months=cfg["max_stock_months"],
                       remaining_days=remaining, inbound=inbound)
    r["safetyDays"], r["peakSafety"] = safety_days, peak
    order_quotes = await _requote_custom(state, r)
    for row in r["orderRows"]:
        log("plan_order", f"{row['label']:<26} 재고 {n(row['currentQty']):>6} 소모 {n(row['expectedUsage']):>6} "
                          f"안전 {n(row['safetyQty']):>5}{'*' if row['safetySource'] == 'CONFIGURED' else ' '}"
                          f"→ 제안 {n(row['proposedQty']):>6}개 {n(row['amount'])}원")
    t = r["totals"]
    log("plan_order", f"{t['specCount']}개 규격 총 {n(t['qty'])}개, {n(t['amount'])}원 | 발주 제외: {r['skipped']} "
                      f"(* = 규격에 설정된 안전 재고)")
    log("plan_order", f"월초 예상 재고 = 현재 {n(t['current'])} − 이달 남은 소모 {n(t['consumeBefore'])}"
                      f"({remaining:g}일) + 입고 예정 {n(t['inbound'])} = {n(t['start'])}")
    progress("plan_order", f"규격 {t['specCount']}종, 총 {kr(t['qty'])}개, 약 {kr(t['amount'])}원어치 주문이 필요해요")
    return {"order_plan": r, "order_quotes": order_quotes, "messages": ["plan_order"]}


async def _requote_custom(state: AgentState, plan: dict) -> dict:
    """맞춤 규격은 1차 견적(월평균 출고 수량)으로 후보를 고른 뒤, 확정된 제안 수량으로 다시 견적을 받아
    주문표 단가·금액·합계를 바꾼다. 2차 견적이 실패하면 1차 단가를 그대로 둔다(게이트 재심사는 하지 않음)"""
    first = {c["code"]: c["quote"] for c in state["candidates"] if c.get("quote")}
    if state.get("custom_unfit"):
        first[state["custom_unfit"]["code"]] = state["custom_unfit"]["quote"]
    out = {}
    for row in plan["orderRows"]:
        q1 = first.get(row["code"])
        if not q1:
            continue
        q = q1 if row["proposedQty"] == q1["qty"] else await mcp_call(
            "plan_order", "business", "quote_custom_box", dims=q1["dims"], qty=row["proposedQty"])
        if not q["ok"]:
            log("plan_order", f"{row['label']} 2차 견적 실패 → 1차 단가 {n(q1['unitPrice'])}원 유지 ({q['note']})")
            continue
        out[row["code"]] = q
        row["unitPrice"], row["amount"] = q["unitPrice"], q["amount"]
        log("plan_order", f"{row['label']} 2차 견적 {n(row['proposedQty'])}개: {n(q['unitPrice'])}원/개 "
                          f"(1차 {n(q1['qty'])}개 {n(q1['unitPrice'])}원)")
    if out:
        plan["totals"]["amount"] = sum(x["amount"] for x in plan["orderRows"])
    return out


def _po_amount(pos: list, ym: str):
    """해당 월 발주 금액. AI 제안서로 만든 정기 발주가 있으면 그것만, 없으면 반려 제외 전체 합계"""
    rows = [p for p in pos if p["targetMonth"] == ym and not p.get("rejected")]
    rows = [p for p in rows if p.get("fromProposal")] or rows
    return sum(p["amountKrw"] for p in rows) if rows else None


def _compare(cur: int, base):
    if not base:
        return {"sign": "", "rate": "—", "diff": "비교할 발주 기록 없음", "client": "비교할 발주 기록 없음"}
    diff = cur - base
    up = diff >= 0
    sign, pm, rate = ("▲" if up else "▼"), ("+" if up else "−"), f"{abs(diff) / base * 100:.1f}"
    return {"sign": sign, "rate": rate, "diff": f"{pm}{n(abs(diff))}원",
            "client": f"{sign} {pm}{rate}%  ({pm}{won(abs(diff))})"}


def _check_items(state: AgentState, sel: list, prev_amt, ly_amt) -> list[str]:
    """'확인 필요' 항목(규칙 기반). 문장 재료는 모두 툴 결과·실행 메타"""
    plan, meta, checks = state["order_plan"], state["data_meta"], []
    cd = {c["candId"]: c for c in state["candidates"]}
    for s in sel:
        c = cd[s["candId"]]
        row = next(r for r in plan["orderRows"] if r["code"] == s["code"])
        if c["type"] == "CUSTOM":
            checks += _custom_checks(c["name"], c["quote"], (state.get("order_quotes") or {}).get(c["code"]))
            continue
        packs = -(-row["proposedQty"] // c["moq"])
        checks.append(f"신규 규격 {c['name']}: 규격 관리(box_spec) 등록 필요. "
                      f"박스왕 묶음 {c['moq']}매 단위라 실주문 {packs}묶음({n(packs * c['moq'])}개)")
        checks.append("박스왕 표기 치수(내치수 가정)와 가격 VAT 포함 여부(포함 가정) 확인")
        if c.get("proposedBefore"):
            checks.append(f"{c['name']} {dims_s(c['dims'])}: {c['proposedBefore']} 제안에도 포함됐으나 아직 미등록 — 등록 여부 결정 필요")
    for rj in state.get("rejections") or []:
        ch = ", ".join(f"{c['key']} {c['before']}→{c['after']}" for c in rj["changes"]) or "기준값 변경 없음"
        checks.append(f"운영자 의견 반영: \"{rj['comment']}\" → {ch}")
        checks += [f"담당자 처리 필요: {u}" for u in rj["unsupported"]]
    replaced = {code for s in sel for code in s["replaces"]}
    sw = [c for c in plan["skipped"] if c in replaced]
    rest = [c for c in plan["skipped"] if c not in replaced]
    if sw:
        checks.append(f"{', '.join(sw)}: 신규 규격 전환으로 소모가 줄어 이번 발주 제외 (현 재고로 충당)")
    if rest:
        checks.append(f"재고 충분으로 이번 발주 제외: {', '.join(rest)}")
    cu = state.get("custom_unfit")
    if cu:
        row = next((r for r in plan["orderRows"] if r["code"] == cu["code"]), None)
        checks.append(f"보유 규격에 안 들어가는 상품 {len(cu['products'])}개(월 {n(cu['qty90d'] // 3)}건)용 "
                      f"{cu['name']} 제안 — 절감이 아니라 포장 가능 여부 기준이라 도입 결정 필요")
        checks += _custom_checks(cu["name"], cu["quote"], (state.get("order_quotes") or {}).get(cu["code"]))
    checks += state.get("custom_notes") or []
    if state.get("unfit_products") and not cu:
        u = state["unfit_products"]
        checks.append(f"보유 규격에 안 들어가는 상품 {len(u)}개(월 {n(sum(x['qty90d'] for x in u) // 3)}건)는 계산에서 제외 — "
                      f"대형 규격 필요 여부 확인")
    if state.get("stockout_products"):
        so = state["stockout_products"]
        checks.append(f"맞는 박스가 품절이라 포장 대기한 출고 월 {n(sum(x['qty90d'] for x in so) // 3)}건"
                      f"({', '.join(sorted({x['box'] for x in so}))}) — 재고 보충 시점 확인")
    if state.get("tilted_products"):
        t = state["tilted_products"]
        checks.append(f"기울여 넣어야 들어가는 상품 {len(t)}개(월 {n(sum(x['qty90d'] for x in t) // 3)}건) — "
                      f"현장 포장 가능 여부 검토")
    if not state["forecast"].get("accuracy_pct"):
        checks.append(f"출고 이력 {len(state['shipment_history'])}개월로 백테스트 불가 — 예측 정확도 미산출")
    hist = {h["month"]: h["shipments"] for h in state["shipment_history"] if not h.get("partial")}
    compared = 0
    for x in state.get("past_proposals", []):        # 지난 제안의 예측 대비 실제(같은 월은 최신 제안 1건만, 최근 3건)
        f = (x.get("forecast") or {}).get("forecast")
        if f and x["targetMonth"] in hist and compared < 3:
            compared += 1
            act = hist.pop(x["targetMonth"])
            checks.append(f"지난 제안 {x['targetMonth']} 예측 {n(f)}건 → 실제 {n(act)}건 (오차 {(f - act) / act * 100:+.1f}%)")
    cfg = state["config"]
    lim = state.get("spec_limit") or {}
    if lim.get("drop"):                              # POL-02 §3 활성 규격 상한 (기존 규격은 비활성화하지 않음)
        checks.append(f"활성 규격 상한 {lim['max_active']}종 때문에 신규 후보 {', '.join(lim['drop'])}를 이번 제안에서 뺌 — "
                      f"상한 조정 여부 결정 필요")
    for r in plan["orderRows"]:                      # POL-04 §2 규격별 재고 보관 상한
        if r.get("overStock"):
            checks.append(f"{r['label']}: 재고(월초 예상 {n(r.get('startQty', r['currentQty']))} + 제안 {n(r['proposedQty'])})가 "
                          f"{cfg['max_stock_months']:g}개월 소모분 {n(r['stockCap'])}개를 넘음 — 보관 공간 확인·입고 분할 결정 필요")
    comb, res0 = state.get("sim_combined") or {}, (state.get("sim_results") or [{}])[0]    # POL-02 §7 충진율 목표
    reg = comb.get("reg_fill_after", res0.get("reg_fill_before"))
    if reg is not None and reg < cfg["fill_target_pct"]:
        checks.append(f"포장 규제 대상(세 변 합 50cm 초과) 출고 충진율 {reg}%로 목표 {cfg['fill_target_pct']:g}%에 못 미침"
                      + ("" if cfg["fill_mode"] else " — 본부 ESG 우선 모드 적용 여부 결정 필요"))
    if plan.get("peakSafety"):                       # POL-03 §1 성수기 안전 재고
        checks.append(f"성수기 안전 재고 {plan['safetyDays']}일분 적용(기본 {cfg['safety_days']}일)")
    if prev_amt:                                     # POL-03 §4 전월 대비 금액 변동 사유
        pct = (plan["totals"]["amount"] / prev_amt - 1) * 100
        if abs(pct) >= cfg["mom_note_pct"]:
            why = ([f"신규 규격 전환·재고 충분으로 {', '.join(plan['skipped'])} 발주 제외"] if pct < 0 and plan["skipped"] else []) \
                + ([f"신규 규격 {', '.join(r['code'] for r in plan['orderRows'] if r['isNew'])} 첫 주문"]
                   if pct > 0 and any(r["isNew"] for r in plan["orderRows"]) else []) \
                + ([f"예상 출고 {n(state['forecast']['forecast'])}건 기준 소모 증가"] if pct > 0 else [])
            checks.append(f"전월 대비 주문금액 {pct:+.1f}%(기준 ±{cfg['mom_note_pct']:g}%) — {'; '.join(why) or '사유 확인 필요'}")
    if not prev_amt or not ly_amt:
        checks.append("전월 또는 전년 동월 발주 기록이 없어 금액 비교 일부 생략")
    if meta.get("pendingEventCount"):
        checks.append(f"입력 기준 {meta['inputAsOf']}, 미처리 이벤트 {meta['pendingEventCount']}건 미반영 가능")
    if meta.get("source") == "mock":
        checks.append("Mock 데이터 기준 결과 (BUSINESS_API_URL 미설정)")
    return checks


def _custom_checks(name: str, q1: dict, q2: dict | None) -> list[str]:
    """q1 = 후보 선정용 1차 견적(월평균 수량), q2 = 제안 수량 기준 2차 견적(없으면 1차 단가 유지)"""
    q = q2 or q1
    first = f", 1차 견적 {n(q1['qty'])}개 {n(q1['unitPrice'])}원" if q2 and q2 is not q1 else ""
    out = [f"{name}: 유팩 참고 견적({q['spec']}, {q['quotedAt'][:10]} 기준, 제안 수량 {n(q['qty'])}개 "
           f"{n(q['unitPrice'])}원/개 VAT 별도{first}, 판비·목형비 {n(q['initCost'])}원 별도) — 실제 주문 견적·샘플 확인 필요",
           f"{name}: 제작 납기 약 {q['leadDate']} 도착 예정(견적일 기준), 규격 관리(box_spec) 등록 필요"]
    if not q2:
        out.append(f"{name}: 제안 수량 기준 재견적 실패 — 1차 견적 {n(q1['qty'])}개 단가로 계산, 단가 재확인 필요")
    return out


def _pct(before, after) -> str:
    return f"{(after / before - 1) * 100:+.1f}%" if before else "—"


FREIGHT_BASIS = ("택배 기본요금표(법제처 생활법령, 2026-09-15) — 세 변 합·무게 등급 중 높은 등급, 동일권 요금(극소 60cm·2kg 5,000원 / "
                 "소 120cm·10kg 6,000원 / 중 140cm·20kg 7,000원 / 대 160cm·30kg 8,000원). 160cm·30kg·최장변 100cm 초과는 경동 정기화물. "
                 "박스 1개 = 1건, 크기는 박스 내치수, 무게는 상품 실중량(박스 무게 제외)")


def _won_change(b, a) -> str:
    """비용 변화: 줄면 'n원 절감 (x%↓)', 늘면 'n원 증가 (x%↑)' — 빼기 기호가 손해로 읽히지 않게"""
    if a == b:
        return "변화 없음"
    pct = f"{abs(a / b - 1) * 100:.1f}%" if b else "—"
    return f"{n(abs(a - b))}원 {'절감' if a < b else '증가'} ({pct}{'↓' if a < b else '↑'})"


def _saving_change(block: dict, before_total: int) -> str:
    """월 박스비 + 운임 변화 — 예상 이익과 같은 값(block.monthly_saving)을 쓴다. 적재량 합계로 다시 빼지 않는다(반올림 차이 방지)"""
    return _won_change(before_total, before_total - block["monthly_saving"])


def _load_rows(comb: dict, before_price: str, after_price: str) -> list[dict]:
    """배정이 바뀌는 출고의 전/후 비교 행 (simulate_boxset.combined · compare_boxsets 결과)"""
    b, a = comb["load"]["before"], comb["load"]["after"]
    rows = [
        {"metric": "대상 출고 충진율", "before": f"{b['fill']}%", "after": f"{a['fill']}%",
         "change": f"{a['fill'] - b['fill']:+.1f}%p"},
        {"metric": "월 박스 적재 부피", "before": f"{b['box_m3']}m³", "after": f"{a['box_m3']}m³",
         "change": _pct(b["box_m3"], a["box_m3"])},
        {"metric": "박스 단가", "before": before_price, "after": after_price, "change": "—"},
        {"metric": "월 박스비", "before": f"{n(b['box_krw'])}원", "after": f"{n(a['box_krw'])}원",
         "change": _won_change(b["box_krw"], a["box_krw"])},
        {"metric": "월 운임(택배)", "before": f"{n(b['freight_krw'])}원", "after": f"{n(a['freight_krw'])}원",
         "change": _won_change(b["freight_krw"], a["freight_krw"])},
        {"metric": "월 박스비 + 운임", "before": f"{n(b['box_krw'] + b['freight_krw'])}원",
         "after": f"{n(b['box_krw'] + b['freight_krw'] - comb['monthly_saving'])}원",      # 전 − 절감액 (예상 이익과 같은 값)
         "change": _saving_change(comb, b["box_krw"] + b["freight_krw"])},
    ]
    if comb.get("overall_fill_before") is not None:
        rows.append({"metric": "센터 전체 충진율", "before": f"{comb['overall_fill_before']}%",
                     "after": f"{comb['overall_fill_after']}%",
                     "change": f"{comb['overall_fill_after'] - comb['overall_fill_before']:+.1f}%p"})
    return rows


def _effect(state: AgentState, sel: list) -> tuple[str, list[dict]]:
    """신규 규격 도입 효과 표 (툴 결과만 사용). 블록:
    ① 이번에 선정한 신규 규격 (없으면 ② 지난 제안으로 등록된 규격이 지금 내는 효과), ③ 안 들어가는 상품용 맞춤 규격"""
    price = {x["code"]: x["price"] for x in state["box_specs"]}
    blocks = []                                   # (이름, 근거 문장, 행)
    comb, recent = state.get("sim_combined"), state.get("recent_effect")
    if sel and comb:
        cd = {c["candId"]: c for c in state["candidates"]}
        names = ", ".join(cd[s["candId"]]["name"] for s in sel)
        b = comb["load"]["before"]
        blocks.append((names, f"{', '.join(comb['load']['fromBoxes'])}에 담기던 월 {n(b['shipments'])}건이 신규 규격 {names}에 "
                              f"담길 때 비교", _load_rows(comb, " · ".join(f"{c} {n(price[c])}원" for c in comb["load"]["fromBoxes"]),
                                                       " · ".join(f"{cd[s['candId']]['name']} {n(s['price'])}원" for s in sel))))
    elif recent:
        names, b = ", ".join(recent["codes"]), recent["load"]["before"]
        blocks.append((names, f"지난 제안으로 등록된 규격({names})이 없었다면 {', '.join(recent['load']['fromBoxes'])}에 담겼을 "
                              f"월 {n(b['shipments'])}건 기준 비교",
                       _load_rows(recent, " · ".join(f"{c} {n(price[c])}원" for c in recent["load"]["fromBoxes"]),
                                  " · ".join(f"{c} {n(price[c])}원" for c in recent["codes"]))))
    if (ce := state.get("custom_effect")) and (cu := state.get("custom_unfit")):
        oq = (state.get("order_quotes") or {}).get(cu["code"]) or cu["quote"]
        nf = "맞는 박스 없음"
        box_krw = round(ce["box_krw"] * oq["unitPrice"] / cu["price"])      # 2차 견적(제안 수량) 단가로 환산
        blocks.append((cu["name"], f"보유 규격에 안 들어가던 월 {n(ce['shipments'])}건을 {cu['name']}에 담을 때",
                       [{"metric": "대상 월 출고", "before": f"{n(ce['shipments'])}건 (포장 불가)", "after": f"{n(ce['shipments'])}건",
                         "change": "—"},
                        {"metric": "박스 단가", "before": nf, "after": f"{n(oq['unitPrice'])}원 (유팩 참고 견적)", "change": "—"},
                        {"metric": "대상 출고 충진율", "before": nf, "after": f"{ce['fill']}%", "change": "—"},
                        {"metric": "월 박스비", "before": nf, "after": f"{n(box_krw)}원", "change": "—"},
                        {"metric": "월 운임(택배)", "before": nf, "after": f"{n(ce['freight_krw'])}원", "change": "—"}]))
    if not blocks:
        return "이번 제안에 신규 규격 없음 — 해당 없음", [{"metric": "신규 규격 없음", "before": "—", "after": "—", "change": "—"}]
    many = len(blocks) > 1
    basis = f"분석 기간 출고를 한 달(30일)로 환산 (박스 단가는 VAT 별도). " + " / ".join(
        (f"[{name}] " if many else "") + text for name, text, _ in blocks)
    rows = [{**r, "metric": (f"[{name}] " if many else "") + r["metric"]} for name, _, rr in blocks for r in rr]
    return basis, rows


def _html_data(state: AgentState, values: dict, sel: list, checks: list[str], reasons: list[str], mom: dict,
               yoy: dict) -> dict:
    """보기용 HTML 제안서 입력 (proposal_server.render_proposal_html). 숫자는 모두 툴 결과·_effect 와 같은 출처"""
    job, fc, plan = state["job"], state["forecast"], state["order_plan"]
    custom_codes = {c["code"] for c in state["candidates"] if c["type"] == "CUSTOM"} | (
        {state["custom_unfit"]["code"]} if state.get("custom_unfit") else set())
    comb, recent = state.get("sim_combined"), state.get("recent_effect")
    block, title = (comb, ", ".join({c["candId"]: c for c in state["candidates"]}[x["candId"]]["name"] for x in sel)) \
        if sel and comb else (recent, ", ".join(recent["codes"])) if recent else (None, "")
    eff, profit = None, {"monthly": None, "note": "이번 제안으로 줄어드는 박스비·운임 없음"}
    kpis = [{"label": f"{int(job['targetMonth'][5:])}월 예상 출고", "value": f"{n(fc['forecast'])}건",
             "sub": f"전년 동월 대비 {(fc['forecast'] / fc['sameMonthLastYear'] - 1) * 100:+.1f}%"
             if fc.get("sameMonthLastYear") else fc["method"]}]
    if block:
        b, a = block["load"]["before"], block["load"]["after"]
        saving = block["monthly_saving"]          # 예상 이익 = 시뮬레이션 절감액 하나 (근거 문장·md 표와 같은 값)
        if saving > 0:
            profit = {"monthly": saving, "note": f"연 환산 {n(round(saving / 30 * 365))}원 · 박스비 + 운임 절감"}
        elif block.get("reg_fill_after") is not None:      # ESG 우선 모드: 비용은 늘지만 규제 대상 충진율 개선
            profit = {"monthly": None, "note": f"ESG 우선 모드 · 월 비용 +{n(-saving)}원, 규제 대상 충진율 "
                                               f"{block['reg_fill_before']}% → {block['reg_fill_after']}%"}
        frm ="·".join(block["load"]["fromBoxes"])
        eff = {"subtitle": f"{title} · 대상 월 {n(b['shipments'])}건 (기존 {frm})",
               "cost": [[f"도입 전 ({frm})", b["box_krw"], b["freight_krw"]], [f"도입 후 ({title})", a["box_krw"], a["freight_krw"]]],
               "rows": [["월 박스비 + 운임", f"{'−' if saving > 0 else '+'}{n(abs(saving))}원 "
                                          f"({_pct(b['box_krw'] + b['freight_krw'], b['box_krw'] + b['freight_krw'] - saving)})", saving > 0],
                        ["월 운임", f"{'−' if a['freight_krw'] < b['freight_krw'] else '+'}{n(abs(b['freight_krw'] - a['freight_krw']))}원", False],
                        ["월 박스비", f"{'−' if a['box_krw'] < b['box_krw'] else '+'}{n(abs(b['box_krw'] - a['box_krw']))}원", False],
                        ["월 박스 적재 부피", f"{b['box_m3']} → {a['box_m3']}m³", False]]
                       + ([["센터 전체 충진율", f"{block['overall_fill_before']} → {block['overall_fill_after']}%", False]]
                          if block.get("overall_fill_before") is not None else [])}
        kpis.append({"label": "대상 출고 충진율", "value": f"{b['fill']} → {a['fill']}%", "sub": f"{a['fill'] - b['fill']:+.1f}%p",
                     "up": a["fill"] > b["fill"]})
        if block.get("overall_fill_before") is not None:
            ob, oa = block["overall_fill_before"], block["overall_fill_after"]
            kpis.append({"label": "센터 전체 충진율", "value": f"{ob} → {oa}%", "sub": f"{oa - ob:+.1f}%p", "up": oa > ob})
    kpis.append({"label": "수요예측 정확도", "value": f"{fc['accuracy_pct']}%" if fc.get("accuracy_pct") is not None else "미산출",
                 "sub": f"최근 {len(fc.get('backtest') or []) or 3}개월 백테스트"})
    custom = None
    if (ce := state.get("custom_effect")) and (cu := state.get("custom_unfit")):
        oq = (state.get("order_quotes") or {}).get(cu["code"]) or cu["quote"]
        custom = {"title": cu["name"], "subtitle": f"포장 불가하던 월 {n(ce['shipments'])}건",
                  "rows": [["박스 단가", "—", f"{n(oq['unitPrice'])}원 (유팩 참고 견적)"],
                           ["대상 출고 충진율", "포장 불가", f"{ce['fill']}%"],
                           ["월 박스비 · 운임", "—", f"{n(round(ce['box_krw'] * oq['unitPrice'] / cu['price']))}원 · "
                                                    f"{n(ce['freight_krw'])}원"]]}
    rows = [{**{k: r[k] for k in ("label", "currentQty", "startQty", "expectedUsage", "safetyQty", "proposedQty", "amount")},
             "isNew": r["isNew"], "isCustom": r["code"] in custom_codes} for r in plan["orderRows"]]
    method = [reasons[3], "도입 효과·예상 이익은 분석 기간 출고를 한 달(30일)로 환산, 연 환산 = 월 ÷ 30 × 365", FREIGHT_BASIS,
              f"전월({values['prevMonthLabel']} 발주 {values['prevMonthAmount']}) {mom['sign']} {mom['rate']}%, "
              f"전년 동월({values['lastYearLabel']} 발주 {values['lastYearAmount']}) {yoy['sign']} {yoy['rate']}%",
              f"입력 기준 {state['data_meta']['inputAsOf']}",
              f"판단 규칙 v{(state.get('rules') or {}).get('version')}"
              + (f", 이 센터 조정값: {', '.join(f'{k}={v}' for k, v in state['rules']['overrides'].items())}"
                 if (state.get("rules") or {}).get("overrides") else ", 센터 조정값 없음(기본값)")]
    return {"warehouseName": job.get("warehouseName", ""), "targetMonth": job["targetMonth"],
            "periodStart": values["periodStart"], "periodEnd": values["periodEnd"], "orderRows": rows,
            "totals": plan["totals"], "skipped": plan["skipped"], "profit": profit, "kpis": kpis[:4],
            "effect": eff, "custom": custom, "details": {"checks": checks, "rationale": reasons[:3], "method": method}}


async def write_proposal(state: AgentState):
    """근거·확인 항목: 규칙 기반 문장(수치는 툴 결과에서만) → 자격 증명이 있으면 LLM이 문장만 다듬는다"""
    tpl = state.get("template") or await mcp_call("write_proposal", "business", "get_template")
    job, meta, fc, plan, rv = state["job"], state["data_meta"], state["forecast"], state["order_plan"], state["review"]
    tm, t, now = job["targetMonth"], plan["totals"], datetime.now(KST)
    y, m = map(int, tm.split("-"))
    dim = calendar.monthrange(y, m)[1]
    prev_m, ly_m = prev_ym(tm, 1), prev_ym(tm, 12)
    prev_amt, ly_amt = _po_amount(state["purchase_orders"], prev_m), _po_amount(state["purchase_orders"], ly_m)
    mom, yoy = _compare(t["amount"], prev_amt), _compare(t["amount"], ly_amt)
    acc = fc["accuracy_pct"] if fc["accuracy_pct"] is not None else "—"
    configured = any(r["safetySource"] == "CONFIGURED" for r in plan["orderRows"])
    safety_days = f"설정값 기준, 미설정 규격 {plan['safetyDays']}" if configured else plan["safetyDays"]

    # 근거 문장 재료 (모두 툴 결과)
    sel = [r for r in state["sim_results"] if r["selected"]]
    cl = {c["clusterId"]: c for c in state["clusters"]}
    reason1 = f"{ym_label(tm)} 출고 {n(fc['forecast'])}건 예측 — {fc['method']}"
    if fc["sameMonthLastYear"]:
        reason1 += (f", 전년 동월 {n(fc['sameMonthLastYear'])}건 대비 "
                    f"{(fc['forecast'] / fc['sameMonthLastYear'] - 1) * 100:+.1f}%")
    if sel:
        # 숫자 규칙: 예상 이익은 sim_combined.monthly_saving 하나만, 단일 품목 충진율은 그 품목(anchor) 값만
        s = sel[0]
        c = cl[s["cluster"]]
        cd0 = {x["candId"]: x for x in state["candidates"]}
        comb0 = state.get("sim_combined") or {}
        saving = comb0.get("monthly_saving", s["monthly_saving"])
        anchor = [r for r in s.get("moved", []) if r["anchor"]]
        others = [r for r in s.get("moved", []) if not r["anchor"]]
        if c.get("scope") == "SKU" and anchor:
            a = anchor[0]
            what = (f"단일 품목 {a['name']}(월 {n(round(a['qty90d'] / 3))}건)이 {a['from']}에 담겨 충진율 "
                    f"{a['fill_before']}% → {a['fill_after']}%로 개선")
        else:
            what = (f"{cats(c)} 구간(출고 비중 {c['share_pct']}%)이 {c['from_box']}에 담겨 충진율 "
                    f"{s['target_fill_before']}% → {s['target_fill_after']}%로 개선")
        nm = cd0[s["candId"]]["name"]
        reason2 = f"신규 규격 {nm if s['type'] == 'CUSTOM' else nm + ' ' + dims_s(s['dims'])} 도입: {what}"
        if others:
            reason2 += (f". 더 작은 박스가 생겨 {', '.join(r['name'] for r in others[:3])}"
                        + (f" 외 {len(others) - 3}개" if len(others) > 3 else "") + " 상품도 함께 옮겨 감")
        if len(sel) > 1:
            reason2 += f". 그 밖에 {', '.join(cd0[x['candId']]['name'] for x in sel[1:])}도 도입"
        if any(x.get("pass_by") == "FILL" for x in sel):
            cfgx = state["config"]
            reason2 += (f". ESG 우선 모드(목표 {cfgx['fill_target_pct']:g}%)로 선정 — 규제 대상 충진율 "
                        f"{comb0.get('reg_fill_before')}% → {comb0.get('reg_fill_after')}%, 월 추가 비용 한도 "
                        f"{n(cfgx['fill_mode_max_extra_cost'])}원")
        reason2 += (f". 선정 규격 전체로 월 박스비·운임 약 {n(saving)}원 절감, "
                    f"{', '.join(sorted({r for x in sel for r in x['replaces']}))} 예상 소모는 그만큼 줄여 반영")
    else:
        reason2 = "신규 규격 후보가 도입 기준(월 절감·충진율 개선)을 넘지 못해 기존 규격만 보충"
    if state.get("custom_unfit"):
        cu = state["custom_unfit"]
        reason2 += (f". 보유 규격에 안 들어가는 상품 {len(cu['products'])}개는 {cu['name']}"
                    f"(유팩 참고 견적 {n(((state.get('order_quotes') or {}).get(cu['code']) or cu['quote'])['unitPrice'])}원/개)"
                    f"으로 주문에 포함")
    old = [r for r in plan["orderRows"] if not r["isNew"]]
    top = max(old, key=lambda r: r["proposedQty"]) if old else None
    days_left = (top["currentQty"] / (top["expectedUsage"] / dim)) if top and top["expectedUsage"] else 0
    reason3 = (f"{top['label']}: 현재 재고 {n(top['currentQty'])}개로 약 {days_left:.0f}일분 — 보충 수량이 가장 큼"
               if top else "기존 규격은 재고 충분")
    tt = plan["totals"]
    formula = (f"제안 수량 = 다음 달 예상 소모 + 안전 재고 − 월초 예상 재고 ({state['config']['rounding_unit']}개 단위 반올림). "
               f"월초 예상 재고 = 현재 재고 − 이달 남은 소모(다음 달 1일까지 {tt.get('remainingDays', 0):g}일) + 입고 예정 발주")

    values = {
        "proposalId": job["proposalId"], "targetMonth": tm,
        "generatedAt": now.isoformat(timespec="seconds"), "inputAsOf": meta["inputAsOf"],
        "generatedAtDisplay": now.strftime("%Y-%m-%d %H:%M"),
        "periodStart": f"{tm}-01", "periodEnd": month_end(tm),
        "targetMonthLabel": ym_label(tm), "forecastShipments": n(fc["forecast"]),
        "specCount": t["specCount"], "totalQty": n(t["qty"]), "forecastAccuracy": acc,
        "safetyDays": safety_days,
        "sumCurrent": n(t["current"]), "sumUsage": n(t["usage"]), "sumSafety": n(t["safety"]),
        "reason1": reason1, "reason2": reason2, "reason3": reason3,
        "roundingUnit": state["config"]["rounding_unit"],
        "totalAmount": n(t["amount"]) + "원",
        "prevMonthLabel": ym_label(prev_m), "prevMonthAmount": n(prev_amt) + "원" if prev_amt else "기록 없음",
        "momSign": mom["sign"], "momRate": mom["rate"], "momDiff": mom["diff"],
        "lastYearLabel": ym_label(ly_m), "lastYearAmount": n(ly_amt) + "원" if ly_amt else "기록 없음",
        "yoySign": yoy["sign"], "yoyRate": yoy["rate"], "yoyDiff": yoy["diff"],
    }
    rows = [{"spec": r["label"], "currentQty": n(r["currentQty"]), "expectedUsage": n(r["expectedUsage"]),
             "safetyQty": n(r["safetyQty"]), "proposedQty": n(r["proposedQty"])} for r in plan["orderRows"]]
    values["effectBasis"], effect_rows = _effect(state, sel)
    values["freightBasis"] = FREIGHT_BASIS

    checks = _check_items(state, sel, prev_amt, ly_amt)
    if state["config"]["demo_eval_loop"] and rv["loop_count"] == 0:
        checks = []                      # --demo-eval-loop: 1회차 초안은 '확인 필요'를 비워 평가 → 재작성 루프를 보여 준다
    checks, citations, sources = await cite_policies(state, checks)

    # 문장 다듬기(LLM). 숫자 출처 = facts. 실패하거나 자격 증명이 없으면 규칙 기반 문장 그대로
    facts = {"values": values, "rows": rows, "forecast": {k: fc[k] for k in ("forecast", "method", "accuracy_pct",
                                                                           "sameMonthLastYear", "backtest")},
             "selectedNewSpecs": sel, "clusters": [{k: c[k] for k in ("clusterId", "categories", "share_pct", "p50",
                                                                      "from_box", "avg_fill")} for c in state["clusters"]],
             "orderRows": plan["orderRows"], "skipped": plan["skipped"], "dataMeta": meta,
             "pastProposals": state.get("past_proposals", []), "newSpecEffect": state.get("sim_combined"),
             "recentSpecEffect": state.get("recent_effect"), "customEffect": state.get("custom_effect"),
             "customQuotes": [c["quote"] for c in state["candidates"] if c.get("quote")]
                             + ([state["custom_unfit"]["quote"]] if state.get("custom_unfit") else [])
                             + list((state.get("order_quotes") or {}).values()),
             "customUnfit": state.get("custom_unfit")}
    llm = {"used": False}
    draft = [reason1, reason2, reason3, *checks]          # 규칙 기반 문장의 숫자도 툴 결과에서 나온 값이라 허용
    out = await llm_writer.polish(facts, [reason1, reason2, reason3], checks, rv["feedback"],
                                  example=tpl.get("example_md")) if checks else None
    if out and "error" not in out:
        reason1, reason2, reason3 = out["reasons"]
        values.update(reason1=reason1, reason2=reason2, reason3=reason3)
        checks = out["checkItems"]
        llm = {"used": True, "model": out["model"], "text": [reason1, reason2, reason3, *checks],
               "allowedNumbers": sorted(llm_writer.numbers(json.dumps(facts, ensure_ascii=False, default=str))
                                        | llm_writer.numbers(" ".join(draft)))}
        log("write_proposal", f"근거·확인 항목 문장: LLM({out['model']})")
    elif out:
        log("write_proposal", f"LLM 문장 작성 실패 → 규칙 기반 문장 사용 ({out['error']})")

    r = await mcp_call("write_proposal", "business", "render_proposal", values=values, rows=rows, check_items=checks,
                       effect_rows=effect_rows)
    html_data = _html_data(state, values, sel, checks, [reason1, reason2, reason3, formula], mom, yoy)
    h = await mcp_call("write_proposal", "business", "render_proposal_html", data=html_data)
    if h["missing"]:
        log("write_proposal", f"HTML 제안서 미기입 {h['missing']}")

    # client BoxOrderView 가 쓰는 모양 (client/src/mocks/orders.js: orderPeriod, proposal, purchaseOrderDraft.items)
    specs = {b["code"]: b for b in state["box_specs"]}
    client_view = {
        "orderPeriod": f"발주 대상 기간  {y}.{m:02d}.01 – {m:02d}.{dim:02d}",
        "createdAt": "생성 " + now.strftime("%Y.%m.%d %H:%M"),
        "headline": f"{m}월 예상 출고 {n(fc['forecast'])}건 기준, {t['specCount']}개 규격 총 {n(t['qty'])}개 주문을 제안합니다.",
        "subline": f"수요예측 정확도 {acc}% · 규격별 안전 재고({safety_days}일분) 반영",
        "rows": [{"id": x["code"], "code": x["code"], "current": n(x["currentQty"]), "expected": n(x["expectedUsage"]),
                  "safety": n(x["safetyQty"]), "suggested": f"{n(x['proposedQty'])}개", "isNew": x["isNew"]}
                 for x in plan["orderRows"]],
        "rationale": [reason1, reason2, reason3, formula],
        "totalAmount": won(t["amount"]),
        "newSpecEffect": {"basis": values["effectBasis"], "rows": effect_rows},
        "comparisons": [
            {"id": "prev-month", "label": f"전월 대비 · {int(prev_m[5:])}월 발주 {won(prev_amt) if prev_amt else '기록 없음'}",
             "change": mom["client"]},
            {"id": "prev-year", "label": f"전년 동월 대비 · {ly_m.replace('-', '.')} 발주 {won(ly_amt) if ly_amt else '기록 없음'}",
             "change": yoy["client"]},
        ],
        "purchaseOrderItems": [{"id": x["code"], "code": x["code"], "size": " × ".join(map(str, x["dims"])),
                                "quantity": x["proposedQty"], "unitPrice": x["unitPrice"], "isNew": x["isNew"],
                                "supplier": specs.get(x["code"], {}).get("supplier")} for x in plan["orderRows"]],
    }
    rv = {**rv, "loop_count": rv["loop_count"] + 1}
    log("write_proposal", f"draft v{rv['loop_count']} (표 {len(rows)}행, 확인 필요 {len(checks)}건, "
                          f"미기입 {r['missing']})")
    progress("write_proposal", "근거 문장을 쓰고 관련 규정을 찾아 붙였어요" if rv["loop_count"] == 1
             else f"검토 의견을 반영해 제안서를 다시 썼어요 ({rv['loop_count']}번째 초안)")
    client_view["sources"] = [{"sourceId": k, **v} for k, v in sources.items()]
    for i in range(0, len(r["markdown"]), 400):          # 화면 답변 영역(TOKEN). 초안마다 draft 번호로 구분
        harness.emit("TOKEN", draft=rv["loop_count"], text=r["markdown"][i:i + 400])
    return {"template": tpl, "proposal_md": r["markdown"], "proposal_html": h["html"], "proposal_html_data": html_data, "client_view": client_view, "review": rv, "llm": llm,
            "citations": citations, "sources": sources, "messages": ["write_proposal"]}


CITE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["citations"], "properties": {
    "citations": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["index", "sourceIds"],
        "properties": {"index": {"type": "integer"}, "sourceIds": {"type": "array", "items": {"type": "string"}}}}}}}


async def cite_policies(state: AgentState, checks: list[str]) -> tuple[list[str], dict, dict]:
    """⑧ '확인 필요' 항목마다 모델이 search_policies 로 규정을 찾아 인용을 붙인다.
    검색 결과에 없는 sourceId 는 버린다(Guardrail). 같은 항목 목록이면 지난 결과를 다시 쓴다(재작성 루프)."""
    sources = dict(state.get("sources") or {})
    if not checks or not harness.llm_enabled():
        return checks, state.get("citations") or {}, sources
    key = json.dumps(checks, ensure_ascii=False)
    cache = state.get("citations") or {}
    if cache.get("key") == key:
        return cache["checks"], cache, sources
    found: dict = {}
    names = ["search_policies"]
    try:
        r = await harness.tool_loop(
            "cite_policies", harness.prompt("policy_citer"),
            json.dumps({"checkItems": [{"index": i, "text": c} for i, c in enumerate(checks)]}, ensure_ascii=False),
            rag_tools(names), rag_executor("write_proposal", names, state["job"]["warehouseId"], found),
            CITE_SCHEMA, "policy_citations")
    except Exception as e:
        log("write_proposal", f"규정 인용 생략 ({str(e)[:120]})")
        return checks, cache, sources
    out = list(checks)
    cited = 0
    for c in r["output"]["citations"]:
        ids = [x for x in dict.fromkeys(c["sourceIds"]) if x in found][:2]
        if ids and 0 <= c["index"] < len(out):
            out[c["index"]] += f" ({', '.join(ids)})"
            cited += 1
            sources.update({x: found[x] for x in ids})
    dropped = sum(len([x for x in c["sourceIds"] if x not in found]) for c in r["output"]["citations"])
    log("write_proposal", f"규정 인용: {cited}/{len(checks)}개 항목 (모델 도구 호출 {len(r['toolCalls'])}회, "
                          f"{r['turns']}턴{f', 검색 결과에 없는 인용 {dropped}개 버림' if dropped else ''})")
    return out, {"key": key, "checks": out}, sources


def evaluate(state: AgentState):
    """양식 완결성·수치 일치 체크 + LLM이 쓴 문장에 툴 결과에 없는 숫자가 있는지 대조"""
    md, plan, score, fb = state["proposal_md"], state["order_plan"], 100, []
    if "{{" in md:
        score -= 30
        missing = sorted(set(re.findall(r"{{(\w+)}}", md)))          # 3.11: f-string 안 백슬래시 불가
        fb.append(f"미기입 자리표시자 {missing}")
    for sec in state["template"]["required_sections"]:
        mm = re.search(re.escape(sec) + r"\n(.*?)(?=\n## |\Z)", md, re.S)
        if not mm or not mm.group(1).strip():
            score -= 25
            fb.append(f"{sec.strip('# ')}: 내용 없음")
    t = plan["totals"]
    if sum(r["proposedQty"] for r in plan["orderRows"]) != t["qty"] or f"**{n(t['qty'])}개**" not in md:
        score -= 20
        fb.append("제안 수량 합계 불일치")
    if f"**{n(t['amount'])}원**" not in md:
        score -= 20
        fb.append("총 주문금액 불일치")
    llm = state.get("llm") or {}
    if llm.get("used"):
        allowed = set(llm["allowedNumbers"])
        unknown = sorted({v for line in llm["text"] for v in llm_writer.numbers(line)
                          if v not in allowed and not (v.isdigit() and int(v) <= 31)})   # 일·월·개수 같은 작은 수는 허용
        if unknown:
            score -= 20
            fb.append(f"툴 결과에 없는 숫자 {unknown} — facts 값만 사용")
    known = set(state.get("sources") or {})
    cited = {x.strip() for grp in re.findall(r"\((POL-\d+ §\d+(?:, POL-\d+ §\d+)*)\)", md) for x in grp.split(",")}
    if cited - known:
        score -= 20
        fb.append(f"검색 결과에 없는 규정 인용 {sorted(cited - known)} — 인용 삭제")
    score = max(score, 0)
    rv = {**state["review"], "score": score, "feedback": fb}
    log("evaluate", f"score={score}/100 loop={rv['loop_count']}/{state['config']['max_loops']} feedback={fb}")
    progress("evaluate", f"제안서를 스스로 점검했어요 (100점 중 {score}점)")
    return {"review": rv, "messages": ["evaluate"]}


def route_eval(state: AgentState) -> Literal["write_proposal", "human_review"]:
    rv, cfg = state["review"], state["config"]
    if rv["score"] < cfg["eval_pass"] and rv["loop_count"] < cfg["max_loops"]:
        log("route_eval", f"score<{cfg['eval_pass']} → write_proposal (재작성)")
        return "write_proposal"
    return "human_review"


def human_review(state: AgentState):
    """save_proposal(변경 도구) 앞에서 멈춘다. 상태는 checkpointer 에 남고, 같은 thread_id 로 재개한다"""
    decision = interrupt({"proposal_md": state["proposal_md"], "score": state["review"]["score"],
                          "tool": "save_proposal", "totalAmount": state["order_plan"]["totals"]["amount"]})
    log("human_review", f"RESUME: {decision}")
    upd = {"approval": {"status": decision["status"], "comment": decision.get("comment", "")},
           "messages": ["human_review"]}
    if decision["status"] == "REJECTED":
        upd["review"] = {"score": 0, "feedback": [decision.get("comment", "")], "loop_count": 0}
    return upd


# 반려 의견으로 바꿀 수 있는 기준값(화이트리스트): key → (형, 최소, 최대)
# 반려 의견으로 바꿀 수 있는 규칙(규칙 파일의 feedback: true). 형·범위 검증도 규칙 파일 기준
FEEDBACK_KEYS = {k: r for k, r in policy_rules.load_rules()["rules"].items() if r.get("feedback")}
INTERP_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["changes", "unsupported", "sources"],
                 "properties": {
                     "changes": {"type": "array", "items": {
                         "type": "object", "additionalProperties": False, "required": ["key", "value", "reason", "sources"],
                         "properties": {"key": {"type": "string", "enum": list(FEEDBACK_KEYS)},
                                        "value": {"type": "string", "description": "true/false 또는 숫자"},
                                        "reason": {"type": "string"},
                                        "sources": {"type": "array", "items": {"type": "string"}}}}},
                     "unsupported": {"type": "array", "items": {"type": "string"}},
                     "sources": {"type": "array", "items": {"type": "string"}}}}


def _cfg_get(cfg: dict, key: str):
    for k in key.split("."):
        cfg = cfg[k]
    return cfg


def _cfg_set(cfg: dict, key: str, value) -> dict:
    cfg = json.loads(json.dumps(cfg))
    *path, last = key.split(".")
    node = cfg
    for k in path:
        node = node.setdefault(k, {})          # 규칙 키 'gate.CATALOG.x' 로 처음 만들 때도 쓴다
    node[last] = value
    return cfg


def _parse_value(key: str, raw: str):
    """화이트리스트 형·범위 검증(규칙 파일 range). 맞지 않으면 ValueError (Guardrail)"""
    return policy_rules.cast(FEEDBACK_KEYS[key], raw)


async def interpret_feedback(state: AgentState):
    """⑩ 반려 의견 → 기준값 변경. 키워드 규칙을 먼저 적용하고, LLM 이 있으면 모델이 rag 도구로
    과거 반려 기록·규정을 찾아 해석한다. 화이트리스트 밖·범위 밖 값과 검색되지 않은 근거는 버린다."""
    comment, before, job = state["approval"]["comment"], state["config"], state["job"]
    cfg = apply_feedback(before, comment)
    changes = [{"key": k, "before": _cfg_get(before, k), "after": _cfg_get(cfg, k), "reason": "키워드 규칙", "sources": []}
               for k in FEEDBACK_KEYS if _cfg_get(before, k) != _cfg_get(cfg, k)]
    rj = {"comment": comment, "method": "keyword", "changes": changes, "unsupported": [], "sources": []}
    sources = dict(state.get("sources") or {})
    if comment.strip() and harness.llm_enabled():
        found: dict = {}
        names = ["search_feedback_cases", "search_policies"]
        try:
            r = await harness.tool_loop(
                "interpret_feedback", harness.prompt("feedback_interpreter"),
                json.dumps({"comment": comment, "targetMonth": job["targetMonth"],
                            "currentConfig": {k: _cfg_get(before, k) for k in FEEDBACK_KEYS}}, ensure_ascii=False),
                rag_tools(names), rag_executor("interpret_feedback", names, job["warehouseId"], found),
                INTERP_SCHEMA, "feedback_interpretation")
            out, rejected = r["output"], []
            for ch in out["changes"]:
                try:
                    v = _parse_value(ch["key"], ch["value"])
                except (ValueError, KeyError) as e:
                    rejected.append(str(e))
                    continue
                src = [x for x in ch["sources"] if x in found]
                if _cfg_get(cfg, ch["key"]) != v:
                    changes = [c for c in changes if c["key"] != ch["key"]]
                    changes.append({"key": ch["key"], "before": _cfg_get(before, ch["key"]), "after": v,
                                    "reason": ch["reason"], "sources": src})
                    cfg = _cfg_set(cfg, ch["key"], v)
            used = {x for c in changes for x in c["sources"]} | {x for x in out["sources"] if x in found}
            sources.update({x: found[x] for x in used})
            rj = {"comment": comment, "method": f"llm:{r['model']}", "changes": changes,
                  "unsupported": out["unsupported"], "sources": sorted(used), "rejectedChanges": rejected,
                  "toolCalls": r["toolCalls"]}
            log("interpret_feedback", f"모델 도구 호출 {[c['name'] for c in r['toolCalls']]} ({r['turns']}턴)"
                                      + (f", 버린 변경 {rejected}" if rejected else ""))
        except Exception as e:
            log("interpret_feedback", f"LLM 해석 실패 → 키워드 규칙만 적용 ({str(e)[:150]})")
    log("interpret_feedback", f"기준값 변경 {[(c['key'], c['before'], c['after']) for c in changes]} "
                              f"미지원 {rj['unsupported']} 근거 {rj['sources']}")
    harness.emit("PROGRESS", node="interpret_feedback", step=None, total=len(STEP_ORDER),
                 text=f"\"{comment[:40]}\" 의견에 맞춰 " + ("기준을 바꿔 다시 계산하고 있어요" if changes else "다시 검토하고 있어요"))
    return {"config": cfg, "rejections": [*(state.get("rejections") or []), rj], "sources": sources,
            "messages": ["interpret_feedback"]}


def apply_feedback(cfg: dict, comment: str) -> dict:
    """반려 의견 → 기준값. 알아듣지 못한 의견은 '확인 필요'에 그대로 기록만 한다"""
    cfg, c = dict(cfg), comment.replace(" ", "")
    if any(k in c for k in ("신규제외", "신규없이", "보충만", "기존규격만")):
        cfg["no_new_specs"] = True
    if any(k in c for k in ("맞춤제외", "맞춤제작제외", "맞춤없이", "맞춤제작빼")):
        cfg["no_custom"] = True
    if m := re.search(r"안전재고(\d+)일", c):
        cfg["safety_days"] = int(m.group(1))
    return cfg


def route_review(state: AgentState) -> Literal["save_result", "interpret_feedback"]:
    return "save_result" if state["approval"]["status"] == "APPROVED" else "interpret_feedback"


async def save_result(state: AgentState):
    plan, job = state["order_plan"], state["job"]
    spec_id = {b["code"]: b["boxSpecId"] for b in state["box_specs"]}
    spec = {b["code"]: b for b in state["box_specs"]}
    cd = {c["code"]: c for c in state["candidates"]}
    oq = state.get("order_quotes") or {}
    custom_codes = {c["code"] for c in state["candidates"] if c["type"] == "CUSTOM"} | (
        {state["custom_unfit"]["code"]} if state.get("custom_unfit") else set())
    sel = [r for r in state["sim_results"] if r["selected"]]
    payload = {
        "proposalId": job["proposalId"], "jobId": job["jobId"], "warehouseId": job["warehouseId"],
        "targetMonth": job["targetMonth"], "inputAsOf": state["data_meta"]["inputAsOf"],
        "dataSource": state["data_meta"].get("source"),
        "generatedAt": datetime.now(KST).isoformat(timespec="seconds"), "approval": state["approval"],
        "totalAmount": plan["totals"]["amount"], "totals": plan["totals"],
        "forecast": {k: state["forecast"][k] for k in ("forecast", "method", "accuracy_pct", "sameMonthLastYear")},
        # boxSpecId 있는 줄 → procurement_proposal_item, 없는 줄(미등록 신규 규격) → summary에만
        # 서버(AgentProposalItemService)는 NEW_SPEC 줄을 박스왕 카탈로그 근거로 box_spec 에 등록한다.
        # 맞춤 규격은 공급사 미등록·정식 견적 전이라 CUSTOM_SPEC 으로 남겨 자동 등록·발주에서 빠지게 한다
        "lines": [{"type": ("CUSTOM_SPEC" if r["code"] in custom_codes else "NEW_SPEC") if r["isNew"] else "REPLENISH",
                   "boxCode": r["code"],
                   "boxSpecId": None if r["isNew"] else spec_id.get(r["code"]), "dims": r["dims"],
                   "boxName": spec.get(r["code"], {}).get("name"), "sizeClass": spec.get(r["code"], {}).get("sizeClass"),
                   "qty": r["proposedQty"], "unitPriceKrw": r["unitPrice"], "currentQty": r["currentQty"],
                   "safetyQty": r["safetyQty"], "expectedUsage": r["expectedUsage"]} for r in plan["orderRows"]],
        # 서버 계획 지표(AgentPlanProjection): 주문이 없는 규격까지 전체 규격의 다음 달 예상 소모 → 대시보드 안전 재고·소진 예측
        "usagePlan": [{"code": r["code"], "dims": r["dims"], "unitPrice": r["unitPrice"], "expectedUsage": r["expectedUsage"]}
                      for r in plan["rows"]],
        # 서버 계획 지표의 월 절감: 선정 규격을 모두 넣었을 때 월 박스비+운임 절감(제안서 예상 이익과 같은 값)
        "simulation": {"monthly_saving": (state.get("sim_combined") or {}).get("monthly_saving", 0)},
        "newSpecs": [{"code": r["code"], "name": cd[r["code"]]["name"], "dims": r["dims"],
                      "price": oq[r["code"]]["unitPrice"] if r["code"] in oq else r["price"],
                      "type": r["type"], "ref": cd[r["code"]]["ref"], "moq": cd[r["code"]]["moq"],
                      "replaces": r["replaces"], "monthlySaving": r["monthly_saving"],
                      "fillBefore": r["target_fill_before"], "fillAfter": r["target_fill_after"],
                      "initCost": r["init_cost"], "paybackM": r["payback_m"], "quote": cd[r["code"]].get("quote"),
                      "orderQuote": oq.get(r["code"])}
                     for r in sel]
                    + ([{"code": cu["code"], "name": cu["name"], "dims": cu["dims"],
                         "price": oq[cu["code"]]["unitPrice"] if cu["code"] in oq else cu["price"],
                         "type": "CUSTOM_UNFIT", "ref": f"upackage:{cu['quote']['spec']}", "moq": 1, "replaces": [],
                         "products": cu["products"], "initCost": cu["init_cost"], "quote": cu["quote"],
                         "orderQuote": oq.get(cu["code"])}]
                       if (cu := state.get("custom_unfit")) else []),
        "clientView": state["client_view"],
        "review": {"score": state["review"]["score"], "rewrites": state["review"]["loop_count"]},
        "textSource": f"llm:{state['llm']['model']}" if (state.get("llm") or {}).get("used") else "rule",
        "sources": [{"sourceId": k, **v} for k, v in (state.get("sources") or {}).items()],
        "rejections": state.get("rejections") or [],
        "specLimit": state.get("spec_limit"),
        "rules": {k: (state.get("rules") or {}).get(k) for k in ("version", "updatedAt", "overrides")}
                 | {"applied": {r["key"]: r["value"] for r in (state.get("rules") or {}).get("rows", [])}},
        "harness": harness.summary(),
        "bodyMarkdown": state["proposal_md"],
        "bodyHtml": state.get("proposal_html"),
        # HTML 을 그린 입력(render_proposal_html). 양식만 바뀌면 이걸로 다시 그린다 — 승인한 숫자는 그대로
        "htmlData": state.get("proposal_html_data"),
    }
    r = await mcp_call("save_result", "business", "save_proposal", payload=payload)
    d = r["db"]
    where = (f"DB procurement_proposal id={r['proposalId']} (item {d['itemRows']}행"
             + (f", summary만: {d['summaryOnly']}" if d["summaryOnly"] else "") + ")") if d["saved"] else "DB 미설정"
    log("save_result", f"저장 완료 {where} | 사본 {Path(r['path']).name}")
    for i, rj in enumerate(state.get("rejections") or [], 1):    # 다음 반려 해석에 쓰도록 rag 에 적재(승인 뒤)
        await mcp_call("save_result", "rag", "add_feedback_case", record={
            "caseId": f"FB-{job['targetMonth']}-{job['jobId'][-8:]}-{i}", "warehouseId": job["warehouseId"],
            "targetMonth": job["targetMonth"], "rejectedAt": datetime.now(KST).date().isoformat(),
            "comment": rj["comment"], "category": "agent",
            "configChange": {c["key"]: c["after"] for c in rj["changes"]}, "unsupported": "; ".join(rj["unsupported"]),
            "action": f"{rj['method']} 해석으로 재작성", "outcome": "APPROVED_AFTER_REWRITE",
            "policyRefs": rj["sources"], "source": "box-agent"})
    return {"messages": ["save_result"]}


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------
def build_graph(checkpointer=None):
    g = StateGraph(AgentState)
    nodes = [("load_context", load_context), ("collect_data", collect_data), ("forecast_demand", forecast_demand),
             ("analyze_fit", analyze_fit), ("diagnose", diagnose), ("find_clusters", find_clusters),
             ("gen_candidates", gen_candidates), ("simulate", simulate), ("plan_order", plan_order),
             ("write_proposal", write_proposal), ("evaluate", evaluate), ("human_review", human_review),
             ("interpret_feedback", interpret_feedback), ("save_result", save_result)]
    for name, fn in nodes:
        g.add_node(name, fn)
    g.add_edge(START, "load_context")
    chain = ["load_context", "collect_data", "forecast_demand", "analyze_fit", "diagnose", "find_clusters",
             "gen_candidates", "simulate", "plan_order", "write_proposal", "evaluate"]
    for a, b in zip(chain, chain[1:]):
        g.add_edge(a, b)
    g.add_conditional_edges("evaluate", route_eval)
    g.add_conditional_edges("human_review", route_review)
    g.add_edge("interpret_feedback", "gen_candidates")
    g.add_edge("save_result", END)
    return g.compile(checkpointer=checkpointer or MemorySaver())


def approval_payload(out: dict) -> dict:
    """승인 대기 이벤트 내용. 재시작 뒤 되살린 작업도 같은 내용을 다시 보낸다(agent_server._restore)"""
    return dict(tool="save_proposal", targetMonth=out["job"]["targetMonth"],
                totalAmount=out["order_plan"]["totals"]["amount"], qty=out["order_plan"]["totals"]["qty"],
                score=out["review"]["score"], newSpecs=[r["code"] for r in out["sim_results"] if r["selected"]],
                sources=[{"sourceId": k, **v} for k, v in (out.get("sources") or {}).items()],
                proposalMarkdown=out["proposal_md"], proposalHtml=out.get("proposal_html"))


@asynccontextmanager
async def open_checkpointer():
    """State·Memory: DB_URL 이 있으면 PostgreSQL(box_agent 스키마) checkpointer, 없으면 메모리.
    thread_id = taskId 로 저장돼 프로세스가 다시 떠도 승인 대기 작업을 재개할 수 있다."""
    if not os.getenv("DB_URL"):
        yield MemorySaver()
        return
    import psycopg
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from urllib.parse import quote

    sys.path.insert(0, str(HERE / "mcp_servers"))
    import chagok_db

    ci = chagok_db._conninfo()
    with psycopg.connect(**ci, autocommit=True) as con:
        con.execute("CREATE SCHEMA IF NOT EXISTS box_agent")
    url = (f"postgresql://{quote(ci['user'])}:{quote(ci['password'])}@{ci['host']}:{ci['port']}/{ci['dbname']}"
           "?options=-csearch_path%3Dbox_agent")
    async with AsyncPostgresSaver.from_conn_string(url) as saver:
        await saver.setup()
        yield saver


def make_job(wh: dict, target_month: str, job_id: str | None = None, demo_eval_loop: bool = False) -> dict:
    job = {"jobId": job_id or f"job-{target_month}-{wh['warehouseId'][:8]}", "warehouseId": wh["warehouseId"],
           "warehouseName": wh["name"], "targetMonth": target_month, "demoEvalLoop": demo_eval_loop}
    job["proposalId"] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"chagok:box-agent:{job['jobId']}"))  # = DB id
    return job


def run_config(task_id: str, job: dict | None = None) -> dict:
    """thread_id = taskId (같은 jobId 를 여러 번 돌려도 상태가 섞이지 않음). LangSmith 용 run 이름·메타데이터"""
    run_id = harness.new_langsmith_run()
    return {"configurable": {"thread_id": task_id}, "run_name": "box_agent", "tags": ["box-agent"],
            **({"run_id": run_id} if run_id else {}),
            "metadata": {"task_id": task_id, "trace_id": harness.trace_id(), **({"job_id": job["jobId"],
                         "target_month": job["targetMonth"]} if job else {})}, "recursion_limit": 60}


async def run_until_pause(app, payload, cfg: dict) -> dict:
    """그래프를 다음 interrupt(승인 대기) 또는 끝까지 실행한다. 한 구간은 TASK_TIMEOUT_S 초 안에 끝나야 한다"""
    out = await asyncio.wait_for(app.ainvoke(payload, cfg), harness.TASK_TIMEOUT_S)
    if "__interrupt__" in out:
        harness.emit("APPROVAL_REQUIRED", **approval_payload(out))
    return out


def parse_args():
    nm = datetime.now(KST).date().replace(day=1) + timedelta(days=32)
    ap = argparse.ArgumentParser(description="박스 주문 제안 에이전트")
    ap.add_argument("--warehouse-id", help="warehouse.id (UUID). 생략하면 센터가 1개일 때 자동 선택")
    ap.add_argument("--target-month", default=f"{nm.year:04d}-{nm.month:02d}", help="YYYY-MM (기본: 다음 달)")
    ap.add_argument("--job-id", help="기본: job-<targetMonth>-<warehouse 앞 8자리>")
    ap.add_argument("--env-file", action="append", default=[], help="추가로 읽을 .env (여러 번 가능)")
    ap.add_argument("--auto-approve", action="store_true", help="운영자 검토 없이 승인")
    ap.add_argument("--reject-once", metavar="COMMENT", help="첫 검토는 이 의견으로 반려 후 승인 (시연·테스트용, ⑩ 해석)")
    ap.add_argument("--no-llm", action="store_true", help="문장 작성에 LLM을 쓰지 않음(규칙 기반)")
    ap.add_argument("--demo-eval-loop", action="store_true", help="1회차 초안의 '확인 필요'를 비워 재작성 루프 시연")
    return ap.parse_args()


async def ask_decision(md: str, score: int) -> dict:
    print("\n" + "-" * 20 + f" 제안서 초안 (eval {score}/100) " + "-" * 20 + "\n" + md + "-" * 60)
    try:
        ans = (await asyncio.to_thread(input, "승인하시겠습니까? [y=승인 / n=반려]: ")).strip().lower()
        comment = (await asyncio.to_thread(
            input, "의견(선택. 반려 시 '보충만'/'안전 재고 N일' 반영): ")).strip()
    except EOFError:
        raise SystemExit("입력을 받을 수 없음(비대화형 실행) — --auto-approve 를 사용하세요") from None
    return {"status": "APPROVED" if ans in ("y", "yes", "승인") else "REJECTED", "comment": comment}


def load_env(extra: list[str] = ()) -> list[str]:
    loaded = load_env_files(HERE / ".env", HERE.parent / "server" / "chagok" / ".env", *(Path(p) for p in extra))
    if os.getenv("LANGSMITH_API_KEY"):
        os.environ.setdefault("LANGSMITH_TRACING", "true")
        os.environ.setdefault("LANGSMITH_PROJECT", "box-agent")
    return loaded


def env_summary() -> str:
    biz = (f"business-mcp {os.environ['BUSINESS_MCP_URL']} 설정 따름" if os.getenv("BUSINESS_MCP_URL")
           else f"Spring 내부 API {os.environ['BUSINESS_API_URL']}" if os.getenv("BUSINESS_API_URL") else "Mock")
    return (f"업무 데이터={biz}"
            f" | checkpointer={'PostgreSQL' if os.getenv('DB_URL') else '메모리'}"
            f" | LLM={llm_writer.model() if llm_writer.enabled() else '사용 안 함(규칙 기반)'}"
            f" | LangSmith={'on (' + os.getenv('LANGSMITH_PROJECT', 'default') + ')' if harness.langsmith_enabled() else 'off'}")


async def pick_warehouse(hub: MCPHub, warehouse_id: str | None) -> dict:
    whs = (await hub.call("business", "list_warehouses"))["items"]
    if warehouse_id:
        wh = next((w for w in whs if w["warehouseId"] == warehouse_id), None)
        if not wh:
            raise ValueError(f"warehouse {warehouse_id} 없음. 목록: {[(w['warehouseId'], w['name']) for w in whs]}")
        return wh
    if len(whs) == 1:
        return whs[0]
    raise ValueError(f"warehouse_id 필요. 목록: {[(w['warehouseId'], w['name']) for w in whs]}")


async def main():
    global HUB
    args = parse_args()
    loaded = load_env(args.env_file)
    if loaded:
        log("env", f"loaded {loaded}")
    if args.no_llm:
        os.environ["BOX_AGENT_LLM"] = "0"
    log("env", env_summary())

    task_id = f"cli-{uuid.uuid4().hex[:12]}"
    harness.start_trace(task_id, source="cli")
    async with open_checkpointer() as saver, MCPHub(MCP_SERVERS) as hub:
        HUB = hub
        app = build_graph(saver)
        try:
            wh = await pick_warehouse(hub, args.warehouse_id)
        except ValueError as e:
            raise SystemExit(str(e)) from None
        job = make_job(wh, args.target_month, args.job_id, args.demo_eval_loop)
        cfg = run_config(task_id, job)
        print("=" * 20, f"RUN task={task_id} trace={harness.trace_id()}", "=" * 20)
        out = await run_until_pause(app, {"job": job, "messages": []}, cfg)
        reject = args.reject_once
        while "__interrupt__" in out:
            score = out["review"]["score"]
            log("human_review", f"INTERRUPT: 운영자 승인 대기 (eval score={score})")
            if reject:
                decision, reject = {"status": "REJECTED", "comment": reject}, None
            elif args.auto_approve:
                decision = {"status": "APPROVED", "comment": "자동 승인(--auto-approve)"}
            else:
                decision = await ask_decision(out["proposal_md"], score)
            out = await run_until_pause(app, Command(resume=decision), cfg)
        links = harness.langsmith_links()
        harness.emit("DONE", proposalId=job["proposalId"], totalAmount=out["order_plan"]["totals"]["amount"],
                     langsmith=links)
        print("=" * 20, "DONE", "=" * 20)
        log("harness", json.dumps(harness.summary(), ensure_ascii=False))
        for u in links:
            log("langsmith", u)
        steps = out["messages"]
        print(f"path({len(steps)} steps):")
        for i in range(0, len(steps), 6):
            print("  " + ("→ " if i else "") + " → ".join(steps[i:i + 6]))


if __name__ == "__main__":
    asyncio.run(main())
