"""
본부 통합 효율화 리포트 에이전트 (LangGraph) — 센터 전체의 월별 성과를 모아 판정하고, 개선 조치를 본부장 승인 뒤 반영한다

  load_context → collect → judge → plan_actions → recommend → write_report → human_review(승인 대기) → apply_actions

- collect: 센터마다 최근 hq_months 개월의 실제 배정(list_product_fits)·박스 규격·지난 제안 → center_month_metrics(도입 전 대비)
- judge: 규제 대상 충진율 목표(fill_target_pct)와 전월 대비 개선 폭(hq_stall_gain)으로 양호·주의·효율 개선 필요
- plan_actions: 효율 개선 필요 센터 → ESG 우선 모드(fill_mode) 켜기, 이미 켜져 있는데도 정체면 규격 상한(spec_max_active) +hq_cap_step
- recommend: 센터별 조정 3개(월 절감액 기준·최소 충진율 개선 폭·목형비 회수 기간) 추천값. 저장하지 않고 결과로 넘긴다(본부장이 화면에서 조정)
- write_report: 확정 양식(templates/hq_report)으로 월별 HTML. 맨 위 '본부 총평'은 규칙 초안을 LLM 이 skills/hq-report-writing 규칙으로 다듬음
- human_review: 조치는 센터 판단 규칙을 바꾸는 일이라 본부장 승인 앞에서 멈춘다(반려하면 조치 없이 끝)
- apply_actions: 승인하면 set_center_overrides 로 센터 덮어쓰기에 저장 → 다음 월말 박스 주문 제안부터 적용
본부 에이전트는 박스 제안 에이전트를 직접 돌리지 않는다(재실행은 다음 제안에서 센터 규칙으로 반영).

실행: python hq_report_agent.py --month 2026-09 [--out proposals/hq]   (서버: agent_server.py POST /internal/agent/hq-reports)
"""
import argparse
import asyncio
import calendar
import operator
import os
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

import box_spec_agent as A
import harness

HERE = Path(__file__).resolve().parent
OUT_DIR = Path(os.getenv("HQ_REPORT_DIR", HERE / "proposals" / "hq"))


class HQState(TypedDict, total=False):
    job: dict                                 # jobId, month(리포트 대상 월), note
    rules: dict                               # 전사 규칙 값(get_policy_rules)
    centers: list                             # [{warehouseId, name, rules}]
    months: list                              # 리포트에 넣는 월(오래된 순)
    rows: dict                                # {월: [센터 행]} — hq_render 입력
    actions: list                             # [{warehouseId, center, changes, reason}]
    recommendations: list                     # [{warehouseId, center, current, recommended, changes, reason}]
    report: dict                              # {paths, dir}
    approval: dict
    applied: list
    messages: Annotated[list, operator.add]


def _months_back(month: str, n: int) -> list[str]:
    y, m = map(int, month.split("-"))
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    return out[::-1]


def _covered_days(month: str, data_as_of: str | None) -> float:
    """그 달에 데이터가 있는 일수. 진행 중인 달(마지막 출고 시각이 그 달 안)은 1일 0시부터 마지막 출고까지(소수 일),
    마감된 달은 달력 일수. 월 값(일평균 × 30)·연간 추정(× 365)을 이 일수로 나눠야 진행 중인 달이 낮게 나오지 않는다"""
    dim = calendar.monthrange(int(month[:4]), int(month[5:]))[1]
    if not data_as_of:
        return dim
    end = datetime.fromisoformat(data_as_of)
    start = datetime(int(month[:4]), int(month[5:]), 1, tzinfo=end.tzinfo)
    if end.strftime("%Y-%m") != month:
        return dim
    return round(min(dim, max((end - start).total_seconds() / 86400, 1)), 2)


def _month_range(month: str) -> tuple[str, str]:
    y, m = map(int, month.split("-"))
    return f"{month}-01", f"{month}-{calendar.monthrange(y, m)[1]:02d}"


async def load_context(state: HQState):
    job = state["job"]
    rules = (await A.mcp_call("load_context", "business", "get_policy_rules"))["values"]
    whs = (await A.mcp_call("load_context", "business", "list_warehouses"))["items"]
    centers = []
    for w in whs:
        r = (await A.mcp_call("load_context", "business", "get_policy_rules", warehouse_id=w["warehouseId"]))
        centers.append({"warehouseId": w["warehouseId"], "name": w["name"], "rules": r["values"], "overrides": r["overrides"]})
    months = _months_back(job["month"], int(rules["hq_months"]))
    A.log("load_context", f"본부 리포트 {job['month']} · 센터 {len(centers)}곳 · 기간 {months[0]}~{months[-1]} "
                          f"· 판정 목표 {rules['fill_target_pct']:g}% / 정체 기준 +{rules['hq_stall_gain']:g}%p")
    return {"rules": rules, "centers": centers, "months": months, "messages": ["load_context"]}


async def collect(state: HQState):
    """센터·월마다 실제 배정으로 도입 전 대비 성과. 출고가 없는 달(데이터 시작 전)과 효율화 전 달(절감 0)은 뺀다"""
    rows = {m: [] for m in state["months"]}
    for c in state["centers"]:
        wid = c["warehouseId"]
        specs = (await A.mcp_call("collect", "business", "list_box_specs", warehouse_id=wid))["items"]
        past = (await A.mcp_call("collect", "business", "list_past_proposals", warehouse_id=wid, limit=24))["items"]
        new_by_month = {}
        for p in past:                        # 같은 대상 월은 최신 제안 1건(목록은 최신순)
            new_by_month.setdefault(p["targetMonth"], len(p.get("newSpecs") or []))
        for m in state["months"]:
            f, t = _month_range(m)
            fits = await A.mcp_call("collect", "business", "list_product_fits", warehouse_id=wid, from_date=f, to_date=t)
            met = await A.mcp_call("collect", "business", "center_month_metrics", rows=fits["items"], box_specs=specs)
            if met["ship"]:
                as_of = (fits.get("meta") or {}).get("dataAsOf")
                rows[m].append({"center": c["name"], "warehouseId": wid, **met, "applied": new_by_month.get(m, 0),
                                "days": _covered_days(m, as_of), "dataAsOf": as_of})
    rows = {m: r for m, r in rows.items() if r}
    while rows and not any(d["saving"] for d in next(iter(rows.values()))):   # 효율화 전(절감 0) 달은 앞에서 뺀다
        rows.pop(next(iter(rows)))
    A.log("collect", f"월별 센터 행 {[(m, len(r)) for m, r in rows.items()]}")
    return {"rows": rows, "months": list(rows), "messages": ["collect"]}


async def judge(state: HQState):
    """달마다 전월 대비 판정(첫 달은 도입 전 대비). 누적 반영 건수·추가 절감도 채운다"""
    rules, prev = state["rules"], {}
    target = {c["name"]: c["rules"]["fill_target_pct"] for c in state["centers"]}
    for m in state["months"]:
        for d in state["rows"][m]:
            p = prev.get(d["center"])
            pf = p["fill"] if p else d["fill_b"]
            j = await A.mcp_call("judge", "business", "judge", fill=d["fill"], prev_fill=pf,
                                 target_pct=target[d["center"]], stall_gain=rules["hq_stall_gain"])
            d.update(prev_fill=pf, approved=True, rerun=None, r=1, low_gate=False, **j)
            d["added"] = max(0, d["saving"] - (p["saving"] if p else 0)) if d["applied"] else 0
            d["applied_cum"] = d["applied"] + (p["applied_cum"] if p else 0)
            prev[d["center"]] = d
    last = state["rows"][state["months"][-1]]
    A.log("judge", " ".join(f"{d['center'].replace(' 센터', '')}{d['fill']}%·{d['status']}" for d in last))
    return {"rows": state["rows"], "messages": ["judge"]}


def plan_actions(state: HQState):
    """마지막 달 '효율 개선 필요' 센터 조치: ESG 우선 모드가 꺼져 있으면 켜고, 이미 켜져 있으면 규격 상한을 올린다"""
    rules, by = state["rules"], {c["name"]: c for c in state["centers"]}
    actions = []
    for d in state["rows"][state["months"][-1]]:
        if d["status"] != "개선 필요":
            continue
        cr = by[d["center"]]["rules"]
        if not cr["fill_mode"]:
            ch, why = {"fill_mode": True}, "ESG 우선 모드로 다음 제안 재실행"
        else:
            cap = min(int(cr["spec_max_active"]) + int(rules["hq_cap_step"]), 30)
            ch, why = {"spec_max_active": cap}, f"ESG 우선 모드에도 정체 → 규격 상한 {cr['spec_max_active']}→{cap}종 상향"
        actions.append({"warehouseId": d["warehouseId"], "center": d["center"], "changes": ch, "reason": why,
                        "fill": d["fill"], "gain": d["gain"]})
        d["action"] = why
    A.log("plan_actions", f"조치 {[(a['center'], a['changes']) for a in actions] or '없음'}")
    return {"actions": actions, "rows": state["rows"], "messages": ["plan_actions"]}


TUNE = {"saving": "gate.CATALOG.monthly_saving", "fill": "gate.CATALOG.delta_fill", "payback": "gate.CUSTOM.payback_m"}


def _tune_changes(v: dict) -> dict:
    """추천값 → 규칙 키. 월 절감액·충진율 개선 폭은 기성품·맞춤 게이트에 같이, 회수 기간은 맞춤만"""
    return {"gate.CATALOG.monthly_saving": v["saving"], "gate.CUSTOM.monthly_saving": v["saving"],
            "gate.CATALOG.delta_fill": v["fill"], "gate.CUSTOM.delta_fill": v["fill"], "gate.CUSTOM.payback_m": v["payback"]}


def recommend(state: HQState):
    """센터별 조정 3개(월 절감액 기준·최소 충진율 개선 폭·목형비 회수 기간) 추천. 적용은 본부장이 화면에서 한다(여기선 저장 안 함).
    주의 = 1단계, 효율 개선 필요 = 2단계만큼 기준을 풀어 제안이 더 나오게 하고, 목표를 넉넉히(+5%p) 넘은 센터는 기본값으로 되돌린다"""
    rules, by = state["rules"], {c["name"]: c for c in state["centers"]}
    default = {k: rules[key] for k, key in TUNE.items()}
    recos = []
    for d in state["rows"][state["months"][-1]]:
        cr = by[d["center"]]["rules"]
        cur = {k: cr[key] for k, key in TUNE.items()}
        n = {"주의": 1, "개선 필요": 2}.get(d["status"], 0)
        if n:
            new = {"saving": max(100000, default["saving"] - n * rules["hq_reco_saving_step"]),
                   "fill": max(0.5, round(default["fill"] - n * rules["hq_reco_fill_step"], 1)),
                   "payback": min(12, default["payback"] + n * rules["hq_reco_payback_step"])}
            new = {"saving": min(cur["saving"], new["saving"]), "fill": min(cur["fill"], new["fill"]),
                   "payback": max(cur["payback"], new["payback"])}         # 이미 더 풀어 둔 값은 그대로
            why = f"{d['status']} — 기준을 {n}단계 풀어 제안 후보를 늘림"
        elif d["fill"] >= cr["fill_target_pct"] + 5:
            new, why = dict(default), "목표를 넉넉히 넘음 — 기본값 유지"
        else:
            new, why = dict(cur), "양호 — 현재 값 유지"
        recos.append({"warehouseId": d["warehouseId"], "center": d["center"], "status": d["status"], "fill": d["fill"],
                      "current": cur, "recommended": new, "changed": new != cur, "reason": why,
                      "changes": _tune_changes(new) if new != cur else {}})
    A.log("recommend", " ".join(f"{r['center'].replace(' 센터', '')}:{r['recommended']}" for r in recos if r["changed"]) or "변경 추천 없음")
    return {"recommendations": recos, "messages": ["recommend"]}


SUMMARY_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["sentences"],
                  "properties": {"sentences": {"type": "array", "items": {"type": "string"}, "description": "본부 총평 3~4문장"}}}


def _krw(v: float) -> str:
    return f"약 {v / 1e8:,.1f}억 원" if abs(v) >= 1e8 else f"{round(v / 1e4):,}만 원"


def _summary_draft(state: HQState) -> tuple[dict, list[str]]:
    """본부 총평 facts·규칙 초안. 월 값은 리포트와 같은 30일 기준"""
    m = state["months"][-1]
    rows = state["rows"][m]
    short = lambda n: n.replace(" 센터", "")
    names = lambda xs: ", ".join(xs) if len(xs) <= 3 else f"{xs[0]} 외 {len(xs) - 1}곳"
    saving = sum(d["saving"] / d["days"] for d in rows) * 30          # 센터마다 데이터 일수로 일평균(진행 중인 달 포함)
    co2 = sum(d["co2_cut"] / d["days"] for d in rows) * 30
    by = {s: [short(d["center"]) for d in rows if d["status"] == s] for s in ("양호", "주의", "개선 필요")}
    facts = {"month": m, "centers": len(rows), "monthlySaving": _krw(saving), "co2Ton": f"{co2 / 1000:,.0f}톤",
             "goodCount": len(by["양호"]), "statusCenters": {k: names(v) for k, v in by.items() if v},
             "actions": [{"center": short(a["center"]), "action": a["reason"]} for a in state.get("actions") or []],
             "recommendations": [{"center": short(r["center"]), "status": r["status"],
                                  "monthlySaving": f"{r['current']['saving'] / 1e4:,.0f}만 → {r['recommended']['saving'] / 1e4:,.0f}만 원",
                                  "minFillGain": f"{r['current']['fill']:g} → {r['recommended']['fill']:g}%p",
                                  "paybackMonths": f"{r['current']['payback']:g} → {r['recommended']['payback']:g}개월"}
                                 for r in state.get("recommendations") or [] if r["changed"]]}
    draft = [f"{int(m[5:])}월 전 센터 월 절감액은 {facts['monthlySaving']}, 탄소 감축은 {facts['co2Ton']}이며 "
             f"목표(규제 대상 충진율 50%) 달성은 {len(by['양호'])}/{len(rows)}곳입니다."]
    if by["개선 필요"] or by["주의"]:
        draft.append(" · ".join(x for x in (f"효율 개선 필요 {names(by['개선 필요'])}" if by["개선 필요"] else "",
                                            f"주의 {names(by['주의'])}" if by["주의"] else "") if x) + "입니다.")
    draft.append("; ".join(f"{a['center']} {a['action']}" for a in facts["actions"]) + " — 본부장 승인 필요입니다."
                 if facts["actions"] else "추가 조치는 없습니다.")
    rc = facts["recommendations"]
    draft.append("설정 추천: " + "; ".join(f"{r['center']} 월 절감액 기준 {r['monthlySaving']}, 충진율 개선 폭 {r['minFillGain']}, "
                                         f"목형비 회수 {r['paybackMonths']}" for r in rc[:3])
                 + (f" 외 {len(rc) - 3}곳" if len(rc) > 3 else "") + "입니다." if rc else "센터별 설정은 현재 값 유지를 추천합니다.")
    return facts, draft


async def _summary(state: HQState) -> list[str]:
    """LLM 이 skills/hq-report-writing 규칙으로 다듬는다. facts·초안에 없는 숫자가 나오면 초안을 쓴다"""
    facts, draft = _summary_draft(state)
    if not harness.llm_enabled():
        return draft
    try:
        out = await harness.llm_json("write_report", harness.instructions("hq_summary", "hq-report-writing"),
                                     {"facts": facts, "draft": draft}, SUMMARY_SCHEMA, "hq_summary")
    except Exception as e:
        A.log("write_report", f"총평 LLM 실패 → 규칙 문장 ({type(e).__name__})")
        return draft
    allowed = A.llm_writer.numbers(" ".join(draft) + " " + str(facts))
    extra = {v for s in out["sentences"] for v in A.llm_writer.numbers(s)} - allowed
    if extra or not 3 <= len(out["sentences"]) <= 4:
        A.log("write_report", f"총평 LLM 문장에 근거 없는 숫자 {sorted(extra)} → 규칙 문장")
        return draft
    return out["sentences"]


async def write_report(state: HQState):
    out = OUT_DIR / state["job"]["month"]
    summary = await _summary(state)
    r = await A.mcp_call("write_report", "business", "render_hq_report", data=state["rows"], out_dir=str(out),
                         note=state["job"].get("note", ""), summary=summary,
                         recommendations=state.get("recommendations") or [], job_id=state["job"]["jobId"])
    A.log("write_report", f"리포트 {len(r['paths'])}개 → {out} | 총평 {summary}")
    return {"report": {"dir": str(out), "paths": r["paths"], "summary": summary,
                       "recommendations": state.get("recommendations") or []}, "messages": ["write_report"]}


def human_review(state: HQState):
    """센터 판단 규칙을 바꾸는 조치 앞에서 멈춘다. 조치가 없으면 바로 끝"""
    if not state.get("actions"):
        return {"approval": {"status": "NONE"}, "messages": ["human_review"]}
    decision = interrupt({"tool": "set_center_overrides", "month": state["job"]["month"], "actions": state["actions"],
                          "recommendations": state.get("recommendations") or [],
                          "report": state["report"]["paths"][-1]})
    A.log("human_review", f"RESUME: {decision}")
    return {"approval": {"status": decision["status"], "comment": decision.get("comment", "")}, "messages": ["human_review"]}


async def apply_actions(state: HQState):
    applied = []
    if state["approval"]["status"] == "APPROVED":
        for a in state["actions"]:
            r = await A.mcp_call("apply_actions", "business", "set_center_overrides", warehouse_id=a["warehouseId"],
                                 changes=a["changes"], actor="본부장 승인(본부 리포트)",
                                 reason=f"{state['job']['month']} 리포트 조치: {a['reason']}")
            applied.append({"center": a["center"], "overrides": r["overrides"]})
    A.log("apply_actions", f"{state['approval']['status']}: 반영 {[x['center'] for x in applied] or '없음'}")
    return {"applied": applied, "messages": ["apply_actions"]}


def build_graph(checkpointer=None):
    g = StateGraph(HQState)
    nodes = [("load_context", load_context), ("collect", collect), ("judge", judge), ("plan_actions", plan_actions),
             ("recommend", recommend), ("write_report", write_report), ("human_review", human_review), ("apply_actions", apply_actions)]
    for name, fn in nodes:
        g.add_node(name, fn)
    g.add_edge(START, nodes[0][0])
    for (a, _), (b, _) in zip(nodes, nodes[1:]):
        g.add_edge(a, b)
    g.add_edge("apply_actions", END)
    from langgraph.checkpoint.memory import MemorySaver
    return g.compile(checkpointer=checkpointer or MemorySaver())


def make_job(month: str, note: str = "") -> dict:
    return {"jobId": f"hq-{month}-{uuid.uuid4().hex[:8]}", "month": month, "note": note}


def run_config(task_id: str, job: dict) -> dict:
    return {"configurable": {"thread_id": task_id}, "run_name": "hq_report_agent", "tags": ["hq-report"],
            "metadata": {"month": job["month"], "jobId": job["jobId"]}}


def approval_payload(out: dict) -> dict:
    """승인 대기 이벤트 내용. 재시작 뒤 되살린 작업도 같은 내용을 다시 보낸다(agent_server._restore)"""
    return dict(tool="set_center_overrides", month=out["job"]["month"], actions=out["actions"],
                recommendations=out.get("recommendations") or [], reportPaths=out["report"]["paths"])


async def run_until_pause(app, payload, cfg: dict) -> dict:
    out = await asyncio.wait_for(app.ainvoke(payload, cfg), harness.TASK_TIMEOUT_S)
    if "__interrupt__" in out:
        harness.emit("APPROVAL_REQUIRED", **approval_payload(out))
    return out


async def main():
    last = (date.today().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    ap = argparse.ArgumentParser(description="본부 통합 효율화 리포트 에이전트")
    ap.add_argument("--month", default=last, help="리포트 대상 월(기본: 지난달)")
    ap.add_argument("--note", default="", help="리포트 상단 표시 문구(시험 데이터 표시 등)")
    ap.add_argument("--yes", action="store_true", help="조치를 묻지 않고 승인(시연용)")
    a = ap.parse_args()
    A.load_env()
    job = make_job(a.month, a.note)
    task = job["jobId"]
    harness.start_trace(task, source="cli")
    async with A.MCPHub(A.MCP_SERVERS) as hub:
        A.HUB = hub
        app = build_graph()
        out = await run_until_pause(app, {"job": job, "messages": []}, run_config(task, job))
        if "__interrupt__" in out:
            for x in out["actions"]:
                print(f"  - {x['center']}: {x['reason']} {x['changes']}")
            ok = a.yes or input("조치를 승인할까요? [y/N] ").strip().lower() == "y"
            from langgraph.types import Command
            out = await run_until_pause(app, Command(resume={"status": "APPROVED" if ok else "REJECTED"}), run_config(task, job))
        print("리포트:", out["report"]["paths"][-1])


if __name__ == "__main__":
    asyncio.run(main())
