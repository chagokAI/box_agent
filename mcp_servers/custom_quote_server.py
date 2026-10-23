"""
custom-quote — 맞춤 제작 박스 참고 견적 (유팩 upackage.co.kr 골판지 박스 견적 계산기)

- quote_custom_box : 내치수·수량으로 유팩 견적 계산기(화면이 쓰는 내부 엔드포인트)에 물어
                     수량 구간별 단가표, 요청 수량 단가, 판비·목형비, 납기 예정일을 돌려준다.
- 고정 사양: A형(일반 상자), B골, 기성품 재질(SK / S.K), 무인쇄, 직접 수령, 내경 기준.
- 공식 API 가 아니므로 사이트가 바뀌면 실패한다 → ToolError 대신 {ok: false, note} 를 돌려주고
  에이전트는 '담당자 견적 필요'로 처리한다.
- robots.txt 를 확인하고 요청 간격은 최소 10초(UPACKAGE_MIN_DELAY). 같은 사양·수량은 하루 캐시
  (UPACKAGE_CACHE_DB, 기본 catalog.db 의 custom_quotes 테이블). 캐시를 못 읽거나 못 써도 견적은 돌려준다.
- 가격은 화면 표기대로 부가세 별도.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.robotparser import RobotFileParser

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("custom-quote", log_level="WARNING")

KST = timezone(timedelta(hours=9))
BASE_URL = os.getenv("UPACKAGE_BASE_URL", "https://www.upackage.co.kr")
PAGE = "/goods/ordermade_estimate_carton.asp?boxType=A"
ENDPOINT = "/goods/ordermade_estimate_carton_ajax.asp"
USER_AGENT = os.getenv("UPACKAGE_USER_AGENT", "BoxSpecAgent-QuoteChecker/1.0 (+ops@yourcompany.example)")
MIN_DELAY = float(os.getenv("UPACKAGE_MIN_DELAY", "10"))
CACHE_HOURS = float(os.getenv("UPACKAGE_CACHE_HOURS", "24"))
ENABLED = os.getenv("CUSTOM_QUOTE", "1") != "0"
DB_PATH = Path(os.getenv("UPACKAGE_CACHE_DB") or os.getenv("CATALOG_DB")
               or Path(__file__).resolve().parent.parent / "catalog.db")
SUPPLIER = "upackage"
SPEC = {"item_type": "A", "item_thick": "B", "item_material_out": ":SK:", "item_material_in": ":S::K:"}
SPEC_LABEL = "A형 B골 SK/S.K 무인쇄"

_last_request = 0.0


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS custom_quotes (key TEXT PRIMARY KEY, body TEXT NOT NULL, "
                "quoted_at TEXT NOT NULL)")
    return con


def _num(s) -> float:
    return float(str(s).replace(",", "").strip() or 0)


def _tiers(d: dict) -> list[dict]:
    """section1~8: '211 ~ 600' / '410~' → [{minQty, maxQty, unitPrice}] (불가·기본 구간은 뺀다)"""
    out = []
    for i in range(1, 9):
        rng, price = d.get(f"section{i}_string", ""), d.get(f"section{i}_price", "")
        if not rng or price in ("basic", "unable", ""):
            continue
        lo, _, hi = rng.partition("~")
        out.append({"minQty": int(_num(lo)) if lo.strip() else 1, "maxQty": int(_num(hi)) if hi.strip() else None,
                    "unitPrice": int(_num(price.rstrip("~")))})
    return out


def _request(dims: list[int], qty: int) -> dict:
    global _last_request
    import httpx

    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=20) as c:
        rp = RobotFileParser()
        r = c.get(f"{BASE_URL}/robots.txt")
        rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        if not rp.can_fetch(USER_AGENT, BASE_URL + ENDPOINT):
            raise RuntimeError("robots.txt 가 견적 경로를 막음")
        wait = _last_request + MIN_DELAY - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        garo, sero, height = dims
        r = c.post(BASE_URL + ENDPOINT, headers={"Referer": BASE_URL + PAGE}, data={
            "mode": "KEEPCNT", "item_garo": garo, "item_sero": sero, "item_height": height, **SPEC,
            "orderCount": qty, "countSetting": "USER", "printYn": "N", "dlvry_type": "self",
            "itemStrapColor": "NONE", "reorderYn": "N", "makeOptSpeed": "N", "sizeBasis": "IN"})
        _last_request = time.monotonic()
        r.raise_for_status()
        return json.loads(r.content.decode("cp949", errors="replace"))


@mcp.tool()
def quote_custom_box(dims: list[int], qty: int) -> dict:
    """맞춤 제작 박스(A형 B골 무인쇄, 내치수 mm 가로·세로·높이) qty 개의 참고 견적을 유팩 견적 계산기에서 받는다.
    unitPrice = 요청 수량의 개당 단가(VAT 별도), initCost = 판비 + 목형비, tiers = 수량 구간별 단가.
    실패하면 ok=false 와 note(담당자 견적 필요)를 돌려준다."""
    dims, qty = [int(v) for v in dims], max(1, int(qty))
    base = {"supplier": SUPPLIER, "spec": SPEC_LABEL, "dims": dims, "qty": qty, "vatIncluded": False,
            "url": BASE_URL + PAGE}
    if not ENABLED:
        return {**base, "ok": False, "note": "맞춤 견적 비활성(CUSTOM_QUOTE=0) — 담당자 견적 필요"}
    key = f"{SPEC_LABEL}|{'x'.join(map(str, dims))}|{qty}"
    try:
        row = _db().execute("SELECT body, quoted_at FROM custom_quotes WHERE key=?", (key,)).fetchone()
    except sqlite3.Error:
        row = None
    if row and datetime.fromisoformat(row[1]) > datetime.now(KST) - timedelta(hours=CACHE_HOURS):
        return {**json.loads(row[0]), "cached": True}
    try:
        d = _request(dims, qty)
    except Exception as e:
        return {**base, "ok": False, "note": f"유팩 견적 실패({type(e).__name__}: {str(e)[:100]}) — 담당자 견적 필요"}
    if d.get("result") != "success":
        return {**base, "ok": False, "note": f"유팩 견적 불가({d.get('Message') or 'result=' + str(d.get('result'))}) "
                                             f"— 담당자 견적 필요"}
    out = {**base, "ok": True, "unitPrice": round(_num(d["orderItemUnitPrice"])),
           "unitPriceExact": _num(d["orderItemUnitPrice"]), "amount": int(_num(d["orderItemPrice"])),
           "initCost": int(_num(d.get("print_plate_price")) + _num(d.get("orderItemFramePrice"))),
           "tiers": _tiers(d), "leadDate": d.get("dlvry_exp_date"), "fastLeadDate": d.get("dlvry_exp_date_fast"),
           "quotedAt": datetime.now(KST).isoformat(timespec="seconds")}
    try:
        with _db() as con:
            con.execute("INSERT OR REPLACE INTO custom_quotes VALUES (?,?,?)",
                        (key, json.dumps(out, ensure_ascii=False), out["quotedAt"]))
    except sqlite3.Error as e:              # 읽기 전용 위치 등: 캐시 없이 진행
        print(f"custom_quotes 캐시 저장 생략 ({e})", file=sys.stderr)
    return out


if __name__ == "__main__":
    if len(sys.argv) >= 5 and sys.argv[1] == "--quote":     # python custom_quote_server.py --quote 300 200 150 1000
        print(json.dumps(quote_custom_box([int(v) for v in sys.argv[2:5]], int(sys.argv[5]) if len(sys.argv) > 5 else 1000),
                         ensure_ascii=False, indent=2))
    else:
        mcp.run()
