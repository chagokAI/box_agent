"""
hq 도구 — 본부 통합 효율화 리포트용 계산·렌더 (business-mcp 에 묶여 노출)

- center_month_metrics: 한 센터·한 달의 실제 배정(list_product_fits 행) → 월 절감·탄소·규제 대상 충진율(도입 전 대비)
  도입 전 = 승인된 AI 제안으로 등록한 규격(description 'catalog:'·'custom:')을 뺀 박스 세트로, 같은 출고를
  서버 규칙(들어가는 가장 작은 박스)대로 포장했다고 가정한다. 그 세트에 안 들어가던 출고는 절감·탄소 비교에서 뺀다.
- judge: 규제 대상 충진율 목표(fill_target_pct)와 전월 대비 개선 폭(hq_stall_gain)으로 양호·주의·효율 개선 필요
- render_hq_report: 확정 양식(templates/hq_report)으로 월별 HTML
탄소: 1톤 탑차(7.168m³ × 적재 효율 70%, 80km, 9km/L, 경유 3.44kgCO2e/L, GLEC) + 골판지(550g/m² × 491kgCO2e/t, FEFCO).
"""
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).resolve().parent / "templates" / "hq_report"))
import hq_render  # noqa: E402
import packing_server as ps  # noqa: E402

mcp = FastMCP("hq", log_level="WARNING")
A = dict(GSM=550, LOADEFF=0.70, KM=80, KMPL=9.0, EF_L=3.44, EF_BOARD=491, TRUCK=7.168)
AI_SPEC_PREFIX = ("catalog:", "custom:")       # 승인된 AI 제안으로 등록한 규격의 description


def _area(d):
    L, W, H = sorted((x / 1000 for x in d), reverse=True)
    return 2 * (L + W) * (W + H)


def _trips(m3):
    return m3 / (A["TRUCK"] * A["LOADEFF"])


def _co2(m3, board_g):
    return _trips(m3) * A["KM"] / A["KMPL"] * A["EF_L"] + board_g / 1000 * A["EF_BOARD"]


@mcp.tool()
def center_month_metrics(rows: list[dict], box_specs: list[dict]) -> dict:
    """한 센터·한 달의 실제 배정 행(list_product_fits 의 items: dims, weightG, box, qty)으로 도입 전 대비 월 성과를 계산한다.
    반환: ship(포장 출고), saving(박스비 + 택배 운임 절감 원), co2_cut(kg), trips_cut(1톤 탑차 대), vol_cut(m³), board_cut(g),
    fill_b / fill(포장 규제 대상 = 세 변 합 50cm 초과 출고 충진율 %, 도입 전 / 실제), newSpecs(AI 제안으로 등록된 규격 코드)"""
    spec = {b["code"]: b for b in box_specs}
    base = sorted((b for b in box_specs if not str(b.get("description") or "").startswith(AI_SPEC_PREFIX)),
                  key=lambda b: (ps.vol(b["dims"]), b["code"]))
    m = dict(ship=0, saving=0, vol_b=0, vol_a=0, board_b=0, board_a=0, rq_b=0, rf_b=0, rq_a=0, rf_a=0)
    for r in rows:
        b1 = spec.get(r.get("box"))
        q = r.get("qty", r.get("qty90d", 0))
        if not b1 or not q:
            continue                          # 포장 대기·비활성 규격 출고는 뺀다
        pd, wt = r["dims"], r.get("weightG")
        m["ship"] += q
        if sum(b1["dims"]) > 500:             # 실제 충진율: 포장한 출고 전부
            m["rq_a"] += q
            m["rf_a"] += q * 100 * ps.vol(pd) / ps.vol(b1["dims"])
        b0 = next((b for b in base if ps.fits(pd, b["dims"], 0)), None)
        if b0 is None:
            continue
        m["saving"] += q * ((b0["price"] + ps.ship_fee(b0["dims"], wt)) - (b1["price"] + ps.ship_fee(b1["dims"], wt)))
        for k, b in (("b", b0), ("a", b1)):
            m["vol_" + k] += q * ps.vol(b["dims"]) / 1e9
            m["board_" + k] += q * _area(b["dims"]) * A["GSM"] / 1000
        if sum(b0["dims"]) > 500:
            m["rq_b"] += q
            m["rf_b"] += q * 100 * ps.vol(pd) / ps.vol(b0["dims"])
    return {"ship": round(m["ship"]), "saving": round(m["saving"]),
            "co2_cut": round(_co2(m["vol_b"], m["board_b"]) - _co2(m["vol_a"], m["board_a"]), 1),
            "trips_cut": round(_trips(m["vol_b"]) - _trips(m["vol_a"]), 1), "vol_cut": round(m["vol_b"] - m["vol_a"], 1),
            "board_cut": round(m["board_b"] - m["board_a"]),
            "fill_b": round(m["rf_b"] / m["rq_b"], 1) if m["rq_b"] else 0, "fill": round(m["rf_a"] / m["rq_a"], 1) if m["rq_a"] else 0,
            "newSpecs": sorted(b["code"] for b in box_specs if b not in base)}


@mcp.tool()
def judge(fill: float, prev_fill: float, target_pct: float, stall_gain: float) -> dict:
    """본부 판정: 규제 대상 충진율 target 이상 = 양호 / 미달·전월 대비 +stall_gain 이상 = 주의 / 미달·그 미만 = 효율 개선 필요"""
    gain = round(fill - prev_fill, 1)
    status = "양호" if fill >= target_pct else ("주의" if gain >= stall_gain else "개선 필요")
    return {"status": status, "gain": gain}


@mcp.tool()
def render_hq_report(data: dict, out_dir: str, note: str = "", summary: list[str] | None = None,
                     recommendations: list[dict] | None = None) -> dict:
    """확정 양식으로 월별 본부 리포트 HTML 을 만든다. data = {"YYYY-MM": [센터 행, ...]} (hq_render 입력 형식),
    summary = 마지막 달 리포트 맨 위 '본부 총평' 문장들, recommendations = 그 아래 센터별 설정 추천 표(hq_report_agent.recommend)"""
    return {"paths": hq_render.render(data, out_dir, note, summary, recommendations)}
