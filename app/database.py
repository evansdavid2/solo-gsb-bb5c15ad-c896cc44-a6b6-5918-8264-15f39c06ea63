"""SQLite 连接管理与建表初始化。

设计取舍：
- SQLite 默认不支持多连接并发写入。本服务写多、数据量小，使用单个共享连接 +
  一把线程锁即可保证安全；uvicorn 多线程下所有事务串行执行，行为简单可靠。
- WAL 模式让读不阻塞写，适合查询接口与回报接口同时被调用的场景。
"""
from __future__ import annotations

import os
import sqlite3
import threading

from . import config

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS releases (
    id              TEXT PRIMARY KEY,
    version         TEXT NOT NULL,
    status          TEXT NOT NULL,              -- running | succeeded | failed | partially_rolled_back
    levels_json     TEXT NOT NULL DEFAULT '[]', -- 拓扑层级: [["a"],["b","c"],["d"]]
    error_code      TEXT,
    error_message   TEXT,
    failed_node     TEXT,
    failed_at       TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nodes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    release_id   TEXT NOT NULL REFERENCES releases(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    node_type    TEXT NOT NULL DEFAULT 'service',
    estimated_seconds INTEGER NOT NULL DEFAULT 0, -- 预估工期（秒），旧批次 / 未填按 0
    max_attempts INTEGER NOT NULL DEFAULT 1,      -- 最大尝试次数（含首次，1～10）；旧批次按 1
    retry_delay_seconds INTEGER NOT NULL DEFAULT 0, -- 自动重试退避延迟（秒，0～3600）
    attempt_count INTEGER NOT NULL DEFAULT 0,     -- 已失败次数（原子递增）
    next_retry_at TEXT,                           -- 下次可重试时刻（ISO8601）；NULL 表示未安排
    retry_scheduled INTEGER NOT NULL DEFAULT 0,   -- 是否处于等待重试的 pending：0|1
    level        INTEGER NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    status       TEXT NOT NULL DEFAULT 'pending', -- pending(可含等待重试) | success | failed | rolled_back
    reported_version TEXT,
    error_code   TEXT,
    error_message TEXT,
    reported_at  TEXT,
    updated_at   TEXT NOT NULL,
    UNIQUE(release_id, name)
);

CREATE TABLE IF NOT EXISTS edges (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    release_id   TEXT NOT NULL REFERENCES releases(id) ON DELETE CASCADE,
    from_node    TEXT NOT NULL, -- 前置（依赖方）
    to_node      TEXT NOT NULL, -- 后置（依赖前置）
    UNIQUE(release_id, from_node, to_node)
);

CREATE INDEX IF NOT EXISTS idx_nodes_release ON nodes(release_id);
CREATE INDEX IF NOT EXISTS idx_edges_release ON edges(release_id);

CREATE TABLE IF NOT EXISTS node_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    release_id   TEXT NOT NULL,
    node_name    TEXT NOT NULL,
    event_type   TEXT NOT NULL, -- report_success | report_failure | rollback | retry_reset | retry_scheduled | retry_exhausted
    version      TEXT,
    detail_json  TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_release ON node_events(release_id);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """对已存在的旧版 SQLite 文件做增量升级（CREATE TABLE IF NOT EXISTS 不会补列）。"""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(nodes)")}
    if "estimated_seconds" not in cols:
        # 旧批次缺失工期：统一按 0 处理，CPM 仍可给出完整分析
        conn.execute(
            "ALTER TABLE nodes ADD COLUMN estimated_seconds INTEGER NOT NULL DEFAULT 0"
        )
    # 自动重试相关列：旧批次按 max_attempts=1（不自动重试）兼容既有失败即回滚语义
    retry_columns = {
        "max_attempts": "INTEGER NOT NULL DEFAULT 1",
        "retry_delay_seconds": "INTEGER NOT NULL DEFAULT 0",
        "attempt_count": "INTEGER NOT NULL DEFAULT 0",
        "next_retry_at": "TEXT",
        "retry_scheduled": "INTEGER NOT NULL DEFAULT 0",
    }
    for column, ddl in retry_columns.items():
        if column not in cols:
            conn.execute(f"ALTER TABLE nodes ADD COLUMN {column} {ddl}")


def init_db(db_path: str | None = None) -> None:
    """创建数据目录、初始化表结构。进程启动时执行一次。"""
    global _conn
    path = db_path or config.DB_PATH
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)

    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    _migrate(conn)
    conn.commit()
    _conn = conn


def get_conn() -> sqlite3.Connection:
    if _conn is None:  # 兼容未走 lifespan 的本地导入场景
        init_db()
    assert _conn is not None
    return _conn


class transaction:
    """简单事务上下文：异常回滚，正常提交；全程持锁串行化写操作。"""

    def __enter__(self) -> sqlite3.Connection:
        _lock.acquire()
        conn = get_conn()
        conn.execute("BEGIN")
        return conn

    def __exit__(self, exc_type, exc, tb) -> None:
        conn = get_conn()
        try:
            if exc_type is None:
                conn.commit()
            else:
                conn.rollback()
        finally:
            _lock.release()


def lock() -> threading.RLock:
    """供只读接口串行化快照读取，避免读到未提交的中间状态。"""
    return _lock
