"""B-B03 数据库初始化与基础连接工具.

职责:
    - 建表 (幂等, IF NOT EXISTS): 阶段02观测/证据表 + 阶段03板块1 4张核心表
    - 阶段03文档重建迁移: 旧版(字段不符文档)同名表自动冻结为 *_legacy, 原数据保留
    - 静态种子: T1~T5 任务定义落库 task_definitions (幂等, 只插不覆盖)
    - run_id 自增规则工具: next_run_id() → BB03-RUN-001 / BB03-RUN-002 / ...

不包含任何业务 CRUD / T1~T5 校验计算逻辑 (阶段03板块1 禁止实现)。

被 main.py import 时自动执行建表+迁移+种子, 启动 uvicorn 即完成数据层落地。

阶段03板块1 数据层约束 (本文件强制):
    - task_definitions 只存稳定业务结构 (4个静态字段, 种子只来自
      constants.TASK_DEFINITIONS), 禁止写入实时库存/需求状态
    - runs.run_status=completed 之后快照列禁止修改、禁止覆盖
      (trg_runs_completed_snapshot_immutable 触发器强制)
"""

import sqlite3
from pathlib import Path

from constants import (
    DEPENDENCY_STATUS,
    PLAN_RESULT_STATUS,
    RUN_ID_PREFIX,
    RUN_STATUS,
    RUN_TYPE,
    TASK_DEFINITIONS,
    TASK_RESULT_STATUS,
    format_run_id,
)

DB_PATH = Path(__file__).parent / "bb03.db"

# 建表 CHECK 约束直接由 constants 常量生成, 保证「常量枚举 = 表约束」单一来源
_DEPS_STATUS_SQL = ", ".join("'%s'" % s for s in DEPENDENCY_STATUS)
_TASK_STATUS_SQL = ", ".join("'%s'" % s for s in TASK_RESULT_STATUS)
_RUN_STATUS_SQL = ", ".join("'%s'" % s for s in RUN_STATUS)
_PLAN_STATUS_SQL = ", ".join("'%s'" % s for s in PLAN_RESULT_STATUS)

_SCHEMA = f"""
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
-- 阶段02板块4: 调配记录 (储备点→服务点), 每次上报追加一行
CREATE TABLE IF NOT EXISTS dispatch_records (
    dispatch_id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_reserve_id TEXT NOT NULL,
    to_service_id TEXT NOT NULL,
    material_id TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    transport_id TEXT,
    dispatched_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
-- 阶段02板块4: 接收记录 (新表; 旧 receipt_records 为历史表, 原样保留不动)
-- receipt_id 为消息自带业务字段, UNIQUE 用于"同一 receipt_id 重复上传不新增"
CREATE TABLE IF NOT EXISTS receipt_records_v2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id TEXT NOT NULL UNIQUE,
    dispatch_id TEXT,
    service_id TEXT NOT NULL,
    material_id TEXT NOT NULL,
    received_quantity INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    source_type TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- ============================================================
-- 阶段03 板块1 (按阶段03文档重建): 4张核心表, 字段严格对齐文档
-- task_definitions / runs / dependency_checks / task_results
-- 旧版同名表(字段不符文档)在 init_db 时已自动冻结为 *_legacy
-- (见 _freeze_legacy_tables, 原数据保留)。dependency_definitions
-- 旧表同样冻结, 新版依赖描述随检查行落 dependency_checks(dep_desc)。
-- ============================================================
CREATE TABLE IF NOT EXISTS task_definitions (
    task_id TEXT PRIMARY KEY,          -- 任务编号 T1~T5
    task_name TEXT NOT NULL,           -- 任务名称
    description TEXT,                  -- 任务描述 (判定目标)
    rule_notes TEXT                    -- 判定规则备注 (依赖规则说明)
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,           -- BB03-RUN-001/002... 自增 (next_run_id)
    scenario_id TEXT NOT NULL,         -- 场景编号 (如 S1)
    run_type TEXT NOT NULL DEFAULT '{RUN_TYPE}',  -- 常量 RUN_TYPE = "reality"
    parent_run_id TEXT,                -- 父 run; 顶层 run 为 NULL
    run_status TEXT NOT NULL DEFAULT 'pending'
        CHECK(run_status IN ({_RUN_STATUS_SQL})),
    input_snapshot_json TEXT,          -- 输入快照; completed 后禁止修改
    assumptions_json TEXT,             -- 假设快照; completed 后禁止修改
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    completed_at TEXT                  -- 完成时间; pending 期间为 NULL
);
CREATE TABLE IF NOT EXISTS dependency_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,              -- 关联 runs.run_id
    task_id TEXT NOT NULL,             -- 关联任务 T1~T5
    dep_key TEXT NOT NULL,             -- 依赖键 (如 T1-D1)
    dep_desc TEXT,                     -- 依赖描述
    status TEXT NOT NULL CHECK(status IN ({_DEPS_STATUS_SQL})),
    evidence TEXT,                     -- 判断依据/证据摘要
    reason TEXT                        -- 判定理由 (不满足原因等)
);
CREATE TABLE IF NOT EXISTS task_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,              -- 关联 runs.run_id
    task_id TEXT NOT NULL,             -- 关联任务 T1~T5
    result_status TEXT NOT NULL CHECK(result_status IN ({_TASK_STATUS_SQL})),
    summary_reason TEXT                -- 结果摘要/理由
);
CREATE INDEX IF NOT EXISTS idx_dependency_checks_run ON dependency_checks(run_id);
CREATE INDEX IF NOT EXISTS idx_task_results_run ON task_results(run_id);

-- 数据层强制: run completed 之后, 输入快照/假设快照禁止修改、禁止覆盖
CREATE TRIGGER IF NOT EXISTS trg_runs_completed_snapshot_immutable
BEFORE UPDATE ON runs
WHEN OLD.run_status = 'completed'
 AND (NEW.input_snapshot_json IS NOT OLD.input_snapshot_json
   OR NEW.assumptions_json IS NOT OLD.assumptions_json)
BEGIN
    SELECT RAISE(ABORT, 'runs: completed run snapshot columns are immutable');
END;

-- ============================================================
-- 阶段04 板块3: 可复用 what-if 假设方案存储
-- 只存用户填写的假设配置 (hypothesis_list), 不存 T1~T5 推演结果;
-- 推演每次实时计算 (板块2 run_whatif_once)。同一 run_id 下 name 唯一。
-- (阶段04 按文档重建前的旧表, 保留不动, 由后续板块处理)
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
-- 阶段04 板块4: what-if 推演快照归档 (旧表, 保留不动, 由后续板块处理)
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

-- ============================================================
-- 阶段02板块4: 统一证据表 evidence_v2 (只新增)
-- 旧 evidence 表为历史遗留 (自增主键/retrieved_at), 原样保留不动;
-- 本表按阶段02规格: evidence_id 用 UUID (代码生成), 无 retrieved_at。
-- status 枚举: ok / unavailable / error; evidence_type 枚举:
-- stock / demand / dispatch / receipt / transport。
-- ============================================================
CREATE TABLE IF NOT EXISTS evidence_v2 (
    evidence_id TEXT PRIMARY KEY,
    evidence_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('ok','unavailable','error')),
    value_json TEXT,
    observed_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    error_message TEXT
);

-- ============================================================
-- 阶段04 板块1: 缺口识别与计划存储 3 张新表
--   blockers:        缺口记录; 同一 run 同一 (服务点, 物资) 只保留一条
--                    (UNIQUE 约束 + gap_check.py UPSERT 写入)
--   plan_definitions:调配计划定义 (本板块只建表, 写入由后续板块实现)
--   plan_results:    计划执行结论; status 仅 executable / non_executable
-- ============================================================
CREATE TABLE IF NOT EXISTS blockers (
    blocker_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    service_id TEXT NOT NULL,
    material_id TEXT NOT NULL,
    required_quantity INTEGER NOT NULL,
    valid_received INTEGER NOT NULL,
    gap INTEGER NOT NULL,
    reason_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(run_id, service_id, material_id)
);
CREATE INDEX IF NOT EXISTS idx_blockers_run ON blockers(run_id);
CREATE TABLE IF NOT EXISTS plan_definitions (
    plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    plan_name TEXT NOT NULL,
    reserve_id TEXT NOT NULL,
    vehicle_id TEXT NOT NULL,
    route_id TEXT NOT NULL,
    material_id TEXT NOT NULL,
    transfer_qty INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_plan_definitions_run ON plan_definitions(run_id);
CREATE TABLE IF NOT EXISTS plan_results (
    plan_result_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    plan_id INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ({_PLAN_STATUS_SQL})),
    reason_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(run_id, plan_id)
);
CREATE INDEX IF NOT EXISTS idx_plan_results_run ON plan_results(run_id);

-- ============================================================
-- 阶段05 板块1: 接收复核请求表 review_requests
--   review_id TEXT 主键 (代码生成: BB03-REV-001/002...);
--   status 枚举: pending / completed (默认 pending);
--   created_at 自动填充; completed_at 复核完成时回填, pending 期间 NULL;
--   幂等: 同一 (dispatch_id, service_id, material_id) 同时最多一条
--   pending 复核单 (部分唯一索引 uq_review_pending_triple);
--   复核完成后同一三元组允许再次生成 (再次待复核)。
--   写入层: review_store.py (本板块只建表, 不提供 API 接口)。
-- ============================================================
CREATE TABLE IF NOT EXISTS review_requests (
    review_id TEXT PRIMARY KEY,
    service_id TEXT NOT NULL,
    material_id TEXT NOT NULL,
    dispatch_id TEXT NOT NULL,
    trigger_run_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','completed')),
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    completed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_review_pending_triple
    ON review_requests(dispatch_id, service_id, material_id)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_review_requests_status
    ON review_requests(status);
CREATE INDEX IF NOT EXISTS idx_review_requests_trigger_run
    ON review_requests(trigger_run_id);
"""

# 阶段03文档重建: 旧版(第一次尝试, 字段不符文档)表 → 冻结名。
# marker 为「新规格特有列」: 表存在但缺该列 → 判定旧版, 重命名冻结;
# marker 为 None 的表新版已取消 (dependency_definitions), 存在即冻结。
_LEGACY_RENAMES = (
    ("task_definitions", "task_definitions_legacy", "rule_notes"),
    ("dependency_definitions", "dependency_definitions_legacy", None),
    ("dependency_checks", "dependency_checks_legacy", "evidence"),
    ("task_results", "task_results_legacy", "summary_reason"),
    ("runs", "runs_legacy", "run_status"),
)


def get_conn() -> sqlite3.Connection:
    """获取数据库连接。row_factory=Row, 便于后续按列名取值。"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def close_conn(conn: sqlite3.Connection):
    """关闭数据库连接, 传入 None 安全。"""
    if conn is not None:
        conn.close()


def _freeze_legacy_tables(conn):
    """旧版同名表一次性冻结为 *_legacy (幂等, 原数据原样保留)。

    只处理「表存在且仍是旧字段结构」的情况; 已冻结过(旧名消失)或
    已是新规格(含 marker 列)的库直接跳过。冻结后由 _SCHEMA 重建新表。
    重复启动不会二次重命名、不会覆盖 *_legacy 已有数据。
    """
    for name, legacy_name, marker in _LEGACY_RENAMES:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        if row is None:
            continue  # 表不存在 (全新库, 或已冻结过)
        if marker is not None:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % name)]
            if marker in cols:
                continue  # 已是新规格表
        try:
            conn.execute("ALTER TABLE %s RENAME TO %s" % (name, legacy_name))
        except sqlite3.OperationalError as exc:
            raise RuntimeError(
                "freeze legacy table %s -> %s failed: %s "
                "(同名 *_legacy 已存在, 请人工确认后处理)" % (name, legacy_name, exc)
            ) from exc


def _seed_task_definitions(conn):
    """T1~T5 静态任务定义落库 (INSERT OR IGNORE, 幂等, 不覆盖已有行)。

    task_definitions 只存稳定业务结构 (任务名/描述/判定规则), 种子只来自
    constants.TASK_DEFINITIONS; 禁止写入实时库存/需求/运输等运行时状态。
    """
    conn.executemany(
        "INSERT OR IGNORE INTO task_definitions "
        "(task_id, task_name, description, rule_notes) VALUES (?,?,?,?)",
        [(t["task_id"], t["task_name"], t["description"], t["rule_notes"])
         for t in TASK_DEFINITIONS],
    )


def next_run_id(conn=None) -> str:
    """按 BB03-RUN-001/002... 自增规则计算下一个 run_id (只读, 不占用序列)。

    序列 = runs 表当前最大序号 + 1 (空表 → BB03-RUN-001)。
    并发写入时的唯一性由后续板块写入层以主键冲突兜底, 本函数只负责生成。
    """
    own = conn is None
    if own:
        conn = get_conn()
    try:
        row = conn.execute(
            "SELECT MAX(CAST(SUBSTR(run_id, ?) AS INTEGER)) FROM runs "
            "WHERE run_id LIKE ?",
            (len(RUN_ID_PREFIX) + 1, RUN_ID_PREFIX + "%"),
        ).fetchone()
        seq = (row[0] or 0) + 1
        return format_run_id(seq)
    finally:
        if own:
            close_conn(conn)


def init_db():
    """建表 + 旧表冻结迁移 + T1~T5 静态种子 (幂等, 可重复调用)。"""
    conn = get_conn()
    try:
        _freeze_legacy_tables(conn)
        conn.executescript(_SCHEMA)
        _seed_task_definitions(conn)
        conn.commit()
    finally:
        close_conn(conn)


init_db()  # 模块被 import 时自动建表, 配合 main.py 的一行 import db
