"""B-B03 阶段02 任务2-1: SQLite 数据库初始化与基础连接工具.

只负责建表和连接管理, 不包含任何业务 CRUD / 插入逻辑 (留给后续 2-2 及以后)。

被 main.py import 时自动执行建表 (main.py 仅新增一行 import db),
启动 uvicorn 即生成数据库文件 bb03.db 及 5 张数据表。
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "bb03.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS stock_observations (
    stock_id INTEGER PRIMARY KEY AUTOINCREMENT,
    reserve_id TEXT NOT NULL,
    material_id TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    source_type TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS demand_records (
    demand_id INTEGER PRIMARY KEY AUTOINCREMENT,
    service_id TEXT NOT NULL,
    material_id TEXT NOT NULL,
    required_quantity INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    source_type TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS transport_observations (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    transport_id TEXT NOT NULL,
    availability TEXT NOT NULL,
    capacity INTEGER,
    observed_at TEXT NOT NULL,
    source_type TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
    evidence_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('ok','unavailable','error')),
    value_json TEXT,
    observed_at TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS alert_records (
    db_id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id INTEGER NOT NULL,
    type TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    material TEXT,
    active INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def get_conn() -> sqlite3.Connection:
    """获取数据库连接。row_factory=Row, 便于后续按列名取值。"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def close_conn(conn: sqlite3.Connection):
    """关闭数据库连接, 传入 None 安全。"""
    if conn is not None:
        conn.close()


def init_db():
    """建表 (IF NOT EXISTS, 幂等, 可重复调用)。"""
    conn = get_conn()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        close_conn(conn)


init_db()  # 模块被 import 时自动建表, 配合 main.py 的一行 import db
