"""규칙 파일·규정 문서·코드가 서로 맞는지 점검한다 (어긋나면 exit 1).

1. 규정 문서 틀의 {키} 가 모두 규칙 파일에 있다
2. 모든 규칙이 규정 문서 어딘가에 {키} 로 쓰인다 (문서에 없는 판단 숫자 방지)
3. 모든 규칙이 에이전트 코드에서 읽힌다 (문서에만 있고 코드가 안 지키는 규칙 방지 — POL-02 §3 같은 누락)
4. 반려 해석 지시문의 기준값 목록 = 규칙 파일의 feedback: true 목록
5. 기본값이 허용 범위 안이다 (load_rules 가 검증)

실행: python tests/policy_rules_check.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp_servers"))
import policy_rules  # noqa: E402

rules = policy_rules.load_rules()["rules"]                       # 5. 범위 검증 포함
docs = {f.name: f.read_text(encoding="utf-8") for f in sorted((ROOT / "knowledge" / "policies").glob("*.md"))}
code = (ROOT / "box_spec_agent.py").read_text(encoding="utf-8")
prompt = (ROOT / "prompts" / "feedback_interpreter.md").read_text(encoding="utf-8")
errors = []

used_in_docs = set()
for name, text in docs.items():                                  # 1
    for key in policy_rules.PLACEHOLDER.findall(text):
        used_in_docs.add(key)
        if key not in rules:
            errors.append(f"{name}: 규칙 파일에 없는 키 {{{key}}}")
for key in rules:                                                # 2
    if key not in used_in_docs:
        errors.append(f"{key}: 규정 문서 어디에도 쓰이지 않음")


def read_in_code(key: str) -> bool:                              # 3. cfg["a"]["b"] 또는 cfg 에서 꺼낸 dict 의 ["b"]
    parts = key.split(".")
    if len(parts) == 1:
        return bool(re.search(rf'\[\s*"{re.escape(key)}"\s*\]', code))
    return bool(re.search(rf'\[\s*"{re.escape(parts[0])}"\s*\]', code))


for key in rules:
    if not read_in_code(key):
        errors.append(f"{key}: 에이전트 코드에서 읽지 않음 (문서에만 있는 규칙)")

fb_rules = {k for k, r in rules.items() if r.get("feedback")}   # 4
fb_prompt = set(re.findall(r"^- ([a-zA-Z_][\w.]*) \(", prompt, re.M))
if fb_rules != fb_prompt:
    errors.append(f"반려 해석 지시문 목록 불일치: 규칙에만 {sorted(fb_rules - fb_prompt)}, 지시문에만 {sorted(fb_prompt - fb_rules)}")

print(f"규칙 {len(rules)}개, 문서 {len(docs)}개 점검")
for e in errors:
    print("  ✗", e)
print("OK" if not errors else f"실패 {len(errors)}건")
sys.exit(1 if errors else 0)
