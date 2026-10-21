"""business-mcp·rag-mcp 공통 실행: --http(또는 MCP_TRANSPORT=http)면 Streamable HTTP(/mcp), 아니면 stdio

HTTP 환경변수: MCP_HOST(기본 127.0.0.1, 컨테이너는 0.0.0.0), MCP_PORT, MCP_ALLOWED_HOSTS(허용 Host 헤더, 예 business-mcp:*)
"""
import os
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

ROOT = Path(__file__).resolve().parent.parent


def load_env() -> None:
    """따로 띄울 때(HTTP) box_agent/.env, server/chagok/.env 를 읽는다. 이미 있는 환경변수가 우선(compose env)"""
    for p in (ROOT / ".env", ROOT.parent / "server" / "chagok" / ".env"):
        if p.is_file():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def serve(server: FastMCP, default_port: int) -> None:
    if "--http" in sys.argv or os.getenv("MCP_TRANSPORT") == "http":
        load_env()
        if "--port" in sys.argv:
            server.settings.port = int(sys.argv[sys.argv.index("--port") + 1])
        elif not os.getenv("MCP_PORT"):
            server.settings.port = default_port
        if "--host" in sys.argv:
            server.settings.host = sys.argv[sys.argv.index("--host") + 1]
        elif os.getenv("MCP_HOST"):
            server.settings.host = os.environ["MCP_HOST"]
        if server.settings.host not in ("127.0.0.1", "localhost", "::1"):
            # FastMCP 는 생성 시점 host 가 localhost 면 Host 헤더를 localhost 로만 제한한다(DNS rebinding 방지).
            # 컨테이너에서는 서비스 이름(business-mcp:9101)으로 오므로 허용 목록을 다시 정한다
            from mcp.server.transport_security import TransportSecuritySettings
            allowed = [h.strip() for h in os.getenv("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
            server.settings.transport_security = TransportSecuritySettings(
                enable_dns_rebinding_protection=bool(allowed),
                allowed_hosts=allowed + ["127.0.0.1:*", "localhost:*"], allowed_origins=[])
        server.run(transport="streamable-http")
    else:
        server.run()
