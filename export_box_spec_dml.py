"""
박스왕 카탈로그(catalog.db) → 서버 PostgreSQL box_spec 시드 DML

wms Mock 과 같은 기본 규격(STANDARD_SIZES 0호~5호)을 카탈로그에서 가장 비슷한 상품으로 골라
warehouse / supplier(박스왕) / box_spec INSERT 를 만든다. 열은 서버 JPA 엔티티(BoxSpec, Supplier) 그대로.

- id = uuid5 → 같은 창고로 다시 돌려도 같은 id 라 ON CONFLICT 로 덮어씀(멱등)
- warehouse 는 없을 때만 넣는다(이미 있으면 그대로 둠)
- safety_qty 는 NULL(미설정 → 에이전트가 일평균 소모 × 7일로 계산), on_hand_qty 는 Mock 재고값

--all : 기본 규격 대신 카탈로그 전체(catalog_boxes)를 box_spec 으로 넣는다(sql/box_spec_catalog.sql)
- code = 박스왕 품번(202호 등), id = uuid5(창고, goodsNo), on_hand_qty = 0
- size_class 는 부피로 나눔: 1호(220×190×90) 이하 S, 3호(340×250×210) 이하 M, 그 위 L
- is_active = false (보유 규격이 아닌 구매 가능 목록이라, 에이전트·재고 화면에서는 빠짐). --active 로 true

  python export_box_spec_dml.py                                  # Mock 창고 id, sql/box_spec_seed.sql 로 저장
  python export_box_spec_dml.py --warehouse-id <UUID> -o out.sql
  python export_box_spec_dml.py --all [--active]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "mcp_servers"))

from wms_server import CATALOG_DB, MOCK_INVENTORY, MOCK_WAREHOUSE, _standard_box_specs  # noqa: E402

SUPPLIER_NAME = "박스왕"
SUPPLIER_LEAD_TIME_DAYS = 3
SIZE_CLASS_MAX_VOLUME = [("S", 220 * 190 * 90), ("M", 340 * 250 * 210)]  # 그 위는 L


def q(v) -> str:
    """SQL 리터럴"""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def size_class_of(dims: list[int]) -> str:
    vol = dims[0] * dims[1] * dims[2]
    return next((c for c, v in SIZE_CLASS_MAX_VOLUME if vol <= v), "L")


def standard_rows(warehouse_id: str) -> list[dict]:
    return [{"id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"chagok:box_spec:{warehouse_id}:{b['code']}")),
             "code": b["code"], "name": b["name"], "sizeClass": b["sizeClass"], "dims": b["dims"],
             "material": b["material"], "price": b["price"], "isActive": True, "description": b["description"],
             "onHandQty": MOCK_INVENTORY[b["code"]][0]} for b in _standard_box_specs()]


def catalog_rows(warehouse_id: str, active: bool) -> list[dict]:
    con = sqlite3.connect(CATALOG_DB)
    rows = con.execute("SELECT goods_no, code, name, inner_width_mm, inner_depth_mm, inner_height_mm, material, "
                       "unit_price_krw, url FROM catalog_boxes WHERE supplier = 'boxking' "
                       "ORDER BY inner_width_mm * inner_depth_mm * inner_height_mm").fetchall()
    con.close()
    if not rows:
        raise RuntimeError(f"박스왕 카탈로그가 비어 있음({CATALOG_DB}) — catalog_server.py --refresh 먼저 실행")
    return [{"id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"chagok:box_spec:{warehouse_id}:boxking:{gno}")),
             "code": code, "name": code, "sizeClass": size_class_of([w, d, h]), "dims": [w, d, h],
             "material": material, "price": price, "isActive": active, "description": f"박스왕 {name} · {url}",
             "onHandQty": 0} for gno, code, name, w, d, h, material, price, url in rows]


def build(warehouse_id: str, specs: list[dict]) -> str:
    supplier_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"chagok:supplier:{warehouse_id}:boxking"))
    lines = [
        "-- 박스왕 카탈로그 기반 box_spec 시드 (export_box_spec_dml.py 로 생성)",
        "BEGIN;",
        "",
        "INSERT INTO warehouse (id, name, address)",
        f"VALUES ({q(warehouse_id)}::uuid, {q(MOCK_WAREHOUSE['name'])}, {q(MOCK_WAREHOUSE['address'])})",
        "ON CONFLICT (id) DO NOTHING;",
        "",
        "INSERT INTO supplier (id, warehouse_id, name, payment_terms, lead_time_days)",
        f"VALUES ({q(supplier_id)}::uuid, {q(warehouse_id)}::uuid, {q(SUPPLIER_NAME)}, NULL, {SUPPLIER_LEAD_TIME_DAYS})",
        "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, lead_time_days = EXCLUDED.lead_time_days;",
        "",
        "INSERT INTO box_spec (id, warehouse_id, code, name, size_class,",
        "                      inner_width_mm, inner_depth_mm, inner_height_mm, material,",
        "                      supplier_id, unit_price_krw, is_custom, is_active, description,",
        "                      on_hand_qty, safety_qty, target_qty, days_to_stockout,",
        "                      stock_updated_at, forecast_updated_at, safety_days, alert_enabled, version)",
        "VALUES",
    ]
    values = []
    for b in specs:
        w, d, h = b["dims"]
        values.append(
            f"    ({q(b['id'])}::uuid, {q(warehouse_id)}::uuid, {q(b['code'])}, {q(b['name'])}, {q(b['sizeClass'])},\n"
            f"     {w}, {d}, {h}, {q(b['material'])},\n"
            f"     {q(supplier_id)}::uuid, {b['price']}, false, {q(b['isActive'])}, {q(b['description'])},\n"
            f"     {b['onHandQty']}, NULL, NULL, NULL,\n"
            f"     now(), NULL, NULL, true, 0)")
    lines.append(",\n".join(values))
    lines += [
        "ON CONFLICT (id) DO UPDATE SET",
        "    code = EXCLUDED.code, name = EXCLUDED.name, size_class = EXCLUDED.size_class,",
        "    inner_width_mm = EXCLUDED.inner_width_mm, inner_depth_mm = EXCLUDED.inner_depth_mm,",
        "    inner_height_mm = EXCLUDED.inner_height_mm, material = EXCLUDED.material,",
        "    supplier_id = EXCLUDED.supplier_id, unit_price_krw = EXCLUDED.unit_price_krw,",
        "    description = EXCLUDED.description, version = box_spec.version + 1;",
        "",
        "COMMIT;",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--warehouse-id", default=MOCK_WAREHOUSE["warehouseId"])
    ap.add_argument("--all", action="store_true", help="카탈로그 전체를 box_spec 으로")
    ap.add_argument("--active", action="store_true", help="--all 일 때 is_active = true")
    ap.add_argument("-o", "--output")
    a = ap.parse_args()
    specs = catalog_rows(a.warehouse_id, a.active) if a.all else standard_rows(a.warehouse_id)
    out = Path(a.output or HERE / "sql" / ("box_spec_catalog.sql" if a.all else "box_spec_seed.sql"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build(a.warehouse_id, specs), encoding="utf-8")
    print(f"저장: {out}")
