-- ============================================================
-- SupplyGuard 阶段03 板块1: S1 场景基线测试脚本
--
-- 用途: 为后续 Modify → Run → Verify 流程准备可重复落库的基线数据。
-- 内容: 2个储备点(R1/R2)库存 + 3个服务点(P1/P2/P3)物资需求
--       + 运输(V1/V2) + 接收(签收)基线数据。
--       数值与 simulator/scenario_data.json 的 S1 场景一致。
--
-- 幂等: 每次执行先删除 source_type='s1_baseline' 的旧基线行再重建,
--       不影响 MQTT 实时观测数据 (其 source_type 默认为 'MQTT')。
--       重复执行结果一致, 可随时"复位基线"。
-- ============================================================

BEGIN TRANSACTION;

-- 清理旧基线行 (只动本脚本种下的数据, MQTT 数据不受影响)
DELETE FROM demand_records         WHERE source_type = 's1_baseline';
DELETE FROM stock_observations     WHERE source_type = 's1_baseline';
DELETE FROM transport_observations WHERE source_type = 's1_baseline';
DELETE FROM receipt_records_v2     WHERE source_type = 's1_baseline';

-- ------------------------------------------------------------
-- 1) 储备点库存基线 (6条): 全网合计 W=10, L=7, R=12
-- ------------------------------------------------------------
INSERT INTO stock_observations
    (reserve_id, material_id, quantity, observed_at, source_type) VALUES
    ('R1', 'W', 2, datetime('now','localtime','-40 minutes'), 's1_baseline'),
    ('R1', 'L', 2, datetime('now','localtime','-40 minutes'), 's1_baseline'),
    ('R1', 'R', 8, datetime('now','localtime','-40 minutes'), 's1_baseline'),
    ('R2', 'W', 8, datetime('now','localtime','-40 minutes'), 's1_baseline'),
    ('R2', 'L', 5, datetime('now','localtime','-40 minutes'), 's1_baseline'),
    ('R2', 'R', 4, datetime('now','localtime','-40 minutes'), 's1_baseline');

-- ------------------------------------------------------------
-- 2) 3个服务点物资需求基线 (9条): 全网合计 W=15, L=14, R=7
--    与库存相比 W/L 存在缺口 → 供 Modify (补库存/降需求) 测试用
-- ------------------------------------------------------------
INSERT INTO demand_records
    (service_id, material_id, required_quantity, observed_at, source_type) VALUES
    ('P1', 'W', 4, datetime('now','localtime','-30 minutes'), 's1_baseline'),
    ('P1', 'L', 1, datetime('now','localtime','-30 minutes'), 's1_baseline'),
    ('P1', 'R', 2, datetime('now','localtime','-30 minutes'), 's1_baseline'),
    ('P2', 'W', 5, datetime('now','localtime','-30 minutes'), 's1_baseline'),
    ('P2', 'L', 5, datetime('now','localtime','-30 minutes'), 's1_baseline'),
    ('P2', 'R', 3, datetime('now','localtime','-30 minutes'), 's1_baseline'),
    ('P3', 'W', 6, datetime('now','localtime','-30 minutes'), 's1_baseline'),
    ('P3', 'L', 8, datetime('now','localtime','-30 minutes'), 's1_baseline'),
    ('P3', 'R', 2, datetime('now','localtime','-30 minutes'), 's1_baseline');

-- ------------------------------------------------------------
-- 3) 运输基线 (2条): V1/V2 均 AVAILABLE, 时间晚于需求 (供 T2/T3 判断)
-- ------------------------------------------------------------
INSERT INTO transport_observations
    (transport_id, availability, capacity, observed_at, source_type) VALUES
    ('V1', 'AVAILABLE', 4, datetime('now','localtime','-20 minutes'), 's1_baseline'),
    ('V2', 'AVAILABLE', 4, datetime('now','localtime','-20 minutes'), 's1_baseline');

-- ------------------------------------------------------------
-- 4) 接收(签收)基线 (3条): 每服务点一条, 时间晚于需求 (供 T3/T4 判断)
--    dispatch_id 留空: 调配记录由后续测试按需补充, 本基线只备接收数据
-- ------------------------------------------------------------
INSERT INTO receipt_records_v2
    (receipt_id, dispatch_id, service_id, material_id,
     received_quantity, observed_at, source_type) VALUES
    ('RCT-S1-001', NULL, 'P1', 'W', 2,
     datetime('now','localtime','-10 minutes'), 's1_baseline'),
    ('RCT-S1-002', NULL, 'P2', 'L', 2,
     datetime('now','localtime','-10 minutes'), 's1_baseline'),
    ('RCT-S1-003', NULL, 'P3', 'R', 2,
     datetime('now','localtime','-10 minutes'), 's1_baseline');

COMMIT;

-- ============================================================
-- 基线预期判定 (供后续板块 Run → Verify 参考, 本板块不实现判定逻辑):
--   T1 (库存≥需求): W 库存10<需求15, L 库存7<需求14 → 预期 blocked
--   T2 (运输可用): V1/V2 AVAILABLE                   → 预期 established
--   T3 (需求后有活动): 需求(-30min) 之后存在运输(-20min)/签收(-10min)
--                                                    → 预期 established
--   T4 (签收闭环): 有签收记录且无在途运输             → 预期 established
--   T5 (证据一致): 无负值/非法状态/时间倒挂           → 预期 established
--   Modify 示例: 给 R1/R2 补 W/L 库存后再 Run, T1 应转 established
-- ============================================================
