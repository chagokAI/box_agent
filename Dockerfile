# 박스 주문 제안 에이전트 이미지 — 같은 이미지로 agent / business-mcp / rag-mcp 세 서비스를 띄운다(command 만 다름)
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN groupadd --system app && useradd --system --gid app --home-dir /app app
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# 코드·양식·규정 문서·박스왕 카탈로그(catalog.db). .env 는 넣지 않는다(.dockerignore) — 설정은 compose 환경변수로
COPY --chown=app:app . .
RUN mkdir -p /app/proposals && chown app:app /app/proposals
USER app

# 기본은 Agent 서버. business-mcp / rag-mcp 는 compose 에서 command 로 바꾼다
EXPOSE 8000 9101 9102
CMD ["python", "agent_server.py", "--host", "0.0.0.0", "--port", "8000"]
