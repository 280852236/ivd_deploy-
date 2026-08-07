FROM python:3.12-slim AS builder
WORKDIR /build
RUN apt-get update && apt-get install -y libpq-dev gcc && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim
WORKDIR /app

# --- 运行时优化 ---
ENV PYTHONOPTIMIZE=2 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y libpq5 unrar-free curl && rm -rf /var/lib/apt/lists/*
COPY --from=builder /install /usr/local
COPY shared.py hybrid_connection.py app.py celery_app.py tasks.py ./
COPY utils/ ./utils/
COPY static/ ./static/
COPY blueprints/ ./blueprints/
COPY services/ ./services/
COPY tests/ ./tests/

# --- 健康检查 ---
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8081/api/health || exit 1

CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:8081", "app:app"]
