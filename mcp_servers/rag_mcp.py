"""
rag-mcp — 규정 문서·반려 기록 검색 서버 (도구: search_policies, search_feedback_cases, add_feedback_case,
knowledge_status / Resource: policy://index, policy://{doc_id}, feedback://{case_id})

자료 적재(문서가 바뀔 때만): python mcp_servers/knowledge_server.py --ingest

실행
  python mcp_servers/rag_mcp.py                       # stdio
  python mcp_servers/rag_mcp.py --http [--port 9102]  # Streamable HTTP, http://host:9102/mcp
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from knowledge_server import mcp  # noqa: E402
from mcp_common import serve  # noqa: E402

mcp.settings.host = os.getenv("MCP_HOST", "127.0.0.1")
mcp.settings.port = int(os.getenv("MCP_PORT", "9102"))

if __name__ == "__main__":
    serve(mcp, 9102)
