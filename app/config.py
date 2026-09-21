import os

# 服务监听地址 / 端口（容器内监听 0.0.0.0:8000）
HOST = os.getenv("APP_HOST", "0.0.0.0")
PORT = int(os.getenv("APP_PORT", "8000"))

# SQLite 数据库文件路径。docker-compose 中挂载到 /data，保证数据持久化。
DB_PATH = os.getenv("DB_PATH", "/data/releases.db")
