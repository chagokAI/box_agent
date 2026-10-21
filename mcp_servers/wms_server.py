"""
wms 도구 — 차곡 업무 데이터 조회 전용 (business-mcp 에 묶여 노출, 읽기만 허용)

- BUSINESS_API_URL 이 있으면 Spring Boot 내부 API(/internal/v1/**, chagok_api.py)로 읽는다. DB 에 직접 접근하지 않는다.
- 없으면 client/src/mocks 와 같은 값의 Mock 데이터를 돌려준다.
- 치수는 [가로(width), 세로(depth), 높이(height)] mm, 단가는 KRW(VAT 별도).
- API 오류는 ToolError 로 돌려주고, 메시지에 다시 시도할 때 확인할 것을 넣는다.

데이터 해석(서버 스키마에 명시가 없어 가정)
- 출고 1건 = item_event 1행. SHIPMENT_EVENT_STATUSES(쉼표 구분)를 주면 그 status만 센다.
  응답 meta.eventStatuses 에 status별 건수를 같이 주므로 중복 집계 여부를 확인할 수 있다.
- 발주 대상 월 = 연결된 procurement_proposal.target_month, 없으면 납품 희망일(requested_delivery_date)의 월.
- 발주 금액 = Σ purchase_order_item.qty × unit_price_krw
- 상품 분류 = category.name(`대분류 > 중분류`), 미매칭 이벤트(product_id 없음)는 상품별 출고량에서 빠지고 월별 건수에는 들어간다.
"""
import calendar
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from mcp.server.fastmcp import FastMCP

from mcp.server.fastmcp.exceptions import ToolError

import chagok_api as api

mcp = FastMCP("wms", log_level="WARNING")
KST = timezone(timedelta(hours=9))
EVENT_STATUSES = [s.strip() for s in os.getenv("SHIPMENT_EVENT_STATUSES", "").split(",") if s.strip()]
REJECTED_PO = {"REJECTED", "CANCELED", "CANCELLED", "반려", "취소"}


def _now() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def _call(fn, *args, **kw):
    """내부 API 호출. 실패하면 ToolError(isError) 로 돌려준다 — 메시지에 재시도 전 확인할 것을 넣는다"""
    try:
        return fn(*args, **kw)
    except api.ApiError as e:
        hint = {0: "Spring 서버 실행 여부와 BUSINESS_API_URL", 401: "INTERNAL_API_TOKEN 이 Spring 과 같은지",
                404: "warehouse_id 가 맞는지(list_warehouses 로 확인)", 400: "인자 형식(날짜 YYYY-MM-DD, 월 YYYY-MM)"}
        raise ToolError(f"{e} — 확인: {hint.get(e.status, '잠시 후 다시 시도')}") from e


def _iso(v) -> str | None:
    return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(KST).isoformat(timespec="seconds") if v else None


def _meta(wh: str) -> dict:
    st = _call(api.get, f"/warehouses/{wh}/event-stats")
    return {"servedAt": _now(), "dataAsOf": _iso(st["lastOccurredAt"]) or _now(),
            # 분석이 끝나지 않은 이벤트(ItemEventStatus.PENDING)는 상품·박스가 아직 반영되지 않았다
            "pendingEventCount": st["pendingEventCount"], "source": "chagok-api",
            "eventStatuses": st["countByStatus"], "countedStatuses": EVENT_STATUSES or "ALL"}


# ---------------------------------------------------------------- Mock 데이터 (client/src/mocks 기준)
MOCK_WAREHOUSE = {"warehouseId": "00000000-0000-0000-0000-000000000001", "name": "OO 물류센터 (이천 본센터)",
                  "address": "경기도 이천시 마장면 물류로 123, 2동 입고장"}
# DB box_spec 에 들어갈 기본 규격. 크기는 기본 규격표(가로×세로×높이 cm)를 기준으로,
# 실제 박스·단가는 박스왕 카탈로그(catalog.db)에서 가장 비슷한 상품을 고른다(내치수 가정, 단가는 개당 VAT 별도).
STANDARD_SIZES = [  # (code, size_class, 기준 크기 mm, 주요 용도)
    ("0호", "S", [170, 130, 90], "소형 화장품, 악세사리, 보조배터리"),
    ("1호", "S", [220, 190, 90], "서류, 작은 악세사리, 샘플"),
    ("2호", "M", [270, 180, 150], "화장품, 식품, 소형 의류"),
    ("2-1호", "M", [350, 250, 100], "평판형 제품, 얇은 의류, 서류 봉투"),
    ("3호", "M", [340, 250, 210], "신발, 소형 전자제품, 운동화 박스"),
    ("4호", "L", [410, 310, 280], "대형 의류, 후드티, 소형 가전"),
    ("5호", "L", [480, 380, 340], "차렵이불, 패딩, 다량 물품"),
]
CATALOG_DB = Path(os.getenv("CATALOG_DB", Path(__file__).resolve().parent.parent / "catalog.db"))
MOCK_INVENTORY = {  # (현재 재고, 안전 재고) — 임시값. 실제 보유 수량은 DB box_spec.on_hand_qty / safety_qty
    "0호": (1800, 0), "1호": (2600, 0), "2호": (3400, 0), "2-1호": (900, 0),
    "3호": (1500, 0), "4호": (700, 0), "5호": (300, 0),
}


def _closest_catalog_box(rows: list, target: list) -> tuple:
    """정렬 치수 차이 합이 가장 작은 상품, 같으면 부피 차이가 작은 상품"""
    t = sorted(target, reverse=True)
    tv = t[0] * t[1] * t[2]

    def key(r):
        b = sorted(r[3:6], reverse=True)
        return sum(abs(x - y) for x, y in zip(b, t)), abs(b[0] * b[1] * b[2] - tv)
    return min(rows, key=key)


def _standard_box_specs() -> list:
    rows = []
    if CATALOG_DB.exists():
        con = sqlite3.connect(CATALOG_DB)
        try:
            rows = con.execute("SELECT goods_no, code, name, inner_width_mm, inner_depth_mm, inner_height_mm, "
                               "material, pack_qty, unit_price_krw "
                               "FROM catalog_boxes WHERE supplier = 'boxking'").fetchall()
        except sqlite3.OperationalError:
            rows = []
        con.close()
    if not rows:
        raise RuntimeError(f"박스왕 카탈로그가 비어 있음({CATALOG_DB}) — catalog_server.py --refresh 먼저 실행")
    out = []
    for code, size_class, size, usage in STANDARD_SIZES:
        r = _closest_catalog_box(rows, size)
        dims = sorted(r[3:6], reverse=True)
        out.append({"boxSpecId": f"mock-{code}", "code": code, "name": r[1] or r[2], "sizeClass": size_class,
                    "description": f"{usage} · 박스왕 {r[2]}", "dims": dims, "price": r[8], "material": r[6],
                    "isCustom": False, "isActive": True, "supplier": "boxking", "leadTimeDays": 3,
                    "catalogRef": f"boxking:{r[0]}", "packQty": r[7], "standardSize": size,
                    "safetyQty": MOCK_INVENTORY[code][1]})
    return out


MOCK_PURCHASE_ORDERS = [  # mocks/orders.js purchaseOrders + 제안서 비교(2025.11 ₩9,120,000)
    ("PO-2025-1101", "2025-10-20", "2025-11-03", "2025-11", True, 9_120_000, "입고 완료", "OO 포장산업"),
    ("PO-2026-0818", "2026-08-18", "2026-08-22", None, False, 1_456_000, "입고 완료", "OO 포장산업"),
    ("PO-2026-0901", "2026-09-01", "2026-09-05", "2026-09", True, 9_210_000, "입고 완료", "OO 포장산업"),
    ("PO-2026-0915", "2026-09-15", "2026-09-19", None, False, 1_845_000, "입고 완료", "△△ 박스"),
    ("PO-2026-0924", "2026-09-24", None, None, False, 1_140_000, "반려", "□□ 패키징"),
    ("PO-2026-1001", "2026-10-01", "2026-10-06", "2026-10", True, 9_780_000, "입고 완료", "OO 포장산업"),
    ("PO-2026-1005", "2026-10-05", "2026-10-10", None, False, 1_736_000, "입고 완료", "△△ 박스"),
    ("PO-2026-1012", "2026-10-12", "2026-10-17", None, False, 2_048_000, "배송 중", "OO 포장산업"),
]
MOCK_META = {"servedAt": "2026-10-19T15:00:00+09:00", "dataAsOf": "2026-10-19T15:00:00+09:00",
             "pendingEventCount": 0, "source": "mock"}


def _mock_specs(active_only: bool) -> list:
    return _standard_box_specs()


def _mock_products(seed: int = 7) -> list:
    rng = np.random.default_rng(seed)
    items = []

    def add(n, prefix, name, mean, sd, qty_range, cat):
        for _ in range(n):
            d = np.maximum(20, rng.normal(mean, sd)).round().astype(int).tolist()
            code = f"{prefix}-{10000 + len(items)}"
            items.append({"productId": f"mock-{code}", "code": code, "name": name, "category": cat,
                          "dims": d, "qty90d": int(rng.integers(*qty_range)), "matchedBox": None,
                          "fillRatePct": None})

    add(38, "BT", "화장품 세트", [212, 146, 50], [4, 3, 3], (300, 1400), "뷰티")
    add(22, "LV", "접이식 우산", [370, 85, 70], [10, 5, 5], (150, 500), "생활용품")
    add(70, "EL", "무선 이어폰 세트", [140, 95, 90], [5, 3, 3], (150, 900), "전자기기")
    add(40, "OD", "캠핑 랜턴", [280, 230, 130], [6, 5, 5], (80, 380), "아웃도어")
    add(30, "FS", "스니커즈", [330, 210, 120], [6, 5, 5], (80, 450), "패션")
    add(10, "SP", "요가 매트", [610, 150, 150], [5, 4, 4], (60, 200), "스포츠")
    return items


def _mock_monthly_shipments(end_month: str, months: int) -> list:
    """추세(연 8%) × 계절성 × 소음으로 만든 월별 출고 건수 (11월 예측 ≈ 35,000건)"""
    season = {1: .92, 2: .88, 3: .95, 4: .97, 5: 1.0, 6: .98, 7: .97, 8: .98, 9: 1.0, 10: 1.03, 11: 1.18, 12: 1.22}
    rng = np.random.default_rng(11)
    y, m = map(int, end_month.split("-"))
    out = []
    for i in range(months - 1, -1, -1):
        yy, mm = y, m - i
        while mm <= 0:
            yy, mm = yy - 1, mm + 12
        val = 25000 * (1.08 ** ((months - 1 - i) / 12)) * season[mm] * (1 + rng.normal(0, 0.02))
        out.append({"month": f"{yy:04d}-{mm:02d}", "shipments": int(round(val))})
    return out


# ---------------------------------------------------------------- Tools
@mcp.tool()
def list_warehouses() -> dict:
    """센터(warehouse) 목록을 반환한다."""
    if api.enabled():
        return {"items": [{"warehouseId": r["warehouseId"], "name": r["name"], "address": r["address"]}
                          for r in _call(api.get, "/warehouses")]}
    return {"items": [MOCK_WAREHOUSE]}


def _spec_rows(warehouse_id: str, active_only: bool) -> list:
    return _call(api.get, f"/warehouses/{warehouse_id}/box-specs", activeOnly=str(active_only).lower())


@mcp.tool()
def list_box_specs(warehouse_id: str, active_only: bool = True) -> dict:
    """센터가 보유한 박스 규격 목록(내치수 [가로, 세로, 높이] mm, 단가 KRW VAT 별도)을 반환한다.
    치수·단가가 비어 있는 규격은 계산에 쓸 수 없어 skipped로 따로 준다."""
    if not api.enabled():
        return {"items": _mock_specs(active_only), "skipped": [], "meta": MOCK_META}
    items, skipped = [], []
    for r in _spec_rows(warehouse_id, active_only):
        dims = [r["innerWidthMm"], r["innerDepthMm"], r["innerHeightMm"]]
        if None in dims or r["unitPriceKrw"] is None or not r["code"]:
            skipped.append(r["code"] or r["boxSpecId"])
            continue
        items.append({"boxSpecId": r["boxSpecId"], "code": r["code"], "name": r["name"],
                      "description": r["description"], "sizeClass": r["sizeClass"], "dims": dims,
                      "price": r["unitPriceKrw"], "material": r["material"], "isCustom": r["isCustom"],
                      "isActive": r["isActive"], "supplier": r["supplier"], "leadTimeDays": r["leadTimeDays"],
                      "safetyQty": r["safetyQty"]})
    return {"items": items, "skipped": skipped, "meta": _meta(warehouse_id)}


@mcp.tool()
def list_product_fits(warehouse_id: str, from_date: str, to_date: str) -> dict:
    """상품별 치수([가로, 세로, 높이] mm)와 기간(from_date~to_date, YYYY-MM-DD, KST) 내 출고 수량(qty90d)을 반환한다.
    치수가 없거나 기간 내 출고가 없는 상품은 제외하고 건수만 알려 준다."""
    if not api.enabled():
        return {"items": _mock_products(), "excluded": {"noDims": 0, "noShipments": 0}, "meta": MOCK_META,
                "period": {"from": from_date, "to": to_date}}
    rows = _call(api.get, f"/warehouses/{warehouse_id}/product-shipments", **{"from": from_date, "to": to_date},
                 statuses=EVENT_STATUSES or None)
    items, no_dims, no_ship = [], 0, 0
    for r in rows:
        dims = [r["widthMm"], r["depthMm"], r["heightMm"]]
        if None in dims or min(dims) <= 0:
            no_dims += 1
            continue
        if r["shipments"] == 0:
            no_ship += 1
            continue
        items.append({"productId": r["productId"], "code": r["code"], "name": r["name"],
                      "category": r["category"] or "미분류", "dims": dims, "qty90d": r["shipments"],
                      "matchedBox": r["matchedBox"], "fillRatePct": r["fillRatePct"]})
    return {"items": items, "excluded": {"noDims": no_dims, "noShipments": no_ship},
            "meta": _meta(warehouse_id), "period": {"from": from_date, "to": to_date}}


@mcp.tool()
def get_box_inventory(warehouse_id: str) -> dict:
    """규격별 현재 재고(box_spec.on_hand_qty)와 설정된 안전 재고(safety_qty)를 반환한다."""
    if not api.enabled():
        return {"items": [{"code": k, "availableQty": v[0], "safetyQty": v[1]} for k, v in MOCK_INVENTORY.items()],
                "meta": MOCK_META}
    return {"items": [{"code": r["code"], "availableQty": r["onHandQty"] or 0, "safetyQty": r["safetyQty"],
                       "stockUpdatedAt": r["stockUpdatedAt"]}
                      for r in _spec_rows(warehouse_id, False) if r["code"]], "meta": _meta(warehouse_id)}


@mcp.tool()
def get_shipment_history(warehouse_id: str, end_month: str, months: int = 27) -> dict:
    """end_month(YYYY-MM, 포함)까지 months개월의 월별 출고 건수(KST 기준)를 반환한다. 출고가 없는 달은 빠진다.
    진행 중인 달은 경과 일수로 한 달치를 환산하고 actual(실제 건수)·partial 을 같이 준다."""
    if not api.enabled():
        return {"items": _mock_monthly_shipments(end_month, months), "meta": MOCK_META}
    y, m = map(int, end_month.split("-"))
    sm = y * 12 + (m - 1) - (months - 1)
    rows = _call(api.get, f"/warehouses/{warehouse_id}/monthly-shipments",
                 fromMonth=f"{sm // 12:04d}-{sm % 12 + 1:02d}", toMonth=end_month, statuses=EVENT_STATUSES or None)
    items = [{"month": r["month"], "shipments": r["shipments"]} for r in rows]
    now = datetime.now(KST)
    cur = f"{now.year:04d}-{now.month:02d}"
    for it in items:                       # 진행 중인 달은 경과 일수로 한 달치 환산 (예측이 낮게 나오는 것 방지)
        if it["month"] == cur:
            elapsed = (now - datetime(now.year, now.month, 1, tzinfo=KST)).total_seconds() / 86400
            dim = calendar.monthrange(now.year, now.month)[1]
            it.update({"actual": it["shipments"], "partial": True, "daysCovered": round(elapsed, 1),
                       "shipments": round(it["shipments"] * dim / max(elapsed, 1))})
    return {"items": items, "meta": _meta(warehouse_id)}


@mcp.tool()
def list_purchase_orders(warehouse_id: str, from_date: str, to_date: str) -> dict:
    """발주일(KST, YYYY-MM-DD)이 기간 내인 박스 발주서 목록. 금액은 VAT 별도 KRW.
    fromProposal=True 는 AI 제안서에서 만든 정기 발주(purchase_order.proposal_id 연결)."""
    if not api.enabled():
        items = [{"purchaseOrderId": f"mock-{no}", "poNo": no, "orderedAt": od, "deliveryDate": dd,
                  "targetMonth": tm or (dd or od)[:7], "fromProposal": fp, "amountKrw": amt, "status": st,
                  "supplier": sup, "rejected": st in REJECTED_PO}
                 for no, od, dd, tm, fp, amt, st, sup in MOCK_PURCHASE_ORDERS if from_date <= od <= to_date]
        return {"items": items, "meta": MOCK_META}
    items = []
    for r in _call(api.get, f"/warehouses/{warehouse_id}/purchase-orders", **{"from": from_date, "to": to_date}):
        od = (_iso(r["createdAt"]) or "")[:10] or None
        dd = r["requestedDeliveryDate"]
        items.append({"purchaseOrderId": r["purchaseOrderId"], "poNo": r["poNo"], "orderedAt": od,
                      "deliveryDate": dd, "targetMonth": r["proposalTargetMonth"] or (dd or od or "")[:7],
                      "fromProposal": r["fromProposal"], "amountKrw": r["amountKrw"], "status": r["status"],
                      "supplier": r["supplier"], "rejected": (r["status"] or "").upper() in REJECTED_PO
                      or r["status"] in REJECTED_PO})
    return {"items": items, "meta": _meta(warehouse_id)}


if __name__ == "__main__":
    mcp.run()  # stdio
