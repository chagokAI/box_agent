"""
Harness — 모델(LLM)을 감싸는 코드. 모델은 다음 응답이나 쓸 도구를 '제안'만 하고, 실행·한도·기록·복구는 여기서 한다.

  Instructions   prompts/*.md 를 읽는다(prompt). 코드에 지시문을 넣지 않는다
  Tools          MCP 도구 결과를 잘라 넘기고(TOOL_RESULT_CHARS), 오류는 function_call_output 으로 모델에 돌려준다
  Context        facts 를 글자 예산(CONTEXT_CHARS) 안으로 줄인다(fit_context)
  State·Memory   LangGraph checkpointer(thread_id = taskId) — box_spec_agent.open_checkpointer
  Agent Loop     tool_loop: 최대 턴(MAX_TURNS), 같은 도구 반복(SAME_TOOL_MAX), 시간(LOOP_TIMEOUT_S) 한도
  Guardrails     도구 결과의 개인정보 마스킹(mask), 변경 도구(destructiveHint)는 모델 도구 목록에 넣지 않음,
                 모델이 인용한 출처가 실제 검색 결과인지 대조(그래프 evaluate)
  Recovery       429·5xx·연결 오류는 2회 재시도(RETRIES), 계속 실패하면 대체 모델(BOX_AGENT_LLM_FALLBACK_MODEL),
                 그것도 없으면 오류를 돌려 호출한 쪽이 규칙 기반으로 진행
  Observability  trace_id 하나로 로그·이벤트·LLM 호출(토큰·지연·비용)을 묶어 proposals/traces/<taskId>.jsonl 에 남긴다.
                 LANGSMITH_API_KEY 가 있으면 LangSmith 로도 보낸다

권장 한도는 PPT 기준(8턴·같은 도구 3회·429/503 2회 재시도)이고 환경변수로 바꾼다. 바꾸면 README 'Harness' 표에 남긴다.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

KST = timezone(timedelta(hours=9))
HERE = Path(__file__).resolve().parent
PROMPTS = HERE / "prompts"
TRACE_DIR = Path(os.getenv("BOX_AGENT_TRACE_DIR", HERE / "proposals" / "traces"))


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


MAX_TURNS = _int("BOX_AGENT_MAX_TURNS", 8)
SAME_TOOL_MAX = _int("BOX_AGENT_SAME_TOOL_MAX", 3)
LOOP_TIMEOUT_S = _int("BOX_AGENT_LOOP_TIMEOUT", 120)
TASK_TIMEOUT_S = _int("BOX_AGENT_TASK_TIMEOUT", 120)
RETRIES = _int("BOX_AGENT_LLM_RETRIES", 2)
TOOL_RESULT_CHARS = _int("BOX_AGENT_TOOL_RESULT_CHARS", 4000)
CONTEXT_CHARS = _int("BOX_AGENT_CONTEXT_CHARS", 24000)

# ---------------------------------------------------------------- Observability: trace·이벤트
_trace: contextvars.ContextVar[dict | None] = contextvars.ContextVar("box_agent_trace", default=None)


def start_trace(task_id: str, emit: Callable[[str, dict], None] | None = None, **meta) -> dict:
    """작업 하나의 trace 를 연다. emit 은 진행 이벤트(TOOL_START 등)를 받을 콜백(agent_server SSE)"""
    tr = {"traceId": uuid.uuid4().hex, "taskId": task_id, "emit": emit, "llmCalls": [], "toolCalls": 0,
          "startedAt": time.monotonic(), "meta": meta}
    _trace.set(tr)
    TRACE_DIR.mkdir(parents=True, exist_ok=True)
    record("TRACE_START", taskId=task_id, **meta)
    return tr


def current() -> dict | None:
    return _trace.get()


def resume_trace(tr: dict) -> None:
    """같은 작업의 다음 구간(승인 후 재개)을 같은 trace_id 로 이어 기록한다"""
    _trace.set(tr)


def trace_id() -> str | None:
    tr = _trace.get()
    return tr["traceId"] if tr else None


def trace_path() -> str | None:
    tr = _trace.get()
    return str(TRACE_DIR / f"{tr['taskId']}.jsonl") if tr else None


def record(kind: str, **data) -> None:
    """trace 파일에 한 줄 남긴다(이벤트·LLM 호출·노드 로그 공통)"""
    tr = _trace.get()
    if not tr:
        return
    row = {"at": datetime.now(KST).isoformat(timespec="milliseconds"), "traceId": tr["traceId"], "kind": kind, **data}
    with open(TRACE_DIR / f"{tr['taskId']}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def emit(event: str, **data) -> None:
    """화면 진행 이벤트: TOKEN · TOOL_START · TOOL_END · PROGRESS(단계 결과 문장) · APPROVAL_REQUIRED · DONE · FAILED"""
    tr = _trace.get()
    if not tr:
        return
    data = {"traceId": tr["traceId"], **data}
    record(event, **{k: v for k, v in data.items() if k != "traceId"})
    if tr["emit"]:
        tr["emit"](event, data)


def summary() -> dict:
    """작업 끝에 남기는 집계: LLM 호출 수·토큰·지연·추정 비용, 도구 호출 수, 재시도·대체 모델 건수"""
    tr = _trace.get()
    if not tr:
        return {}
    calls = tr["llmCalls"]
    return {"traceId": tr["traceId"], "tracePath": trace_path(), "elapsedS": round(time.monotonic() - tr["startedAt"], 1),
            "toolCalls": tr["toolCalls"], "llmCalls": len(calls),
            "inputTokens": sum(c.get("inputTokens", 0) for c in calls),
            "outputTokens": sum(c.get("outputTokens", 0) for c in calls),
            "llmLatencyS": round(sum(c.get("latencyS", 0) for c in calls), 2),
            "estCostUsd": round(sum(c.get("costUsd", 0) for c in calls), 5),
            "retries": sum(c.get("retries", 0) for c in calls), "fallbacks": sum(1 for c in calls if c.get("fallback")),
            "limitHits": tr.get("limitHits", 0), "langsmithRunIds": tr.get("lsRuns", [])}


def langsmith_enabled() -> bool:
    return bool(os.getenv("LANGSMITH_API_KEY")) and os.getenv("LANGSMITH_TRACING", "true").lower() != "false"


def new_langsmith_run() -> str | None:
    """그래프 한 구간(실행·재개)마다 LangSmith root run id 를 정해 trace 에 모은다"""
    tr = _trace.get()
    if not (tr and langsmith_enabled()):
        return None
    rid = str(uuid.uuid4())
    tr.setdefault("lsRuns", []).append(rid)
    return rid


def langsmith_links() -> list[str]:
    """이번 작업의 LangSmith run 링크. 전송이 끝날 때까지 기다린 뒤 조회한다(실패하면 run id 만)"""
    tr = _trace.get()
    if not (tr and tr.get("lsRuns")):
        return []
    try:
        from langchain_core.tracers.langchain import wait_for_all_tracers
        from langsmith import Client

        wait_for_all_tracers()
        client, project = Client(), os.getenv("LANGSMITH_PROJECT", "default")
        links = []
        for rid in tr["lsRuns"]:
            for attempt in range(3):          # 방금 끝난 run 은 서버 반영까지 잠깐 걸린다
                try:
                    links.append(client.get_run_url(run=client.read_run(rid), project_name=project))
                    break
                except Exception:
                    time.sleep(1.5)
            else:
                links.append(f"langsmith-run:{rid}")
        return links
    except Exception as e:
        record("LANGSMITH_LINK_FAIL", error=str(e)[:200])
        return [f"langsmith-run:{r}" for r in tr["lsRuns"]]


def count_tool_call() -> None:
    tr = _trace.get()
    if tr:
        tr["toolCalls"] += 1


# ---------------------------------------------------------------- Instructions
def prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8").strip()


SKILLS = Path(__file__).resolve().parent / "skills"


def skill(name: str) -> str:
    """skills/<name>/SKILL.md 본문(앞 메타데이터 제외). 문장 규칙 원본 — Claude Code 스킬도 같은 파일을 가리킨다"""
    text = (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
    if text.startswith("---"):
        text = text.split("---", 2)[2]
    return text.strip()


def instructions(prompt_name: str, skill_name: str) -> str:
    """LLM 지시문 = 역할·출력 형식(prompts/<prompt_name>.md) + 문장 규칙 스킬(skills/<skill_name>)"""
    return prompt(prompt_name) + "\n\n" + skill(skill_name)


# ---------------------------------------------------------------- Guardrails·Tools·Context
_PII = [(re.compile(r"\b\d{6}-?[1-4]\d{6}\b"), "[주민번호]"),
        (re.compile(r"\b01[016789]-?\d{3,4}-?\d{4}\b"), "[휴대폰]"),
        (re.compile(r"\b0\d{1,2}-\d{3,4}-\d{4}\b"), "[전화번호]"),
        (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[이메일]")]


def mask(text: str) -> str:
    for pat, rep in _PII:
        text = pat.sub(rep, text)
    return text


def clip(obj: Any, limit: int = TOOL_RESULT_CHARS) -> str:
    """도구 결과 → 모델에 넘길 문자열: 개인정보 마스킹 + 길이 제한"""
    s = mask(obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, default=str))
    return s if len(s) <= limit else s[:limit] + f"…(이하 {len(s) - limit}자 생략)"


def fit_context(facts: dict, budget: int = CONTEXT_CHARS) -> dict:
    """facts 를 글자 예산 안으로: 가장 긴 리스트부터 앞쪽만 남긴다(숫자 대조용 원본은 호출한 쪽이 따로 가진다)"""
    facts = json.loads(json.dumps(facts, ensure_ascii=False, default=str))

    def size():
        return len(json.dumps(facts, ensure_ascii=False))

    while size() > budget:
        lists = [(k, v) for k, v in facts.items() if isinstance(v, list) and len(v) > 1]
        if not lists:
            break
        k, v = max(lists, key=lambda kv: len(json.dumps(kv[1], ensure_ascii=False)))
        facts[k] = v[: max(1, len(v) // 2)]
    return facts


# ---------------------------------------------------------------- Recovery: LLM 호출
def llm_enabled() -> bool:
    return os.getenv("BOX_AGENT_LLM", "1") != "0" and bool(os.getenv("OPENAI_API_KEY"))


def llm_model() -> str:
    return os.getenv("BOX_AGENT_LLM_MODEL") or "gpt-5.4-mini"


def _client():
    import openai

    client = openai.AsyncOpenAI()
    if os.getenv("LANGSMITH_API_KEY"):
        try:
            from langsmith.wrappers import wrap_openai
            client = wrap_openai(client)
        except ImportError:
            pass
    return client


def _cost(model: str, usage) -> float:
    """BOX_AGENT_PRICE_IN/OUT (USD / 1M 토큰)를 주면 추정 비용을 계산한다. 없으면 0"""
    pin, pout = float(os.getenv("BOX_AGENT_PRICE_IN", 0)), float(os.getenv("BOX_AGENT_PRICE_OUT", 0))
    return ((usage.input_tokens or 0) * pin + (usage.output_tokens or 0) * pout) / 1e6 if usage else 0.0


async def llm_call(purpose: str, **kwargs):
    """Responses API 호출. 429·5xx·연결·시간 초과는 RETRIES 회 재시도 후 대체 모델, 그래도 실패하면 예외"""
    import openai

    models = [kwargs.pop("model", None) or llm_model()]
    if os.getenv("BOX_AGENT_LLM_FALLBACK_MODEL"):
        models.append(os.environ["BOX_AGENT_LLM_FALLBACK_MODEL"])
    tr = _trace.get()
    last = None
    for mi, model in enumerate(models):
        for attempt in range(RETRIES + 1):
            t0 = time.monotonic()
            try:
                async with _client() as client:
                    res = await client.responses.create(model=model, **kwargs)
            except (openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError,
                    openai.InternalServerError) as e:
                last = e
                record("LLM_RETRY", purpose=purpose, model=model, attempt=attempt + 1, error=type(e).__name__)
                if attempt < RETRIES:
                    await asyncio.sleep(1.5 * (attempt + 1))
                continue
            except openai.APIStatusError as e:      # 400·401 등은 재시도해도 같다
                last = e
                break
            u = res.usage
            call = {"purpose": purpose, "model": res.model, "latencyS": round(time.monotonic() - t0, 2),
                    "inputTokens": u.input_tokens if u else 0, "outputTokens": u.output_tokens if u else 0,
                    "costUsd": _cost(model, u), "retries": attempt, "fallback": mi > 0}
            if tr:
                tr["llmCalls"].append(call)
            record("LLM_CALL", **call)
            return res
    raise RuntimeError(f"LLM 호출 실패({purpose}): {type(last).__name__}: {str(last)[:200]}")


async def llm_json(purpose: str, instructions: str, payload: dict, schema: dict, name: str) -> dict:
    """도구 없이 JSON 스키마(strict) 응답 하나를 받는다"""
    res = await llm_call(purpose, instructions=instructions, input=json.dumps(payload, ensure_ascii=False, default=str),
                         text={"format": {"type": "json_schema", "name": name, "schema": schema, "strict": True}})
    if res.status != "completed":
        raise RuntimeError(f"status={res.status}")
    return json.loads(res.output_text) | {"_model": res.model}


# ---------------------------------------------------------------- Agent Loop: 모델이 도구를 고르는 루프
async def tool_loop(purpose: str, instructions: str, user_input: str, tools: list[dict],
                    call_tool: Callable[[str, dict], Awaitable[Any]], schema: dict, name: str) -> dict:
    """모델이 tools 중에서 고른 도구를 실행해 결과를 돌려주고, 더 부를 도구가 없으면 schema 모양의 답을 받는다.
    한도: MAX_TURNS 턴, 같은 도구 SAME_TOOL_MAX 회, LOOP_TIMEOUT_S 초. 마지막 턴은 도구 없이 답만 받는다.
    반환: {"output": dict, "toolCalls": [{"name", "args", "ok"}], "turns": n, "model": ...}"""
    items: list = [{"role": "user", "content": user_input}]
    used: dict[str, int] = {}
    calls: list[dict] = []
    tr = _trace.get()

    async def run():
        for turn in range(1, MAX_TURNS + 1):
            last_turn = turn == MAX_TURNS
            extra = {"tools": tools, "tool_choice": "none" if last_turn else "auto"} if tools else {}
            res = await llm_call(purpose, instructions=instructions, input=items, **extra,
                                 text={"format": {"type": "json_schema", "name": name, "schema": schema,
                                                  "strict": True}})
            fcs = [o for o in res.output if o.type == "function_call"]
            if not fcs:
                return {"output": json.loads(res.output_text), "toolCalls": calls, "turns": turn, "model": res.model}
            items.extend(o.model_dump(exclude_none=True) for o in res.output)
            for fc in fcs:
                used[fc.name] = used.get(fc.name, 0) + 1
                try:
                    args = json.loads(fc.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                if used[fc.name] > SAME_TOOL_MAX:
                    out, ok = f"한도 초과: {fc.name} 는 {SAME_TOOL_MAX}회까지. 지금까지 결과로 답하라", False
                    if tr:
                        tr["limitHits"] = tr.get("limitHits", 0) + 1
                else:
                    try:
                        out, ok = clip(await call_tool(fc.name, args)), True
                    except Exception as e:  # 도구 오류는 모델에 돌려줘 다른 인자로 다시 부르게 한다
                        out, ok = f"도구 오류: {str(e)[:500]}", False
                calls.append({"name": fc.name, "args": args, "ok": ok})
                record("AGENT_TOOL", purpose=purpose, turn=turn, name=fc.name, args=args, ok=ok)
                items.append({"type": "function_call_output", "call_id": fc.call_id, "output": out})
        raise RuntimeError("턴 한도 도달")

    try:
        return await asyncio.wait_for(run(), LOOP_TIMEOUT_S)
    except asyncio.TimeoutError as e:
        if tr:
            tr["limitHits"] = tr.get("limitHits", 0) + 1
        raise RuntimeError(f"{purpose}: 시간 한도 {LOOP_TIMEOUT_S}초 초과") from e


def mcp_tool_spec(tool, description_suffix: str = "") -> dict:
    """MCP 도구 정의(tools/list) → Responses API function 도구 정의. 모델은 이 설명만 보고 도구를 고른다"""
    return {"type": "function", "name": tool.name, "description": (tool.description or "") + description_suffix,
            "parameters": tool.inputSchema, "strict": False}


READ_RESOURCE_SPEC = {
    "type": "function", "name": "read_resource",
    "description": "MCP Resource 를 주소로 읽는다(가상 도구). 규정 원문 policy://POL-02, 규정 목록 policy://index, "
                   "반려 기록 원본 feedback://FB-2026-03-07",
    "parameters": {"type": "object", "properties": {"uri": {"type": "string"}}, "required": ["uri"]},
    "strict": False,
}
