#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
mode=${1:?Use deploy IMAGE CONFIG, rollback '' CONFIG or check '' CONFIG}
image=${2:-}
config=${3:?Set the absolute server configuration path}
[[ "$config" = /* && -f "$config" ]] || { echo 'Use an existing absolute config path' >&2; exit 1; }
# Trusted server administrator configuration; Bash arrays hold the Compose file list.
source "$config"
: "${COMPOSE_PROJECT_DIRECTORY:?Set COMPOSE_PROJECT_DIRECTORY}"
: "${COMPOSE_PROJECT_NAME:?Set COMPOSE_PROJECT_NAME}"
: "${DEPLOY_STATE_DIR:?Set DEPLOY_STATE_DIR}"
: "${SMOKE_BASE_URL:?Set SMOKE_BASE_URL}"
[ "${#COMPOSE_FILES[@]}" -gt 0 ] || { echo 'Set COMPOSE_FILES' >&2; exit 1; }
export COMPOSE_PROJECT_DIRECTORY COMPOSE_PROJECT_NAME COMPOSE_ENV_FILE DEPLOY_STATE_DIR
export DEPLOY_LOCK_FILE IMAGE_REPOSITORY DB_BACKUP_DIR SMOKE_BASE_URL PUBLIC_BASE_URL
export HEALTH_TIMEOUT HTTP_ATTEMPTS HTTP_RETRY_DELAY
python3 - "$mode" "$image" "${COMPOSE_FILES[@]}" <<'DEPLOY_PYTHON'
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

KIND = "agent"
SERVICES = ["business-mcp", "rag-mcp", "agent"] if KIND == "agent" else ["dl-worker"]
DEFAULT_REPOSITORY = "ghcr.io/chagokai/box_agent" if KIND == "agent" else "ghcr.io/chagokai/item-dimension-api"
ROLLBACK_PREFIX = "chagok-box-agent-rollback" if KIND == "agent" else "chagok-dimension-rollback"
OVERRIDE_NAME = "agent.image.yaml" if KIND == "agent" else "worker.image.yaml"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def run(*args, input=None, timeout=60):
    return subprocess.run(args, input=input, check=True, text=True, stdout=subprocess.PIPE, timeout=timeout).stdout.strip()


def atomic(path, content):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content)
    temporary.replace(path)


def write_json(path, value):
    atomic(path, json.dumps(value, indent=2) + "\n")


def interrupt(signum, frame):
    raise RuntimeError(f"Deployment interrupted by signal {signum}")


def main():
    mode, image, *files = sys.argv[1:]
    require(mode in {"deploy", "rollback", "check"}, "Use deploy IMAGE CONFIG, rollback '' CONFIG or check '' CONFIG")
    root = Path(os.environ["COMPOSE_PROJECT_DIRECTORY"])
    state = Path(os.environ["DEPLOY_STATE_DIR"])
    require(root.is_absolute() and root.is_dir() and state.is_absolute(), "Use existing absolute server paths")
    require(files and all(Path(f).is_absolute() and Path(f).is_file() for f in files), "COMPOSE_FILES must contain existing absolute paths")
    health_timeout = int(os.getenv("HEALTH_TIMEOUT", "300"))
    attempts = int(os.getenv("HTTP_ATTEMPTS", "8"))
    delay = float(os.getenv("HTTP_RETRY_DELAY", "3"))
    require(health_timeout > 0 and attempts > 0 and delay >= 0, "Invalid health or retry settings")
    state.mkdir(parents=True, exist_ok=True)
    lock_path = Path(os.getenv("DEPLOY_LOCK_FILE", str(state / "deploy.lock")))
    require(lock_path.is_absolute(), "DEPLOY_LOCK_FILE must be absolute")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = lock_path.open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError("Another stack deployment is already running") from None

    compose = ["docker", "compose", "--project-directory", str(root), "--project-name", os.environ["COMPOSE_PROJECT_NAME"]]
    if env_file := os.getenv("COMPOSE_ENV_FILE"):
        require(Path(env_file).is_absolute() and Path(env_file).is_file(), "Compose environment file is missing")
        compose += ["--env-file", env_file]
    override = state / OVERRIDE_NAME
    require(str(override) not in files, "Own image override is managed by this script; omit it from COMPOSE_FILES")
    for path in files:
        compose += ["-f", path]
    active = compose + (["-f", str(override)] if override.exists() else [])
    run(*active, "config", "--quiet")

    def container(service):
        value = run(*active, "ps", "--status", "running", "-q", service)
        require(value and "\n" not in value, f"Expected exactly one running {service}; bootstrap the stack first")
        return value

    containers = {service: container(service) for service in SERVICES}
    old_ids = {service: run("docker", "inspect", "--format", "{{.Image}}", cid) for service, cid in containers.items()}
    require(all(re.fullmatch(r"sha256:[a-f0-9]{64}", value) for value in old_ids.values()), "Cannot determine running image IDs")
    previous = {service: f"{ROLLBACK_PREFIX}:{service}-{value[7:]}" for service, value in old_ids.items()}

    def smoke_once():
        for service in SERVICES:
            cid = container(service)
            require(run("docker", "inspect", "--format", "{{.State.Running}}", cid) == "true", f"{service} is stopped")
            status = run("docker", "inspect", "--format", "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}", cid)
            require(status in {"healthy", "none"}, f"{service} health is {status}")
        if KIND == "agent":
            run("docker", "exec", container("agent"), "python", "-c", AGENT_SMOKE, timeout=60)
        else:
            # This works for old images without the new heartbeat module as well.
            # Only the member at this container's IP counts; another worker cannot mask a bad release.
            run("docker", "exec", container("dl-worker"), "python", "-c", WORKER_SMOKE, timeout=30)
        for base in (os.environ["SMOKE_BASE_URL"], os.getenv("PUBLIC_BASE_URL", "")):
            if not base:
                continue
            raw = run("curl", "--fail", "--silent", "--show-error", "--connect-timeout", "5", "--max-time", "10", base.rstrip("/") + "/api/v1/auth/csrf", timeout=15)
            data = json.loads(raw)["data"]
            require(all(isinstance(data.get(key), str) and data[key] for key in ("token", "headerName")), "Invalid backend CSRF response")

    def smoke():
        for attempt in range(attempts):
            try:
                smoke_once()
                return
            except (RuntimeError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
                if attempt == attempts - 1:
                    raise RuntimeError(f"Smoke checks failed: {error}") from error
                time.sleep(delay)

    if mode == "check":
        smoke()
        print(f"{KIND}: existing services, Kafka/MCP connections and backend checks passed.")
        return

    if mode == "rollback":
        record = state / "previous-images.json"
        require(record.is_file(), "No previous successful deployment is recorded")
        target = json.loads(record.read_text())
        require(isinstance(target, dict) and set(target) == set(SERVICES), "Invalid rollback service list")
        for service, reference in target.items():
            require(isinstance(reference, str) and re.fullmatch(re.escape(f"{ROLLBACK_PREFIX}:{service}-") + r"[a-f0-9]{64}", reference), "Invalid recorded rollback image")
    else:
        repository = os.getenv("IMAGE_REPOSITORY", DEFAULT_REPOSITORY)
        require(re.fullmatch(re.escape(repository) + r"@sha256:[a-f0-9]{64}", image), "Use the configured GHCR repository with a full sha256 digest")
        run("docker", "pull", image, timeout=600)
        target = {service: image for service in SERVICES}
    new_ids = {service: run("docker", "image", "inspect", "--format", "{{.Id}}", reference) for service, reference in target.items()}
    if new_ids == old_ids:
        smoke()
        print("The requested images are already running; rollback history was preserved.")
        return

    if mode == "deploy":
        imports = "import agent_server; import mcp_servers.business_mcp; import mcp_servers.rag_mcp" if KIND == "agent" else "import app.worker, app.main, app.loader, app.healthcheck"
        run("docker", "run", "--rm", "--network", "none", "--entrypoint", "python", image, "-c", imports, timeout=90)
        if KIND == "agent":
            # The old image may contain the only catalog copy. Preserve it in the existing writable volume.
            run("docker", "exec", containers["business-mcp"], "python", "-c", CATALOG_BACKUP)
            backup_dir = Path(os.environ["DB_BACKUP_DIR"])
            require(backup_dir.is_absolute(), "DB_BACKUP_DIR must be absolute")
            backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            database = container("postgres")
            backup = backup_dir / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + f"-{os.getpid()}.dump")
            temporary = backup.with_suffix(".tmp")
            try:
                with temporary.open("wb") as output:
                    subprocess.run(["docker", "exec", database, "sh", "-c", 'PGPASSWORD="$POSTGRES_PASSWORD" exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom'], check=True, stdout=output, timeout=180)
                require(temporary.stat().st_size > 0, "Database backup was empty")
                temporary.replace(backup)
            finally:
                temporary.unlink(missing_ok=True)
            print(f"Database backup saved: {backup}")
        else:
            # Validate the candidate against the server's mounted model and cached DINOv2 weights.
            # Read-only mounts and offline mode prevent modifying the live model/cache during preflight.
            run("docker", "run", "--rm", "--network", "none", "--volumes-from", containers["dl-worker"] + ":ro", "--env", "DEVICE=cpu", "--env", "HF_HOME=/app/.cache/huggingface", "--env", "HF_HUB_OFFLINE=1", "--env", "TRANSFORMERS_OFFLINE=1", "--entrypoint", "python", image, "-c", "from app.loader import load_measurer; load_measurer(); print('Server model and cached weights loaded successfully')", timeout=180)

    for service, reference in previous.items():
        run("docker", "image", "tag", old_ids[service], reference)
    release = compose + ["-f", str(override)]
    active[:] = release

    def write_override(images):
        services = {service: {"image": reference} for service, reference in images.items()}
        if KIND == "agent":
            services["business-mcp"]["environment"] = {"CATALOG_DB": "/app/proposals/catalog.db"}
            services["rag-ingest"] = {"image": images["agent"]}
        write_json(override, {"services": services})

    def start():
        flags = ["up", "--detach", "--no-deps", "--no-build", "--pull", "never", "--wait", "--wait-timeout", str(health_timeout)]
        if KIND == "agent":
            # Close the old MCP sessions before replacing their servers, then reconnect the agent.
            run(*release, "stop", "--timeout", "60", "agent", timeout=90)
            run(*release, *flags, "business-mcp", "rag-mcp", timeout=health_timeout + 90)
            run(*release, *flags, "--force-recreate", "agent", timeout=health_timeout + 90)
        else:
            run(*release, *flags, "dl-worker", timeout=health_timeout + 90)

    def history(action, images):
        with (state / "history.jsonl").open("a") as output:
            output.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "action": action, "images": images}) + "\n")

    records = {name: (state / name).read_bytes() if (state / name).exists() else None for name in ("current-images.json", "previous-images.json")}
    for signum in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, interrupt)
    try:
        write_override(target)
        start()
        smoke()
        write_json(state / "previous-images.json", previous)
        write_json(state / "current-images.json", target)
        history(mode, target)
    except Exception:
        print("Deployment failed; restoring the exact previous service images.", file=sys.stderr)
        for signum in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, signal.SIG_IGN)
        try:
            write_override(previous)
            start()
            smoke()
            history("restored", previous)
            print("Previous services restored. This deployment remains failed.", file=sys.stderr)
        except Exception as restore_error:
            history("rollback-failed", previous)
            print(f"ERROR: Automatic rollback failed: {restore_error}", file=sys.stderr)
        finally:
            for name, content in records.items():
                if content is None:
                    (state / name).unlink(missing_ok=True)
                else:
                    atomic(state / name, content.decode())
        raise
    print(f"{KIND} {mode} succeeded: {json.dumps(target)}")


CATALOG_BACKUP = r'''
import os, sqlite3
from pathlib import Path
source = Path(os.getenv("CATALOG_DB", "/app/catalog.db"))
target = Path("/app/proposals/catalog.db")
if not target.exists():
    if not source.is_file():
        raise RuntimeError("The existing catalog is missing; restore catalog.db before deployment")
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as old, sqlite3.connect(target) as new:
        old.backup(new)
with sqlite3.connect(f"file:{target}?mode=ro", uri=True) as catalog:
    count = catalog.execute("SELECT count(*) FROM catalog_boxes").fetchone()[0]
    if not count:
        raise RuntimeError("The preserved catalog is empty")
print(f"Persistent catalog ready: {count} boxes")
'''

AGENT_SMOKE = r'''
import asyncio, json, os, urllib.request
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
with urllib.request.urlopen("http://127.0.0.1:8000/internal/agent/health", timeout=5) as response:
    health = json.load(response)
assert health["status"] == "UP" and set(health["mcp"]) == {"business", "rag"}, health
async def check():
    for name, variable, tool, arguments in [
        ("business", "BUSINESS_MCP_URL", "catalog_status", {}),
        ("rag", "RAG_MCP_URL", "search_policies", {"query": "신규 규격", "k": 3}),
    ]:
        async with streamablehttp_client(os.environ[variable]) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool, arguments)
                assert not result.isError, f"{name}: {result}"
                payload = json.loads(result.content[0].text)
                if name == "business":
                    assert payload["items"] > 0, "Persistent catalog is empty"
                if name == "rag":
                    assert payload["items"], "Knowledge search returned no policies"
asyncio.run(check())
print("Agent, business MCP catalog and RAG policy search passed")
'''

WORKER_SMOKE = r'''
import os, socket
from confluent_kafka.admin import AdminClient
admin = AdminClient({"bootstrap.servers": os.environ["KAFKA_BOOTSTRAP_SERVERS"]})
group = os.getenv("DL_WORKER_GROUP", "chagok-dl-worker")
topic = os.getenv("DL_REQUEST_TOPIC", "chagok.dl.analysis-request.v1")
ip = socket.gethostbyname(socket.gethostname())
status = admin.describe_consumer_groups([group], request_timeout=10)[group].result(15)
assert any(member.host.lstrip("/") == ip and any(partition.topic == topic for partition in member.assignment.topic_partitions) for member in status.members), "This worker has no request partition assignment"
print("This worker has a live Kafka request partition assignment")
'''

if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
DEPLOY_PYTHON
