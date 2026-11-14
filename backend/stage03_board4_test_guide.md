# 阶段03 板块4 交付说明（含完整测试方法）

## 交付文件清单

| 文件 | 说明 |
|---|---|
| `t2_check.py` / `t3_check.py` / `t4_check.py` / `t5_check.py` | T2~T5 校验纯判断模块，互相隔离，只读快照，与 T1 共用同一套依赖状态/任务结果状态 |
| `task_execute.py` | execute 编排器：读 task_definitions 静态定义 → 串行执行 T1~T5 → 每任务独立落库 dependency_checks + task_results → 同事务原子置 completed |
| `t1_check.py` | 重构为 `check_t1(snapshot)` 纯判断 + 查询（T1 判断逻辑不变，落库上移至编排器） |
| `run_store_v2.py` | 快照扩展：新增 stocks（每储备点×物资最新）、transports（每运输资源最新），各条目带 created_at |
| `main.py` | `POST /api/runs/{run_id}/execute` 升级为 T1~T5 全套执行；`GET .../task-result` 兼容 task_id=T1~T5 |
| `frontend/T1Verify.html` | 升级为 T1~T5 通关页面：5 张任务卡 + 依赖明细展开 + 历史只读 |
| 本文档 | 板块4 交付说明 + 完整测试方法 |

## 关键设计决策

1. **快照扩展（唯一对板块2 的调整）**：T2/T4/T5 分别需要运输、库存、各观测落库时间，而板块2 快照只有服务点/物资/需求/接收。本次在 `_capture_snapshot` 追加 `stocks`、`transports` 与各条目 `created_at`。约束不破：接口不变、校验仍只读快照、T1 判断逻辑不变（只用 demands/receipts）。
2. **校验模块隔离**：t1~t5_check 各导出 `check_tX(snapshot)` 纯函数（无 DB 访问、无相互 import），状态体系严格复用既有枚举（依赖 satisfied/unsatisfied/unknown；任务 established/blocked/pending_verification/unknown），未新增任何状态。
3. **任务级聚合规则（T2~T5 统一）**：任一 unsatisfied → blocked；全部 satisfied → established；全部 unknown（无有效观测）→ unknown；satisfied 与 unknown 混合 → pending_verification。
4. **execute 保护逻辑不变**：不存在 404、completed 409、整套执行同一事务、`UPDATE runs ... WHERE run_status='pending'` 原子完成闸门 + 触发器双保险。
5. **T1 原样保留**：`check_t1` 自板块3 的逐条判断与聚合规则未改动；T2~T5 卡片样式与 T1 一致（通关页面统一风格）。
6. **前端兼容现有接口**：历史 run 用 5 次并行 `GET task-result?task_id=T1..T5` 组装；新建 run 直接用 execute 返回的全量 tasks。

---

# 测试方法

> 后端启动：`cd E:\supplyguard\backend && uvicorn main:app --reload --port 8000`
> 前端：浏览器打开 `frontend/T1Verify.html`。
> 命令在 Git Bash 执行；PowerShell 5.1 请把 `curl` 换成 `curl.exe`。

## 0. 准备确定性基线（清空观测表后种 S1 基线）

```bash
cd E:\supplyguard\backend
python -c "import db; conn = db.get_conn()
conn.execute('DELETE FROM demand_records'); conn.execute('DELETE FROM stock_observations')
conn.execute('DELETE FROM transport_observations'); conn.execute('DELETE FROM receipt_records_v2')
conn.commit(); conn.close()
conn = db.get_conn(); conn.executescript(open('seed_s1_baseline.sql', encoding='utf-8').read()); conn.commit(); db.close_conn(conn)"
```

## 1. 全套执行：建 run → execute → T1~T5 逐任务落库

```bash
curl -s -X POST http://127.0.0.1:8000/api/runs -H "Content-Type: application/json" -d '{"scenario_id":"S1"}'
# 记下 run_id，执行
curl -s -X POST http://127.0.0.1:8000/api/runs/<run_id>/execute
```

预期（execute 响应 `tasks` 数组，5 个任务）：

| 任务 | 预期状态 | 依据 |
|---|---|---|
| T1 关键服务点最低需求保障 | blocked | 8/9 项需求无签收或签收不足 |
| T2 运输链路可达保障 | established | V1/V2 AVAILABLE |
| T3 调度响应时效保障 | established | 需求(-30min)后有运输(-20min)/签收(-10min) |
| T4 入库签收合规保障 | established | 3 条签收物资均可闭环到库存观测 |
| T5 证据一致性校验 | established | 无负值/非法状态/时间倒挂 |

同时验证：`run_status=completed`、`completed_at` 已写；库中 `dependency_checks` 新增 9+1+9+1+1=21 行、`task_results` 5 行。

## 2. Modify→Run→Verify 场景（各任务 blocked 触发）

**场景A：T1 转 established（板块3 注释场景复用）**——清需求只留 3 条、签收满足后，把 P3-R 签收量 1→2，新 run 的 T1 变 established。

**场景B：T2 转 blocked**
```bash
python -c "import db; conn = db.get_conn(); conn.execute(\"UPDATE transport_observations SET availability='FAULT' WHERE source_type='s1_baseline'\"); conn.commit(); db.close_conn(conn)"
# 建新 run + execute → T2=blocked (全部运输 FAULT); 改回 AVAILABLE 后 T2 恢复 established
```

**场景C：T4 转 blocked**
```bash
python -c "import db; conn = db.get_conn(); conn.execute(\"INSERT INTO receipt_records_v2 (receipt_id, dispatch_id, service_id, material_id, received_quantity, observed_at, source_type) VALUES ('RCT-X-001', NULL, 'P1', 'X', 1, datetime('now','localtime','-5 minutes'), 's1_baseline')\"); conn.commit(); db.close_conn(conn)"
# 建新 run + execute → T4=blocked (签收 X 物资无库存观测, 无法闭环)
```

**场景D：T5 转 blocked**
```bash
python -c "import db; conn = db.get_conn()
conn.execute(\"INSERT INTO stock_observations (reserve_id, material_id, quantity, observed_at, source_type) VALUES ('R1','W',-3,datetime('now','localtime','-2 minutes'),'s1_baseline')\")
conn.execute(\"UPDATE transport_observations SET availability='BROKEN' WHERE transport_id='V1' AND source_type='s1_baseline'\")
conn.commit(); db.close_conn(conn)"
# 建新 run + execute → T5=blocked (负库存矛盾 + 非法运输状态 BROKEN)
```

每个场景测试后重新执行第 0 步即可复位基线。

## 3. GET task-result 兼容 T1~T5

```bash
for T in T1 T2 T3 T4 T5; do
  curl -s "http://127.0.0.1:8000/api/runs/<run_id>/task-result?task_id=$T"
done
```

预期：每个 task_id 返回对应任务结果 + 该任务全部依赖明细（evidence 为解析后对象）。板块3 前的旧 run 查询 T2~T5 返回 `task_result=null`、`dependency_checks=[]`（历史 run 只读，无法补执行）。

## 4. execute 保护逻辑复核

```bash
curl -s -w " [%{http_code}]" -X POST http://127.0.0.1:8000/api/runs/<已完成run_id>/execute   # 409
curl -s -w " [%{http_code}]" -X POST http://127.0.0.1:8000/api/runs/BB03-RUN-999/execute   # 404
```

## 5. 快照只读验证

建 run（execute 完成）后修改实时观测数据，再查该 run 的 task-result——结果不随实时数据变化（校验基于冻结快照）。可用场景B/C/D 中"改回数据"后的旧 run 结果做对照：旧 run 的 T2 仍为 blocked，新 run 才反映新数据。

## 6. 前端通关页面验证（frontend/T1Verify.html）

1. 台账列表：历史 run 展示编号/场景/状态（已验算/待验算），点击选中；
2. 「新建 run 并验算」：自动 POST 建 run + execute，右侧立即展示 5 张任务卡；
3. **5 张任务卡同时展示**：T1~T5 各卡含任务编号、名称、描述、结论徽章（成立绿/受阻红/待验证琥珀/未知灰/未验算）、满足·不满足·未知计数 chips；
4. **点击卡片展开**：显示该任务分布条 + 全部依赖检查条目（依赖键/检查对象/结论/evidence JSON/理由）+ summary；
5. 历史 run 只读：页面无编辑入口；旧 run（板块3 前）的 T2~T5 卡显示"未验算"；
6. 打开页面自动选中最新一份验算单。

## 7. 回归确认

- 板块1~3 全部能力不变：run 生命周期 3 接口、`GET task-result?task_id=T1` 返回结构、T1 判断结果、404/409、触发器快照冻结、阶段02 接口与 MQTT 落库；
- 表结构/常量/触发器零改动（可用 `PRAGMA table_info` 与 `SELECT * FROM task_definitions` 抽查）。

## 8. 测试数据清理（进入下一板块前）

```bash
python -c "import db; conn = db.get_conn(); conn.execute('DELETE FROM dependency_checks'); conn.execute('DELETE FROM task_results'); conn.execute('DELETE FROM runs'); conn.commit(); db.close_conn(conn)"
```
