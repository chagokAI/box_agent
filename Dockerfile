# 박스 주문 제안 에이전트 이미지 — 같은 이미지로 agent / business-mcp / rag-mcp 세 서비스를 띄운다(command 만 다름)
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
ENV CATALOG_DB=/app/proposals/catalog.db
RUN groupadd --system app && useradd --system --gid app --home-dir /app app
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# Runtime files only. Catalog data lives in the proposals volume, outside the image.
COPY --chown=app:app agent_server.py box_spec_agent.py harness.py llm_writer.py initialize_knowledge.py ./
COPY --chown=app:app mcp_servers ./mcp_servers
COPY --chown=app:app knowledge ./knowledge
COPY --chown=app:app prompts ./prompts
COPY --chown=app:app skills ./skills
COPY --chown=app:app rules ./rules
RUN mkdir -p /app/proposals && chown app:app /app/proposals
USER app

# 기본은 Agent 서버. business-mcp / rag-mcp 는 compose 에서 command 로 바꾼다
EXPOSE 8000 9101 9102
CMD ["python", "agent_server.py", "--host", "0.0.0.0", "--port", "8000"]
