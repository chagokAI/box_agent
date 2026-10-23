# 박스 주문 제안 에이전트 (LangGraph + MCP + Harness)

다음 달 박스 주문 제안서를 회사 양식(`mcp_servers/templates/box_order_proposal.md`)대로 작성한다.
충진율이 낮은 치수 구간을 찾아 기준을 넘는 신규 규격은 주문에 포함하고, 대체되는 기존 박스 소모는 줄여 반영한다.
범위는 제안서까지(발주서는 만들지 않음).

## 구성 (Capstone 레퍼런스 아키텍처 기준)
```
Spring Boot ──내부 HTTP──▶ agent_server.py (FastAPI, LangGraph + Harness) ──MCP──▶ business-mcp ──/internal/v1/**──▶ Spring ─▶ PostgreSQL
     ▲   SSE 이벤트 ◀─────────────┘        │ checkpointer(box_agent 스키마)         └──▶ rag-mcp ──▶ pgvector(box_agent.knowledge)
     └ (Spring 의 /api/ai/tasks·SSE 중계·Vue 화면은 아직 없음 — 지금은 agent_server 를 직접 호출)       └──▶ LLM(OpenAI)
```
| 구성 | 파일 | 역할 | 지키는 규칙 |
|---|---|---|---|
| Agent 서버 | `agent_server.py` | 작업 실행(202)·SSE 진행 이벤트·승인 후 재개 | DB 업무 데이터를 직접 쓰지 않음(checkpointer만) |
| 그래프 | `box_spec_agent.py` | ①~⑪ StateGraph. CLI 실행도 같은 그래프 | 숫자는 MCP 계산 도구만 만든다 |
| Harness | `harness.py`, `prompts/*.md` | 한도·재시도·기록·마스킹·인용 대조 | 아래 'Harness' 표 |
| business-mcp | `mcp_servers/business_mcp.py` | 조회·계산·제안 저장 26개 도구 (wms·catalog·custom_quote·packing·forecast·proposal 모듈) | DB 직접 접근 없음, Spring `/internal/v1/**` 호출 |
| rag-mcp | `mcp_servers/rag_mcp.py` | 규정·반려 기록 검색, Resource `policy://`·`feedback://` | 업무 데이터 변경 없음 |
| Spring 내부 API | `server/chagok/.../domain/agent` | 에이전트용 조회·제안 저장, 센터·규격 소속 검증 | `X-Internal-Token`(INTERNAL_API_TOKEN), 세션·CSRF 없음 |

모델이 도구를 고르는 곳(나머지 계산 단계는 순서 고정):
- ⑧ `cite_policies` — '확인 필요' 항목마다 `search_policies`/`read_resource`로 규정 조항을 찾아 `(POL-02 §4)`처럼 인용
- ⑩ `interpret_feedback` — 반려 의견을 `search_feedback_cases`·`search_policies`로 해석해 기준값 변경(화이트리스트)과
  담당자 처리 항목(미지원)으로 나눔. 승인 뒤 그 반려를 rag 에 적재해 다음 해석에 쓴다

## 실행 순서
Python 3.11 이상.
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 0) 로컬 DB·서버 (처음 한 번): "로컬 개발 DB" 절. Spring 은 INTERNAL_API_TOKEN 을 주고 띄운다 (JDK 25 없으면 Docker)
docker run -d --name chagok-backend-local -p 8080:8080 -e SPRING_PROFILES_ACTIVE=web \
  -e DB_URL=jdbc:postgresql://host.docker.internal:15432/chagok -e DB_PASSWORD=postgres \
  -e JOB_WORKER_ENABLED=false -e JOB_SCHEDULING_ENABLED=false -e INTERNAL_API_TOKEN=<INTERNAL_API_TOKEN> chagok-backend:local
# box_agent/.env: OPENAI_API_KEY, BUSINESS_API_URL=http://localhost:8080, INTERNAL_API_TOKEN=<INTERNAL_API_TOKEN>
#   (DB_URL 은 server/chagok/.env 에서 읽음 → checkpointer·rag 벡터 검색용)

# 1) 박스왕 택배박스 가격 수집 (전 페이지, 요청 간격 10초 → 약 4~5분). 하루 1회 배치 권장
python mcp_servers/catalog_server.py --refresh
# 2) 규정·반려 기록 색인 (문서가 바뀔 때만)
python mcp_servers/knowledge_server.py --ingest

# 3-a) CLI 실행 (business·rag MCP 를 stdio 하위 프로세스로 띄워 접속)
python box_spec_agent.py                                   # 다음 달, 운영자 승인은 터미널에서 입력
python box_spec_agent.py --target-month 2026-11 --warehouse-id <warehouse UUID> --auto-approve
python box_spec_agent.py --reject-once "안전 재고 넉넉하게, 4호는 빼주세요" --auto-approve   # ⑩ 반려 해석 시연
python box_spec_agent.py --no-llm                          # LLM 없이(규칙 기반 문장, 인용·반려 해석은 키워드만)

# 3-b) Agent 서버 (Spring 이 부를 내부 HTTP)
python agent_server.py --port 8000
curl -X POST localhost:8000/internal/agent/tasks -H 'X-Internal-Token: <INTERNAL_API_TOKEN>' \
     -H 'content-type: application/json' -d '{"targetMonth":"2026-11","taskId":"t-1"}'          # → 202
curl -N localhost:8000/internal/agent/tasks/t-1/events -H 'X-Internal-Token: <INTERNAL_API_TOKEN>'  # SSE
curl -X POST localhost:8000/internal/agent/tasks/t-1/approval -H 'X-Internal-Token: <INTERNAL_API_TOKEN>' \
     -H 'content-type: application/json' -d '{"decision":"APPROVED"}'                         # 재개

# MCP 를 Streamable HTTP 로 따로 띄울 때 (compose): 에이전트에 BUSINESS_MCP_URL·RAG_MCP_URL 을 주면 stdio 대신 연결
python mcp_servers/business_mcp.py --http --host 0.0.0.0 --port 9101   # http://business-mcp:9101/mcp
python mcp_servers/rag_mcp.py --http --host 0.0.0.0 --port 9102        # http://rag-mcp:9102/mcp
```

## 배포 (docker compose)
같은 이미지(`Dockerfile`, python:3.11-slim, 비루트 실행) 하나로 세 서비스를 띄운다. 공개 포트는 없다.

| 서비스 | 명령 | 포트(내부) | 붙는 곳 |
|---|---|---|---|
| business-mcp | `business_mcp.py --http` | 9101 `/mcp` | backend:8080 `/internal/v1/**` |
| rag-mcp | `rag_mcp.py --http` | 9102 `/mcp` | postgres(pgvector), 임베딩 API |
| agent | `agent_server.py` | 8000 | business-mcp, rag-mcp, postgres(checkpointer), LLM API, LangSmith |
| rag-ingest (`--profile init`) | `knowledge_server.py --ingest` | - | 1회성 색인 |

```bash
# Spring 스택이 먼저 떠 있어야 한다 (backend 에 INTERNAL_API_TOKEN 전달: server/chagok/compose.web.yaml)
cp .env.example .env        # OPENAI_API_KEY, INTERNAL_API_TOKEN, DB_PASSWORD, AGENT_NETWORK, LANGSMITH_* 채우기
docker compose --profile init run --rm rag-ingest            # 규정·반려 기록 색인 (문서가 바뀔 때만)
docker compose up -d --build                                  # 운영: 포트 공개 없음
docker compose -f compose.yaml -f compose.dev.yaml up -d --build   # 로컬 확인: agent 를 127.0.0.1:8000 에
```
- `AGENT_NETWORK`: 합류할 docker network. `compose.web.yaml` 이면 `chagok-web_default`, 로컬 인프라(`compose.yaml`)면 `chagok_default`.
  로컬에서 Spring 을 단독 컨테이너로 띄웠다면 `docker network connect --alias backend chagok_default chagok-backend-local`
- 컨테이너 안 business-mcp 의 Spring 주소는 `AGENT_BUSINESS_API_URL`(기본 `http://backend:8080`). `.env` 의 `BUSINESS_API_URL` 은 로컬 CLI 용
- MCP HTTP 는 `MCP_ALLOWED_HOSTS`(서비스 이름)만 Host 헤더로 받는다(DNS rebinding 방지)
- 제안서 사본·trace 는 `proposals` 볼륨(`/app/proposals`)에 남는다
- MCP 라이브러리는 `mcp` 1.x 의 FastMCP 를 쓴다(PPT 의 FastMCP 4.1 과 API 가 다르지만 stdio·Streamable HTTP·Resource·annotations 는 같다)

## Agent 서버 API (`agent_server.py`, 헤더 `X-Internal-Token`)
| Method | Path | 응답 |
|---|---|---|
| POST | `/internal/agent/tasks` `{warehouseId?, targetMonth?, jobId?, taskId?}` | 202 `{taskId, traceId, job}` |
| GET | `/internal/agent/tasks/{taskId}` | `{status, job, result, error, traceId, harness}` |
| GET | `/internal/agent/tasks/{taskId}/events` | SSE (`Last-Event-ID`·`?after=` 로 이어 받기) |
| POST | `/internal/agent/tasks/{taskId}/approval` `{decision: APPROVED\|REJECTED, comment}` | 202 |
| GET | `/internal/agent/health` | `{status, tasks, mcp}` |

- 상태: `RUNNING → WAITING_APPROVAL → RUNNING → DONE`(반려면 다시 `WAITING_APPROVAL`), 실패 `FAILED`
- 이벤트: `TOOL_START`·`TOOL_END`(node·server·tool), `TOKEN`(초안 본문 400자씩, draft 번호),
  `APPROVAL_REQUIRED`(tool=save_proposal·금액·수량·신규 규격·근거·본문), `DONE`(proposalId·금액·근거·harness 집계), `FAILED`(error·tracePath)
- thread_id = taskId. 상태는 PostgreSQL `box_agent` 스키마 checkpointer 에 남아 서버가 다시 떠도 승인 대기 작업을 재개한다
- 동시 실행 `BOX_AGENT_MAX_CONCURRENT`(기본 2)

## Harness (`harness.py`)
| 요소 | 적용 위치 | 기준 (환경변수) |
|---|---|---|
| Instructions | `prompts/writer.md`, `feedback_interpreter.md`, `policy_citer.md` | 역할·도구 규칙·금지·출력 형식. 코드에 지시문 없음 |
| Tools | `harness.tool_loop`, `rag_tools` | 도구 결과 4,000자로 자름(`BOX_AGENT_TOOL_RESULT_CHARS`), 도구 오류는 모델에 돌려줌. 변경 도구(destructiveHint)는 모델 도구 목록에서 제외. Resource 는 가상 도구 `read_resource` |
| Agent Loop | `tool_loop`, 그래프 | 최대 8턴(`BOX_AGENT_MAX_TURNS`), 같은 도구 3회(`BOX_AGENT_SAME_TOOL_MAX`), 루프 120초(`BOX_AGENT_LOOP_TIMEOUT`), 그래프 한 구간 120초(`BOX_AGENT_TASK_TIMEOUT`), ⑧ 재작성 3회 |
| Context | `fit_context` | facts 24,000자 예산(`BOX_AGENT_CONTEXT_CHARS`), 넘으면 긴 리스트부터 줄임 |
| State·Memory | `open_checkpointer` | PostgreSQL checkpointer(thread_id = taskId), DB_URL 없으면 메모리 |
| Guardrails | `human_review`, `interpret_feedback`, `evaluate`, `mask` | save_proposal 앞 interrupt(승인), 반려 해석은 화이트리스트·범위 검증, 인용은 실제 검색 결과만(아니면 −20), 숫자는 facts 만(아니면 −20), 도구 결과 개인정보 마스킹 |
| Recovery | `llm_call`, `chagok_api` | 429·5xx·연결 오류 2회 재시도(`BOX_AGENT_LLM_RETRIES`) → 대체 모델(`BOX_AGENT_LLM_FALLBACK_MODEL`) → 규칙 기반. 내부 API 도 2회 재시도, 실패는 ToolError(재시도 힌트) |
| Observability | `start_trace`, `record`, `summary` | trace_id 하나로 로그·이벤트·LLM 호출(토큰·지연·비용)을 `proposals/traces/<taskId>.jsonl` 에. 제안 summary·DONE 이벤트에 집계(`harness`), FAILED 는 tracePath. `LANGSMITH_API_KEY`(+`LANGSMITH_PROJECT`, 기본 box-agent)가 있으면 LangSmith 에 그래프 노드(chain)·LLM 호출(llm)·MCP 도구(tool)를 한 trace 로 보내고, 링크를 DONE·FAILED 이벤트와 CLI 로그에 남긴다 |

## 평가
```bash
python tests/feedback_eval.py        # ⑩ 반려 해석: keyword / llm(도구 없음) / llm+rag 결과표 → tests/results/
python tests/knowledge_eval.py       # rag 검색 적중 (top-3, --k 1)
```
- 2026-10-21 반려 해석 12문장: keyword 2/12, llm 3/12, **llm+rag 12/12** (`tests/results/feedback_eval_20261021_1140.md`).
  llm(도구 없음)은 지시문이 도구 사용을 전제로 해 불리한 비교다
- 프롬프트·한도·모델을 바꾸면 두 평가를 다시 돌려 결과표를 남긴다

## 보기용 HTML 제안서
- 양식 `mcp_servers/templates/box_order_proposal.html` (2026-10-23 확정 'A 임원 보고형', client 브랜드 색: 네이비·골드·크림),
  렌더러 `render_proposal_html`(proposal_server). ⑧ write_proposal 이 회사 양식 md 와 함께 만든다
- 구성: 요약 3줄(주문 개수 · 주문금액 · 예상 이익 월/연) → 숫자 4칸 → 규격별 제안 수량 → 도입 효과(박스비·운임 누적 막대) →
  예상 총 주문금액(규격별 막대) → 상세보기(확인 필요 · 근거 · 산출 방법, 전월·전년 비교는 여기에)
- 예상 이익 = 도입 효과 블록의 `monthly_saving`(선정 시 `sim_combined`, 없으면 등록 규격 `recent_effect`) 하나만 쓴다.
  근거 문장·md 표·HTML 이 같은 값이고, 적재량 합계로 다시 빼지 않는다. 맞춤 규격 제외, 연 환산 × 12
- 단일 품목 후보의 근거 문장은 `sim_results[].moved` 의 anchor 상품 값(충진율 전후·이전 박스)으로 쓰고, 함께 옮겨 가는 상품은 따로 적는다
- 나가는 곳: `APPROVAL_REQUIRED.proposalHtml`, 저장 `summary.bodyHtml`, `./proposals/<jobId>.html`. ⑨ 평가는 md 로만 한다
- 외부 라이브러리 없이 HTML 한 파일(인라인 SVG 차트·다크 모드). 차트 색은 색각 검사 통과 값 — 바꾸면 다시 검사

## 문장 작성(⑧)과 검토(⑨)
- `sample_proposal_2026-11.md` = 양식대로 채운 예시 제안서. `get_template`의 `example_md`로 LLM에 넘겨 문체·구성만 참고하게 한다
  (숫자·규격명은 쓰지 않음, 가져오면 ⑨ 숫자 대조에서 감점). 경로는 `PROPOSAL_SAMPLE`로 바꿀 수 있다
- 숫자·표·합계는 전부 MCP 툴 결과로 채운다. 근거 3줄과 '확인 필요'는 먼저 규칙 기반으로 만든다.
- `OPENAI_API_KEY`(`box_agent/.env`)가 있으면 ⑧에서 모델이 규정 인용을 붙이고(`cite_policies`), `llm_writer.py`가
  facts(툴 결과)와 규칙 기반 초안을 주고 문장만 다듬게 한다(`gpt-5.4-mini`, `BOX_AGENT_LLM_MODEL`).
  실패하거나 자격 증명이 없으면 규칙 기반 문장을 그대로 쓴다.
- ⑨ 감점: 자리표시자 −30, 빈 섹션 −25/개, 수량 합계 불일치 −20, 총액 불일치 −20,
  LLM 문장에 facts에 없는 숫자(31 초과) −20, 검색 결과에 없는 규정 인용 −20. 80점 미만이면 피드백을 들고 ⑧로(최대 3회).
- 신규 규격 후보는 박스왕 기성품을 먼저 보고, 없으면 맞춤 제작(유팩 참고 견적)을 본다. 아래 '맞춤 제작 제안' 절.
- ⑩ 반려: 키워드 규칙(`보충만`/`신규 제외`, `안전 재고 N일`) 위에 모델 해석을 얹는다. 바꿀 수 있는 기준값은
  `no_new_specs, no_custom, safety_days(3~30), rounding_unit(50~1000), gate.CATALOG.monthly_saving·share_pct·delta_fill,
  gate.CUSTOM.monthly_saving·payback_m`.
  그 밖의 요청은 '확인 필요'에 "담당자 처리 필요"로 적는다.
- ① 과거 제안: 같은 규격을 지난 제안에서도 냈는데 아직 미등록이면 '확인 필요'에 표시하고,
  지난 제안의 예측 출고와 그 달 실제 출고를 비교해 적는다.
- 제안 summary 에 `sources`(근거 목록, Resource URI 포함 — 근거 패널용), `rejections`(반려 해석), `harness`(집계)를 남긴다.

## 규정·반려 기록 검색 (rag-mcp)
- 자료(목업, 실제 사내 규정 아님): `knowledge/policies/POL-01~05_*.md` 규정 5개(조항 20개),
  `knowledge/feedback_cases.jsonl` 반려 기록 21건(의견, 바꾼 기준값 `configChange`, 기준값으로 못 바꾼 내용 `unsupported`, 결과)
- 도구: `search_policies`, `search_feedback_cases`, `add_feedback_case`(destructiveHint), `knowledge_status`
- Resource: `policy://index`, `policy://{doc_id}`(원문 markdown), `feedback://{case_id}`(원본 JSON)
- 저장: DB_URL 이 있으면 `box_agent.knowledge`(pgvector, 별도 스키마라 서버 Hibernate가 건드리지 않음), 없으면 파일을 읽어 메모리 검색.
  임베딩 모델 이름은 행마다 `embed_model` 로 남기고, 검색은 같은 모델로 적재된 행만 본다
- 임베딩: `OPENAI_API_KEY` 있으면 `text-embedding-3-small`(1536차원), 없거나 `KNOWLEDGE_EMBED=local`이면 글자 2-gram 해싱
- 2026-10-21 점검: OpenAI 임베딩 top-3 13/13, top-1 11/13 · 로컬 2-gram(DB 없이) top-3 11/13
- 주의: 비슷한 문장 ≠ 같은 뜻("안전 재고 넉넉하게"의 1순위가 "5일이면 충분"). 그래서 configChange 를 복사하지 않고 모델이 해석한다

## 차곡 server 연동 (Spring 내부 API)
business-mcp 는 **DB 에 직접 접근하지 않고** Spring `/internal/v1/**` 를 호출한다(`mcp_servers/chagok_api.py`).
접속: `BUSINESS_API_URL`, `INTERNAL_API_TOKEN`(Spring 과 같은 값). 없으면 Mock 데이터.

| API | 쓰는 도구 |
|---|---|
| `GET /warehouses` | list_warehouses |
| `GET /warehouses/{id}/box-specs?activeOnly=` | list_box_specs, get_box_inventory |
| `GET /warehouses/{id}/event-stats` | 모든 조회의 meta(입력 기준 시각·status별 건수·PENDING 수) |
| `GET /warehouses/{id}/product-shipments?from&to&statuses` | list_product_fits (치수·`weightG`·출고 수) |
| `GET /warehouses/{id}/monthly-shipments?fromMonth&toMonth&statuses` | get_shipment_history |
| `GET /warehouses/{id}/purchase-orders?from&to` | list_purchase_orders |
| `GET /warehouses/{id}/procurement-proposals?limit` | list_past_proposals |
| `PUT /warehouses/{id}/procurement-proposals/{proposalId}` | save_proposal (한 트랜잭션, 센터·규격 소속 검증) |

- 기간은 센터 시간대(Asia/Seoul) 날짜로 Spring 이 해석한다. 출고 1건 = `item_event` 1행(`SHIPMENT_EVENT_STATUSES`로 status 제한 가능).
  진행 중인 달은 경과 일수로 한 달치 환산(business-mcp)
- 발주 대상 월 = 연결된 `procurement_proposal.target_month`, 없으면 납품 희망일의 월.
  전월·전년 비교 금액은 제안서로 만든 정기 발주(`proposal_id` 있음)만, 없으면 반려 제외 전체 합계
- 저장: `procurement_proposal` 1행 + 기존 규격별 `procurement_proposal_item`. id = uuid5(jobId) → 같은 jobId 는 덮어씀(멱등).
  신규 규격 줄은 box_spec 이 없어 item 에 못 넣고 `summary.newSpecs` 에만 둔다. `./proposals/<jobId>.json|md` 사본은 항상 남김
- `summary.clientView` = client `BoxOrderView` 가 쓰는 `mocks/orders.js` 모양(+ `newSpecEffect`, `sources`)

## 박스왕 수집 규칙
- 대상: `https://www.boxking.co.kr/goods/goods_list.php?page=N&cateCd=011` (택배박스), 마지막 페이지까지
- robots.txt를 매 수집마다 확인, 차단 시 중단. 요청 간격 최소 10초(Crawl-delay 10)
- 상품명 `202호 (200 x 150 x 100) B골 160매` 에서 치수·골·매수 추출, 판매가 ÷ 매수 ÷ 1.1 = 개당 VAT 별도 단가
- 규격이 없는 상품(묶음 세트 등)은 건너뜀
- 가정(확인 필요): 표기 치수 = 내치수(mm), 표시 가격 = VAT 포함. 다르면 환경변수 BOXKING_PRICE_INCLUDES_VAT=0
- 사이트 구조가 바뀌면 `--refresh` 결과의 skipped_no_dims 가 급증하거나 items가 0이 됨 → 파서 점검
- `catalog_boxes` 열은 서버 `box_spec`과 같은 것은 이름·양식을 맞춤: `code`, `name`,
  `inner_width_mm/inner_depth_mm/inner_height_mm`, `material`(`골판지 B골`, 없으면 NULL), `unit_price_krw`(개당 VAT 별도).
  예전 열 이름(l, w, h, flute, unit_price_ex_vat)의 catalog.db는 처음 열 때 자동 변환

## box_spec 시드 DML
```bash
python export_box_spec_dml.py                               # → sql/box_spec_seed.sql (Mock 창고 id)
python export_box_spec_dml.py --warehouse-id <UUID>
psql "$DB" -f sql/box_spec_seed.sql
```
- 기본 규격 0호~5호(wms Mock과 같은 값)를 카탈로그에서 골라 warehouse(없을 때만) / supplier(박스왕) / box_spec INSERT
- id = uuid5 라 다시 돌려도 덮어씀(멱등). 재고(`on_hand_qty`)는 처음 넣을 때만, `safety_qty`는 NULL(미설정)
- `--all [--active]`: 카탈로그 전체 491건 → `sql/box_spec_catalog.sql`. code = 박스왕 품번, 재고 0,
  size_class 는 부피 기준(1호 이하 S, 3호 이하 M, 그 위 L), 기본 `is_active=false`(에이전트·재고 화면 제외)

## 테스트
`tests/fake_boxking.py` : 박스왕 목록 구조를 흉내 낸 로컬 서버(실제 상품 15개 값)
```bash
python tests/fake_boxking.py 8765 &
BOXKING_BASE_URL=http://127.0.0.1:8765 BOXKING_MIN_DELAY=0 CATALOG_DB=test.db python mcp_servers/catalog_server.py --refresh
```

## 신규 규격 후보 구간 (군집 + 단일 품목)
- 군집 `find_misfit_clusters`: 저충진(45% 미만) 상품을 정렬 치수의 **로그**(납작·길쭉한 형태가 반영되게)로, **출고량 가중** k-means.
  시작점 10번 중 오차 최소, 군집 수는 2~`max_clusters`(기본 5) 중 가중 실루엣 점수 최대
- 단일 품목 `find_sku_candidates`: 충진율 20% 미만 · 출고 비중 1% 이상 상품을 낭비 부피 × 출고량 순으로 최대 5개(`sku_*` 설정).
  군집에 묻히는 고출고 단일 품목(예: 키보드)을 따로 본다. 기성품 검색 부피 상한은 '지금 박스보다 작음'만(군집은 이상 치수 2배까지)
- 단일 품목은 게이트에서 출고 비중(3%) 조건을 보지 않는다(월 절감·충진율 개선·회수 기간만)
- 선정: 군집·단일 후보가 같은 출고를 두고 겹치므로 절감이 큰 순으로 넣으며 **이미 고른 것 위에 더 줄어드는 만큼**(`marginal_saving`)으로 다시 판정
- 주의: 새 박스가 들어오면 그 박스가 '들어가는 가장 작은 박스'가 되는 다른 상품도 함께 옮겨 간다(서버 매칭 규칙과 같음).
  작은 슬림 박스는 단가가 높아 오히려 손해가 되는 경우가 많고(운임은 2만㎤ 이하 3,000원 동일), 그런 후보는 게이트에서 떨어진다

## 맞춤 제작 제안 (유팩 참고 견적)
- 언제: ① 군집에 커버율 80% 이상인 박스왕 기성품이 없을 때 → 군집 P90 이상 치수(`ideal_box_dims`)로 `CUSTOM` 후보,
  ② 보유 규격 어디에도 안 들어가는 상품이 있을 때 → 그 상품들이 모두 들어가는 치수 1종(`custom_unfit`)
- 견적: `quote_custom_box(dims, qty)` (`mcp_servers/custom_quote_server.py`). 유팩 견적 계산기 화면이 쓰는 내부 엔드포인트
  (`/goods/ordermade_estimate_carton_ajax.asp`)에 A형·B골·기성품 재질·무인쇄·직접 수령·내경 기준으로 묻는다.
  수량 = 대상 출고 월평균(90일 ÷ 3, 100개 단위 올림). 결과: 요청 수량 단가, 수량 구간별 단가표, 판비+목형비(`initCost`), 납기 예정일. VAT 별도
- 유팩 A형 최소 제작 크기(세로 80mm, 높이 50mm)보다 작은 치수는 그 크기까지 올려서 견적을 받는다(그보다 작으면 견적 거절)
- robots.txt 확인, 요청 간격 10초(`UPACKAGE_MIN_DELAY`), 같은 치수·수량은 하루 캐시(`catalog.db`의 `custom_quotes`, `UPACKAGE_CACHE_HOURS`).
  compose 에서는 `/app` 이 쓰기 불가라 `UPACKAGE_CACHE_DB=/app/proposals/custom_quotes.db`(볼륨). 캐시를 못 써도 견적은 돌려준다.
  `CUSTOM_QUOTE=0` 이면 견적을 묻지 않는다
- 공식 API 가 아니라 사이트가 바뀌면 실패한다 → 후보에서 빼고 '확인 필요'에 "담당자 견적 필요"로 적는다(작업은 계속)
- 게이트 `gate.CUSTOM`: 월 절감 30만 원·출고 비중 3%·충진율 +10%p + 초기비 회수 6개월(`payback_m`).
  ② 안 들어가는 상품용은 게이트 없이 주문에 넣고(그 상품 출고도 소모량에 포함) 담당자 결정을 '확인 필요'에 적는다
- 견적 두 번: 1차는 후보 선정용(월평균 수량), plan_order 뒤 2차는 확정 제안 수량으로 다시 받아 주문표 단가·금액·합계에 쓴다.
  2차가 실패하면 1차 단가를 두고 '단가 재확인 필요'. 2차 단가로 게이트를 다시 심사하지는 않는다
- '확인 필요': 견적일·제안 수량 단가(1차 단가 병기)·초기비, 납기. 초기비(판비·목형비)는 주문 합계에 넣지 않고 따로 적는다
- 반려로 끌 수 있다: `no_custom`(키워드 "맞춤 제외"), `gate.CUSTOM.monthly_saving`, `gate.CUSTOM.payback_m`
- 저장: `summary.newSpecs` 에 `type` = `CUSTOM` / `CUSTOM_UNFIT`, `quote`(1차), `orderQuote`(2차), `initCost`
- 단독 확인: `python mcp_servers/custom_quote_server.py --quote 300 200 150 1000`

## 운임 (경동 정기화물)
- 운임표: `mcp_servers/templates/kyungdong_regular_freight.md`(`FREIGHT_TABLE`, 2026-10-22 추출). 실행할 때 md 표를 읽는다
- 1건 운임: 50,000㎤ 이하(소화물)는 소화물 표준운임(부피)만, 그 위는 경동 부피요율표(㎤)와 무게 운임(중대형화물 표준운임 → 운임표 4, kg) 중
  높은 금액. 부피 = 배정 박스 내치수(외치수 없음), 무게 = 상품 실중량(`product.weight_g`, 박스 무게 제외). 무게가 없으면 부피 운임만
- 표 문구("부피 및 무게 운임 중 높은 금액")를 소화물에도 그대로 적용하면 무게표 최저 4,500원 때문에 소화물 3,000~4,000원 구간이
  쓰이지 않는다 → 운영 판단(2026-10-22)으로 소화물은 부피 운임 우선. 문구 그대로 계산은 `FREIGHT_MODE=max`
- 쓰는 곳: `simulate_boxset` 월 절감(박스비 + 운임) → 신규 규격 게이트, 제안서 '신규 규격 도입 효과'의 `월 운임`·`월 박스비 + 운임` 줄과
- 도입 효과 표 블록: ① 이번에 선정한 신규 규격, 선정이 없으면 ② 서버가 승인 제안으로 등록한 규격(`description` = `catalog:boxking:<품번>`)이
  없었을 때 대비 지금 효과(`compare_boxsets`), ③ 안 들어가는 상품용 맞춤 규격(포장 불가 → 맞춤 박스, 박스비는 2차 견적 단가). 둘 이상이면 행에 `[규격]`
- 저장 시 맞춤 규격 줄은 `lines[].type = CUSTOM_SPEC` — 서버(`AgentProposalItemService`)는 `NEW_SPEC` 줄만 박스왕 카탈로그 근거로
  box_spec 에 등록·발주하므로, 공급사 미등록인 맞춤 규격은 자동 등록·발주에서 빠지고 담당자가 처리한다
  `운임 기준` 문장(`freightBasis`)

## 들어가는지 판정·충진율 (packing-engine)
- 상품 치수를 6방향 회전(90도)으로 먼저 본다.
- 판정 기준은 서버 상품 매칭(`FillRateCalculator`)과 같다. 매 실행 `GET /internal/v1/matching-policy`로 서버 `chagok.matching.tilted`(기울임 사용·여유)를 읽고, 기본 판정 여유는 0mm. 기울임이 켜져 있으면 안 들어가는 박스에 한 평면 기울임 판정(Carver 조건)을 적용한다:
  상품 변 (p,q,r) × 박스 변 (a,b,c)의 모든 순서에서 r ≤ c 이고 rectFits(p,q,a,b)이면 들어감.
  보수적 판정이라 통과하면 실제로 들어간다. 기울여야 들어가는 배정은 `fit="TILTED"`로 표시하고 '확인 필요'에 검토 대상으로 적는다.
- 충진율은 기울임과 관계없이 부피비(상품 부피 / 박스 부피).
- 신규 후보: 군집 P50 이상 들어가는 박스왕 기성품을 부피 작은 순으로 최대 50개 보고, 군집 출고량 80% 이상 들어가는 첫 박스를 후보로 한다.
- 신규 규격 도입 효과(제안서 섹션): 선정 규격을 모두 넣었을 때 배정이 바뀌는 출고(최근 90일 ÷ 3)를 기존 박스 vs 신규 규격으로
  비교한다. 대상 출고 충진율, 월 박스 적재 부피(m³), 박스 단가(기존 → 신규), 월 박스비, 센터 전체 충진율
  (`simulate_boxset` 의 `combined.load`). 신규 규격이 없으면 '해당 없음' 한 줄
- 배송 중(IN_TRANSIT) 발주가 있는 동안에는 제안서 작업을 하지 않는다는 운영 전제라 들어올 재고는 따로 빼지 않는다.

## 주문 수량 계산
- 다음 달 예상 소모 = 예측 출고 × (신규 규격 포함 박스 세트로 재배정한 규격별 사용 비중)
- 안전 재고 = 일평균 예상 소모 × 7일, 제안 수량 = 예상 소모 + 안전 재고 − 현재 재고 (100개 단위 반올림)
- 출고 예측 = 최근 3개월 평균 × 전년 동월 계절 지수, 정확도 = 100 − 최근 6개월 백테스트 MAPE
  (`mcp_servers/forecast_server.py` 의 `forecast_shipments`, 규칙 계산이며 LLM을 쓰지 않음)
- 월별 출고 이력 = item_event 월별 건수(KST)

## 로컬 개발 DB (db/chagok_dev_seed.sql)
```bash
cd ../server/chagok && cp .env.example .env    # DB_PORT/DB_URL 은 로컬 5432 충돌 시 15432 등으로
docker compose up -d
# 서버를 한 번 기동해 테이블 생성 (JDK 25 필요. 없으면 Dockerfile 이미지로 1회 실행)
docker build -t chagok-backend:local . && docker run --rm -d --name chagok-schema-init \
  -e SPRING_PROFILES_ACTIVE=web -e DB_URL=jdbc:postgresql://host.docker.internal:15432/chagok \
  -e DB_PASSWORD=postgres -e JOB_WORKER_ENABLED=false chagok-backend:local   # Started 로그 확인 후 docker stop
docker exec -i chagok-postgres-1 psql -v ON_ERROR_STOP=1 -U postgres -d chagok < ../../db/chagok_dev_seed.sql
cd ../../box_agent && docker exec -i chagok-postgres-1 psql -U postgres -d chagok < sql/box_spec_seed_local.sql
```
- `sql/box_spec_seed_local.sql` = `export_box_spec_dml.py --warehouse-id 2b7424cf-37a9-5119-98d7-a23142cbb251`
  (시드 센터 `로컬 테스트 센터`에 기본 규격 0호~5호, 공급사 박스왕, 재고는 Mock 값)
- 시드 데이터 한계(db/README.md): 출고 이력이 2026-05~09 5개월뿐이라 계절 지수·정확도는 미산출,
  최근 평균으로 예측한다. 발주 이력이 없어 전월·전년 비교도 생략된다. → 아래 시뮬레이션 시드로 보완

### 시뮬레이션 시드 (선택)
```bash
docker exec -i chagok-postgres-1 psql -v ON_ERROR_STOP=1 -U postgres -d chagok < sql/sim_item_event_local.sql
docker exec -i chagok-postgres-1 psql -v ON_ERROR_STOP=1 -U postgres -d chagok < sql/sim_orders_local.sql
```
- `sim_item_event_local.sql`: 2026-05 이벤트를 복제해 2025-02~2026-10(10월은 1~20일) 출고 이력을 만든다.
  월별 건수에 추세·계절성(11~12월 성수기, 2월 비수기)을 줘서 계절 지수·정확도가 나온다. event_id `sim-*`
- `sim_orders_local.sql`: box_spec 7종 재고를 약 2개월치로(넉넉히), `safety_days=7`,
  2025-10~2026-10 월별 정기 제안 + 입고 완료 발주(RECEIVED, 배송 중 없음). summary `dataSource=simulation-seed`
- 재고가 넉넉하므로 보충 주문은 0이고, 게이트를 통과한 신규 규격만 제안서에 오른다
