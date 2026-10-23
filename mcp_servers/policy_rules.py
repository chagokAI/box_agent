"""
박스 주문 규칙 로더 — rules/box_order_rules.yaml (+ rules/overrides/<warehouseId>.yaml) 를 읽어 검증한다.

business-mcp get_policy_rules 와 rag 적재(규정 문서 숫자 채우기)가 같이 쓴다.
나중에 DB(policy_rule, policy_rule_override)로 옮기면 load_rules / load_overrides 안쪽만 바꾼다.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
RULES_PATH = Path(os.getenv("POLICY_RULES", ROOT / "rules" / "box_order_rules.yaml"))
OVERRIDE_DIR = Path(os.getenv("POLICY_RULE_OVERRIDES", ROOT / "rules" / "overrides"))
PLACEHOLDER = re.compile(r"\{([a-zA-Z_][\w.]*)\}")


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
    """센터별 덮어쓰기 {key: value}. 파일 rules/overrides/<warehouseId>.yaml (없으면 빈 값)"""
    if not warehouse_id:
        return {}
    f = OVERRIDE_DIR / f"{warehouse_id}.yaml"
    return (yaml.safe_load(f.read_text(encoding="utf-8")) or {}) if f.exists() else {}


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
             "scope": r.get("scope", "global"), "feedback": bool(r.get("feedback")), "type": r["type"],
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
