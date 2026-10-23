"""
packing-engine MCP 서버 — 충진율·원인 분석·군집·시뮬레이션·주문 수량 계산

AI가 직접 하면 틀리기 쉬운 계산은 전부 여기서 한다. 상태를 갖지 않으며(stateless)
입력으로 받은 데이터만 계산해서 돌려준다.

들어가는지 판정: 상품 치수 + margin_mm 를 6방향 회전으로 먼저 보고, allow_tilt 이면 안 들어가는 박스에
한 평면 기울임(Carver 조건, fits_tilted)을 적용한다. 기울임 배정은 fit="TILTED"로 표시하고 검토 대상으로 둔다.
충진율은 기울임과 관계없이 부피비(상품 부피 / 박스 부피).

운임: 경동 정기화물 운임표(templates/kyungdong_regular_freight.md, FREIGHT_TABLE)로 박스 1개씩 계산한다.
  - 50,000㎤ 이하(소화물): 소화물 표준운임(부피)만. 무게 운임은 보지 않는다
  - 50,000㎤ 초과: 경동 부피요율표(㎤)와 무게 운임(중대형화물 표준운임 → 운임표 4, kg) 중 높은 금액
  부피 = 박스 내치수(외치수 없음), 무게 = 상품 실중량(weightG, 박스 무게 없음). 무게가 없으면 부피 운임만.
  표 범위(2,000,000㎤ · 1,010kg)를 넘으면 마지막 구간 운임을 쓴다.
  FREIGHT_MODE=max 이면 표 문구 그대로 모든 크기에서 부피·무게 중 높은 금액(소화물도 무게표 최저 4,500원 적용).
"""
import bisect
import itertools
import math
import os
import re
from pathlib import Path

import numpy as np
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("packing-engine", log_level="WARNING")

FREIGHT_TABLE = Path(os.getenv("FREIGHT_TABLE", Path(__file__).resolve().parent / "templates" / "kyungdong_regular_freight.md"))


def _load_freight(path: Path) -> tuple[list, list]:
    """운임표 md → (부피 구간 [(상한 ㎤, 운임)], 무게 구간 [(상한 kg, 운임)]). '## ' 제목에 '부피'·'무게'·'운임표 4'로 나눈다"""
    vol, wt, cur = [], [], None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            cur = vol if "부피" in line else wt if ("무게" in line or "운임표 4" in line) else None
            continue
        m = re.match(r"\|\s*(.+?)이하\s*\|\s*([\d,]+)\s*\|", line)
        if cur is not None and m:
            cap = float(re.findall(r"[\d,]+", m.group(1))[-1].replace(",", ""))
            cur.append((cap, int(m.group(2).replace(",", ""))))
    vol, wt = sorted(set(vol)), sorted(set(wt))
    if not vol or not wt:
        raise RuntimeError(f"운임표를 읽지 못함: {path}")
    return vol, wt


VOL_TIERS, WT_TIERS = _load_freight(FREIGHT_TABLE)
SMALL_PARCEL_CM3 = 50_000                 # 소화물 표준운임 표의 상한
FREIGHT_MODE = os.getenv("FREIGHT_MODE", "split")   # split(기본) | max


# ---------------------------------------------------------------- 계산 함수
def sdims(d):
    return sorted(d, reverse=True)


def vol(d):
    return d[0] * d[1] * d[2]


def rect_fits(p, q, a, b):
    """Carver 조건: p×q 직사각형이 a×b 직사각형 안에 (기울여서라도) 들어가는가"""
    p, q = max(p, q), min(p, q)
    a, b = max(a, b), min(a, b)
    if p <= a and q <= b:
        return True
    d2 = p * p + q * q
    return (p > a and q <= b and d2 >= a * a
            and b >= (2 * p * q * a + (p * p - q * q) * math.sqrt(d2 - a * a)) / d2)


def fits_tilted(item, box):
    """상품 변 (p,q,r) × 박스 변 (a,b,c) 모든 순서에서 r <= c 이고 rect_fits(p,q,a,b).
    한 평면 안에서만 기울이는 보수적 판정(통과하면 실제로 들어감)"""
    return any(r <= c and rect_fits(p, q, a, b)
               for p, q, r in itertools.permutations(item) for a, b, c in itertools.permutations(box))


def fit_source(item, box, margin, allow_tilt=False, tilt_clearance=0):
    """'UPRIGHT'(6방향 회전) / 'TILTED'(기울여야 들어감, allow_tilt일 때만) / None.
    서버 FillRateCalculator 와 같은 판정: 기본 판정은 상품+margin(서버는 0), 기울임 판정은 상품+tilt_clearance"""
    it = [v + margin for v in item]
    if all(i <= b for i, b in zip(sdims(it), sdims(box))):
        return "UPRIGHT"
    if allow_tilt and fits_tilted([v + tilt_clearance for v in item], box):
        return "TILTED"
    return None


def fits(item, box, margin, allow_tilt=False, tilt_clearance=0):
    return fit_source(item, box, margin, allow_tilt, tilt_clearance) is not None


def _tier_fee(tiers, x):
    i = bisect.bisect_left([c for c, _ in tiers], x)
    return tiers[min(i, len(tiers) - 1)][1]


def ship_fee(box_dims, weight_g=None):
    """경동 정기화물 1건 운임: 소화물(50,000㎤ 이하)은 부피 운임만, 그 위는 부피·무게 운임 중 높은 금액"""
    cm3, fee = vol(box_dims) / 1000, _tier_fee(VOL_TIERS, vol(box_dims) / 1000)
    if not weight_g or (FREIGHT_MODE != "max" and cm3 <= SMALL_PARCEL_CM3):
        return fee
    return max(fee, _tier_fee(WT_TIERS, weight_g / 1000))


def best_fit(item_dims, boxes, margin, allow_tilt=False, tilt_clearance=0):
    ok = [b for b in boxes if fits(item_dims, b["dims"], margin, allow_tilt, tilt_clearance)]
    return min(ok, key=lambda b: vol(b["dims"])) if ok else None


def _simulate(products, boxes, margin, allow_tilt=False, tilt_clearance=0):
    tot_q = item_v = box_v = box_cost = ship_cost = 0
    assign = {}
    for p in products:
        b = best_fit(p["dims"], boxes, margin, allow_tilt, tilt_clearance)
        q = p["qty90d"]
        assign[p["productId"]] = b["code"]
        tot_q += q
        item_v += vol(p["dims"]) * q
        box_v += vol(b["dims"]) * q
        box_cost += b["price"] * q
        ship_cost += ship_fee(b["dims"], p.get("weightG")) * q
    return {"fill": 100 * item_v / box_v, "box_cost": box_cost, "ship_cost": ship_cost,
            "qty": tot_q, "assign": assign}


def _load(products, assign, boxes):
    """배정된 박스 기준 월(90일 ÷ 3) 적재량: 상품 부피·박스 부피(m³), 박스비(KRW, 단가 × 출고)"""
    spec = {b["code"]: b for b in boxes}
    q = item_v = box_v = cost = freight = 0
    for p in products:
        b = spec[assign[p["productId"]]]
        q += p["qty90d"]
        item_v += vol(p["dims"]) * p["qty90d"]
        box_v += vol(b["dims"]) * p["qty90d"]
        cost += b["price"] * p["qty90d"]
        freight += ship_fee(b["dims"], p.get("weightG")) * p["qty90d"]
    return {"shipments": round(q / 3), "item_m3": round(item_v / 3 / 1e9, 2), "box_m3": round(box_v / 3 / 1e9, 2),
            "box_krw": round(cost / 3), "freight_krw": round(freight / 3),
            "fill": round(100 * item_v / box_v, 1) if box_v else None}


def _reg_fill(products, assign, boxes, limit_mm=500):
    """포장 규제 대상(박스 세 변 합 > limit_mm, 포장 규칙 별표 1 비고 11) 출고의 출고량 가중 평균 충진율(%)"""
    spec = {b["code"]: b for b in boxes}
    q = f = 0
    for p in products:
        b = spec.get(assign.get(p["productId"]))
        if b and sum(b["dims"]) > limit_mm:
            q += p["qty90d"]
            f += p["qty90d"] * 100 * vol(p["dims"]) / vol(b["dims"])
    return round(f / q, 1) if q else 0.0


def _kmeans(x, k, w=None, iters=30, seeds=10):
    """출고량 가중 k-means. 시작점을 seeds 번 바꿔 가중 제곱오차가 가장 작은 결과를 쓴다"""
    w = np.ones(len(x)) if w is None else np.asarray(w, float)
    best, best_err = None, None
    for seed in range(seeds):
        rng = np.random.default_rng(seed)
        c = x[rng.choice(len(x), k, replace=False)]
        for _ in range(iters):
            lab = np.argmin(((x[:, None] - c[None]) ** 2).sum(-1), 1)
            c = np.array([np.average(x[lab == j], axis=0, weights=w[lab == j]) if (lab == j).any() else c[j]
                          for j in range(k)])
        err = float((w * ((x - c[lab]) ** 2).sum(-1)).sum())
        if best_err is None or err < best_err:
            best, best_err = lab, err
    return best


def _silhouette(x, lab, w):
    """출고량 가중 평균 실루엣 점수 (군집 개수 고르기용)"""
    k = lab.max() + 1
    if k < 2:
        return -1.0
    d = np.sqrt(((x[:, None] - x[None]) ** 2).sum(-1))
    sc = []
    for i in range(len(x)):
        same = (lab == lab[i])
        if same.sum() <= 1:
            sc.append(0.0)
            continue
        a = d[i, same & (np.arange(len(x)) != i)].mean()
        b = min(d[i, lab == j].mean() for j in range(k) if j != lab[i] and (lab == j).any())
        sc.append((b - a) / max(a, b))
    return float(np.average(sc, weights=w))


# ---------------------------------------------------------------- Tools
@mcp.tool()
def analyze_fill_rate(products: list[dict], box_specs: list[dict], margin_mm: int = 5,
                      low_fill_pct: float = 45, allow_tilt: bool = False, tilt_clearance_mm: int = 0) -> dict:
    """상품마다 들어가는 가장 작은 박스(6방향 회전, allow_tilt면 기울임 포함)를 배정하고 충진율·축별 여유를 계산한다.
    어떤 박스에도 안 들어가는 상품은 products에서 빼고 unfit으로 돌려준다."""
    out, unfit = [], []
    for p in products:
        b = best_fit(p["dims"], box_specs, margin_mm, allow_tilt, tilt_clearance_mm)
        if b is None:
            unfit.append({"productId": p["productId"], "code": p.get("code"), "dims": p["dims"], "qty90d": p["qty90d"],
                          "weightG": p.get("weightG")})
            continue
        slack = [max(0, bd - pd) for bd, pd in zip(sdims(b["dims"]), sdims(p["dims"]))]
        out.append({**p, "box": b["code"], "fill": round(100 * vol(p["dims"]) / vol(b["dims"]), 2),   # 서버와 같은 둘째 자리
                    "slack": slack, "fit": fit_source(p["dims"], b["dims"], margin_mm, allow_tilt, tilt_clearance_mm)})
    base = _simulate(out, box_specs, margin_mm, allow_tilt, tilt_clearance_mm)
    low = [p for p in out if p["fill"] < low_fill_pct]
    tilted = [{"productId": p["productId"], "code": p.get("code"), "dims": p["dims"], "box": p["box"],
               "qty90d": p["qty90d"]} for p in out if p["fit"] == "TILTED"]
    return {"products": out, "overall_fill": round(base["fill"], 1), "low_fill_skus": len(low),
            "low_fill_qty_share": round(100 * sum(p["qty90d"] for p in low) / base["qty"], 1), "unfit": unfit,
            "tilted": tilted}


@mcp.tool()
def diagnose_inefficiency(products: list[dict], box_specs: list[dict], margin_mm: int = 5,
                          low_fill_pct: float = 45) -> dict:
    """저충진 상품의 원인을 SPEC_GAP(아래 규격을 조금 넘침) / SHAPE_MISMATCH(한 축만 과도) 로 분류한다."""
    order = sorted(box_specs, key=lambda b: vol(b["dims"]))
    codes = [b["code"] for b in order]
    share = {"SPEC_GAP": 0, "SHAPE_MISMATCH": 0, "OK": 0}
    out = []
    for p in products:
        cause = "OK"
        if p["fill"] < low_fill_pct:
            idx = codes.index(p["box"])
            smaller = order[idx - 1] if idx > 0 else None
            over = ([max(0, i + margin_mm - b) for i, b in zip(sdims(p["dims"]), sdims(smaller["dims"]))]
                    if smaller else [999])
            s = sorted(p["slack"])
            if s[-1] > 3 * max(1, s[0]) and s[-1] > 100:
                cause = "SHAPE_MISMATCH"
            elif sum(over) <= 40:
                cause = "SPEC_GAP"
            else:
                cause = "SHAPE_MISMATCH"
        share[cause] += p["qty90d"]
        out.append({**p, "cause": cause})
    tot = sum(share.values())
    return {"products": out, "share_pct": {k: round(100 * v / tot, 1) for k, v in share.items()}}


@mcp.tool()
def find_misfit_clusters(products: list[dict], max_clusters: int = 5, min_clusters: int = 2) -> dict:
    """저효율 상품을 정렬 치수(로그, 형태가 반영되게)·출고량 가중 k-means 로 묶는다.
    군집 수는 min_clusters~max_clusters 중 가중 실루엣 점수가 가장 높은 값. 출고량 비중이 큰 '애매한 WDH' 구간을 반환한다."""
    mis = [p for p in products if p.get("cause", "OK") != "OK"]
    if not mis:
        return {"clusters": [], "k": 0}
    x = np.log(np.array([sdims(p["dims"]) for p in mis], float))
    w = np.array([p["qty90d"] for p in mis], float)
    k, lab, score = 1, np.zeros(len(mis), int), None
    for kk in range(min_clusters, min(max_clusters, len(mis) - 1) + 1):
        lb = _kmeans(x, kk, w)
        sc = _silhouette(x, lb, w)
        if score is None or sc > score:
            k, lab, score = kk, lb, sc
    tot = sum(p["qty90d"] for p in products)
    clusters = []
    for j in range(k):
        mem = [mis[i] for i in range(len(mis)) if lab[i] == j]
        if not mem:
            continue
        q = sum(p["qty90d"] for p in mem)
        arr = np.array([sdims(p["dims"]) for p in mem])
        by_box, by_cat = {}, {}
        for p in mem:
            by_box[p["box"]] = by_box.get(p["box"], 0) + p["qty90d"]
            by_cat[p["category"]] = by_cat.get(p["category"], 0) + p["qty90d"]
        clusters.append({
            "clusterId": "", "skus": len(mem), "qty90d": q, "share_pct": round(100 * q / tot, 1),
            "p50": np.percentile(arr, 50, 0).round().astype(int).tolist(),
            "p90": np.percentile(arr, 90, 0).round().astype(int).tolist(),
            "from_box": max(by_box, key=by_box.get),
            "avg_fill": round(sum(p["fill"] * p["qty90d"] for p in mem) / q, 1),
            "categories": sorted(by_cat, key=lambda k: -by_cat[k]),          # 출고량 많은 분류부터
            "productIds": [p["productId"] for p in mem],
        })
    clusters.sort(key=lambda c: -c["qty90d"])
    for i, c in enumerate(clusters, 1):
        c["clusterId"] = f"C{i}"
    return {"clusters": clusters, "k": len(clusters), "silhouette": round(score, 3) if score is not None else None}


@mcp.tool()
def find_sku_candidates(products: list[dict], max_fill_pct: float = 20, min_share_pct: float = 1.0,
                        limit: int = 5) -> dict:
    """군집과 별도로 볼 단일 품목: 충진율 max_fill_pct 미만이고 출고 비중 min_share_pct 이상인 상품을
    낭비 부피(박스 부피 − 상품 부피) × 출고량이 큰 순으로 limit 개. 군집과 같은 모양(SKU 1개짜리)으로 돌려준다."""
    tot = sum(p["qty90d"] for p in products) or 1
    pick = [p for p in products if p["fill"] < max_fill_pct and 100 * p["qty90d"] / tot >= min_share_pct]
    pick.sort(key=lambda p: -(vol(p["dims"]) * (100 / max(p["fill"], 0.1) - 1) * p["qty90d"]))
    out = []
    for i, p in enumerate(pick[:limit], 1):
        d = sdims(p["dims"])
        out.append({"clusterId": f"S{i}", "scope": "SKU", "skus": 1, "qty90d": p["qty90d"],
                    "share_pct": round(100 * p["qty90d"] / tot, 1), "p50": d, "p90": d, "from_box": p["box"],
                    "avg_fill": p["fill"], "categories": [p.get("category") or "미분류"], "productIds": [p["productId"]],
                    "name": p.get("name") or p.get("code")})
    return {"clusters": out}


@mcp.tool()
def ideal_box_dims(p90_dims: list[int], margin_mm: int = 5, round_to_mm: int = 5) -> dict:
    """군집 P90 치수 + 포장 여유를 round_to_mm 단위로 올려 이상 치수를 만든다."""
    return {"dims": [int(math.ceil((v + margin_mm) / round_to_mm) * round_to_mm) for v in p90_dims]}


@mcp.tool()
def coverage(products: list[dict], box_dims: list[int], margin_mm: int = 5, allow_tilt: bool = False,
             tilt_clearance_mm: int = 0) -> dict:
    """주어진 박스에 들어가는 상품의 출고량 비율(%)."""
    tot = sum(p["qty90d"] for p in products)
    ok = sum(p["qty90d"] for p in products if fits(p["dims"], box_dims, margin_mm, allow_tilt, tilt_clearance_mm))
    return {"coverage_pct": round(100 * ok / tot, 1) if tot else 0.0}


@mcp.tool()
def simulate_boxset(products: list[dict], box_specs: list[dict], candidates: list[dict], gate: dict,
                    margin_mm: int = 5, allow_tilt: bool = False, tilt_clearance_mm: int = 0,
                    fill_mode: dict | None = None) -> dict:
    """후보 박스를 하나씩 추가해 과거 출고를 재배정하고 충진율 변화·월 절감·회수기간·게이트 판정을 계산한다.
    게이트를 통과한 후보 중 군집당 월 절감액이 가장 큰 1개를 선정한다(겹치면 추가 절감 기준, marginal_saving).
    moved = 그 후보로 옮겨 가는 상품별 이전 박스·충진율 전후(anchor = 후보를 만든 군집·단일 품목의 상품).
    combined.monthly_saving = 선정 규격을 모두 넣었을 때의 월 절감 — 제안서의 예상 이익은 이 값 하나만 쓴다.
    combined.load = 선정 규격을 모두 넣었을 때 배정이 바뀌는 출고의 도입 전/후 월 적재량(_load).
    reg_fill_* = 포장 규제 대상(세 변 합 50cm 초과) 출고의 센터 전체 충진율.
    fill_mode = {"min_gain": %p, "max_extra_cost": 원} 이면 충진율 목표 모드: 절감 게이트를 못 넘어도 규제 대상 충진율을
    min_gain 이상 올리고 월 추가 비용(누적)이 max_extra_cost 안이면 선정(pass_by = "FILL"). 절감 통과 후보를 먼저 고른다."""
    base = _simulate(products, box_specs, margin_mm, allow_tilt, tilt_clearance_mm)
    base_reg = _reg_fill(products, base["assign"], box_specs)
    res = []
    for c in candidates:
        new = _simulate(products, box_specs + [{"code": c["code"], "dims": c["dims"], "price": c["price"]}],
                        margin_mm, allow_tilt, tilt_clearance_mm)
        moved = [p for p in products if new["assign"][p["productId"]] == c["code"]]
        mq = sum(p["qty90d"] for p in moved)
        if mq == 0:
            continue
        spec = {b["code"]: b for b in box_specs}
        iv = sum(vol(p["dims"]) * p["qty90d"] for p in moved)
        b_fill = 100 * iv / sum(vol(spec[base["assign"][p["productId"]]]["dims"]) * p["qty90d"] for p in moved)
        a_fill = 100 * iv / (vol(c["dims"]) * mq)
        monthly = round(((base["box_cost"] + base["ship_cost"]) - (new["box_cost"] + new["ship_cost"])) / 3)
        payback = round(c["init_cost"] / monthly, 1) if monthly > 0 else None
        g = gate[c["type"]]
        share = round(100 * mq / base["qty"], 1)
        checks = {"monthly_saving": monthly >= g["monthly_saving"], "share_pct": share >= g["share_pct"],
                  "delta_fill": a_fill - b_fill >= g["delta_fill"]}
        if c.get("scope") == "SKU":          # 단일 품목은 출고 비중 조건을 보지 않는다(그 품목 하나라 비중이 작음)
            checks.pop("share_pct")
        if "payback_m" in g:
            checks["payback_m"] = payback is not None and payback <= g["payback_m"]
        reg_a = _reg_fill(products, new["assign"], box_specs + [{"code": c["code"], "dims": c["dims"]}])
        pass_by = "SAVING" if all(checks.values()) else (
            "FILL" if fill_mode and reg_a - base_reg >= fill_mode["min_gain"] and monthly >= -fill_mode["max_extra_cost"] else None)
        anchor = set(c.get("productIds") or [])
        moved_rows = sorted(({"productId": p["productId"], "name": p.get("name") or p.get("code"), "qty90d": p["qty90d"],
                              "from": base["assign"][p["productId"]], "anchor": p["productId"] in anchor,
                              "fill_before": round(100 * vol(p["dims"]) / vol(spec[base["assign"][p["productId"]]]["dims"]), 1),
                              "fill_after": round(100 * vol(p["dims"]) / vol(c["dims"]), 1)} for p in moved),
                            key=lambda r: (not r["anchor"], -r["qty90d"]))
        res.append({"candId": c["candId"], "cluster": c["cluster"], "type": c["type"], "scope": c.get("scope", "CLUSTER"),
                    "code": c["code"], "moved": moved_rows,
                    "dims": c["dims"], "price": c["price"], "target_share_pct": share,
                    "target_fill_before": round(b_fill, 1), "target_fill_after": round(a_fill, 1),
                    "overall_fill_before": round(base["fill"], 1), "overall_fill_after": round(new["fill"], 1),
                    "monthly_saving": monthly, "init_cost": c["init_cost"], "payback_m": payback,
                    "replaces": sorted({base["assign"][p["productId"]] for p in moved}),
                    "reg_fill_before": base_reg, "reg_fill_after": reg_a,
                    "checks": checks, "pass": pass_by is not None, "pass_by": pass_by, "selected": False})
    # 군집·단일 후보는 같은 출고를 두고 겹칠 수 있다 → 절감이 큰 순으로 넣으며 '이미 고른 것 위에 더 줄어드는 만큼'으로 다시 판정
    chosen, cur = [], base
    cand_by = {c["candId"]: c for c in candidates}
    for r in sorted([r for r in res if r["pass_by"] == "SAVING"], key=lambda r: -r["monthly_saving"]):
        if any(x["cluster"] == r["cluster"] for x in chosen):
            continue                                   # 군집(단일)당 1개
        trial = _simulate(products, box_specs + [{"code": x["code"], "dims": x["dims"], "price": x["price"]}
                                                 for x in chosen + [r]], margin_mm, allow_tilt, tilt_clearance_mm)
        marginal = round(((cur["box_cost"] + cur["ship_cost"]) - (trial["box_cost"] + trial["ship_cost"])) / 3)
        g = gate[r["type"]]
        payback_ok = "payback_m" not in g or (marginal > 0 and cand_by[r["candId"]]["init_cost"] / marginal <= g["payback_m"])
        r["marginal_saving"] = marginal
        if marginal >= g["monthly_saving"] and payback_ok:
            r["selected"] = True
            chosen.append(r)
            cur = trial
    if fill_mode:                                      # 충진율 목표 모드: 규제 대상 충진율을 가장 많이 올리는 후보부터, 추가 비용 한도 안에서
        extra = 0
        for r in sorted([r for r in res if r["pass_by"] == "FILL"], key=lambda r: -(r["reg_fill_after"] - r["reg_fill_before"])):
            if any(x["cluster"] == r["cluster"] for x in chosen):
                continue
            boxes = box_specs + [{"code": x["code"], "dims": x["dims"], "price": x["price"]} for x in chosen + [r]]
            trial = _simulate(products, boxes, margin_mm, allow_tilt, tilt_clearance_mm)
            marginal = round(((cur["box_cost"] + cur["ship_cost"]) - (trial["box_cost"] + trial["ship_cost"])) / 3)
            gain = _reg_fill(products, trial["assign"], boxes) - _reg_fill(products, cur["assign"], boxes)
            r["marginal_saving"], r["marginal_reg_gain"] = marginal, round(gain, 1)
            if gain >= fill_mode["min_gain"] and extra + max(0, -marginal) <= fill_mode["max_extra_cost"]:
                r["selected"] = True
                chosen.append(r)
                cur, extra = trial, extra + max(0, -marginal)
    sel = [r for r in res if r["selected"]]
    combined = None
    if sel:
        comb = _simulate(products, box_specs + [{"code": r["code"], "dims": r["dims"], "price": r["price"]}
                                                for r in sel], margin_mm, allow_tilt, tilt_clearance_mm)
        boxes = box_specs + [{"code": r["code"], "dims": r["dims"], "price": r["price"]} for r in sel]
        moved = [p for p in products if comb["assign"][p["productId"]] != base["assign"][p["productId"]]]
        combined = {"overall_fill_before": round(base["fill"], 1), "overall_fill_after": round(comb["fill"], 1),
                    "reg_fill_before": base_reg, "reg_fill_after": _reg_fill(products, comb["assign"], boxes),
                    "monthly_saving": round(((base["box_cost"] + base["ship_cost"])
                                             - (comb["box_cost"] + comb["ship_cost"])) / 3),
                    "load": {"before": _load(moved, base["assign"], boxes), "after": _load(moved, comb["assign"], boxes),
                             "fromBoxes": sorted({base["assign"][p["productId"]] for p in moved})}}
    return {"results": res, "combined": combined}


@mcp.tool()
def compare_boxsets(products: list[dict], before_specs: list[dict], after_specs: list[dict], margin_mm: int = 5,
                    allow_tilt: bool = False, tilt_clearance_mm: int = 0) -> dict:
    """박스 세트 두 개(before → after)로 같은 출고를 배정해 비교한다. simulate_boxset 의 combined 와 같은 모양.
    load = 두 세트 모두에 들어가는데 배정이 바뀌는 출고의 월 적재량(전/후),
    newly_fit = before 에는 안 들어가고 after 에만 들어가는 출고의 월 적재량(after 기준, 맞춤 규격 효과용)."""
    def fit_all(specs):
        return [p for p in products if best_fit(p["dims"], specs, margin_mm, allow_tilt, tilt_clearance_mm)]
    both = [p for p in fit_all(after_specs) if best_fit(p["dims"], before_specs, margin_mm, allow_tilt, tilt_clearance_mm)]
    only_after = [p for p in fit_all(after_specs) if p not in both]
    base = _simulate(both, before_specs, margin_mm, allow_tilt, tilt_clearance_mm) if both else None
    new = _simulate(both + only_after, after_specs, margin_mm, allow_tilt, tilt_clearance_mm)
    moved = [p for p in both if base and new["assign"][p["productId"]] != base["assign"][p["productId"]]]
    boxes = {b["code"]: b for b in before_specs + after_specs}.values()
    boxes = list(boxes)
    after_both = _simulate(both, after_specs, margin_mm, allow_tilt, tilt_clearance_mm) if both else None
    return {"monthly_saving": round(((base["box_cost"] + base["ship_cost"]) - (after_both["box_cost"] + after_both["ship_cost"])) / 3)
            if base else 0,
            "overall_fill_before": round(base["fill"], 1) if base else None,
            "overall_fill_after": round(_simulate(both, after_specs, margin_mm, allow_tilt, tilt_clearance_mm)["fill"], 1)
            if both else None,
            "load": {"before": _load(moved, base["assign"], boxes), "after": _load(moved, new["assign"], boxes),
                     "fromBoxes": sorted({base["assign"][p["productId"]] for p in moved})} if moved else None,
            "newly_fit": {**_load(only_after, new["assign"], boxes),
                          "toBoxes": sorted({new["assign"][p["productId"]] for p in only_after})} if only_after else None}


@mcp.tool()
def check_spec_limit(products: list[dict], box_specs: list[dict], new_specs: list[dict], max_active: int,
                     margin_mm: int = 5, allow_tilt: bool = False, tilt_clearance_mm: int = 0) -> dict:
    """활성 규격 수 상한(POL-02 §3). 기존 규격은 비활성화하지 않는다(새 상품·남은 재고 대비).
    기존 + 신규가 상한을 넘으면 신규 규격을 우선순위(priority, 큰 값이 먼저 유지) 낮은 순으로 제외 후보로 돌려준다.
    share_pct = 기존 + 신규로 출고를 다시 배정했을 때 규격별 사용 비중(참고)."""
    boxes = box_specs + new_specs
    sim = _simulate(products, boxes, margin_mm, allow_tilt, tilt_clearance_mm)
    q = {}
    for p in products:
        q[sim["assign"][p["productId"]]] = q.get(sim["assign"][p["productId"]], 0) + p["qty90d"]
    tot = sum(q.values()) or 1
    over = len(boxes) - max_active
    drop = [b["code"] for b in sorted(new_specs, key=lambda b: b.get("priority", 0))[:max(over, 0)]]
    return {"count_before": len(box_specs), "count_proposed": len(boxes), "count_after": len(boxes) - len(drop),
            "max_active": max_active, "over": max(over, 0), "drop": drop,
            "share_pct": {b["code"]: round(100 * q.get(b["code"], 0) / tot, 1) for b in boxes}}


@mcp.tool()
def plan_order(products: list[dict], box_specs: list[dict], new_specs: list[dict], inventory: list[dict],
               forecast_shipments: int, days_in_month: int, safety_days: int = 7, rounding_unit: int = 100,
               margin_mm: int = 5, use_configured_safety: bool = True, allow_tilt: bool = False,
               tilt_clearance_mm: int = 0, max_stock_months: float | None = None) -> dict:
    """신규 규격을 포함한 박스 세트로 상품을 재배정해 규격별 사용 비중을 구하고,
    다음 달 예상 소모 = 예측 출고 × 비중,
    안전 재고 = inventory의 safetyQty(규격에 설정된 값, use_configured_safety) 또는 일평균 소모 × safety_days,
    제안 수량 = 예상 소모 + 안전 재고 − 현재 재고 (rounding_unit 단위 반올림, 0 이하면 0).
    max_stock_months 가 있으면 재고(현재 + 제안)가 예상 소모 × max_stock_months 를 넘는 규격에 overStock 표시(POL-04 §2)."""
    boxes = box_specs + new_specs
    sim = _simulate(products, boxes, margin_mm, allow_tilt, tilt_clearance_mm)
    qty_by = {}
    for p in products:
        c = sim["assign"][p["productId"]]
        qty_by[c] = qty_by.get(c, 0) + p["qty90d"]
    tot = sum(qty_by.values()) or 1
    inv = {i["code"]: i["availableQty"] for i in inventory}
    cfg_safety = {i["code"]: i.get("safetyQty") for i in inventory} if use_configured_safety else {}
    new_codes = {b["code"] for b in new_specs}
    rows = []
    for b in boxes:
        share = qty_by.get(b["code"], 0) / tot
        usage = round(forecast_shipments * share)
        cur = inv.get(b["code"], 0)
        if usage == 0 and cur == 0:
            continue
        safety, src = round(usage / days_in_month * safety_days), "DAYS"
        if cfg_safety.get(b["code"]):
            safety, src = cfg_safety[b["code"]], "CONFIGURED"
        need = usage + safety - cur
        qty = int(math.floor(need / rounding_unit + 0.5) * rounding_unit) if need > 0 else 0
        if need > 0 and qty == 0:
            qty = rounding_unit                              # 필요량이 있으면 최소 1단위
        rows.append({"code": b["code"], "label": b.get("label", b["code"]), "dims": b["dims"],
                     "isNew": b["code"] in new_codes, "sharePct": round(100 * share, 1), "currentQty": cur,
                     "expectedUsage": usage, "safetyQty": safety, "safetySource": src, "proposedQty": qty,
                     "unitPrice": b["price"], "amount": qty * b["price"],
                     "stockCap": round(usage * max_stock_months) if max_stock_months else None,
                     "overStock": bool(max_stock_months and usage and cur + qty > usage * max_stock_months)})
    rows.sort(key=lambda r: (not r["isNew"], -r["proposedQty"]))
    order_rows = [r for r in rows if r["proposedQty"] > 0]
    o = order_rows   # 표·합계는 주문이 필요한 규격 기준
    return {"rows": rows, "orderRows": o, "skipped": [r["code"] for r in rows if r["proposedQty"] == 0],
            "totals": {"current": sum(r["currentQty"] for r in o), "usage": sum(r["expectedUsage"] for r in o),
                       "safety": sum(r["safetyQty"] for r in o), "qty": sum(r["proposedQty"] for r in o),
                       "amount": sum(r["amount"] for r in o), "specCount": len(o)}}


if __name__ == "__main__":
    mcp.run()
