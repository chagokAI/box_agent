"""
제안서 문장 작성(⑧)용 LLM 호출 — OpenAI Responses API

- 숫자는 만들지 않는다. facts(③⑥⑦ 툴 결과에서 뽑은 값)와 규칙 기반 초안을 주고 문장만 다듬게 한다.
  ⑨ evaluate 가 facts 에 없는 숫자를 찾아 감점하므로, 지어낸 숫자는 재작성 루프에서 걸러진다.
- OPENAI_API_KEY 가 없거나 호출이 실패하면 None/error → 에이전트는 규칙 기반 문장을 그대로 쓴다.
- 모델: BOX_AGENT_LLM_MODEL (기본 gpt-5.4-mini). 응답은 JSON 스키마(strict)로 받는다.
- 지시문은 prompts/writer.md(역할·출력) + skills/proposal-writing(문장 규칙), 재시도·대체 모델·토큰 기록은 harness.llm_json.
"""
from __future__ import annotations

import re

import harness

NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")

SCHEMA = {
    "type": "object",
    "properties": {
        "reasons": {"type": "array", "items": {"type": "string"},
                    "description": "제안 근거 3개(수요 예측, 신규 규격 효과, 보충이 가장 큰 규격 순)"},
        "checkItems": {"type": "array", "items": {"type": "string"},
                       "description": "확인 필요 항목. draft 와 같은 개수·순서"},
    },
    "required": ["reasons", "checkItems"],
    "additionalProperties": False,
}


def model() -> str:
    return harness.llm_model()


def enabled() -> bool:
    return harness.llm_enabled()


def numbers(text: str) -> set[str]:
    """'1,330,961원', '40.1%' → {'1330961', '40.1'}. 끝의 .0 은 떼어 같은 값으로 본다"""
    out = set()
    for m in NUM_RE.findall(text):
        v = m.replace(",", "")
        out.add(v[:-2] if v.endswith(".0") else v)
    return out


async def polish(facts: dict, reasons: list[str], checks: list[str], feedback: list[str],
                 example: str | None = None) -> dict | None:
    """성공하면 {"reasons": [...3], "checkItems": [...], "model": ...}, 실패하면 {"error": ...}, 꺼져 있으면 None.
    example: 예시 제안서 markdown(문체 참고용). facts 는 Context 예산 안으로 줄여 넘긴다"""
    if not enabled():
        return None
    payload = {"facts": harness.fit_context(facts), "draft": {"reasons": reasons, "checkItems": checks},
               "feedback": feedback}
    if example:
        payload["example"] = example
    try:
        out = await harness.llm_json("write_proposal", harness.instructions("writer", "proposal-writing"), payload, SCHEMA, "proposal_text")
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    if len(out["reasons"]) != 3 or len(out["checkItems"]) != len(checks):
        return {"error": f"항목 수 불일치 (근거 {len(out['reasons'])}/3, 확인 {len(out['checkItems'])}/{len(checks)})"}
    return {"reasons": out["reasons"], "checkItems": out["checkItems"], "model": out["_model"]}
