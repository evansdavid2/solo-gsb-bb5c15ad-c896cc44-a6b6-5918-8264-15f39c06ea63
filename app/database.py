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
    max_attempts INTEGER NOT NULL DEFAULT 1,      -- 最大尝试次数（含首次），旧批次兼容为 1
    retry_delay_seconds INTEGER NOT NULL DEFAULT 0, -- 自动重试延迟（秒）
    attempt_count INTEGER NOT NULL DEFAULT 0,     -- 已失败回报次数（原子递增）
    retry_scheduled INTEGER NOT NULL DEFAULT 0,   -- 1=已安排自动重试，等待 next_retry_at 到期
    next_retry_at TEXT,                           -- 重试到期时间（ISO8601），到期前不进入 next_nodes
    level        INTEGER NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    status       TEXT NOT NULL DEFAULT 'pending', -- pending | success | failed | rolled_back
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
    event_type   TEXT NOT NULL, -- report_success | report_failure | rollback | retry_reset | retry_scheduled
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
    if "max_attempts" not in cols:
        # 旧批次按 max_attempts=1 兼容：失败即终态回滚，行为与升级前一致
        conn.execute(
            "ALTER TABLE nodes ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 1"
        )
    if "retry_delay_seconds" not in cols:
        conn.execute(
            "ALTER TABLE nodes ADD COLUMN retry_delay_seconds INTEGER NOT NULL DEFAULT 0"
        )
    if "attempt_count" not in cols:
        conn.execute(
            "ALTER TABLE nodes ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0"
        )
    if "retry_scheduled" not in cols:
        conn.execute(
            "ALTER TABLE nodes ADD COLUMN retry_scheduled INTEGER NOT NULL DEFAULT 0"
        )
    if "next_retry_at" not in cols:
        conn.execute("ALTER TABLE nodes ADD COLUMN next_retry_at TEXT")


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
