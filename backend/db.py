"""B-B03 数据库初始化与基础连接工具.

只负责建表和连接管理, 不包含任何业务 CRUD / 插入逻辑。

被 main.py import 时自动执行建表 (main.py 仅新增一行 import db),
启动 uvicorn 即生成数据库文件 bb03.db
(阶段02 7张表 + 阶段03板块1 4张表 + 阶段04板块3、板块4各1张表)。
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
CREATE TABLE IF NOT EXISTS receipt_records (
    receipt_id TEXT PRIMARY KEY,
    reserve_id TEXT NOT NULL,
    material_id TEXT NOT NULL,
    quantity_change INTEGER NOT NULL,
    received_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','completed')),
    payload TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT
);

-- ============================================================
-- 阶段03 板块1: 任务/依赖检查/任务结果存储 (仅建表, 无业务逻辑)
-- 说明: runs 表阶段02已存在(旧规格 status pending/completed),
-- 本板块按约定不重建; 阶段03新 runs 规格(run_status
-- running/completed/error 等)的合并方案留待后续板块。
-- 规则备忘: run_status=completed 的 run 后续业务层不允许修改。
-- ============================================================
CREATE TABLE IF NOT EXISTS task_definitions (
    task_id TEXT PRIMARY KEY,
    task_name TEXT NOT NULL,
    task_desc TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS dependency_definitions (
    dep_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    dep_name TEXT NOT NULL,
    dep_desc TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS dependency_checks (
    check_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    dep_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('satisfied','unsatisfied','unknown')),
    check_msg TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS task_results (
    result_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('established','blocked','pending_verification','unknown')),
    result_msg TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- ============================================================
-- 阶段04 板块3: 可复用 what-if 假设方案存储
-- 只存用户填写的假设配置 (hypothesis_list), 不存 T1~T5 推演结果;
-- 推演每次实时计算 (板块2 run_whatif_once)。同一 run_id 下 name 唯一。
-- ============================================================
CREATE TABLE IF NOT EXISTS hypothesis_scenario (
    scenario_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    hypothesis_list TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(run_id, name)
);

-- ============================================================
-- 阶段04 板块4: what-if 推演快照归档
-- 只归档查阅: 保存推演当时的 hypothesis_list + result_json (与方案解耦,
-- scenario_id 仅记录来源方案, 可为空)。后续推演依然实时计算, 绝不把
-- 快照 result_json 当输入。同 run_id 下 name 唯一。
-- ============================================================
CREATE TABLE IF NOT EXISTS hypothesis_result_snapshot (
    snapshot_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    scenario_id TEXT,
    name TEXT NOT NULL,
    description TEXT,
    hypothesis_list TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(run_id, name)
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
