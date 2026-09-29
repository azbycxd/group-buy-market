# syntax=docker/dockerfile:1

FROM python:3.12-slim

ARG TORCH_VERSION=2.14.0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/opt/huggingface \
    HUGGINGFACE_HUB_CACHE=/opt/huggingface/hub \
    HF_HUB_DISABLE_XET=1

WORKDIR /app

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /app/data /opt/huggingface \
    && chown -R app:app /app /opt/huggingface

COPY pyproject.toml /app/pyproject.toml
COPY *.py /app/
COPY fake_java /app/fake_java
COPY rag /app/rag
COPY tools /app/tools
COPY docs/rules /app/docs/rules
COPY demo_static /app/demo_static

RUN python -m pip install --upgrade pip \
    && python -m pip install \
       --index-url https://download.pytorch.org/whl/cpu \
       "torch==${TORCH_VERSION}" \
    && python -m pip install .

# Bake both retrieval models into the final image. Runtime is forced offline below.
RUN python -c "from sentence_transformers import SentenceTransformer, CrossEncoder; SentenceTransformer('BAAI/bge-small-zh-v1.5'); CrossEncoder('BAAI/bge-reranker-base', device='cpu')"

ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    CHECKPOINT_DB_PATH=/app/data/checkpoints.sqlite \
    AGENT_ACTION_DB_PATH=/app/data/agent_actions.sqlite \
    DEMO_STATE_DB_PATH=/app/data/demo_state.sqlite \
    RAG_DENSE_CACHE_DIR=/app/data/rag_cache

VOLUME ["/app/data"]
EXPOSE 8000

USER app

CMD ["python", "-m", "uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
