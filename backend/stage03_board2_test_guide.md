# 阶段03 板块2 交付说明（含完整测试方法）

## 交付文件清单

| 文件 | 说明 |
|---|---|
| `run_store_v2.py` | 板块2 数据层：`create_run`（自动生成 run_id + 抓取并冻结快照）、`list_runs`、`get_run`；不做任何 T1~T5 验算 |
| `main.py` | 新增 3 个接口：`POST /api/runs`、`GET /api/runs`、`GET /api/runs/{run_id}`；无 `/execute` 接口 |
| 本文档 | 板块2 交付说明 + 完整测试方法 |

## 关键设计决策

1. **run_id 自动生成**：复用板块1 `db.next_run_id()`（BB03-RUN-001/002/... 自增）；并发创建撞主键时自动重试 3 次，不会漏号报错。
2. **快照口径**（input_snapshot_json，create 时同事务抓取并冻结）：
   - `snapshot_at`：抓取时刻；
   - `services` / `materials`：需求表 ∪ 接收表 distinct 的服务点/物资清单；
   - `demands`：每个（服务点, 物资）的**最新一条**需求观测（9 条基线数据对应 9 条）；
   - `receipts`：**全部**接收（签收）记录（追加型数据全量冻结）。
3. **assumptions_json 初始为空 JSON 对象 `{}`**（不是 null），供后续板块填充假设。
4. **completed 禁改双重兜底**：板块2 不提供任何更新/覆盖接口（只有 POST 新建 + GET 查询），数据库层再有板块1 触发器强制拦截，两层都不会放行修改。
5. **run_type 强制 reality**：入参默认 `"reality"`，传其他值返回 400；所有新建 run 均为 reality 类型用于 S1 基线。
6. **快照忠实抓取**：快照如实反映建 run 时刻 DB 中全部数据（含历史测试遗留数据），不做过滤——后续 Run/Verify 以快照为准，可复现。

---

# 测试方法

> 后端启动方式不变：`cd E:\supplyguard\backend && uvicorn main:app --reload --port 8000`
> 以下命令在 Git Bash 中执行；若用 Windows PowerShell 5.1（`curl` 是别名），把 `curl` 换成 `curl.exe`。
> 首次启动后端时板块1 的迁移/种子已自动完成，无需额外步骤。

## 1. 创建 S1 reality run（POST /api/runs）

```bash
curl -s -X POST http://127.0.0.1:8000/api/runs \
  -H "Content-Type: application/json" -d '{"scenario_id":"S1"}'
```

预期（HTTP 201，逐项核对）：
- `run_id` 为 `BB03-RUN-001`（再建一次依次 002、003 自增）；
- `scenario_id`=`"S1"`，`run_type`=`"reality"`，`parent_run_id`=`null`，`run_status`=`"pending"`，`created_at` 已写入、`completed_at`=`null`；
- `input_snapshot_json` 为对象，含 5 个键：`snapshot_at`、`services`、`materials`、`demands`（每个服务点×物资最新一条，S1 基线为 9 条）、`receipts`（全部接收记录）；
- `assumptions_json` = `{}`（空对象，不是 null）。

## 2. run_id 自增验证

再执行一次上一步 POST，预期 `run_id`=`"BB03-RUN-002"`。

## 3. run 列表（GET /api/runs）

```bash
curl -s http://127.0.0.1:8000/api/runs
```

预期：数组按 `created_at` 倒序（新的在前）；每项只含 7 个字段 `run_id`、`scenario_id`、`run_type`、`parent_run_id`、`run_status`、`created_at`、`completed_at`，**不含** `input_snapshot_json` / `assumptions_json`。

## 4. 单 run 详情（GET /api/runs/{run_id}）

```bash
curl -s http://127.0.0.1:8000/api/runs/BB03-RUN-001
```

预期：完整详情，`input_snapshot_json` 与 `assumptions_json` 均为解析后的 JSON 对象（不是字符串），内容与创建时返回一致。

## 5. 404 用例

```bash
curl -s -w " [%{http_code}]" http://127.0.0.1:8000/api/runs/BB03-RUN-999
```

预期：`{"detail":"run BB03-RUN-999 not found"} [404]`。

## 6. 入参校验用例

```bash
# run_type 非 reality → 400
curl -s -w " [%{http_code}]" -X POST http://127.0.0.1:8000/api/runs \
  -H "Content-Type: application/json" -d '{"scenario_id":"S1","run_type":"whatif"}'
# scenario_id 空白 → 400
curl -s -w " [%{http_code}]" -X POST http://127.0.0.1:8000/api/runs \
  -H "Content-Type: application/json" -d '{"scenario_id":"  "}'
# 缺 scenario_id → 422 (FastAPI 模型校验)
curl -s -w " [%{http_code}]" -X POST http://127.0.0.1:8000/api/runs \
  -H "Content-Type: application/json" -d '{}'
```

预期：分别为 `[400]`、`[400]`、`[422]`。

## 7. parent_run_id 用例

```bash
curl -s -X POST http://127.0.0.1:8000/api/runs \
  -H "Content-Type: application/json" -d '{"scenario_id":"S1","parent_run_id":"BB03-RUN-001"}'
```

预期：新 run 的 `parent_run_id`=`"BB03-RUN-001"`，其余字段同普通创建。

## 8. 快照冻结语义验证（核心）

1. 建 run A（记录其 `input_snapshot_json` 内容）；
2. 修改库中数据（例如执行 `seed_s1_baseline.sql` 或让模拟器推送新观测，或手动插一条需求）；
3. 再建 run B：B 的快照应包含新数据，**A 的快照保持不变**（冻结，不随库变化）。

## 9. completed 禁改验证（约束项）

```bash
cd E:\supplyguard\backend
python -c 'import db, sqlite3
conn = db.get_conn()
conn.execute("UPDATE runs SET run_status=\"completed\", completed_at=\"2026-11-14 11:00:00\" WHERE run_id=\"BB03-RUN-001\"")
conn.commit()
try:
    conn.execute("UPDATE runs SET input_snapshot_json=\"{}\" WHERE run_id=\"BB03-RUN-001\"")
    print("FAIL: snapshot overwritten")
except sqlite3.IntegrityError as e:
    print("PASS: completed snapshot update blocked ->", str(e)[:60])
db.close_conn(conn)'
```

预期：`PASS`（触发器拦截）；completed 的 run 仍可被 `GET /api/runs/{run_id}` 正常读取（只禁改不禁读）。

## 10. 回归确认（板块2 不破坏现有功能）

- `GET /state`、`/api/history/*`、`/api/evidences`、`/ws/guarantee` 行为不变；
- 模拟器 MQTT 消息继续正常落库。

## 11. 测试数据清理（进入板块3 前）

```bash
cd E:\supplyguard\backend
python -c "import db; conn = db.get_conn(); conn.execute(\"DELETE FROM runs\"); conn.commit(); db.close_conn(conn)"
```

清空新建 runs 表后，正式 run 序列重新从 `BB03-RUN-001` 开始；历史旧数据在 `runs_legacy` 不受影响。

> 提示：真实库 `receipt_records_v2` 中存在历史测试遗留记录（如 `TEST‑RC‑101`、`RCT-REG-001`），快照会如实全量抓取（services 列表可能因此出现 `R2`）。若希望 S1 基线快照干净，可先清理：
> `DELETE FROM receipt_records_v2 WHERE receipt_id IN ('TEST‑RC‑101','RCT-REG-001');`
