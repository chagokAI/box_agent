"""
충진율 계산 일치 확인 — 에이전트(packing_server)가 서버 상품 매칭(FillRateCalculator)과 같은 박스·충진율을 고르는지 본다.

서버가 저장한 상품별 매칭 박스·충진율(product.matched_box_spec_id, fill_rate_pct)을 내부 API 로 읽고,
같은 활성 규격·같은 정책(GET /internal/v1/matching-policy)으로 에이전트가 다시 계산해 비교한다.
서버 매칭 박스가 지금 비활성이면(보유 규격이 아니면) 비교에서 뺀다.

실행: .venv/bin/python tests/parity_eval.py   (Spring 내부 API 가 떠 있어야 함)
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "mcp_servers"))
from box_spec_agent import load_env  # noqa: E402

load_env()
import packing_server  # noqa: E402
import wms_server  # noqa: E402

WH = os.getenv("EVAL_WAREHOUSE_ID", "2b7424cf-37a9-5119-98d7-a23142cbb251")


def main() -> int:
    pol = wms_server.get_matching_policy()
    specs = wms_server.list_box_specs(WH)["items"]
    prods = wms_server.list_product_fits(WH, "2000-01-01", "2100-12-31")["items"]
    active = {b["code"] for b in specs}
    r = packing_server.analyze_fill_rate(prods, specs, margin_mm=0, allow_tilt=pol["tiltedEnabled"],
                                         tilt_clearance_mm=pol["tiltedClearanceMm"])
    got = {p["productId"]: p for p in r["products"]}
    same = diff = skipped = 0
    for p in prods:
        if not p["matchedBox"] or p["matchedBox"] not in active:
            skipped += 1
            continue
        a = got.get(p["productId"])
        ok = a and a["box"] == p["matchedBox"] and abs(a["fill"] - (p["fillRatePct"] or 0)) < 0.01
        if ok:
            same += 1
        else:
            diff += 1
            print(f"✗ {p['name']}: 서버 {p['matchedBox']} {p['fillRatePct']}% / 에이전트 "
                  f"{a and a['box']} {a and a['fill']}%")
    print(f"정책: 기울임 {pol['tiltedEnabled']} 여유 {pol['tiltedClearanceMm']}mm · 활성 규격 {len(specs)}종")
    print(f"일치 {same} / 불일치 {diff} / 제외(서버 매칭 박스 비활성·미매칭) {skipped}")
    return 1 if diff else 0


if __name__ == "__main__":
    sys.exit(main())
