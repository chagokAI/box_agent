"""
proposal MCP 서버 — 박스 주문 제안서 양식 제공·렌더링, 결과 저장, 과거 제안 조회

- 양식: templates/box_order_proposal.md (회사 양식 원본 그대로)
- 예시: ../sample_proposal_2026-11.md (양식대로 채운 완성본). 문장 작성 시 문체·구성 참고용으로만 쓰고 숫자는 쓰지 않는다
- 저장: BUSINESS_API_URL 이 있으면 Spring 내부 API(PUT /internal/v1/warehouses/{id}/procurement-proposals/{proposalId})로
  procurement_proposal / procurement_proposal_item 에 쓴다(DB 직접 접근 안 함). 같은 jobId는 같은 id(uuid5)로 덮어쓴다(멱등).
  save_proposal 은 데이터를 바꾸는 도구라 destructiveHint 를 달고, 에이전트는 담당자 승인(interrupt) 뒤에만 부른다.
  summary(jsonb)에는 제안 전체와 client 화면(BoxOrderView의 proposal) 모양의 clientView를 넣는다.
  procurement_proposal_item은 box_spec FK가 필요해 기존 규격 줄만 넣고, 미등록 신규 규격은 summary.newSpecs에 둔다.
  서버 계약(read-api-contracts PO-API-01~02)대로 품목에 당시 규격(box_snapshot)과 적용 단가(unit_price_krw)를 남겨
  POST purchase-orders/from-proposal 로 바로 발주서 초안을 만들 수 있게 한다. job_id 는 서버 analysis_job 용이라 비운다.
- 어느 경우든 ./proposals 폴더에 json/md 사본을 남긴다.
"""
import json
import os
import re
import uuid
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

import chagok_api as api

mcp = FastMCP("proposal", log_level="WARNING")
HERE = Path(__file__).resolve().parent
TEMPLATE_PATH = Path(os.getenv("PROPOSAL_TEMPLATE", HERE / "templates" / "box_order_proposal.md"))
SAMPLE_PATH = Path(os.getenv("PROPOSAL_SAMPLE", HERE.parent / "sample_proposal_2026-11.md"))
STORE = Path(os.getenv("PROPOSAL_DIR", HERE.parent / "proposals"))
PH = re.compile(r"\{\{(\w+)\}\}")


@mcp.tool()
def get_template(kind: str = "BOX_ORDER") -> dict:
    """회사 박스 주문 제안서 양식과 채워야 할 값 목록을 반환한다.
    표의 {{spec}} 줄·{{metric}} 줄과 {{checkItem}} 줄은 행 단위로 반복된다.
    example_md 는 양식대로 채운 예시 제안서(문체·구성 참고용, 숫자는 다른 달 값)."""
    md = TEMPLATE_PATH.read_text(encoding="utf-8")
    row_fields = sorted(set(PH.findall(next(l for l in md.splitlines() if "{{spec}}" in l))))
    effect_fields = sorted(set(PH.findall(next(l for l in md.splitlines() if "{{metric}}" in l))))
    fields = sorted(set(PH.findall(md)) - set(row_fields) - set(effect_fields) - {"checkItem"})
    sections = [l.strip() for l in md.splitlines() if l.startswith("## ")]
    example = SAMPLE_PATH.read_text(encoding="utf-8") if SAMPLE_PATH.exists() else None
    return {"kind": kind, "template_md": md, "example_md": example, "fields": fields, "row_fields": row_fields,
            "effect_fields": effect_fields,
            "list_fields": ["checkItem"], "required_sections": sections}


@mcp.tool()
def render_proposal(values: dict, rows: list[dict], check_items: list[str],
                    effect_rows: list[dict] | None = None) -> dict:
    """양식에 값을 채운다. rows는 표의 규격 행, effect_rows는 신규 규격 도입 효과 표의 행,
    check_items는 '확인 필요' 항목. 채워지지 않은 자리표시자는 missing으로 돌려준다."""
    md = TEMPLATE_PATH.read_text(encoding="utf-8")
    out = []
    for line in md.splitlines():
        if "{{spec}}" in line or "{{metric}}" in line:
            for r in (rows if "{{spec}}" in line else effect_rows or []):
                out.append(PH.sub(lambda m, r=r: str(r.get(m.group(1), m.group(0))), line))
        elif "{{checkItem}}" in line:
            for c in check_items:
                out.append(line.replace("{{checkItem}}", c))
        else:
            out.append(PH.sub(lambda m: str(values.get(m.group(1), m.group(0))), line))
    text = "\n".join(out) + "\n"
    return {"markdown": text, "missing": sorted(set(PH.findall(text)))}


@mcp.tool()
def list_past_proposals(warehouse_id: str, limit: int = 5) -> dict:
    """같은 센터의 과거 제안을 최신순으로 반환한다."""
    if api.enabled():
        try:
            rows = api.get(f"/warehouses/{warehouse_id}/procurement-proposals", limit=limit)
        except api.ApiError as e:
            raise ToolError(f"{e} — 확인: Spring 서버·INTERNAL_API_TOKEN·warehouse_id") from e
        # jobId 가 없는 행은 서버 작업(OBSERVATION_BASELINE)이 만든 참고 제안이다
        return {"items": [{"proposalId": r["proposalId"], "jobId": r["jobId"],
                           "source": "box-agent" if r["jobId"] else "server-job", "targetMonth": r["targetMonth"] or "",
                           "generatedAt": r["generatedAt"], "totalAmount": r["totalAmount"],
                           "newSpecs": r["newSpecs"] or [], "forecast": r["forecast"]} for r in rows]}
    STORE.mkdir(exist_ok=True)
    items = []
    for f in sorted(STORE.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        d = json.loads(f.read_text(encoding="utf-8"))
        if d.get("warehouseId") == warehouse_id:
            items.append({**{k: d.get(k) for k in ("proposalId", "jobId", "targetMonth", "generatedAt", "totalAmount",
                                                   "forecast")}, "source": "box-agent",
                          "newSpecs": d.get("newSpecs") or []})
    return {"items": items[:limit]}


def _snapshot(ln: dict) -> dict:
    """서버 BoxSnapshot(record) JSON 모양: 생성 시점 규격 표시 정보"""
    w, d, h = ln["dims"]
    return {"id": ln["boxSpecId"], "code": ln["boxCode"], "name": ln.get("boxName"), "sizeClass": ln.get("sizeClass"),
            "innerWidthMm": w, "innerDepthMm": d, "innerHeightMm": h}


def _save_api(pid: str, p: dict) -> int:
    """Spring 이 한 트랜잭션으로 제안 + 품목을 덮어쓴다(센터 검증·규격 소속 검증 포함)"""
    lines = [ln for ln in p["lines"] if ln.get("boxSpecId")]
    body = {"targetMonth": p["targetMonth"], "summary": p, "generatedAt": p["generatedAt"],
            "dataAsOf": p.get("inputAsOf"),
            "items": [{"boxSpecId": ln["boxSpecId"], "onHandQty": ln["currentQty"], "safetyQty": ln["safetyQty"],
                       "forecastUsageQty": ln["expectedUsage"], "proposedQty": ln["qty"],
                       "unitPriceKrw": ln["unitPriceKrw"], "boxSnapshot": _snapshot(ln)} for ln in lines]}
    try:
        return api.put(f"/warehouses/{p['warehouseId']}/procurement-proposals/{pid}", body)["itemCount"]
    except api.ApiError as e:
        raise ToolError(f"{e} — 제안을 저장하지 못함. 확인: Spring 서버·센터·규격 id") from e


@mcp.tool(annotations=ToolAnnotations(title="제안 저장", destructiveHint=True, idempotentHint=True))
def save_proposal(payload: dict) -> dict:
    """제안 결과(구조화 라인 + markdown 본문 + clientView)를 저장한다. 같은 jobId는 덮어쓴다(멱등).
    담당자 승인 뒤에만 호출한다. 내부 API 모드면 procurement_proposal(+item)에, 아니면 ./proposals 에만 저장한다."""
    STORE.mkdir(exist_ok=True)
    job = payload["jobId"]
    pid = payload.get("proposalId") or str(uuid.uuid5(uuid.NAMESPACE_URL, f"chagok:box-agent:{job}"))
    pid = str(uuid.UUID(pid))                                    # procurement_proposal.id 는 UUID
    payload = {**payload, "proposalId": pid}
    items = _save_api(pid, payload) if api.enabled() else 0
    (STORE / f"{job}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (STORE / f"{job}.md").write_text(payload["bodyMarkdown"], encoding="utf-8")
    return {"proposalId": pid, "path": str(STORE / f"{job}.md"),
            "db": {"saved": api.enabled(), "itemRows": items,
                   "summaryOnly": [ln["boxCode"] for ln in payload["lines"] if not ln.get("boxSpecId")]}}


if __name__ == "__main__":
    mcp.run()
