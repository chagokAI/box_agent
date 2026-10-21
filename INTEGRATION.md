# 차곡 연동 가이드 — FE · BE · Agent(FastAPI) · MCP

박스 주문 화면의 [개선 실행] → Spring → 에이전트 서버 → MCP → Spring 내부 API 로 이어지는 연동에 필요한 설정과 DB 구조.
DB 는 에이전트 실험에 쓴 DB(`db/chagok.dump`)를 최종본으로 다 같이 쓴다.

- 브랜치: server `feat/agent-internal-api`, client `feat/agent-improve-run`, box_agent `main`
- ⚠️ `.env` 값(키·토큰·비밀번호)은 git·공개 채널에 올리지 않는다. 실제 값은 `env/` 폴더 파일을 받는다.

---

## 1. 구성과 연결

```
[브라우저] ─▶ FE (Vue, :5173 개발 / :80 배포)
               │  /api/v1/**  (세션 쿠키 + CSRF)
               ▼
            BE (Spring, :8080)  ──────────────────────────────▶ PostgreSQL (pgvector, :5432 / 로컬 15432)
               │  ▲  POST·GET /internal/agent/** (X-Internal-Token)             ▲            ▲
               ▼  │  SSE 진행 이벤트                                               │            │
            Agent (FastAPI, :8000) ── checkpointer(box_agent 스키마) ──────────────┘            │
               │  MCP Streamable HTTP                                                          │
               ├─▶ business-mcp (:9101/mcp) ── GET·PUT /internal/v1/** (X-Internal-Token) ─▶ BE │
               └─▶ rag-mcp (:9102/mcp) ── 벡터 검색(box_agent.knowledge) ─────────────────────────┘
            Agent ─▶ OpenAI API, LangSmith (외부)
```

| 서비스 | 위치 | 포트 | 공개 | 누가 부르나 |
|---|---|---|---|---|
| FE | `client` | 5173(개발) / 80 | 공개 | 브라우저 |
| BE | `server/chagok` | 8080 | 공개 | FE, business-mcp |
| Agent | `box_agent/agent_server.py` | 8000 | 내부 | BE |
| business-mcp | `box_agent/mcp_servers/business_mcp.py` | 9101 | 내부 | Agent |
| rag-mcp | `box_agent/mcp_servers/rag_mcp.py` | 9102 | 내부 | Agent |
| PostgreSQL | `server/chagok/compose.yaml` | 5432(컨테이너) / 15432(로컬) | 내부 | BE, Agent, rag-mcp |

**규칙**: FE 는 BE 만 부른다. Agent·MCP 는 DB 업무 데이터를 직접 쓰지 않는다(BE 내부 API 경유). LLM 은 Agent 만 부른다.

---

## 2. 환경변수 (.env)

### 반드시 서로 같아야 하는 값

| 값 | BE (`server/chagok/.env`) | Agent·MCP (`box_agent/.env`) | 용도 |
|---|---|---|---|
| 내부 토큰 | `INTERNAL_API_TOKEN` | `INTERNAL_API_TOKEN` | BE ↔ business-mcp, BE ↔ Agent 인증 (`X-Internal-Token`) |
| DB | `DB_URL`·`DB_USERNAME`·`DB_PASSWORD` | `DB_PASSWORD`(+`DB_HOST`·`DB_NAME`) | 같은 DB 를 써야 checkpointer·규정 검색·업무 데이터가 맞는다 |

### BE — `server/chagok/.env`
| 키 | 로컬 값(예) | 설명 |
|---|---|---|
| `DB_URL` | `jdbc:postgresql://localhost:15432/chagok` | compose 안에서는 `jdbc:postgresql://postgres:5432/chagok` |
| `DB_USERNAME` / `DB_PASSWORD` | `postgres` / `postgres` | |
| `DDL_AUTO` | `update` | `ai_task` 테이블은 처음 기동 때 생성(덤프에는 이미 있음) |
| `AUTH_ENABLED` | `true` | 세션 로그인 |
| `CORS_ALLOWED_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173` | FE 개발 서버 |
| `INTERNAL_API_TOKEN` | (env 파일 값) | 비어 있으면 `/internal/**` 전부 401 |
| `AGENT_API_URL` | 로컬 Spring: `http://localhost:8000` / 컨테이너: `http://agent:8000` | 비어 있으면 `/ai-tasks` 가 503 |
| `BOOTSTRAP_ENABLED` | `true` | 계정이 없을 때만 생성(있으면 그대로) |
| `BOOTSTRAP_EMPLOYEE_ID` / `BOOTSTRAP_PASSWORD` | `chagok-team` / (env 파일 값) | 팀 공용 로그인 계정 |
| `BOOTSTRAP_WAREHOUSE_ID` | `2b7424cf-37a9-5119-98d7-a23142cbb251` | 로컬 테스트 센터 |
| `JOB_SCHEDULING_ENABLED` / `JOB_WORKER_ENABLED` | `false` / `false` | 서버 자체 참고 계산 작업. 에이전트 연동과 무관 |
| `KAFKA_BOOTSTRAP_SERVERS` | `localhost:9092` | DL 분석용. web 프로필에서는 꺼짐 |
| (application.yaml) `chagok.matching.tilted.enabled` / `clearance-mm` | `false` / `0` | 박스 수용 판정 정책. 에이전트가 `/internal/v1/matching-policy` 로 읽어 같은 기준으로 계산 |

### Agent·MCP — `box_agent/.env`
| 키 | 로컬 값(예) | 설명 |
|---|---|---|
| `OPENAI_API_KEY` | (env 파일 값) | 없으면 규칙 기반 문장, 규정 인용·반려 해석은 키워드만 |
| `BOX_AGENT_LLM_MODEL` | `gpt-5.4-mini` (기본) | |
| `BUSINESS_API_URL` | `http://localhost:8080` | CLI·stdio 실행 때 business-mcp 가 부르는 BE 주소 |
| `AGENT_BUSINESS_API_URL` | `http://backend:8080` (기본) | compose 안 business-mcp 가 부르는 BE 주소 |
| `INTERNAL_API_TOKEN` | BE 와 같은 값 | |
| `DB_PASSWORD` / `DB_HOST` / `DB_NAME` | `postgres` / `postgres` / `chagok` | compose 안 agent(checkpointer)·rag-mcp 용 |
| `AGENT_NETWORK` | `chagok_default`(로컬 인프라) / `chagok-web_default`(compose.web) | compose 가 합류할 docker network |
| `LANGSMITH_API_KEY` / `LANGSMITH_PROJECT` | (env 파일 값) / `box-agent` | 선택. 있으면 trace 전송 |

CLI 실행 때 DB 접속(`DB_URL`)은 `server/chagok/.env` 에서 같이 읽는다.

### FE — `client`
| 키 | 기본값 | 설명 |
|---|---|---|
| `VITE_API_BASE_URL` | `/api/v1` | 바꿀 일 없음 |
| `API_PROXY_TARGET` (vite 개발 서버) | `http://127.0.0.1:8080` | `/api` 를 BE 로 프록시. SSE 도 같은 경로 |

---

## 3. DB (최종본: `db/chagok.dump`)

PostgreSQL 17 + pgvector. 복원:
```bash
docker exec chagok-postgres-1 psql -U postgres -d postgres -c "DROP DATABASE IF EXISTS chagok WITH (FORCE)" -c "CREATE DATABASE chagok"
docker cp db/chagok.dump chagok-postgres-1:/tmp/chagok.dump
docker exec chagok-postgres-1 pg_restore -U postgres -d chagok --no-owner /tmp/chagok.dump
```

### 3-1. 센터·로그인
| 항목 | 값 |
|---|---|
| 센터 | `로컬 테스트 센터` `2b7424cf-37a9-5119-98d7-a23142cbb251` |
| 계정 | `chagok-team`(차곡팀, OPERATOR) · `local-operator`(초기 운영자, OPERATOR) |
| 공급사 | 박스왕 `8a60024a-b4c0-5651-a14f-4012c8104395`, 리드타임 3일 |

### 3-2. `public` 스키마 (BE 소유, JPA 엔티티)
| 테이블 | 행 | 내용 | 쓰는 곳 |
|---|---:|---|---|
| `warehouse` | 1 | 센터 | 전부 |
| `box_spec` | 7 | 보유 박스(아래 표). 재고·단가 포함, `safety_days=7` | 에이전트 ②⑦, 화면 |
| `product` | 40 | 상품 치수(mm)·분류. 매칭 박스는 비어 있음(에이전트가 직접 계산) | 에이전트 ④ |
| `category` | 26 | `대분류 > 중분류` | |
| `item_event` | 134,162 | 출고 이벤트 2025-02 ~ 2026-10-20(합성 시각, 성수기 11~12월). 1행 = 출고 1건, FAILED 포함 | 에이전트 ②③ |
| `dl_measurement_sample` | 33,335 | DL 측정 원본(공유 전용, 엔티티 없음) | 참고 |
| `purchase_order` / `_item` | 13 / 91 | 2025-10 ~ 2026-10 월별 정기 발주, 전부 입고 완료(RECEIVED) | 에이전트 ⑧ 전월·전년 비교 |
| `procurement_proposal` / `_item` | 14 | 과거 제안 13건(시뮬레이션) + 에이전트 11월 제안 1건. `summary`(jsonb)에 본문·clientView·근거 | FE 제안서 카드, 에이전트 ① |
| `ai_task` | 2 | AI 작업 이력: 요청자·결정자·결정 의견·traceId·LangSmith 링크·결과 | FE·BE |
| `app_user` | 2 | 로그인 계정 | BE |
| `analysis_job`·`cost_forecast`·`inventory_movement`·`notification` | 0 | 비어 있음 | |

보유 박스 7종:

| 코드 | 내치수(mm) | 단가(원, VAT 별도) | 재고 |
|---|---|---:|---:|
| 0호 | 190×120×90 | 146 | 2,500 |
| 1호 | 220×190×90 | 224 | 3,800 |
| 2호 | 270×180×150 | 265 | 3,500 |
| 2-1호 | 350×250×100 | 559 | 2,200 |
| 3호 | 340×250×210 | 566 | 1,000 |
| 4호 | 410×310×280 | 1,126 | 4,000 |
| 5호 | 480×370×340 | 2,073 | 600 |

### 3-3. `box_agent` 스키마 (에이전트 소유, 엔티티 없음 — Hibernate 가 건드리지 않음)
| 테이블 | 내용 | 쓰는 곳 |
|---|---|---|
| `knowledge` | 규정 조항 20 + 반려 기록 22(1536차원, `text-embedding-3-small`) | rag-mcp |
| `checkpoints`·`checkpoint_writes`·`checkpoint_blobs`·`checkpoint_migrations` | LangGraph 상태(thread_id = AI 작업 id). 승인 대기 작업 재개용 | Agent |

### 3-4. DB 밖 데이터
| 파일 | 위치 | 비고 |
|---|---|---|
| `catalog.db` (박스왕 491종, SQLite) | `box_agent/catalog.db` | 신규 박스 후보 검색. 없으면 `python mcp_servers/catalog_server.py --refresh`(약 5분) |
| 규정 원문 | `box_agent/knowledge/` (git 포함) | 바뀌면 `knowledge_server.py --ingest` |

---

## 4. 연동 API

### FE → BE (세션 로그인, 실행·승인은 OPERATOR 이상)
| Method | Path | 응답 |
|---|---|---|
| POST | `/api/v1/warehouses/{wid}/ai-tasks` `{targetMonth}` | 202 작업(진행 중인 같은 달 작업이 있으면 그 작업) |
| GET | `/api/v1/warehouses/{wid}/ai-tasks/{id}` | 상태 RUNNING·WAITING_APPROVAL·DONE·FAILED, 결과, traceLinks |
| GET | `/api/v1/warehouses/{wid}/ai-tasks/{id}/stream` | SSE (EventSource, withCredentials) |
| POST | `/api/v1/warehouses/{wid}/ai-tasks/{id}/approval` `{decision: APPROVED\|REJECTED, comment}` | 202 |
| GET | `/api/v1/warehouses/{wid}/ai-tasks?mine=true` | 내 작업 20건 |
| GET | `/api/v1/warehouses/{wid}/procurement-proposals/latest?targetMonth=` | 완료 후 제안서. 에이전트 제안은 `analysis.clientView` 로 표시 |

SSE 이벤트: `TOOL_START`·`TOOL_END`(node·tool) · `TOKEN`(초안 본문) · `APPROVAL_REQUIRED`(금액·수량·신규 규격·근거·본문) · `DONE` · `FAILED`

### BE → Agent (`X-Internal-Token`)
`POST /internal/agent/tasks` `{taskId(=ai_task.id), warehouseId, targetMonth}` · `GET /internal/agent/tasks/{id}` ·
`GET /internal/agent/tasks/{id}/events`(SSE, Last-Event-ID) · `POST /internal/agent/tasks/{id}/approval` · `GET /internal/agent/health`

### business-mcp → BE (`X-Internal-Token`, `/internal/v1`)
`GET /matching-policy` · `GET /warehouses` · `GET /warehouses/{wid}/box-specs` · `/event-stats` · `/product-shipments?from&to` ·
`/monthly-shipments?fromMonth&toMonth` · `/purchase-orders?from&to` · `/procurement-proposals?limit` ·
`PUT /warehouses/{wid}/procurement-proposals/{proposalId}`

---

## 5. 기동 순서 (로컬)

```bash
# 1) DB  (server/chagok)
docker compose up -d postgres                      # 이후 3절 복원
# 2) BE  (JDK 25 없으면 Docker)
docker build -t chagok-backend:local . && docker run -d --name chagok-backend-local \
  --network chagok_default --network-alias backend -p 8080:8080 --env-file .env \
  -e SPRING_PROFILES_ACTIVE=web -e DB_URL=jdbc:postgresql://postgres:5432/chagok -e AGENT_API_URL=http://agent:8000 \
  chagok-backend:local
# 3) Agent + MCP  (box_agent, AGENT_NETWORK=chagok_default)
docker compose -f compose.yaml -f compose.dev.yaml up -d --build
# 4) FE  (client)
npm ci && npx vite --port 5173
```

## 6. 연동 확인 체크리스트
| # | 확인 | 기대 |
|---|---|---|
| 1 | `curl localhost:8080/actuator/health` | `UP` |
| 2 | `curl localhost:8000/internal/agent/health` | `{"status":"UP","mcp":["business","rag"]}` |
| 3 | `curl -H "X-Internal-Token: <토큰>" localhost:8080/internal/v1/matching-policy` | `tiltedEnabled:false` (토큰 없으면 401) |
| 4 | FE 로그인 `chagok-team` | 대시보드 진입 |
| 5 | 박스 주문 → [개선 실행] | 진행 단계 표시 → 승인 카드 |
| 6 | [승인하고 저장] | 제안서 카드가 새 AI 제안서로 바뀜, `ai_task` DONE + LangSmith 링크 |
| 7 | `cd box_agent && .venv/bin/python tests/parity_eval.py` | 불일치 0 |

## 7. 자주 막히는 곳
| 증상 | 원인 | 해결 |
|---|---|---|
| `/ai-tasks` 503 "AI 에이전트가 연결되지 않았습니다" | BE 에 `AGENT_API_URL`·`INTERNAL_API_TOKEN` 없음 | BE env 확인 후 재기동 |
| business-mcp 도구 오류 `401` | 토큰 불일치 | 두 `.env` 의 `INTERNAL_API_TOKEN` 맞추기 |
| Agent 가 `business-mcp` 연결 실패 | 다른 docker network | `AGENT_NETWORK` 와 BE 컨테이너 network 일치, BE alias `backend` |
| MCP HTTP 421·400 | Host 헤더 검사 | `MCP_ALLOWED_HOSTS` 에 서비스 이름 |
| 신규 박스 후보가 안 나옴 | `catalog.db` 없음 | `catalog_server.py --refresh` |
| 로그인 실패 | 계정 없음 | BE 를 `BOOTSTRAP_*` 와 함께 기동 |
| FE 진행 표시가 늦거나 승인 대기 중 끊김 | 프록시 버퍼링·타임아웃 | client `nginx.conf` 의 `/ai-tasks/{id}/stream` 전용 location(버퍼링 끄기, 15분) 반영 여부 확인 |
