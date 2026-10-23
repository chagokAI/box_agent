"""
business-mcp — 박스 주문 제안 업무 도구 서버 (조회·계산·제안 저장)

도구는 모듈별로 나눠 두고(wms_server, catalog_server, custom_quote_server, packing_server, forecast_server, proposal_server)
이 서버가 한 곳에서 노출한다. 업무 데이터는 Spring 내부 API(/internal/v1/**)로만 읽고 쓴다(chagok_api.py).

  조회   list_warehouses, list_box_specs, list_product_fits, get_box_inventory, get_shipment_history,
         list_purchase_orders, search_catalog, catalog_status, get_template, list_past_proposals,
         quote_custom_box(유팩 맞춤 제작 참고 견적)
  계산   analyze_fill_rate, diagnose_inefficiency, find_misfit_clusters, find_sku_candidates, ideal_box_dims, coverage, compare_boxsets,
         simulate_boxset, plan_order, forecast_shipments, render_proposal, render_proposal_html
  변경   save_proposal, refresh_catalog            (destructiveHint — 에이전트는 승인 뒤에만 부른다)

실행
  python mcp_servers/business_mcp.py                       # stdio (로컬 개발, 에이전트가 하위 프로세스로 실행)
  python mcp_servers/business_mcp.py --http [--port 9101] [--host 0.0.0.0]  # Streamable HTTP, http://host:9101/mcp
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.fastmcp import FastMCP  # noqa: E402

import catalog_server  # noqa: E402
import custom_quote_server  # noqa: E402
import forecast_server  # noqa: E402
import packing_server  # noqa: E402
import proposal_server  # noqa: E402
import wms_server  # noqa: E402
from mcp_common import serve  # noqa: E402

MODULES = [wms_server, catalog_server, custom_quote_server, packing_server, forecast_server, proposal_server]


def compose(name: str, modules: list) -> FastMCP:
    """모듈별 FastMCP 에 등록된 도구를 이름·설명·annotations 그대로 한 서버에 다시 등록한다"""
    server = FastMCP(name, log_level="WARNING", host=os.getenv("MCP_HOST", "127.0.0.1"),
                     port=int(os.getenv("MCP_PORT", "9101")), streamable_http_path="/mcp")
    for m in modules:
        for t in m.mcp._tool_manager.list_tools():
            server.add_tool(t.fn, name=t.name, title=t.title, description=t.description, annotations=t.annotations)
    return server


mcp = compose("business", MODULES)

if __name__ == "__main__":
    serve(mcp, 9101)
