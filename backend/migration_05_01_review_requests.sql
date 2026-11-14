-- ============================================================
-- SupplyGuard 阶段05 板块1: 建表迁移脚本
--   review_requests (接收复核请求表)
--
-- 说明:
--   - 本脚本 DDL 与 backend/db.py 的 _SCHEMA 中 review_requests 段
--     及 review_store.py 的 _REVIEW_SCHEMA 完全一致;
--     db.py 在应用启动时 (被 main.py import) 自动执行建表,
--     正常重启后端即完成迁移, 无需手工执行本脚本。
--   - 本脚本供手工迁移 / 评审查看: 可对既有 bb03.db 直接执行,
--     幂等 (IF NOT EXISTS), 重复执行安全, 不影响存量数据。
--
-- 手工应用方式 (二选一, 在仓库根目录执行):
--   sqlite3 backend/bb03.db < backend/migration_05_01_review_requests.sql
--   python -c "import sys; sys.path.insert(0,'backend'); import review_store; review_store.init_review_tables()"
--
-- 表说明:
--   review_requests 接收复核请求; status 枚举 pending / completed
--   (默认 pending); completed_at 复核完成时回填, pending 期间 NULL;
--   幂等: 同一 (dispatch_id, service_id, material_id) 同时最多一条
--   pending 复核单 (部分唯一索引 uq_review_pending_triple);
--   复核完成后同一三元组允许再次生成 (再次待复核)。
--   写入/流转层: review_store.py (本板块只建表, 不提供 API 接口)。
-- ============================================================

BEGIN TRANSACTION;

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

COMMIT;
