"""
policy-rules — 박스 주문 판단 규칙 조회 (business-mcp 에 포함)

- get_policy_rules : 기본값(rules/box_order_rules.yaml) + 센터별 덮어쓰기(rules/overrides/<warehouseId>.yaml)
  지금은 파일, 나중에는 Spring 내부 API(policy_rule, policy_rule_override)로 바뀐다. 에이전트는 이 도구로만 규칙을 읽는다.
"""
from mcp.server.fastmcp import FastMCP

import policy_rules

mcp = FastMCP("policy-rules", log_level="WARNING")


@mcp.tool()
def get_policy_rules(warehouse_id: str | None = None) -> dict:
    """센터에 적용할 판단 규칙. values = {key: 값}(설정 경로, 예: gate.CATALOG.monthly_saving),
    rows = 규칙별 기본값·적용값·단위·근거 조항·센터 덮어쓰기 여부, rejected = 범위 밖이라 버린 덮어쓰기"""
    return policy_rules.effective(warehouse_id)


if __name__ == "__main__":
    mcp.run()
