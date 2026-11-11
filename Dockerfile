FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    METER_DB_PATH=/srv/data/meter_store.db

WORKDIR /srv/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# 数据目录由命名卷挂载；先创建并归属非 root 用户，保证容器内可写
RUN useradd --create-home appuser \
    && mkdir -p /srv/data \
    && chown -R appuser:appuser /srv
USER appuser

VOLUME ["/srv/data"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
