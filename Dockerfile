# CommerceMind 运行镜像
#
# 镜像内包含：
#   - FastAPI 应用
#   - Redis Python 客户端与 ChromaDB Python 客户端
#
# Redis 服务端和 ChromaDB 服务端由 docker-compose 单独提供；
# 应用镜像本身不启动这两个服务。

FROM python:3.12-slim AS base

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

# 允许在构建时指定 pip 镜像，默认使用官方源。
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install -r requirements.txt

# ── 生产运行镜像 ──────────────────────────────────────────────────────────────
FROM base AS production

RUN useradd --create-home --uid 1000 commercemind

COPY --chown=commercemind:commercemind api ./api
COPY --chown=commercemind:commercemind core ./core
COPY --chown=commercemind:commercemind memory ./memory
COPY --chown=commercemind:commercemind data ./data
COPY --chown=commercemind:commercemind skills ./skills

# ChromaDB 本地嵌入式模式需要可写目录；连接独立 chromadb 服务时不使用。
RUN mkdir -p /app/data/chroma && \
    chown -R commercemind:commercemind /app/data

USER commercemind

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).read()"

CMD ["python", "-m", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
