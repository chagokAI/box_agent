"""
박스 주문 규칙 로더 — 기본값·범위는 rules/box_order_rules.yaml(코드와 함께 검토·배포), 센터별 조정값은 DB 에 둔다.

- 센터 조정: DB_URL 이 있으면 box_agent.center_rule(센터 × 규칙 키 → 값) + center_rule_history(바꿀 때마다 이전→새 값·이유·바꾼 사람).
  DB 가 없으면(로컬 CLI·테스트) rules/overrides/<warehouseId>.yaml 파일. DB 를 처음 쓸 때 파일 조정값이 있으면 한 번 옮긴다.
- business-mcp get_policy_rules 와 rag(규정 문서 숫자 채우기)가 같이 쓴다.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

import chagok_db as db

ROOT = Path(__file__).resolve().parent.parent
RULES_PATH = Path(os.getenv("POLICY_RULES", ROOT / "rules" / "box_order_rules.yaml"))
OVERRIDE_DIR = Path(os.getenv("POLICY_RULE_OVERRIDES", ROOT / "rules" / "overrides"))
PLACEHOLDER = re.compile(r"\{([a-zA-Z_][\w.]*)\}")
DDL = """
CREATE SCHEMA IF NOT EXISTS box_agent;
CREATE TABLE IF NOT EXISTS box_agent.center_rule (
    warehouse_id uuid         NOT NULL,
    rule_key     varchar(80)  NOT NULL,
    value        jsonb        NOT NULL,
    updated_by   varchar(100),
    updated_at   timestamptz  NOT NULL DEFAULT now(),
    PRIMARY KEY (warehouse_id, rule_key)
);
CREATE TABLE IF NOT EXISTS box_agent.center_rule_history (
    id           bigserial    PRIMARY KEY,
    warehouse_id uuid         NOT NULL,
    rule_key     varchar(80)  NOT NULL,
    before_value jsonb,                          -- NULL = 기본값이었음
    after_value  jsonb,                          -- NULL = 기본값으로 되돌림
    reason       varchar(200),
    changed_by   varchar(100),
    changed_at   timestamptz  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_center_rule_history_wh ON box_agent.center_rule_history (warehouse_id, changed_at DESC);
"""
_READY = False


def _db() -> bool:
    """DB 저장을 쓰는지. 처음 쓸 때 테이블을 만들고, 비어 있으면 파일 조정값을 한 번 옮긴다"""
    global _READY
    if not db.enabled():
        return False
    if not _READY:
        from psycopg.types.json import Jsonb
        with db.connect() as con:
            con.execute(DDL)
            if not con.execute("SELECT 1 FROM box_agent.center_rule LIMIT 1").fetchone():
                for f in sorted(OVERRIDE_DIR.glob("*.yaml")) if OVERRIDE_DIR.exists() else []:
                    for k, v in (yaml.safe_load(f.read_text(encoding="utf-8")) or {}).items():
                        con.execute("INSERT INTO box_agent.center_rule (warehouse_id, rule_key, value, updated_by) "
                                    "VALUES (%s, %s, %s, 'file') ON CONFLICT DO NOTHING", (f.stem, k, Jsonb(v)))
                        con.execute("INSERT INTO box_agent.center_rule_history (warehouse_id, rule_key, after_value, reason, "
                                    "changed_by) VALUES (%s, %s, %s, '파일 조정값 옮김', 'file')", (f.stem, k, Jsonb(v)))
        _READY = True
    return True


def load_rules() -> dict:
    """{'version', 'updated_at', 'rules': {key: rule}}"""
    doc = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    rules = {r["key"]: r for r in doc["rules"]}
    for r in rules.values():
        r["value"] = cast(r, r["value"])          # 기본값도 같은 검증
    return {"version": doc["version"], "updated_at": str(doc.get("updated_at", "")), "rules": rules}


def cast(rule: dict, raw):
    """형식·범위 검증. 맞지 않으면 ValueError"""
    t, rng = rule["type"], rule.get("range")
    if t == "bool":
        if isinstance(raw, bool):
            return raw
        if str(raw).strip().lower() in ("true", "false"):
            return str(raw).strip().lower() == "true"
        raise ValueError(f"{rule['key']}: true/false 가 아님({raw})")
    if t == "int_list":
        vals = [int(v) for v in (raw if isinstance(raw, list) else str(raw).split(","))]
        if rng and any(not rng[0] <= v <= rng[1] for v in vals):
            raise ValueError(f"{rule['key']}: 허용 범위 {rng[0]}~{rng[1]} 밖({vals})")
        return vals
    v = float(str(raw).replace(",", ""))
    v = int(round(v)) if t == "int" else v
    if rng and not rng[0] <= v <= rng[1]:
        raise ValueError(f"{rule['key']}: 허용 범위 {rng[0]}~{rng[1]} 밖({v})")
    return v


def load_overrides(warehouse_id: str | None) -> dict:
    """센터별 덮어쓰기 {key: value}. DB box_agent.center_rule, DB 가 없으면 파일 rules/overrides/<warehouseId>.yaml"""
    if not warehouse_id:
        return {}
    if _db():
        return {r["rule_key"]: r["value"] for r in
                db.query("SELECT rule_key, value FROM box_agent.center_rule WHERE warehouse_id = %(w)s", {"w": warehouse_id})}
    f = OVERRIDE_DIR / f"{warehouse_id}.yaml"
    return (yaml.safe_load(f.read_text(encoding="utf-8")) or {}) if f.exists() else {}


def history(warehouse_id: str, limit: int = 50) -> list[dict]:
    """센터 규칙 변경 이력(최신순). DB 가 없으면 빈 목록"""
    if not _db():
        return []
    return [{**r, "changedAt": r.pop("changed_at").isoformat()} for r in db.query(
        "SELECT rule_key AS key, before_value AS before, after_value AS after, reason, changed_by AS \"changedBy\", changed_at "
        "FROM box_agent.center_rule_history WHERE warehouse_id = %(w)s ORDER BY changed_at DESC, id DESC LIMIT %(n)s",
        {"w": warehouse_id, "n": limit})]


def save_overrides(warehouse_id: str, changes: dict, actor: str | None = None, reason: str | None = None) -> dict:
    """센터 덮어쓰기에 changes 를 합쳐 저장한다(본부 승인·본부장 지침서 수정). 값이 None 이면 그 키의 덮어쓰기를 지워 기본값으로.
    없는 규칙·전사 고정·범위 밖 값은 ValueError"""
    rules = load_rules()["rules"]
    clean, drop = {}, set()
    for k, raw in changes.items():
        r = rules.get(k)
        if not r:
            raise ValueError(f"{k}: 없는 규칙")
        if r.get("scope") != "center":
            raise ValueError(f"{k}: 전사 고정 규칙이라 센터별로 바꿀 수 없음")
        if raw is None:
            drop.add(k)
            continue
        clean[k] = cast(r, raw)
    if _db():                                # 한 트랜잭션: 조정값 반영 + 바뀐 것만 이력
        from psycopg.types.json import Jsonb
        with db.connect() as con:
            before = {r["rule_key"]: r["value"] for r in con.execute(
                "SELECT rule_key, value FROM box_agent.center_rule WHERE warehouse_id = %s FOR UPDATE", (warehouse_id,))}
            for k, v in clean.items():
                if before.get(k) == v:
                    continue
                con.execute("INSERT INTO box_agent.center_rule (warehouse_id, rule_key, value, updated_by, updated_at) "
                            "VALUES (%s, %s, %s, %s, now()) ON CONFLICT (warehouse_id, rule_key) DO UPDATE SET "
                            "value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, updated_at = now()",
                            (warehouse_id, k, Jsonb(v), actor))
                con.execute("INSERT INTO box_agent.center_rule_history (warehouse_id, rule_key, before_value, after_value, "
                            "reason, changed_by) VALUES (%s, %s, %s, %s, %s, %s)",
                            (warehouse_id, k, Jsonb(before[k]) if k in before else None, Jsonb(v), reason, actor))
            for k in drop & set(before):
                con.execute("DELETE FROM box_agent.center_rule WHERE warehouse_id = %s AND rule_key = %s", (warehouse_id, k))
                con.execute("INSERT INTO box_agent.center_rule_history (warehouse_id, rule_key, before_value, after_value, "
                            "reason, changed_by) VALUES (%s, %s, %s, NULL, %s, %s)",
                            (warehouse_id, k, Jsonb(before[k]), reason, actor))
        return load_overrides(warehouse_id)
    cur = load_overrides(warehouse_id)
    cur.update(clean)
    for k in drop:
        cur.pop(k, None)
    OVERRIDE_DIR.mkdir(parents=True, exist_ok=True)
    (OVERRIDE_DIR / f"{warehouse_id}.yaml").write_text(yaml.safe_dump(cur, allow_unicode=True, sort_keys=True), encoding="utf-8")
    return cur


def effective(warehouse_id: str | None = None) -> dict:
    """기본값 + 센터별 덮어쓰기. 범위 밖·전사 고정(global) 키의 덮어쓰기는 버리고 rejected 에 남긴다"""
    base = load_rules()
    values = {k: r["value"] for k, r in base["rules"].items()}
    applied, rejected = {}, []
    for k, raw in load_overrides(warehouse_id).items():
        r = base["rules"].get(k)
        if not r:
            rejected.append(f"{k}: 없는 규칙")
            continue
        if r.get("scope") != "center":
            rejected.append(f"{k}: 전사 고정 규칙이라 센터별로 바꿀 수 없음")
            continue
        try:
            values[k] = cast(r, raw)
            applied[k] = values[k]
        except ValueError as e:
            rejected.append(str(e))
    rows = [{"key": k, "value": values[k], "default": r["value"], "overridden": k in applied,
             "unit": r.get("unit", ""), "label": r.get("label", k), "source": r.get("source", ""),
             "scope": r.get("scope", "global"), "feedback": bool(r.get("feedback")), "hqTune": bool(r.get("hq_tune")),
             "type": r["type"],
             "range": r.get("range")} for k, r in base["rules"].items()]
    return {"version": base["version"], "updatedAt": base["updated_at"], "warehouseId": warehouse_id,
            "values": values, "rows": rows, "overrides": applied, "rejected": rejected}


def fmt(rule: dict, value) -> str:
    """문서·화면 표기: 300000 원 → '30만 원', 10 %p → '10%p', bool → 켜기/끄기"""
    u = rule.get("unit", "")
    if rule["type"] == "bool":
        return "켜기" if value else "끄기"
    if rule["type"] == "int_list":
        return ", ".join(str(v) for v in value)
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if u == "원":
        return f"{value // 10000:,}만 원" if value >= 10000 and value % 10000 == 0 else f"{value:,}원"
    return f"{value:,}{u}"


def render(text: str, values: dict | None = None) -> str:
    """규정 문서 틀의 {key} 를 규칙값으로 채운다(모르는 키는 그대로 두어 점검에서 걸리게)"""
    base = load_rules()["rules"]
    values = values or {k: r["value"] for k, r in base.items()}
    return PLACEHOLDER.sub(lambda m: fmt(base[m.group(1)], values[m.group(1)]) if m.group(1) in base else m.group(0),
                           text)
