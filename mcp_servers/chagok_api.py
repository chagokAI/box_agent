"""
차곡 Spring Boot 내부 API(/internal/v1/**) 클라이언트 — business-mcp 가 업무 데이터를 읽고 제안을 저장하는 유일한 경로

- 업무 규칙(기간 해석, 센터 검증, 저장 검증)은 Spring 한 곳에 두고, business-mcp 는 DB 에 직접 접근하지 않는다.
- 접속: BUSINESS_API_URL(예: http://localhost:8080, compose 에서는 http://backend:8080),
  INTERNAL_API_TOKEN(Spring 의 INTERNAL_API_TOKEN 과 같은 값, X-Internal-Token 헤더).
  BUSINESS_API_URL 이 없으면 enabled() == False → 각 도구는 Mock 데이터로 동작한다.
- 응답은 {data, meta} 봉투. data 만 돌려준다. 429·502·503·504·연결 오류는 2회 재시도한다.
"""
import os
import time

import httpx

RETRY_STATUS = {429, 502, 503, 504}


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"차곡 내부 API {status}: {message}")
        self.status = status


def enabled() -> bool:
    return bool(os.getenv("BUSINESS_API_URL"))


def _request(method: str, path: str, *, params=None, body=None, retries: int = 2):
    url = os.environ["BUSINESS_API_URL"].rstrip("/") + "/internal/v1" + path
    headers = {"X-Internal-Token": os.getenv("INTERNAL_API_TOKEN", "")}
    last = None
    for attempt in range(retries + 1):
        try:
            r = httpx.request(method, url, params=params, json=body, headers=headers, timeout=30)
        except httpx.TransportError as e:
            last = ApiError(0, f"연결 실패({type(e).__name__}) — Spring 서버({url})가 실행 중인지 확인")
        else:
            if r.status_code < 400:
                return r.json()["data"]
            try:
                err = r.json().get("error") or {}
                msg = err.get("message") or r.text[:200]
                if err.get("fieldErrors"):
                    msg += " " + "; ".join(f"{f.get('field')}: {f.get('message')}" for f in err["fieldErrors"])
            except ValueError:
                msg = r.text[:200]
            last = ApiError(r.status_code, msg)
            if r.status_code not in RETRY_STATUS:
                raise last
        if attempt < retries:
            time.sleep(0.5 * (attempt + 1))
    raise last


def get(path: str, **params):
    return _request("GET", path, params={k: v for k, v in params.items() if v is not None})


def put(path: str, body: dict):
    return _request("PUT", path, body=body)
