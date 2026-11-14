-- ============================================================
-- SupplyGuard 阶段04 板块1: 建表迁移脚本
--   blockers / plan_definitions / plan_results
--
-- 说明:
--   - 本脚本 DDL 与 backend/db.py 的 _SCHEMA 同段完全一致;
--     db.py 在应用启动时 (被 main.py import) 自动执行建表,
--     正常重启后端即完成迁移, 无需手工执行本脚本。
--   - 本脚本供手工迁移 / 评审查看: 可对既有 bb03.db 直接执行,
--     幂等 (IF NOT EXISTS), 重复执行安全, 不影响存量数据。
--
-- 手工应用方式 (二选一, 在仓库根目录执行):
--   sqlite3 backend/bb03.db < backend/migration_04_01_blockers_plans.sql
--   python -c "import sys; sys.path.insert(0,'backend'); import db"
--
-- 表说明:
--   blockers        缺口记录; 同一 run 同一 (服务点, 物资) 只保留一条
--                   (UNIQUE 约束 + gap_check.py UPSERT 写入)
--   plan_definitions 调配计划定义 (本板块只建表, 写入由后续板块实现)
--   plan_results     计划执行结论; status 仅 executable / non_executable
-- ============================================================

BEGIN TRANSACTION;

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
    status TEXT NOT NULL CHECK(status IN ('executable','non_executable')),
    reason_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(run_id, plan_id)
);
CREATE INDEX IF NOT EXISTS idx_plan_results_run ON plan_results(run_id);

COMMIT;
