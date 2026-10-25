"""Run the real deployment script with simulated Docker/HTTP process boundaries."""
import fcntl
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

KIND = "agent"
SCRIPT = Path(__file__).resolve().parents[1] / "deploy-agent.sh"
SERVICES = ["business-mcp", "rag-mcp", "agent"] if KIND == "agent" else ["dl-worker"]
REPOSITORY = "ghcr.io/chagokai/box_agent" if KIND == "agent" else "ghcr.io/chagokai/item-dimension-api"
IMAGE = REPOSITORY + "@sha256:" + "f" * 64
NEW_ID = "sha256:" + "e" * 64
OVERRIDE = "agent.image.yaml" if KIND == "agent" else "worker.image.yaml"
PREFIX = "chagok-box-agent-rollback" if KIND == "agent" else "chagok-dimension-rollback"

FAKE = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
root = pathlib.Path(os.environ["MOCK_ROOT"])
path = root / "docker.json"
s = json.loads(path.read_text())
a = sys.argv[1:]
with (root / "commands.jsonl").open("a") as log:
    log.write(json.dumps([pathlib.Path(sys.argv[0]).name, *a]) + "\n")
def save():
    path.write_text(json.dumps(s))
def fail():
    sys.exit(1)
def changed():
    return any(value == s["new"] for value in s["current"].values())
if pathlib.Path(sys.argv[0]).name == "curl":
    if s.get("bad_api") and changed():
        print("<html>SPA fallback</html>")
    else:
        print(json.dumps({"data": {"token": "ci", "headerName": "X-CSRF-TOKEN"}}))
elif a[0] == "compose":
    if "config" in a:
        pass
    elif "ps" in a:
        if not s.get("missing_container"):
            print(a[-1] + "-container")
    elif "stop" in a:
        pass
    elif "up" in a:
        last = len(a) - 1 - a[::-1].index("-f")
        selected = json.loads(pathlib.Path(a[last + 1]).read_text())["services"]
        for service in s["current"]:
            if service in a:
                s["current"][service] = s["images"][selected[service]["image"]]
        save()
        if s.get("bad_start") and changed() or s.get("bad_rollback"):
            fail()
    else:
        fail()
elif a[0] == "inspect":
    service = a[-1].removesuffix("-container")
    if a[2] == "{{.Image}}":
        print(s["current"][service])
    elif a[2] == "{{json .Config.Env}}":
        print(json.dumps(["DB_URL=jdbc:postgresql://postgres:5432/chagok", "DB_USERNAME=app", "DB_PASSWORD=test-only"]))
    elif a[2] == "{{.State.Running}}":
        print("true")
    else:
        print("healthy")
elif a[0] == "pull":
    if s.get("bad_pull"):
        fail()
elif a[:2] == ["image", "inspect"]:
    print(s["images"][a[-1]])
elif a[:2] == ["image", "tag"]:
    s["images"][a[-1]] = a[-2]
    save()
elif a[0] == "run":
    if s.get("bad_preflight"):
        fail()
elif a[0] == "exec":
    if "pg_dump" in a[-1]:
        if s.get("bad_backup"):
            fail()
        print("test database dump")
    elif (s.get("bad_dependency") or s.get("bad_assignment")) and changed():
        fail()
else:
    fail()
'''


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.state_dir = self.root / "releases"
        self.state_dir.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("docker", "curl"):
            path = self.bin / name
            path.write_text(FAKE)
            path.chmod(0o755)
        self.old = {name: "sha256:" + hex(i + 1)[2:] * 64 for i, name in enumerate(SERVICES)}
        self.state = {"current": self.old.copy(), "new": NEW_ID, "images": {IMAGE: NEW_ID}}
        self.save()
        compose = self.root / "compose.yaml"
        compose.write_text("services: {}\n")
        self.lock_file = self.root / "shared.lock"
        self.config = self.root / "deploy.conf"
        variables = {
            "COMPOSE_PROJECT_DIRECTORY": self.root, "COMPOSE_PROJECT_NAME": "existing-stack",
            "DEPLOY_STATE_DIR": self.state_dir, "DEPLOY_LOCK_FILE": self.lock_file,
            "DB_BACKUP_DIR": self.root / "backups", "SMOKE_BASE_URL": "http://127.0.0.1:8080",
            "HTTP_ATTEMPTS": "1", "HTTP_RETRY_DELAY": "0",
        }
        self.config.write_text("".join(f"{key}={shlex.quote(str(value))}\n" for key, value in variables.items()) + f"COMPOSE_FILES=({shlex.quote(str(compose))})\n")

    def save(self):
        (self.root / "docker.json").write_text(json.dumps(self.state))

    def deploy(self, mode="deploy", image=IMAGE):
        return subprocess.run(["bash", str(SCRIPT), mode, image, str(self.config)], env={**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "MOCK_ROOT": str(self.root)}, capture_output=True, text=True, timeout=15)

    def current(self):
        return json.loads((self.root / "docker.json").read_text())["current"]

    def commands(self):
        path = self.root / "commands.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def ups(self):
        return [command for command in self.commands() if command[:2] == ["docker", "compose"] and "up" in command]

    def test_success_updates_only_target_services_and_records_each_old_image(self):
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.current(), {name: NEW_ID for name in SERVICES})
        old = json.loads((self.state_dir / "previous-images.json").read_text())
        self.assertEqual(old, {name: f"{PREFIX}:{name}-{value[7:]}" for name, value in self.old.items()})
        for command in self.ups():
            for flag in ("--no-deps", "--no-build", "--wait"):
                self.assertIn(flag, command)
            self.assertEqual(command[command.index("--pull") + 1], "never")
            self.assertNotIn("backend", command)
            self.assertNotIn("postgres", command)
        self.assertFalse(any("down" in command or "prune" in command for command in self.commands()))
        if KIND == "agent":
            override = json.loads((self.state_dir / OVERRIDE).read_text())["services"]
            self.assertEqual(override["rag-ingest"]["image"], IMAGE)
            self.assertEqual(override["business-mcp"]["environment"]["CATALOG_DB"], "/app/proposals/catalog.db")
            self.assertEqual(override["business-mcp"]["environment"]["DB_URL"], "jdbc:postgresql://postgres:5432/chagok")
            self.assertEqual(override["business-mcp"]["environment"]["DB_PASSWORD"], "test-only")
            self.assertEqual((self.state_dir / OVERRIDE).stat().st_mode & 0o777, 0o600)
            self.assertEqual(len(list((self.root / "backups").glob("*.dump"))), 1)

    def test_failed_pull_preserves_services_and_state(self):
        self.state["bad_pull"] = True
        self.save()
        result = self.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.current(), self.old)
        self.assertEqual(self.ups(), [])
        self.assertFalse((self.state_dir / OVERRIDE).exists())

    def test_failed_native_preflight_preserves_services(self):
        self.state["bad_preflight"] = True
        self.save()
        self.assertNotEqual(self.deploy().returncode, 0)
        self.assertEqual(self.current(), self.old)
        self.assertEqual(self.ups(), [])

    def test_unhealthy_new_release_restores_each_exact_old_image(self):
        self.state["bad_start"] = True
        self.save()
        result = self.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.current(), self.old)
        self.assertIn("Previous services restored", result.stderr)
        self.assertFalse((self.state_dir / "current-images.json").exists())

    def test_html_api_response_rolls_back(self):
        self.state["bad_api"] = True
        self.save()
        result = self.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.current(), self.old)
        self.assertIn("restored", (self.state_dir / "history.jsonl").read_text())

    def test_dependency_or_missing_partition_assignment_rolls_back(self):
        self.state["bad_dependency" if KIND == "agent" else "bad_assignment"] = True
        self.save()
        result = self.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.current(), self.old)

    def test_failed_automatic_rollback_remains_a_failed_deployment(self):
        self.state.update(bad_start=True, bad_rollback=True)
        self.save()
        result = self.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Automatic rollback failed", result.stderr)
        self.assertIn("rollback-failed", (self.state_dir / "history.jsonl").read_text())

    def test_manual_rollback_restores_mixed_images_and_supports_switching_back(self):
        first = self.deploy()
        self.assertEqual(first.returncode, 0, first.stderr)
        result = self.deploy(mode="rollback", image="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.current(), self.old)
        result = self.deploy(mode="rollback", image="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.current(), {name: NEW_ID for name in SERVICES})

    def test_same_image_preserves_rollback_history(self):
        self.assertEqual(self.deploy().returncode, 0)
        before = (self.state_dir / "previous-images.json").read_bytes()
        count = len(self.ups())
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.ups()), count)
        self.assertEqual((self.state_dir / "previous-images.json").read_bytes(), before)

    def test_mutable_or_foreign_image_is_rejected_before_pull(self):
        for image in (REPOSITORY + ":latest", IMAGE.replace("chagokai/", "other/")):
            self.assertNotEqual(self.deploy(image=image).returncode, 0)
        self.assertFalse(any(command[:2] == ["docker", "pull"] for command in self.commands()))

    def test_missing_service_requires_bootstrap(self):
        self.state["missing_container"] = True
        self.save()
        result = self.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("bootstrap the stack first", result.stderr)
        self.assertEqual(self.ups(), [])

    def test_shared_lock_blocks_another_repository_deployment(self):
        with self.lock_file.open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Another stack deployment", result.stderr)
        self.assertEqual(self.commands(), [])

    def test_check_is_read_only_for_services_and_release_history(self):
        result = self.deploy(mode="check", image="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.current(), self.old)
        self.assertEqual(self.ups(), [])
        self.assertFalse((self.state_dir / OVERRIDE).exists())

    @unittest.skipUnless(KIND == "agent", "Only the agent uses PostgreSQL")
    def test_failed_database_backup_leaves_running_services_untouched(self):
        self.state["bad_backup"] = True
        self.save()
        self.assertNotEqual(self.deploy().returncode, 0)
        self.assertEqual(self.current(), self.old)
        self.assertEqual(self.ups(), [])
        self.assertEqual(list((self.root / "backups").glob("*.tmp")), [])
