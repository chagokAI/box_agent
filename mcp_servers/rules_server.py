"""
policy-rules — 박스 주문 판단 규칙 조회 (business-mcp 에 포함)

- get_policy_rules : 기본값(rules/box_order_rules.yaml) + 센터별 덮어쓰기(rules/overrides/<warehouseId>.yaml)
- set_center_overrides : 본부 승인한 조치(ESG 우선 모드·규격 상한 등)를 센터 덮어쓰기에 저장(변경 도구)
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


@mcp.tool()
def set_center_overrides(warehouse_id: str, changes: dict) -> dict:
    """[변경] 센터별 규칙 덮어쓰기에 changes({규칙 키: 값, null = 기본값으로})를 합쳐 저장한다.
    본부장 승인 뒤(본부 리포트 에이전트)나 본부장이 지침서 화면에서 고칠 때만 부른다.
    형·범위·센터 조정 가능 여부는 규칙 파일 기준으로 검증하고, 맞지 않으면 오류. 반환: 저장 후 그 센터의 덮어쓰기 전체"""
    return {"warehouseId": warehouse_id, "overrides": policy_rules.save_overrides(warehouse_id, changes)}


if __name__ == "__main__":
    mcp.run()
