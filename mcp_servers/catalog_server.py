"""
box-catalog MCP 서버 — 박스왕(boxking.co.kr) 택배박스 가격 전용

구성
- refresh_catalog : 택배박스 카테고리(cateCd=011) 목록을 1페이지부터 마지막 페이지까지 수집해
                    로컬 SQLite(catalog.db)에 저장한다. (관리자/배치용 — 에이전트 그래프에서는 호출 안 함)
- search_catalog  : 저장된 데이터에서 치수 조건에 맞는 박스를 찾는다. (에이전트가 호출)
- catalog_status  : 수집 건수·마지막 수집 시각 확인

수집 규칙
- robots.txt를 매번 확인하고, 차단된 URL은 요청하지 않는다.
- 요청 간격은 최소 10초(boxking robots.txt의 Crawl-delay 10 준수).  25페이지 ≈ 4~5분.
- 실행 중 사이트를 실시간으로 긁지 않는다. 하루 1회 배치:  python catalog_server.py --refresh

가정(확인 필요)
- 상품명의 (가로 x 세로 x 높이)는 mm 단위, 내치수로 가정.
- 표시 가격은 묶음(N매) 판매가, VAT 포함으로 가정 → 개당 VAT 별도 단가로 환산해 저장.

catalog_boxes 열은 서버 box_spec 과 같은 것은 이름·양식을 맞춘다
- code, name, inner_width_mm/inner_depth_mm/inner_height_mm(상품명 표기 순서), unit_price_krw(개당 VAT 별도)
- material = '골판지 B골' 양식(client materialOptions), 골 표기가 없으면 NULL. code 도 없으면 NULL
- supplier(텍스트 키), goods_no, pack_qty, pack_price, url, collected_at 은 카탈로그 전용
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.robotparser import RobotFileParser

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

mcp = FastMCP("box-catalog", log_level="WARNING")

KST = timezone(timedelta(hours=9))
BASE_URL = os.getenv("BOXKING_BASE_URL", "https://www.boxking.co.kr")
CATEGORY = os.getenv("BOXKING_CATEGORY", "011")                   # 택배박스
USER_AGENT = os.getenv("BOXKING_USER_AGENT", "BoxSpecAgent-PriceCollector/1.0 (+ops@yourcompany.example)")
MIN_DELAY = float(os.getenv("BOXKING_MIN_DELAY", "10"))           # 초. robots Crawl-delay 10
PRICE_INCLUDES_VAT = os.getenv("BOXKING_PRICE_INCLUDES_VAT", "1") == "1"
DB_PATH = Path(os.getenv("CATALOG_DB", Path(__file__).resolve().parent.parent / "catalog.db"))
SUPPLIER = "boxking"

# "202호 (200 x 150 x 100) B골 160매", "E-16호 (300 x 200 x 150) B골 105매", "034호 (400 x 310 x 210) AB골 33매"
NAME_RE = re.compile(
    r"(?P<code>[A-Za-z0-9\-]+호)?\s*\(\s*(?P<l>\d{2,4})\s*[x×X*]\s*(?P<w>\d{2,4})\s*[x×X*]\s*(?P<h>\d{2,4})\s*\)"
    r"\s*(?P<flute>[A-Z]{1,2}골)?\s*(?P<qty>\d{1,4})\s*매")
PRICE_RE = re.compile(r"(\d{1,3}(?:,\d{3})+|\d{4,})\s*원")
GOODS_RE = re.compile(r"goods_view\.php\?.*goodsNo=(\d+)")


# ---------------------------------------------------------------- DB
def _db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS catalog_boxes (
        supplier TEXT, goods_no TEXT, code TEXT, name TEXT,
        inner_width_mm INTEGER, inner_depth_mm INTEGER, inner_height_mm INTEGER, material TEXT,
        pack_qty INTEGER, pack_price INTEGER, unit_price_krw INTEGER,
        url TEXT, collected_at TEXT, PRIMARY KEY (supplier, goods_no))""")
    _migrate_columns(con)
    con.execute("DROP TABLE IF EXISTS crawl_runs")                 # 수집 이력은 남기지 않음(collected_at 으로 확인)
    return con


def _migrate_columns(con: sqlite3.Connection) -> None:
    """예전 열 이름(l, w, h, flute, unit_price_ex_vat)으로 만든 catalog.db 를 box_spec 열 이름·양식으로 바꾼다"""
    cols = {r[1] for r in con.execute("PRAGMA table_info(catalog_boxes)")}
    if "l" not in cols:
        return
    for old, new in [("l", "inner_width_mm"), ("w", "inner_depth_mm"), ("h", "inner_height_mm"),
                     ("flute", "material"), ("unit_price_ex_vat", "unit_price_krw")]:
        con.execute(f"ALTER TABLE catalog_boxes RENAME COLUMN {old} TO {new}")
    con.execute("UPDATE catalog_boxes SET material = CASE WHEN coalesce(material, '') = '' THEN NULL "
                "ELSE '골판지 ' || material || '골' END")
    con.execute("UPDATE catalog_boxes SET code = NULL WHERE code = ''")
    con.commit()


def material_of(flute: str | None) -> str | None:
    """'B골' / 'B' → '골판지 B골' (box_spec.material 양식)"""
    f = (flute or "").replace("골", "")
    return f"골판지 {f}골" if f else None


# ---------------------------------------------------------------- 파싱
def parse_list_page(html: str, page_url: str) -> tuple[list[dict], int, int]:
    """목록 HTML → (상품 리스트, 규격 없는 상품 수, 마지막 페이지 번호).
    스킨(HTML 구조)이 바뀌어도 동작하도록, 상품 링크(goodsNo)를 기준으로
    해당 상품 하나만 감싸는 가장 바깥 요소를 찾아 그 안의 텍스트를 정규식으로 읽는다."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    items, skipped, seen = [], 0, set()
    for a in soup.select('a[href*="goodsNo="]'):
        m = GOODS_RE.search(a.get("href", ""))
        if not m or m.group(1) in seen:
            continue
        gno = m.group(1)
        box = a
        while box.parent is not None and box.parent.name not in ("body", "html", "[document]"):
            nos = {GOODS_RE.search(x.get("href", "")).group(1)
                   for x in box.parent.select('a[href*="goodsNo="]') if GOODS_RE.search(x.get("href", ""))}
            if len(nos) > 1:
                break
            box = box.parent
        seen.add(gno)
        # 정가(취소선)는 제외하고 판매가만 본다
        for s in box.select("del, strike, s, .item_cost, .org_price"):
            s.extract()
        text = " ".join(box.get_text(" ").split())
        nm, prices = NAME_RE.search(text), [int(p.replace(",", "")) for p in PRICE_RE.findall(text)]
        prices = [p for p in prices if p >= 1000]           # 적립금 등 소액 제외
        if not nm or not prices:
            skipped += 1
            continue
        qty, pack_price = int(nm["qty"]), min(prices)
        unit = pack_price / qty / (1.1 if PRICE_INCLUDES_VAT else 1)
        items.append({
            "goods_no": gno, "code": nm["code"], "name": nm.group(0),
            "dims": [int(nm["l"]), int(nm["w"]), int(nm["h"])], "material": material_of(nm["flute"]),
            "pack_qty": qty, "pack_price": pack_price, "unit_price_krw": round(unit),
            "url": urljoin(page_url, f"/goods/goods_view.php?goodsNo={gno}"),
        })
    last = 1
    for a in soup.select('a[href*="page="]'):
        q = parse_qs(urlparse(a.get("href", "")).query)
        if q.get("page", [""])[0].isdigit():
            last = max(last, int(q["page"][0]))
    return items, skipped, last


# ---------------------------------------------------------------- 수집
def _robots(client) -> RobotFileParser:
    rp = RobotFileParser()
    r = client.get(urljoin(BASE_URL, "/robots.txt"))
    rp.parse(r.text.splitlines() if r.status_code == 200 else [])
    return rp


def crawl(max_pages: int | None = None, log=print) -> dict:
    import httpx

    con = _db()
    pages = total_skipped = 0
    rows: dict[str, dict] = {}
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=20, follow_redirects=True) as c:
        rp = _robots(c)
        delay = max(MIN_DELAY, rp.crawl_delay(USER_AGENT) or 0)
        page, last = 1, 1
        while page <= last and (max_pages is None or page <= max_pages):
            url = f"{BASE_URL}/goods/goods_list.php?page={page}&cateCd={CATEGORY}"
            if not rp.can_fetch(USER_AGENT, url):
                return {"status": "BLOCKED_BY_ROBOTS", "url": url}
            for attempt in range(3):
                r = c.get(url)
                if r.status_code < 500:
                    break
                time.sleep(delay * (attempt + 1))
            r.raise_for_status()
            items, skipped, last_seen = parse_list_page(r.text, url)
            last = max(last, last_seen)
            pages += 1
            total_skipped += skipped
            for it in items:
                rows[it["goods_no"]] = it
            log(f"page {page}/{last}: {len(items)}개 수집, 규격 없음 {skipped}개")
            if not items and skipped == 0:
                break                                        # 빈 페이지 = 끝
            page += 1
            if page <= last:
                time.sleep(delay)

    now = datetime.now(KST).isoformat(timespec="seconds")
    if rows:  # 이번 수집 결과로 교체(판매 중단 상품 제거)
        con.execute("DELETE FROM catalog_boxes WHERE supplier=?", (SUPPLIER,))
        con.executemany("INSERT INTO catalog_boxes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", [
            (SUPPLIER, it["goods_no"], it["code"], it["name"], *it["dims"], it["material"],
             it["pack_qty"], it["pack_price"], it["unit_price_krw"], it["url"], now) for it in rows.values()])
    con.commit()
    return {"status": "OK" if rows else "EMPTY", "pages": pages, "items": len(rows),
            "skipped_no_dims": total_skipped, "collectedAt": now}


# ---------------------------------------------------------------- Tools
@mcp.tool(annotations=ToolAnnotations(title="박스왕 카탈로그 재수집", destructiveHint=True, openWorldHint=True))
def refresh_catalog(max_pages: int | None = None) -> dict:
    """[관리자용] 박스왕 택배박스 목록 전 페이지를 수집해 저장한다(요청 간격 10초 이상, 수 분 소요)."""
    return crawl(max_pages, log=lambda m: print(m, file=sys.stderr))


@mcp.tool()
def catalog_status() -> dict:
    """저장된 박스왕 카탈로그 건수와 마지막 수집 정보를 반환한다."""
    con = _db()
    n = con.execute("SELECT COUNT(*) FROM catalog_boxes WHERE supplier=?", (SUPPLIER,)).fetchone()[0]
    last = con.execute("SELECT max(collected_at) FROM catalog_boxes WHERE supplier=?", (SUPPLIER,)).fetchone()[0]
    return {"supplier": SUPPLIER, "items": n, "collectedAt": last}


@mcp.tool()
def search_catalog(min_inner_dims: list[int], max_volume_mm3: int, limit: int = 5,
                   exclude_refs: list[str] | None = None) -> dict:
    """회전을 고려해 min_inner_dims 이상이 들어가고 부피가 max_volume_mm3 이하인 박스왕 박스를
    부피가 작은 순으로 반환한다. 단가는 개당 VAT 별도(KRW).
    exclude_refs('boxking:<goodsNo>')는 이미 보유 규격으로 쓰는 상품이라 후보에서 뺀다."""
    skip = set(exclude_refs or [])
    need = sorted(min_inner_dims, reverse=True)
    con = _db()
    out = []
    for r in con.execute("SELECT goods_no, code, name, inner_width_mm, inner_depth_mm, inner_height_mm, material, "
                         "pack_qty, pack_price, unit_price_krw, url, collected_at FROM catalog_boxes WHERE supplier=?",
                         (SUPPLIER,)):
        dims = [r[3], r[4], r[5]]
        vol = dims[0] * dims[1] * dims[2]
        if f"{SUPPLIER}:{r[0]}" in skip:
            continue
        if all(b >= n for b, n in zip(sorted(dims, reverse=True), need)) and vol <= max_volume_mm3:
            out.append({"supplier": SUPPLIER, "goodsNo": r[0], "name": r[1] or r[2], "dims": dims, "material": r[6],
                        "moq": r[7], "packPrice": r[8], "price": r[9], "volume": vol, "url": r[10],
                        "collectedAt": r[11]})
    out.sort(key=lambda x: x["volume"])
    status = None if out else ("카탈로그가 비어 있음 — refresh_catalog 먼저 실행" if not con.execute(
        "SELECT 1 FROM catalog_boxes LIMIT 1").fetchone() else "조건에 맞는 기성 박스 없음")
    return {"items": out[:limit], "note": status}


if __name__ == "__main__":
    if "--refresh" in sys.argv:          # 배치 수집: python catalog_server.py --refresh [--max-pages N]
        mp = int(sys.argv[sys.argv.index("--max-pages") + 1]) if "--max-pages" in sys.argv else None
        print(crawl(mp))
    else:
        mcp.run()
