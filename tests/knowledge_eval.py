"""
knowledge MCP 검색 점검 — 목업에 없는 새 문장으로 물었을 때 기대한 규정·반려 기록이 상위 k개에 나오는지 본다.

실행: .venv/bin/python tests/knowledge_eval.py [--k 3]
DB_URL 이 있으면 box_agent.knowledge(먼저 --ingest), 없으면 파일에서 바로 검색한다.
"""
import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from box_spec_agent import load_env_files  # noqa: E402

# (도구, 질문, 정답 sourceId 들 중 하나라도 상위 k 에 있으면 통과)
CASES = [
    ("search_policies", "기울여서 들어가는 상품은 바로 확정해도 되나요?", ["POL-01 §3"]),
    ("search_policies", "새 박스 규격을 넣으려면 절감액이 얼마 이상이어야 해?", ["POL-02 §2"]),
    ("search_policies", "규격이 너무 많아지면 어떻게 해야 하나", ["POL-02 §3"]),
    ("search_policies", "12월에는 안전 재고를 며칠로 잡나요", ["POL-03 §1"]),
    ("search_policies", "아직 도착 안 한 발주가 있으면 제안서를 만들어도 되나", ["POL-03 §3"]),
    ("search_policies", "빈 박스 쌓아둘 공간이 부족해", ["POL-04 §1", "POL-04 §2"]),
    ("search_policies", "확인 필요 항목은 어떤 순서로 써야 하지", ["POL-05 §3"]),
    ("search_feedback_cases", "신규 규격은 이번에 패스하고 있는 박스만 채워주세요", ["FB-2025-11-01", "FB-2026-09-19"]),
    ("search_feedback_cases", "11월이라 물량 많으니 안전 재고 좀 넉넉하게", ["FB-2025-12-02"]),
    ("search_feedback_cases", "현장에서 비스듬히 넣는 포장은 불가능합니다", ["FB-2026-05-11", "FB-2026-05-12"]),
    ("search_feedback_cases", "3호는 이번 발주에서 제외해 주세요", ["FB-2026-03-08"]),
    ("search_feedback_cases", "공급사 박스 묶음 수량에 맞춰서 주문해 주세요", ["FB-2026-10-20", "FB-2026-04-09"]),
    ("search_feedback_cases", "월 절감 금액 기준을 더 높여 주세요", ["FB-2026-03-07"]),
]


async def main(k: int):
    load_env_files(HERE / ".env", HERE.parent / "server" / "chagok" / ".env")
    import os
    params = StdioServerParameters(command=sys.executable, args=[str(HERE / "mcp_servers" / "knowledge_server.py")],
                                   env=dict(os.environ))
    ok = 0
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        st = json.loads((await s.call_tool("knowledge_status", {})).content[0].text)
        print(f"store={st['store']} embed={st['embedModel']}\n")
        for tool, q, want in CASES:
            arg = {"query": q} if tool == "search_policies" else {"comment": q}
            res = json.loads((await s.call_tool(tool, {**arg, "k": k})).content[0].text)["items"]
            got = [h["sourceId"] for h in res]
            hit = any(g in want for g in got)
            ok += hit
            print(f"{'✓' if hit else '✗'} {q}\n    상위 {k}: " + ", ".join(f"{h['sourceId']}({h['score']})" for h in res)
                  + ("" if hit else f"  ← 기대 {want}"))
    print(f"\n적중 {ok}/{len(CASES)} (top-{k})")


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[sys.argv.index("--k") + 1]) if "--k" in sys.argv else 3))
