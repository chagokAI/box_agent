"""
⑩ 반려 해석 평가 — 같은 평가셋(tests/feedback_eval_set.jsonl, 목업 반려 기록과 겹치지 않는 문장 12개)을
세 방식으로 돌려 결과표를 tests/results/feedback_eval_<날짜>.md 에 남긴다. 프롬프트·한도·모델을 바꾸면 다시 돌린다.

  keyword   키워드 규칙만 (LLM 없음)
  llm       LLM 해석, 도구 없음 (BOX_AGENT_RAG=0)
  llm+rag   LLM 이 rag 도구(과거 반려 기록·규정)를 골라 쓰며 해석 (에이전트 기본)

채점: 기대 기준값 변경과 정확히 같으면 정답(다른 key 를 더 바꾸면 오답). 기준값으로 못 바꾸는 요청은
unsupported 에 담겨야 정답. 실행: .venv/bin/python tests/feedback_eval.py [--modes keyword,llm,llm+rag]
"""
import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import box_spec_agent as agent  # noqa: E402
import harness  # noqa: E402

CASES = [json.loads(x) for x in (HERE / "tests" / "feedback_eval_set.jsonl").read_text(encoding="utf-8").splitlines()
         if x.strip()]
WH = os.getenv("EVAL_WAREHOUSE_ID", "2b7424cf-37a9-5119-98d7-a23142cbb251")


async def run_case(case: dict) -> dict:
    job = {"jobId": "eval", "warehouseId": WH, "targetMonth": "2026-11"}
    state = {"job": job, "config": agent.default_config(job), "approval": {"comment": case["comment"]}}
    t0 = time.monotonic()
    out = await agent.interpret_feedback(state)
    rj = out["rejections"][-1]
    got = {c["key"]: c["after"] for c in rj["changes"]}
    ok_changes = got == case["expect"]
    ok_unsup = bool(rj["unsupported"]) == case["expectUnsupported"]
    return {"id": case["id"], "ok": ok_changes and ok_unsup, "got": got, "unsupported": rj["unsupported"],
            "sources": rj["sources"], "s": round(time.monotonic() - t0, 1)}


async def main(modes: list[str]):
    agent.load_env()
    results = {}
    async with agent.MCPHub(agent.MCP_SERVERS) as hub:
        agent.HUB = hub
        for mode in modes:
            os.environ["BOX_AGENT_LLM"] = "0" if mode == "keyword" else "1"
            os.environ["BOX_AGENT_RAG"] = "0" if mode == "llm" else "1"
            harness.start_trace(f"eval-{mode}-{datetime.now():%H%M%S}", source="feedback_eval", mode=mode)
            results[mode] = [await run_case(c) for c in CASES]
            summ = harness.summary()
            print(f"{mode:<8} 정답 {sum(r['ok'] for r in results[mode])}/{len(CASES)} "
                  f"LLM {summ['llmCalls']}회 토큰 {summ['inputTokens'] + summ['outputTokens']:,} {summ['elapsedS']}s")
            results[mode].append({"summary": summ})
    lines = [f"# 반려 해석 평가 ({datetime.now():%Y-%m-%d %H:%M}, 모델 {harness.llm_model()})", "",
             "| 방식 | 정답 | LLM 호출 | 토큰 | 소요 |", "|---|---:|---:|---:|---:|"]
    for mode, rs in results.items():
        s = rs[-1]["summary"]
        lines.append(f"| {mode} | {sum(r['ok'] for r in rs[:-1])}/{len(CASES)} | {s['llmCalls']} | "
                     f"{s['inputTokens'] + s['outputTokens']:,} | {s['elapsedS']}s |")
    lines += ["", "| id | 반려 의견 | 기대 | " + " | ".join(results) + " |",
              "|---|---|---|" + "---|" * len(results)]
    for i, c in enumerate(CASES):
        exp = json.dumps(c["expect"], ensure_ascii=False) + (" +미지원" if c["expectUnsupported"] else "")
        cells = []
        for mode, rs in results.items():
            r = rs[i]
            cells.append(("✓ " if r["ok"] else "✗ ") + json.dumps(r["got"], ensure_ascii=False)
                         + (" +미지원" if r["unsupported"] else ""))
        lines.append(f"| {c['id']} | {c['comment']} | {exp} | " + " | ".join(cells) + " |")
    out = HERE / "tests" / "results" / f"feedback_eval_{datetime.now():%Y%m%d_%H%M}.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"결과표: {out}")


if __name__ == "__main__":
    modes = sys.argv[sys.argv.index("--modes") + 1].split(",") if "--modes" in sys.argv else ["keyword", "llm", "llm+rag"]
    asyncio.run(main(modes))
