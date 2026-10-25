"""본부 통합 효율화 리포트(확정 양식, 2026-10-23) — 센터별·월별 지표 JSON → 월별 리포트 HTML.
실행: hq_render.py <out dir> <hq_data.json> [상단 표시 문구, 예: 목업 센터]   (스타일·차트: 같은 폴더 hq_style.css, hq_chart.js)
코드에서: render(data, out_dir, note) → 만든 파일 경로 목록 (본부 리포트 에이전트 hq_server.render_hq_report 가 부른다)

입력 {"YYYY-MM": [센터 행, ...], ...} — 센터 행(월 기준, 도입 전 = 효율화 시작 전 보유 규격으로 포장했을 때 대비):
  center, ship(월 출고), saving(월 절감 원), co2_cut(월 탄소 감축 kg), trips_cut, vol_cut(m³), board_cut(g),
  fill_b / fill(포장 규제 대상 = 박스 세 변 합 50cm 초과 출고 충진율 %, 도입 전 / 이번 달 최종),
  status(양호|주의|개선 필요 — 재실행까지 끝낸 최종 값 vs 전월), gain(전월 대비 %p), prev_fill, approved,
  rerun(None | {new, fill_before, fill_after, saving_change}), applied, applied_cum, low_gate(신규 규격 기준 하향 여부)
요약: 월 절감액 = 도입 전 대비 월 수준(전월까지 개선 + 이번 달 개선), 연간 추정 = 일평균 × 365, 괄호 = 이번 달(전월 대비). 월 값은 한 달 = 30일 기준.
금액은 요약에서만 천 원 단위 반올림 표시(r3), 데이터는 원 단위."""
import calendar, json, math, sys
from pathlib import Path

OUT, NOTE, SUMMARY, RECO = Path("."), "", None, None         # render() 가 정한다(출력 폴더, 상단 표시 문구, 마지막 달 본부 총평)
PINE_KG = 6.6   # 30년생 소나무 1그루 연간 CO2 흡수량(kg) — 국립산림과학원 발표(판본에 따라 다름)


data, MONTHS, FIRST = {}, [], ""
DAYS30, DAYS365 = 30, 365


def _load(data_in: dict) -> None:
    """월 값은 한 달 = 30일로 맞춘다(달마다 일수가 달라 생기는 증감 제거). 누적은 실제 합계를 쓴다"""
    global data, MONTHS, FIRST
    data = json.loads(json.dumps(data_in))
    MONTHS = list(data)
    for _m, _rows in data.items():
        _dim = calendar.monthrange(int(_m[:4]), int(_m[5:]))[1]
        for _d in _rows:
            _d.setdefault("saving_act", _d["saving"]); _d.setdefault("co2_act", _d["co2_cut"])
            _d["saving"] = round(_d["saving_act"] / _dim * DAYS30)
            _d["co2_cut"] = round(_d["co2_act"] / _dim * DAYS30, 1)
            _d["ship30"] = _d["ship"] / _dim * DAYS30
    FIRST = f"{int(MONTHS[0][5:])}월" if MONTHS else ""


won = lambda v: f"{round(v):,}원"


def kwon(v, sign=False):
    """카드용 짧은 금액: 1억 이상 'n.n억 원', 1만 이상 'n,nnn만 원' (정확한 값은 title 로)"""
    a, pm = abs(v), ("+" if v >= 0 else "−") if sign else ("−" if v < 0 else "")
    body = f"{a / 1e8:,.1f}억 원" if a >= 1e8 else f"{a / 1e4:,.0f}만 원" if a >= 1e4 else f"{round(a):,}원"
    return pm + body


FLAT_PCT = 1.0                                   # 이번 달 변화가 월 효과의 ±1% 미만이면 '변화 없음'
flat = lambda inc, base: abs(inc) < abs(base) * FLAT_PCT / 100
short = lambda name: name.replace(" 센터", "")      # 센터명 짧게(판교, 양재 …)
kkg = lambda v: f"{v / 1000:,.1f}톤" if abs(v) >= 1000 else f"{v:,.0f} kg"          # 카드용 탄소
kship = lambda v: f"{v / 1e4:,.0f}만 건" if v >= 1e4 else f"{v:,}건"                    # 카드용 출고


def eok(v):
    """억 단위 강조 표기: 261,340,848 → '약 2억 6천만 원' (천만 원 단위 반올림)"""
    if v < 1e8:                              # 1억 미만은 만 원 단위: 43,498,638 → '약 4,350만 원'
        return f"약 {round(v / 1e4):,}만 원"
    t = round(v / 1e7)                       # 천만 원 단위
    e, c = divmod(t, 10)
    return f"약 {e:,}억" + (f" {c}천만" if c else "") + " 원"
CSS = open(Path(__file__).with_name("hq_style.css")).read()


def report(mi):
    m = MONTHS[mi]; rows = sorted(data[m], key=lambda d: -d["saving"])
    tot = lambda k: sum(d[k] for d in rows)
    pr = {}
    # 판정(run_sim, 재실행까지 끝낸 최종 값 vs 전월): 목표(규제 대상 충진율 50%) 달성 = 양호 / 미달·+2%p 이상 = 주의 / 미달·+2%p 미만 = 개선 필요
    low = [d for d in rows if d["status"] == "개선 필요"]
    rer = [d for d in rows if d.get("rerun") or d.get("action")]     # 재실행 결과 또는 본부 에이전트의 조치안
    cum_saving = sum(sum(d["saving_act"] for d in data[x]) for x in MONTHS[:mi + 1])
    cum_co2 = sum(sum(d["co2_act"] for d in data[x]) for x in MONTHS[:mi + 1])
    rem = 12 - (mi + 1)                                      # 1년(12개월) 중 남은 달: 시작 달부터 12개월
    span = f"{int(MONTHS[0][5:])}~{int(m[5:])}월" if mi else f"{int(m[5:])}월"
    trend = [[x[5:] + "월", sum(d["saving"] for d in data[x]), sum(d["co2_cut"] for d in data[x])] for x in MONTHS[:mi + 1]]
    prev = {d["center"]: d for d in data[MONTHS[mi - 1]]} if mi else {}
    # 이번 달 값 = 전월 대비 늘어난 효율화(첫 달은 도입 전 대비). 출고량 변동은 빼고 효율 변화만:
    # (이번 달 출고 1건당 절감 − 전월 출고 1건당 절감) × 이번 달 출고(30일 기준). 누적 = 도입 전 대비 월 효과의 합
    for d in rows:
        pv = prev.get(d["center"])
        if not pv or not pv["ship"] or not d["ship"]:
            d["inc_s"], d["inc_c"] = d["saving"], d["co2_cut"]
            continue
        ps, pc = pv["saving_act"] / pv["ship"], pv["co2_act"] / pv["ship"]
        d["inc_s"] = round(d["saving"] - ps * d["ship30"])
        d["inc_c"] = round(d["co2_cut"] - pc * d["ship30"], 1)
    rows.sort(key=lambda d: -d["inc_s"])
    inc = lambda k: sum(d[k] for d in rows)
    r3 = lambda v: round(v / 1000) * 1000                    # 요약 표시용: 천 원 단위(데이터는 그대로)
    sgn = lambda v: ("+" if v >= 0 else "−") + won(abs(v))
    def delta(d):
        if not prev:
            return ""
        x = round(d["fill"] - prev[d["center"]]["fill"], 1)
        return f' <span class="{"up" if x >= 0 else "dn"}">{"▲" if x >= 0 else "▼"}{abs(x)}%p</span>'
    tr = "".join(
        f"<tr><td>{i}</td><td>{d['center']}</td><td>{d['ship']:,}</td><td><b>{sgn(d['inc_s'])}</b></td><td>{eok(d['inc_s'] / DAYS30 * DAYS365) if d['inc_s'] > 0 else '—'}</td>"
        f"<td>{d['inc_c']:+,} kg</td><td>{d['fill_b']} → {d['fill']}%{delta(d)}</td><td>{d['applied']}건 / 누적 {d['applied_cum']}건</td></tr>"
        for i, d in enumerate(rows, 1))
    def act(d):
        if d.get("action") and not d.get("rerun"):   # 본부 리포트 에이전트: 본부장 승인 뒤 다음 제안부터 반영
            return (f"<li><b>{d['center']}</b> — 충진율 {d['prev_fill']}% → {d['fill']}%(전월 대비 {d['gain']:+.1f}%p, 목표 50%). "
                    f"조치: <b>{d['action']}</b> (본부장 승인 뒤 다음 월말 제안부터 적용)</li>")
        rr = d.get("rerun") or {}
        why = "이번 달 제안 반려" if not d["approved"] else "절감 기준을 넘는 새 후보 없음"
        res = (f"재실행(ESG 우선 모드) 결과 신규 {len(rr.get('new', []))}종 반영, 충진율 "
               f"{rr.get('fill_before')}% → <b>{rr.get('fill_after')}%</b>"
               + (f", 월 절감 {sgn(rr['saving_change'])}" if rr.get("saving_change") else "")
               + (f". 규격 상한에 걸려 <b>본부장 승인으로 상한 {rr['cap']['from']}→{rr['cap']['to']}종 상향</b>" if rr.get("cap") else "")
               if rr.get("new") else "재실행했으나 ESG 우선 모드 기준(+2%p, 월 추가 비용 30만 원 이내)을 넘는 후보 없음 → 담당자 검토(봉투 포장, 한도 조정)")
        return (f"<li><b>{d['center']}</b> — 1차 후 충진율 {d['prev_fill']}% → {rr['fill_before']}%"
                f"(전월 대비 {rr['fill_before'] - d['prev_fill']:+.1f}%p, {why}). {res} → 최종 전월 대비 {d['gain']:+.1f}%p, <b>{d['status']}</b></li>")
    lowhtml = "".join(act(d) for d in rer) or "<li>이번 달 재실행 센터 없음</li>"
    def status(d):
        gap = round(50 - d["fill"], 1)
        if d["status"] == "개선 필요":
            return "bad", "개선 필요", ("재실행 후에도 정체" if d.get("rerun") else "충진율 정체") + f" · 전월 대비 {d['gain']:+.1f}%p"
        if d["status"] == "주의":
            return "warn", "주의", f"개선 중 {d['gain']:+.1f}%p · 목표까지 {gap}%p"
        return "ok", "양호", "충진율 목표 달성"
    def card(d):
        cls, label, why = status(d)
        hist = [x for mm in MONTHS[:mi + 1] for x in data[mm] if x["center"] == d["center"]]
        cum_s, cum_c = sum(x["saving_act"] for x in hist), sum(x["co2_act"] for x in hist)
        pf = prev[d["center"]]["fill"] if prev else d["fill_b"]
        dm = round(d["fill"] - pf, 1)
        # 월별 막대: 도입 전 충진율 + 전월까지 누적 상승 + 그 달 상승, 50% 기준선
        n, H, W = len(hist), 70, 100
        bw = min(14, (W - 8) / n - 4)
        y = lambda v: H - v / 70 * (H - 6)
        bars = ""
        for k, x in enumerate(hist):
            cx = 4 + (k + .5) * (W - 8) / n
            p0 = hist[k - 1]["fill"] if k else x["fill_b"]
            segs = [(0, x["fill_b"], "k0"), (x["fill_b"], max(x["fill_b"], p0), "k1"), (max(x["fill_b"], p0), max(p0, x["fill"]), "k2")]
            bars += "".join(f'<rect class="{c}" x="{cx - bw / 2:.1f}" y="{y(hi):.1f}" width="{bw:.1f}" height="{y(lo) - y(hi):.1f}"/>'
                            for lo, hi, c in segs if hi > lo)
            bars += f'<text x="{cx:.1f}" y="{H + 9}">{int(MONTHS[k][5:])}월</text>'
        chart = (f'<svg viewBox="0 0 {W} {H + 12}" class="fchart" role="img" aria-label="월별 충진율">{bars}'
                 f'<line class="kg" x1="2" x2="{W - 2}" y1="{y(50):.1f}" y2="{y(50):.1f}"/></svg>')
        return (f'<div class="cc cc--{cls}"><div class="cc-h"><b>{d["center"]}</b><span class="badge badge--{cls}">{label}</span></div>'
                f'<div class="cc-why">{why}{" · 신규 규격 기준 하향" if d.get("low_gate") else ""}</div>'
                f'<div class="cc-row"><span>출고</span><b title="{d["ship"]:,}건">{kship(d["ship"])}</b></div>'
                f'<div class="cc-row"><span>이번 달 절감액 <small>전월 대비</small></span><b title="{sgn(d["inc_s"])}">{"변화 없음" if flat(d["inc_s"], d["saving"]) else kwon(d["inc_s"], True)}</b></div>'
                f'<div class="cc-row"><span>누적 절감액 <small>{span}</small></span><b title="{won(cum_s)}">{kwon(cum_s)}</b></div>'
                f'<div class="cc-row"><span>누적 탄소 감축 <small>{span}</small></span><b title="{cum_c:,.0f} kg">{kkg(cum_c)}</b></div>'
                f'<div class="cc-row"><span>센터 충진율</span><b>{d["fill"]}%</b></div>'
                f'<div class="cc-row"><span>전월 대비 충진율</span><b class="{"up" if dm >= 0 else "dn"}">{dm:+.1f}%p</b></div>'
                f'<div class="cc-f">{chart}</div></div>')
    cards = "".join(card(d) for d in sorted(data[m], key=lambda d: [x["center"] for x in data[m]].index(d["center"])))
    chart = json.dumps({"bars": [[d["center"], max(0, d["inc_s"]), max(0, d["inc_c"])] for d in rows], "trend": trend}, ensure_ascii=False)
    html = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{m[:4]}년 {int(m[5:])}월 본부 효율화 리포트</title><style>{CSS}</style></head><body><div class="page">
<div class="top"><div><span class="tag">AI 생성</span>본부 통합 효율화 리포트 · 센터 {len(rows)}곳</div><div>대상 {m}{' · <b>' + NOTE + '</b>' if NOTE else ''}</div></div>
<div class="hero"><div class="k">요약 · {m[:4]}년 {int(m[5:])}월</div>
<div class="b2">
<div><div class="l">월 절감액 · 도입 이후 개선 합계</div><div class="big">{won(r3(tot('saving')))}<small>(이번 달 {"변화 없음" if flat(inc('inc_s'), tot('saving')) else sgn(r3(inc('inc_s')))})</small></div><div class="yr">연간 추정 <b>{eok(tot('saving') / DAYS30 * DAYS365)}</b></div></div>
<div class="side"><div class="s"><div class="l">월 탄소 감축 · 운송 + 포장재</div><b>{tot('co2_cut'):,.0f} kg</b> <small>(이번 달 {"변화 없음" if flat(inc('inc_c'), tot('co2_cut')) else f"{inc('inc_c'):+,.0f} kg"})</small><span>연간 {tot('co2_cut') / DAYS30 * DAYS365 / 1000:,.1f}톤 · 소나무 약 {tot('co2_cut') / DAYS30 * DAYS365 / PINE_KG:,.0f}그루 1년 흡수량</span></div>
<div class="s"><div class="l">효율 개선 필요 센터 · 목표 충진율 50%</div><b>{len(low)}곳</b> <small>{', '.join(short(d['center']) for d in low)}</small><span>{'이번 달 재실행 센터 - ' + ', '.join(short(d['center']) for d in rer) + ' · ' if rer else ''}목표 달성 {sum(1 for d in rows if d['fill'] >= 50)}곳 · 주의 {sum(1 for d in rows if d['status'] == '주의')}곳</span></div></div>
</div></div>
{'<section class="hqsum"><h2>본부 총평 <small>AI 작성 · 숫자는 집계 결과 그대로</small></h2><ul>' + "".join(f"<li>{x}</li>" for x in SUMMARY) + "</ul>" + reco_table() + "</section>" if SUMMARY and mi == len(MONTHS) - 1 else ""}
<section class="cs"><h2>센터 현황 <small>{m}</small></h2><div class="cleg"><span><i class="k0"></i>도입 전 충진율</span><span><i class="k1"></i>누적 상승</span><span><i class="k2"></i>이번 달 상승</span><span><i class="kg"></i>50% 포장공간비율 기준 · 세 변 합 50cm 초과 박스 규제 범위</span></div><div class="cgrid">{cards}</div></section>
<div class="kpis">
<div class="kpi"><div class="l">누적 절감 ({FIRST}~)</div><div class="v">{won(cum_saving)}</div><div class="s">{len(MONTHS[:mi + 1])}개월 합계</div></div>
<div class="kpi"><div class="l">누적 탄소 감축 ({FIRST}~)</div><div class="v">{cum_co2:,.0f} kg</div><div class="s">30년생 소나무 약 {cum_co2 / PINE_KG:,.0f}그루 1년 흡수량</div></div>
<div class="kpi"><div class="l">박스 적재 부피 감소</div><div class="v">{tot('vol_cut'):,.0f} m³</div><div class="s up">이번 달 · 1톤 탑차 {tot('trips_cut'):.0f}대분</div></div>
<div class="kpi"><div class="l">평균 충진율</div><div class="v">{sum(d['fill_b'] * d['ship'] for d in rows) / tot('ship'):.1f} → {sum(d['fill'] * d['ship'] for d in rows) / tot('ship'):.1f}%</div><div class="s">세 변 합 50cm 초과 출고, 출고량 가중</div></div></div>
<section><h2>센터별 효율화 내역 <small>이번 달 전월 대비 절감 순</small></h2>
<div class="legend"><span><i style="background:var(--s1)"></i>이번 달 절감액 (전월 대비)</span><span><i style="background:var(--s2)"></i>탄소 감축 (전월 대비)</span></div>
<svg id="bars" viewBox="0 0 820 {len(rows) * 46 + 10}" width="100%" role="img" aria-label="센터별 월 절감액과 탄소 감축"></svg>
<table><tr><th>순위</th><th>센터</th><th>출고</th><th>이번 달 절감 <small>전월 대비</small></th><th>연간 추정</th><th>탄소 감축 <small>전월 대비</small></th><th>충진율 <small>전월 대비</small></th><th>개선 반영</th></tr>{tr}</table></section>
<section><h2>월별 추이 <small>전 센터 월 절감 수준 (도입 전 대비)</small></h2><svg id="trend" viewBox="0 0 820 220" width="100%" role="img" aria-label="월별 절감액 추이"></svg></section>
<section><h2>이번 달 재실행과 결과 <small>1차 후 목표 미달·전월 대비 +2%p 미만 → ESG 우선 모드로 재실행</small></h2><ul class="act">{lowhtml}</ul></section>
<details><summary>상세보기</summary>
<h3>산정 기준</h3><ul>
<li>운송 탄소: 필요 운행 = 박스 적재 부피 ÷ (7.168 m³ × 적재 효율 70%), 운행당 80 km ÷ 9.0 km/L × 경유 3.44 kg CO₂e/L (GLEC v3.0 WTW, STD-01·04)</li>
<li>포장재 탄소: 골판지 중량(전개 면적 × 550 g/m²) × 491 kg CO₂e/t (FEFCO 2021, STD-02)</li>
<li>충진율: 포장 규제 대상 출고(박스 세 변 합 50cm 초과)만 계산 — 50cm 이하는 포장공간비율 기준 미적용(포장 규칙 별표 1 비고 11)이라 제외. 상품 부피 ÷ 박스 내부 부피, 출고량 가중</li>
<li>규제 준수율: 세 변 합 50cm 초과 출고 중 포장공간비율 50% 이하(부피 기준 근사) 비율 (포장 규칙 별표 1 비고 11, STD-03)</li>
<li>요약 금액은 천 원 단위로 반올림해 표시(계산은 원 단위). 월 절감액 = 전월까지 도입한 개선의 월 절감 + 이번 달 새 개선의 월 절감(도입 전 대비), 월 값은 한 달 = 30일(그 달 일평균 × 30)로 맞춰 일수 차이로 생기는 증감을 없앤다. 연간 추정 = 일평균 × 365. 월 탄소 감축도 같은 방식. 이번 달 값(절감·탄소): 그달 월 효과의 ±1% 미만이면 '변화 없음'. 출고량 변동을 뺀 효율 변화 = (이번 달 출고 1건당 절감 − 전월 출고 1건당 절감) × 이번 달 출고(30일 기준), 첫 달은 도입 전 대비. 누적은 실제 합계. 누적 = 시작 달부터 각 달의 도입 전 대비 월 효과 합</li>
<li>절감 기준 하향: 전월에 새 규격이 없던 센터는 그달 신규 규격 기준을 월 절감 30만 → 3만 원, 대상 충진율 개선 +10 → +3%p, 출고 비중 3 → 1%로 낮춰 제안(본부 센터별 조정)</li>
<li>개선 반영: 그달 승인되어 반영된 개선 제안 건수(신규 규격 도입 등)</li>
<li>판정 (목표: 포장 규제 대상 출고 충진율 50%, POL-02 §7) — 양호: 50% 이상 / 주의: 미달이지만 전월 대비 +2%p 이상 개선 / 효율 개선 필요: 미달이면서 +2%p 미만(정체, 반려 포함)</li>
<li>센터별로 한 달에 한 번 제안·개선하고, 1차 후 목표 미달이면서 전월 대비 +2%p 미만인 센터는 같은 달에 ESG 우선 모드(ISO 18602 기준 · 규제 대상 충진율 +2%p 이상, 월 추가 비용 30만 원 이내)로 한 번 더 제안한다. 양호·주의·개선 필요 판정과 카드·표의 값은 재실행 반영 후 기준</li>
<li>월간 절감액: 그 달 한 달의 박스비 + 택배 운임 감소(그때까지 반영된 개선 제안 효과 모두 포함, 승인 제안의 예상 효과 기준). 월 값은 30일 기준, 연간 추정 = 일평균 × 365. 누적 = {FIRST}부터 각 달 실제 절감액의 합</li>
<li>나무 환산: 연간 탄소 감축 ÷ 30년생 소나무 1그루 연 6.6 kg CO₂ (국립산림과학원 발표, 판본에 따라 값이 다름 — 최신 '주요 산림수종의 표준 탄소흡수량' 확인 필요). 참고: 30년생 소나무 숲 1ha 연 10.8톤 CO₂(2022-04 배포)</li></ul>
<h3>가정</h3><ul>{'<li>' + NOTE + '</li>' if NOTE else ''}<li>배송 거리, 연비, 적재 효율, 평량은 가정값</li></ul></details></div>
<div class="tip" id="tip"></div>
<script id="cd" type="application/json">{chart}</script><script>{open(Path(__file__).with_name('hq_chart.js')).read()}</script></body></html>"""
    path = OUT / f"hq_report_{m}.html"
    path.write_text(html, encoding="utf-8")
    return path


def reco_table():
    """센터별 조정 3개 추천(현재 → 추천). 본부장이 화면에서 센터별로 미세조정하는 값"""
    if not RECO:
        return ""
    def cell(cur, new, fmt):
        return fmt(cur) if cur == new else f"{fmt(cur)} → <b>{fmt(new)}</b>"
    rows = "".join(
        f"<tr><td>{short(r['center'])}</td><td>{r['status'].replace('개선 필요', '효율 개선 필요')}</td>"
        f"<td>{cell(r['current']['saving'], r['recommended']['saving'], lambda v: f'{v / 1e4:,.0f}만 원')}</td>"
        f"<td>{cell(r['current']['fill'], r['recommended']['fill'], lambda v: f'{v:g}%p')}</td>"
        f"<td>{cell(r['current']['payback'], r['recommended']['payback'], lambda v: f'{v:g}개월')}</td>"
        f"<td class='muted'>{r['reason']}</td></tr>" for r in sorted(RECO, key=lambda r: not r["changed"]))
    return ("<h3>센터별 설정 추천 <small>본부장이 센터별로 조정 · 승인 전 미반영</small></h3><div class='tw'><table class='reco'>"
            "<thead><tr><th>센터</th><th>판정</th><th>월 절감액 기준</th><th>최소 충진율 개선 폭</th><th>목형비 회수 기간</th><th>근거</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></div>")


def render(data_in: dict, out_dir, note: str = "", summary: list[str] | None = None, reco: list[dict] | None = None) -> list[str]:
    """월별 리포트 HTML 을 out_dir 에 만든다. data_in = {"YYYY-MM": [센터 행, ...]} (월 순서대로), summary = 마지막 달 본부 총평"""
    global OUT, NOTE, SUMMARY, RECO
    OUT, NOTE, SUMMARY, RECO = Path(out_dir), note, summary, reco
    OUT.mkdir(parents=True, exist_ok=True)
    _load(data_in)
    return [str(report(i)) for i in range(len(MONTHS))]


if __name__ == "__main__":
    print("reports", [Path(p).stem[10:] for p in render(json.load(open(sys.argv[2])), sys.argv[1],
                                                         sys.argv[3] if len(sys.argv) > 3 else "")])
