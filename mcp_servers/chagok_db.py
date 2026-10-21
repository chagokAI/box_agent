"""
차곡 서버(server/chagok)의 PostgreSQL 접속 도우미

- 서버 application.yaml 과 같은 환경변수를 쓴다:
    DB_URL=jdbc:postgresql://localhost:5432/chagok, DB_USERNAME, DB_PASSWORD
- DB_URL이 없으면 enabled() == False → 각 MCP 서버는 Mock 데이터로 동작한다.
- 테이블·컬럼명은 서버 JPA 엔티티(@Table/@Column)를 그대로 따른다. 서버 코드는 수정하지 않는다.
"""
import os
from urllib.parse import urlparse


def enabled() -> bool:
    return bool(os.getenv("DB_URL"))


def _conninfo() -> dict:
    url = os.environ["DB_URL"].removeprefix("jdbc:")          # jdbc:postgresql://host:port/db?...
    u = urlparse(url)
    return {"host": u.hostname or "localhost", "port": u.port or 5432, "dbname": u.path.lstrip("/"),
            "user": os.getenv("DB_USERNAME", u.username or "postgres"),
            "password": os.getenv("DB_PASSWORD", u.password or "")}


def connect():
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(**_conninfo(), row_factory=dict_row, connect_timeout=5)


def query(sql: str, params=None) -> list[dict]:
    with connect() as con:
        return con.execute(sql, params or {}).fetchall()
