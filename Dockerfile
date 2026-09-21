FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/releases.db \
    APP_HOST=0.0.0.0 \
    APP_PORT=8000

# 依赖在镜像构建阶段安装；容器启动即直接运行服务
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# SQLite 数据目录（compose 中通过命名卷持久化）；首次启动自动建表初始化
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# uvicorn 启动时触发 lifespan 初始化 SQLite（建表、WAL 模式）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
