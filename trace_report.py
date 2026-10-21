"""
에이전트 판단 기록(trace) → 사람이 읽는 markdown

각 노드는 {"node", "at", "tools", "saw", "decided", "why", "next", "edge"} 를 남긴다.
- saw     : 이 단계에서 본 입력(툴 결과 요약)
- decided : 내린 판단·산출 결과
- why     : 판단 근거(기준값·규칙)
- next    : 다음으로 가는 곳과 이유(분기 노드만)
- edge    : 흐름도에서 이 단계로 들어오는 화살표 이름(재작성·반려 같은 되돌아감)
숫자는 모두 MCP 툴 결과·기준값에서 온 것이다. LLM 이 만든 문장은 '제안서 작성' 단계에 출처만 남긴다.
"""
from __future__ import annotations

TITLES = {
    "load_context": "① 준비", "collect_data": "② 데이터 수집", "forecast_demand": "③ 수요 예측",
    "analyze_fit": "④-1 충진율 계산", "diagnose": "④-2 원인 분류", "find_clusters": "④-3 애매한 구간 묶기",
    "gen_candidates": "⑤ 후보 찾기", "simulate": "⑥ 효과 계산", "plan_order": "⑦ 주문 수량",
    "write_proposal": "⑧ 제안서 작성", "evaluate": "⑨ 자체 검토", "human_review": "⑩ 담당자 승인",
    "save_result": "⑪ 저장",
}


def _flowchart(trace: list) -> list[str]:
    out = ["```mermaid", "flowchart TD"]
    for i, t in enumerate(trace, 1):
        label = f"{i}. {TITLES.get(t['node'], t['node'])}"
        out.append(f'    s{i}["{label}"]')
        if i > 1:
            edge = t.get("edge")
            out.append(f"    s{i - 1} -->|{edge}| s{i}" if edge else f"    s{i - 1} --> s{i}")
    out.append("```")
    return out


def _bullets(title: str, items: list) -> list[str]:
    return [f"**{title}**", *[f"- {x}" for x in items], ""] if items else []


def render(job: dict, trace: list, result: dict) -> str:
    """result: status(완료/실패), summary(결론 줄), config, error, calls, textSource"""
    lines = [
        f"# 에이전트 판단 기록 — {job['targetMonth']} 박스 주문 제안",
        "",
        f"`{job['jobId']}` · {job.get('warehouseName', '')} · 결과 **{result['status']}** · "
        f"{len(trace)}단계 · MCP 툴 호출 {result['calls']}회 · 문장 작성 {result.get('textSource', '-')}",
        "",
    ]
    if result.get("error"):
        lines += [f"> **실행 중단:** {result['error']}", ""]
    if result.get("summary"):
        lines += ["## 한눈에 보기", "", *[f"- {s}" for s in result["summary"]], ""]
    lines += ["## 흐름", "", *_flowchart(trace), ""]
    lines += ["## 단계별 판단", ""]
    for i, t in enumerate(trace, 1):
        lines += [f"### {i}. {TITLES.get(t['node'], t['node'])} `{t['node']}`", ""]
        if t.get("edge"):
            lines += [f"_되돌아온 이유: {t['edge']}_", ""]
        lines += _bullets("본 것", t.get("saw", []))
        lines += _bullets("판단", t.get("decided", []))
        lines += _bullets("근거", t.get("why", []))
        if t.get("next"):
            lines += [f"**다음** → {t['next']}", ""]
        if t.get("tools"):
            lines += ["<sub>호출한 툴: " + ", ".join(f"{x['tool']} ({x['ms']}ms)" for x in t["tools"]) + "</sub>", ""]
    if result.get("config"):
        c = result["config"]
        g = c["gate"]
        lines += [
            "## 이번 실행의 기준값", "",
            "| 항목 | 값 |", "|:---|:---|",
            f"| 포장 여유 | 축마다 {c['margin_mm']}mm |",
            f"| 저충진 기준 | {c['low_fill_pct']}% 미만 |",
            f"| 신규 도입(기성품) | 월 절감 {g['CATALOG']['monthly_saving']:,}원 · 출고 비중 {g['CATALOG']['share_pct']}% · "
            f"충진율 +{g['CATALOG']['delta_fill']}%p 이상 |",
            f"| 안전 재고 · 반올림 | {c['safety_days']}일분 · {c['rounding_unit']}개 단위 |",
            f"| 검토 통과 · 최대 재작성 | {c['eval_pass']}점 · {c['max_loops']}회 |",
            f"| 운영자 의견 반영 | 신규 규격 없이 보충만 {c['no_new_specs']} |",
            "",
        ]
    return "\n".join(lines)
