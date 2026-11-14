-- ============================================================
-- SupplyGuard 阶段03 板块1: 基础查询SQL示例
-- 覆盖: run列表 / 单run详情 / 任务结果 / 依赖检查明细 / 任务定义
-- 用法: sqlite3 bb03.db < board1_query_examples.sql
--       或复制单条到任意SQLite客户端执行
-- ============================================================

-- 1. run 列表 (全部 run, 新在前)
SELECT run_id, scenario_id, run_type, parent_run_id, run_status,
       created_at, completed_at
FROM runs
ORDER BY created_at DESC, run_id DESC;

-- 2. 单 run 详情 (含输入快照/假设快照)
SELECT * FROM runs WHERE run_id = 'BB03-RUN-001';

-- 3. 某 run 的任务结果明细 (关联任务名)
SELECT tr.id, tr.run_id, tr.task_id, td.task_name,
       tr.result_status, tr.summary_reason
FROM task_results tr
LEFT JOIN task_definitions td ON td.task_id = tr.task_id
WHERE tr.run_id = 'BB03-RUN-001'
ORDER BY tr.task_id;

-- 4. 某 run 的依赖检查明细 (关联任务名, 按任务+依赖键排序)
SELECT dc.id, dc.run_id, dc.task_id, td.task_name,
       dc.dep_key, dc.dep_desc, dc.status, dc.evidence, dc.reason
FROM dependency_checks dc
LEFT JOIN task_definitions td ON td.task_id = dc.task_id
WHERE dc.run_id = 'BB03-RUN-001'
ORDER BY dc.task_id, dc.dep_key;

-- 5. 静态任务定义 (T1~T5, 只含稳定业务结构, 无实时状态)
SELECT task_id, task_name, description, rule_notes
FROM task_definitions
ORDER BY task_id;

-- 6. 单任务的结果历史 (同一任务多次 run 的结果, 用于跨 run 对比)
SELECT tr.run_id, tr.result_status, tr.summary_reason
FROM task_results tr
WHERE tr.task_id = 'T1'
ORDER BY tr.id DESC;
