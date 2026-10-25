"""
rag-mcp — 규정 문서·반려 기록 검색 (RAG). 실행 진입점은 rag_mcp.py

- 자료: ../knowledge/policies/*.md (규정, '## ' 조항 단위로 자름. {규칙 키} 자리는 rules/box_order_rules.yaml 값으로 채움), ../knowledge/feedback_cases.jsonl (반려 기록)
- 저장: DB_URL 이 있으면 서버 PostgreSQL 의 box_agent.knowledge 테이블(pgvector). 서버 엔티티에 매핑되지 않는
  별도 스키마라 서버(Hibernate)는 건드리지 않는다. DB_URL 이 없으면 파일을 읽어 메모리에서 검색한다.
- 임베딩: OPENAI_API_KEY 가 있으면 OpenAI(KNOWLEDGE_EMBED_MODEL, 기본 text-embedding-3-small),
  없으면 글자 2-gram 해싱(local-bigram, 의미 검색이 아니라 표현이 겹치는 문장을 찾는 대체 수단).
  임베딩 방식이 바뀌면 --ingest 로 다시 적재한다(검색은 같은 방식으로 적재된 행만 본다).
- 결과에는 항상 sourceId(POL-02 §2, FB-2026-03-07 등)를 붙인다. 문장에 인용할 때는 이 값만 쓴다.
- Resource(원문 읽기, 근거 패널용): policy://index(규정 목록), policy://{doc_id}(규정 원문 markdown),
  feedback://{case_id}(반려 기록 원본 JSON). 검색 도구는 조항 chunk 를, Resource 는 원문 전체를 준다.

적재: python mcp_servers/knowledge_server.py --ingest
"""
import hashlib
import json
import math
import os
import re
import sys
import uuid
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

import chagok_db as db
import policy_rules

mcp = FastMCP("rag", log_level="WARNING")
HERE = Path(__file__).resolve().parent
KDIR = Path(os.getenv("KNOWLEDGE_DIR", HERE.parent / "knowledge"))
# 본부장이 화면에서 고친 규정 문서(전 센터 공통): DB 가 있으면 box_agent.policy_doc(+ policy_doc_history),
# 없으면(로컬 CLI·테스트) 이 폴더의 같은 파일 이름. 고친 문서가 있으면 기본 문서(knowledge/policies) 대신 쓴다
DOC_OVERRIDE_DIR = Path(os.getenv("POLICY_DOC_OVERRIDES", HERE.parent / "knowledge" / "policy_overrides"))
NS = uuid.UUID("2f1c7a52-0d7e-5b6a-9a1e-6b1f3c0a9e11")
LOCAL_DIM = 1024

DDL = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS box_agent;
CREATE TABLE IF NOT EXISTS box_agent.knowledge (
    id           uuid PRIMARY KEY,
    kind         varchar(20)  NOT NULL,          -- policy | feedback_case
    source_id    varchar(40)  NOT NULL,          -- POL-02 §2 / FB-2026-03-07
    warehouse_id uuid,                           -- 반려 기록만 (규정은 NULL = 전 센터 공통)
    title        varchar(200),
    content      text         NOT NULL,          -- 검색 결과로 돌려주는 본문
    meta         jsonb,
    embed_model  varchar(40)  NOT NULL,
    embedding    vector       NOT NULL,
    updated_at   timestamptz  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_knowledge_kind ON box_agent.knowledge (kind, embed_model);
"""


# ---------------------------------------------------------------- 임베딩
def embed_model() -> str:
    if os.getenv("OPENAI_API_KEY") and os.getenv("KNOWLEDGE_EMBED", "openai") != "local":
        return os.getenv("KNOWLEDGE_EMBED_MODEL", "text-embedding-3-small")
    return "local-bigram"


def _local_vec(text: str) -> list[float]:
    t = re.sub(r"\s+", " ", text.lower())
    v = [0.0] * LOCAL_DIM
    for g in (t[i:i + 2] for i in range(len(t) - 1)):
        if g.strip():
            v[int(hashlib.md5(g.encode()).hexdigest()[:8], 16) % LOCAL_DIM] += 1.0
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


def embed(texts: list[str]) -> list[list[float]]:
    m = embed_model()
    if m == "local-bigram":
        return [_local_vec(t) for t in texts]
    import openai

    res = openai.OpenAI().embeddings.create(model=m, input=texts)
    return [d.embedding for d in res.data]


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b)) / ((math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))) or 1)


# ---------------------------------------------------------------- 자료 읽기
def load_policies(values: dict | None = None) -> list[dict]:
    """규정 md → 조항('## ') 단위 청크. 제목 줄의 (POL-xx) 가 문서 id. values = 채울 규칙값(없으면 전사 기본값)"""
    out = []
    for d in _policy_docs().values():
        text = policy_rules.render(d["text"], values)   # {규칙 키} → 규칙값 (문서 숫자 = 판단 숫자)
        title = text.splitlines()[0].lstrip("# ").strip()
        doc_id = (re.search(r"\((POL-\d+)\)", title) or re.search(r"(POL-\d+)", d["name"])).group(1)
        for sec in re.split(r"\n(?=## )", text)[1:]:
            head = sec.splitlines()[0].lstrip("# ").strip()
            num = (re.match(r"(\d+)\.", head) or [None, "?"])[1]
            body = sec.split("\n", 1)[1].strip() if "\n" in sec else ""
            out.append({"kind": "policy", "sourceId": f"{doc_id} §{num}", "warehouseId": None,
                        "title": f"{title} — {head}", "content": body,
                        "embedText": f"{title} {head}\n{body}",
                        "meta": {"docId": doc_id, "section": num, "heading": head, "file": d["name"]}})
    return out


def load_feedback_cases() -> list[dict]:
    f = KDIR / "feedback_cases.jsonl"
    if not f.exists():
        return []
    return [_case_doc(json.loads(line)) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]


def _case_doc(r: dict) -> dict:
    """반려 기록 1건 → 검색 문서. 임베딩은 반려 의견(+분류)으로, 돌려줄 때는 처리 내용까지"""
    content = (f"반려 의견: {r['comment']}\n처리: {r.get('action', '')}\n"
               f"기준값 변경: {json.dumps(r.get('configChange') or {}, ensure_ascii=False)}"
               + (f"\n미지원: {r['unsupported']}" if r.get("unsupported") else "")
               + f"\n결과: {r.get('outcome', '')}")
    return {"kind": "feedback_case", "sourceId": r["caseId"], "warehouseId": r.get("warehouseId"),
            "title": f"{r.get('targetMonth', '')} 반려 ({r.get('category', '')})", "content": content,
            "embedText": f"{r.get('category', '')} {r['comment']}", "meta": r}


# ---------------------------------------------------------------- 저장소
def _vec_sql(v):
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def _upsert(docs: list[dict]) -> int:
    from psycopg.types.json import Jsonb

    m, vecs = embed_model(), embed([d["embedText"] for d in docs])
    with db.connect() as con:
        con.execute(DDL)
        for d, v in zip(docs, vecs):
            con.execute(
                "INSERT INTO box_agent.knowledge (id, kind, source_id, warehouse_id, title, content, meta, embed_model, "
                "embedding, updated_at) VALUES (%(id)s, %(kind)s, %(sid)s, %(wh)s, %(title)s, %(content)s, %(meta)s, "
                "%(m)s, %(v)s::vector, now()) ON CONFLICT (id) DO UPDATE SET kind = EXCLUDED.kind, "
                "source_id = EXCLUDED.source_id, warehouse_id = EXCLUDED.warehouse_id, title = EXCLUDED.title, "
                "content = EXCLUDED.content, meta = EXCLUDED.meta, embed_model = EXCLUDED.embed_model, "
                "embedding = EXCLUDED.embedding, updated_at = now()",
                {"id": uuid.uuid5(NS, f"{d['kind']}:{d['sourceId']}"), "kind": d["kind"], "sid": d["sourceId"],
                 "wh": d["warehouseId"], "title": d["title"], "content": d["content"], "meta": Jsonb(d["meta"]),
                 "m": m, "v": _vec_sql(v)})
    return len(docs)


_MEM: dict = {}


def _search(kind: str, query: str, k: int, warehouse_id: str | None, min_score: float) -> list[dict]:
    m = embed_model()
    qv = embed([query])[0]
    if db.enabled():
        rows = db.query(
            "SELECT source_id, title, content, meta, 1 - (embedding <=> %(v)s::vector) AS score "
            "FROM box_agent.knowledge WHERE kind = %(kind)s AND embed_model = %(m)s "
            "AND (%(wh)s::uuid IS NULL OR warehouse_id IS NULL OR warehouse_id = %(wh)s::uuid) "
            "ORDER BY embedding <=> %(v)s::vector LIMIT %(k)s",
            {"v": _vec_sql(qv), "kind": kind, "m": m, "wh": warehouse_id, "k": k})
        hits = [{"sourceId": r["source_id"], "title": r["title"], "content": r["content"], "meta": r["meta"],
                 "score": round(float(r["score"]), 3)} for r in rows]
    else:
        if (kind, m) not in _MEM:
            docs = load_policies() if kind == "policy" else load_feedback_cases()
            _MEM[(kind, m)] = list(zip(docs, embed([d["embedText"] for d in docs]))) if docs else []
        scored = [(d, _cos(qv, v)) for d, v in _MEM[(kind, m)]
                  if not warehouse_id or not d["warehouseId"] or d["warehouseId"] == warehouse_id]
        scored.sort(key=lambda x: -x[1])
        hits = [{"sourceId": d["sourceId"], "title": d["title"], "content": d["content"], "meta": d["meta"],
                 "score": round(s, 3)} for d, s in scored[:k]]
    return [h for h in hits if h["score"] >= min_score]


def _center_policies(hits: list[dict], warehouse_id: str | None) -> list[dict]:
    """인덱스는 전사 기본값으로 채운 공통 본문 → 센터 덮어쓰기가 있으면 그 센터 값으로 다시 채운 본문으로 바꾼다"""
    if not warehouse_id or not policy_rules.load_overrides(warehouse_id):
        return hits
    body = {d["sourceId"]: d["content"] for d in load_policies(policy_rules.effective(warehouse_id)["values"])}
    return [{**h, "content": body.get(h["sourceId"], h["content"])} for h in hits]


def _guarded_search(kind, query, k, warehouse_id, min_score):
    if not query or not query.strip():
        raise ToolError("검색어가 비어 있음 — 한국어 질문이나 키워드를 넣어 다시 호출")
    if not 1 <= k <= 10:
        raise ToolError(f"k={k} 범위 밖 — 1~10 으로 다시 호출")
    try:
        return _search(kind, query, k, warehouse_id, min_score)
    except Exception as e:  # 임베딩 API·DB 장애
        raise ToolError(f"검색 실패({type(e).__name__}: {str(e)[:120]}) — 잠시 후 다시 시도하거나 "
                        f"KNOWLEDGE_EMBED=local 로 로컬 검색 사용") from e


# ---------------------------------------------------------------- Tools
@mcp.tool()
def search_policies(query: str, k: int = 3, min_score: float = 0.0, warehouse_id: str | None = None) -> dict:
    """규정 문서에서 query(한국어 질문·키워드)와 관련된 조항을 찾는다. 결과의 sourceId(예: 'POL-02 §2')로 인용한다.
    k 는 1~10. warehouse_id 를 주면 본문 숫자를 그 센터 규칙값으로 채운다. 원문 전체는 Resource policy://{docId} 로 읽는다."""
    return {"items": _center_policies(_guarded_search("policy", query, k, None, min_score), warehouse_id),
            "embedModel": embed_model()}


@mcp.tool()
def search_feedback_cases(comment: str, k: int = 3, warehouse_id: str | None = None, min_score: float = 0.0) -> dict:
    """반려 의견과 비슷한 과거 반려 기록을 찾는다. meta.configChange = 그때 바꾼 기준값,
    meta.unsupported = 기준값으로 못 바꿔 담당자가 처리한 내용, meta.outcome = 재작성 후 결과."""
    return {"items": _guarded_search("feedback_case", comment, k, warehouse_id, min_score), "embedModel": embed_model()}


@mcp.tool(annotations=ToolAnnotations(title="반려 기록 적재", destructiveHint=True, idempotentHint=True))
def add_feedback_case(record: dict) -> dict:
    """반려 기록 1건을 적재한다(caseId, comment 필수). 같은 caseId 는 덮어쓴다. DB_URL 이 없으면 jsonl 에 덧붙인다."""
    if not record.get("caseId") or not record.get("comment"):
        return {"ok": False, "error": "caseId, comment 필수"}
    if db.enabled():
        _upsert([_case_doc(record)])
    else:
        with (KDIR / "feedback_cases.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        _MEM.clear()
    return {"ok": True, "sourceId": record["caseId"]}


@mcp.tool()
def knowledge_status() -> dict:
    """적재 상태: 종류별 건수와 임베딩 방식"""
    if not db.enabled():
        return {"store": "memory(files)", "policy": len(load_policies()), "feedback_case": len(load_feedback_cases()),
                "embedModel": embed_model()}
    rows = db.query("SELECT kind, embed_model, count(*) AS n FROM box_agent.knowledge GROUP BY 1, 2 ORDER BY 1, 2") \
        if db.query("SELECT to_regclass('box_agent.knowledge') AS t")[0]["t"] else []
    return {"store": "postgres box_agent.knowledge", "rows": rows, "embedModel": embed_model()}


# ---------------------------------------------------------------- Resources (원문 읽기)
DOC_DDL = """
CREATE SCHEMA IF NOT EXISTS box_agent;
CREATE TABLE IF NOT EXISTS box_agent.policy_doc (
    doc_id     varchar(20)  PRIMARY KEY,         -- POL-02
    file_name  varchar(200) NOT NULL,            -- 기본 문서 파일 이름(knowledge/policies)
    markdown   text         NOT NULL,            -- {규칙 키} 자리를 그대로 둔 원문 틀
    updated_by varchar(100),
    updated_at timestamptz  NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS box_agent.policy_doc_history (
    id         bigserial    PRIMARY KEY,
    doc_id     varchar(20)  NOT NULL,
    markdown   text         NOT NULL,            -- 저장한 원문(이전 판은 바로 앞 행)
    changed_by varchar(100),
    changed_at timestamptz  NOT NULL DEFAULT now()
);
"""
_DOC_READY = False


def _doc_db() -> bool:
    """고친 문서를 DB 에 두는지. 처음 쓸 때 테이블을 만들고, 비어 있으면 폴더의 고친 문서를 한 번 옮긴다"""
    global _DOC_READY
    if not db.enabled():
        return False
    if not _DOC_READY:
        with db.connect() as con:
            con.execute(DOC_DDL)
            if not con.execute("SELECT 1 FROM box_agent.policy_doc LIMIT 1").fetchone():
                for f in sorted(DOC_OVERRIDE_DIR.glob("*.md")) if DOC_OVERRIDE_DIR.exists() else []:
                    m = re.search(r"(POL-\d+)", f.name)
                    if m:
                        con.execute("INSERT INTO box_agent.policy_doc (doc_id, file_name, markdown, updated_by) "
                                    "VALUES (%s, %s, %s, 'file') ON CONFLICT DO NOTHING", (m.group(1), f.name, f.read_text(encoding="utf-8")))
        _DOC_READY = True
    return True


def _policy_docs() -> dict:
    """docId → {name, text(원문 틀), edited, updatedBy, updatedAt}. 본부장이 고친 문서가 있으면 그것"""
    out = {}
    for f in sorted((KDIR / "policies").glob("*.md")):
        m = re.search(r"(POL-\d+)", f.name)
        if m:
            out[m.group(1)] = {"name": f.name, "text": f.read_text(encoding="utf-8"), "edited": False,
                               "updatedBy": None, "updatedAt": None}
    if _doc_db():
        for r in db.query("SELECT doc_id, markdown, updated_by, updated_at FROM box_agent.policy_doc"):
            if r["doc_id"] in out:
                out[r["doc_id"]].update(text=r["markdown"], edited=True, updatedBy=r["updated_by"],
                                        updatedAt=r["updated_at"].isoformat())
    else:
        for doc_id, d in out.items():
            o = DOC_OVERRIDE_DIR / d["name"]
            if o.exists():
                d.update(text=o.read_text(encoding="utf-8"), edited=True)
    return out


@mcp.tool()
def get_policy_template(doc_id: str) -> dict:
    """규정 문서 원문 틀(고치기용). {규칙 키} 자리는 센터마다 규칙값으로 채워진다. placeholders = 쓸 수 있는 규칙 키와 이름"""
    d = _policy_docs().get(doc_id.upper())
    if not d:
        raise ToolError(f"없는 규정: {doc_id}")
    rules = policy_rules.load_rules()["rules"]
    return {"docId": doc_id.upper(), "markdown": d["text"], "edited": d["edited"], "updatedBy": d["updatedBy"],
            "updatedAt": d["updatedAt"],
            "placeholders": [{"key": k, "label": r.get("label", k)} for k, r in rules.items()]}


@mcp.tool(annotations=ToolAnnotations(title="규정 문서 수정", destructiveHint=True, idempotentHint=True))
def update_policy_document(doc_id: str, markdown: str, actor: str | None = None) -> dict:
    """[변경] 규정 문서(전 센터 공통)를 고쳐 저장하고 그 문서의 검색 색인을 다시 만든다. 본부장이 지침서 화면에서 고칠 때만 부른다.
    첫 줄 '# 제목 (POL-xx)', 조항 '## n. 제목' 형식과 {규칙 키}(규칙 파일에 있는 키만)를 지켜야 한다"""
    doc_id = doc_id.upper()
    doc = _policy_docs().get(doc_id)
    if not doc:
        raise ToolError(f"없는 규정: {doc_id}")
    text = markdown.replace("\r\n", "\n").strip() + "\n"
    first = text.splitlines()[0]
    if not first.startswith("# ") or f"({doc_id})" not in first:
        raise ToolError(f"첫 줄은 '# 제목 ({doc_id})' 형식이어야 합니다")
    if not re.search(r"^## \d+\.", text, re.M):
        raise ToolError("조항이 없습니다. '## 1. 제목' 형식으로 조항을 하나 이상 두세요")
    unknown = sorted({k for k in policy_rules.PLACEHOLDER.findall(text)} - set(policy_rules.load_rules()["rules"]))
    if unknown:
        raise ToolError(f"규칙 파일에 없는 기준값 자리: {', '.join('{' + k + '}' for k in unknown)}")
    if _doc_db():                             # 고친 문서 + 저장 이력(전 판 원문)
        with db.connect() as con:
            con.execute("INSERT INTO box_agent.policy_doc (doc_id, file_name, markdown, updated_by, updated_at) "
                        "VALUES (%s, %s, %s, %s, now()) ON CONFLICT (doc_id) DO UPDATE SET markdown = EXCLUDED.markdown, "
                        "updated_by = EXCLUDED.updated_by, updated_at = now()", (doc_id, doc["name"], text, actor))
            con.execute("INSERT INTO box_agent.policy_doc_history (doc_id, markdown, changed_by) VALUES (%s, %s, %s)",
                        (doc_id, text, actor))
    else:
        DOC_OVERRIDE_DIR.mkdir(parents=True, exist_ok=True)
        (DOC_OVERRIDE_DIR / doc["name"]).write_text(text, encoding="utf-8")
    _MEM.clear()
    sections = [d for d in load_policies() if d["meta"]["docId"] == doc_id]
    if db.enabled():                          # 없어진 조항은 지우고 남은 조항을 다시 임베딩
        with db.connect() as con:
            con.execute("DELETE FROM box_agent.knowledge WHERE kind = 'policy' AND source_id LIKE %s", (f"{doc_id} §%",))
        _upsert(sections)
    return {"docId": doc_id, "sections": len(sections)}


@mcp.resource("policy://index", name="policy-index", mime_type="application/json",
              description="규정 문서 목록(docId, 제목, 조항 제목). 원문은 policy://{doc_id}")
def policy_index() -> dict:
    items = []
    for doc_id, d in _policy_docs().items():
        text = d["text"]
        items.append({"docId": doc_id, "uri": f"policy://{doc_id}", "title": text.splitlines()[0].lstrip("# ").strip(),
                      "sections": [ln.lstrip("# ").strip() for ln in text.splitlines() if ln.startswith("## ")]})
    return {"items": items}


@mcp.resource("policy://{doc_id}", name="policy", mime_type="text/markdown",
              description="규정 원문 markdown (예: policy://POL-02). 검색 결과 sourceId 'POL-02 §2' 의 문서 부분")
def policy_document(doc_id: str) -> str:
    d = _policy_docs().get(doc_id.upper())
    if not d:
        raise ValueError(f"없는 규정: {doc_id}. 있는 규정: {', '.join(_policy_docs())}")
    return policy_rules.render(d["text"])


@mcp.resource("policy://{doc_id}/center/{warehouse_id}", name="policy-center", mime_type="text/markdown",
              description="센터 규칙값으로 채운 규정 원문 (예: policy://POL-02/center/<warehouseId>)")
def policy_document_center(doc_id: str, warehouse_id: str) -> str:
    d = _policy_docs().get(doc_id.upper())
    if not d:
        raise ValueError(f"없는 규정: {doc_id}. 있는 규정: {', '.join(_policy_docs())}")
    return policy_rules.render(d["text"], policy_rules.effective(warehouse_id)["values"])


@mcp.resource("feedback://{case_id}", name="feedback-case", mime_type="application/json",
              description="반려 기록 원본 (예: feedback://FB-2026-03-07)")
def feedback_case(case_id: str) -> dict:
    if db.enabled():
        rows = db.query("SELECT meta FROM box_agent.knowledge WHERE kind = 'feedback_case' AND source_id = %(id)s "
                        "LIMIT 1", {"id": case_id})
        if rows:
            return rows[0]["meta"]
    for d in load_feedback_cases():
        if d["sourceId"] == case_id:
            return d["meta"]
    raise ValueError(f"없는 반려 기록: {case_id}")


def _load_env():
    for p in (HERE.parent / ".env", HERE.parent.parent / "server" / "chagok" / ".env"):
        if p.is_file():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


if __name__ == "__main__":
    if "--ingest" in sys.argv:          # 배치 적재: python knowledge_server.py --ingest
        _load_env()
        if not db.enabled():
            sys.exit("DB_URL 이 없어 적재할 곳이 없음 (DB 없이도 검색은 파일에서 바로 됨)")
        pol, fb = load_policies(), load_feedback_cases()
        print({"policy": _upsert(pol), "feedback_case": _upsert(fb), "embedModel": embed_model()})
    else:
        mcp.run()  # 진입점은 rag_mcp.py (stdio / --http)
