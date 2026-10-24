"""Preserve restored knowledge; only initialize an empty knowledge store."""
import subprocess
import sys

from mcp_servers import chagok_db as db


def main():
    if db.query("SELECT to_regclass('box_agent.knowledge') AS name")[0]["name"]:
        count = db.query("SELECT count(*) AS n FROM box_agent.knowledge")[0]["n"]
        if count:
            print(f"기존 knowledge {count}건 보존: 최초 색인을 건너뜁니다.", flush=True)
            return
    subprocess.run([sys.executable, "mcp_servers/knowledge_server.py", "--ingest"], check=True)


if __name__ == "__main__":
    main()
