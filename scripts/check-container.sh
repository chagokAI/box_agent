#!/usr/bin/env bash
set -Eeuo pipefail
image=${1:?Usage: bash scripts/check-container.sh IMAGE}
name="chagok-agent-check-${RANDOM}-$$"
cleanup() {
  local status=$?
  trap - EXIT
  if [ "$status" -ne 0 ]; then
    for service in agent business rag; do docker logs "$name-$service" >&2 2>/dev/null || true; done
  fi
  docker rm -fv "$name-agent" "$name-business" "$name-rag" "$name-ingest" "$name-db" >/dev/null 2>&1 || true
  docker network rm "$name" >/dev/null 2>&1 || true
  exit "$status"
}
trap cleanup EXIT

docker run --rm --network none --entrypoint python "$image" -c '
from pathlib import Path
import subprocess
assert not Path("/app/.git").exists()
assert not Path("/app/.env").exists()
assert not Path("/app/_handoff").exists()
assert not Path("/app/catalog.db").exists()
subprocess.run(["python", "-m", "pip", "check"], check=True)
'
docker network create "$name" >/dev/null
docker run --detach --name "$name-db" --network "$name" --network-alias postgres \
  --env POSTGRES_DB=chagok_ci --env POSTGRES_USER=postgres --env POSTGRES_PASSWORD=ci-password \
  pgvector/pgvector:0.8.7-pg17 >/dev/null
deadline=$((SECONDS + 120))
until docker exec "$name-db" pg_isready -U postgres -d chagok_ci >/dev/null 2>&1; do
  [ "$SECONDS" -lt "$deadline" ] || { echo 'Test PostgreSQL did not start.' >&2; exit 1; }
  sleep 2
done
db_env=(--env DB_URL=jdbc:postgresql://postgres:5432/chagok_ci --env DB_USERNAME=postgres --env DB_PASSWORD=ci-password --env KNOWLEDGE_EMBED=local)
docker run --rm --name "$name-ingest" --network "$name" "${db_env[@]}" "$image" python initialize_knowledge.py
docker run --detach --name "$name-business" --network "$name" --network-alias business-mcp \
  --env MCP_HOST=0.0.0.0 --env MCP_PORT=9101 --env 'MCP_ALLOWED_HOSTS=business-mcp:*' \
  --env CATALOG_DB=/app/proposals/catalog.db --env INTERNAL_API_TOKEN=ci-token \
  "$image" python mcp_servers/business_mcp.py --http >/dev/null
docker run --detach --name "$name-rag" --network "$name" --network-alias rag-mcp \
  "${db_env[@]}" --env MCP_HOST=0.0.0.0 --env MCP_PORT=9102 --env 'MCP_ALLOWED_HOSTS=rag-mcp:*' \
  "$image" python mcp_servers/rag_mcp.py --http >/dev/null
for service_port in business:9101 rag:9102; do
  service=${service_port%:*}
  port=${service_port#*:}
  deadline=$((SECONDS + 60))
  until docker exec "$name-$service" python -c "import socket; socket.create_connection(('127.0.0.1', $port), 2)" >/dev/null 2>&1; do
    [ "$SECONDS" -lt "$deadline" ] || { echo "Test $service MCP did not start." >&2; exit 1; }
    sleep 2
  done
done
docker run --detach --name "$name-agent" --network "$name" "${db_env[@]}" \
  --env BUSINESS_MCP_URL=http://business-mcp:9101/mcp --env RAG_MCP_URL=http://rag-mcp:9102/mcp \
  --env INTERNAL_API_TOKEN=ci-token "$image" >/dev/null
deadline=$((SECONDS + 90))
until docker exec "$name-agent" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/internal/agent/health', timeout=3)" >/dev/null 2>&1; do
  [ "$(docker inspect --format '{{.State.Running}}' "$name-agent")" = true ] || { echo 'Test agent exited.' >&2; exit 1; }
  [ "$SECONDS" -lt "$deadline" ] || { echo 'Test agent did not start.' >&2; exit 1; }
  sleep 2
done

docker exec -i "$name-agent" python - <<'PY'
import asyncio, json, os, urllib.error, urllib.request
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
with urllib.request.urlopen("http://127.0.0.1:8000/internal/agent/health", timeout=5) as response:
    health = json.load(response)
assert health["status"] == "UP" and set(health["mcp"]) == {"business", "rag"}, health
try:
    urllib.request.urlopen("http://127.0.0.1:8000/internal/agent/tasks/unauthorized", timeout=5)
    raise AssertionError("Internal API must require its token")
except urllib.error.HTTPError as error:
    assert error.code == 401, error.code
async def check():
    for variable, tool, arguments in [
        ("BUSINESS_MCP_URL", "catalog_status", {}),
        ("RAG_MCP_URL", "search_policies", {"query": "신규 규격", "k": 3}),
    ]:
        async with streamablehttp_client(os.environ[variable]) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool, arguments)
                assert not result.isError, result
                payload = json.loads(result.content[0].text)
                if tool == "search_policies":
                    assert payload["items"], payload
asyncio.run(check())
print("Agent, PostgreSQL checkpointer, MCP, local RAG and authentication checks passed.")
PY
