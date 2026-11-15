# 标准失败演示脚本（固定可复现流程）

> 流程：S5 基线（取证失败）→ T1 unknown、T4 pending_verification → 发起复核流程 → 回填合法签收凭证 → 生成新 run，T1 恢复 established
> 环境：**干净环境（新 clone，无 bb03.db）** 下 run_id 固定为 BB03-RUN-001~002；
> 在已有历史库中执行时 run_id 顺序顺延，以命令输出为准。
> 核心语义：**签收取证失败 ≠ 未送达**——证据 unavailable 判「未知/待验证」，绝不输出「未收到」。

## 前置条件

```bash
cd backend && uvicorn main:app --port 8000          # 终端① 启动后端
# 浏览器打开 http://127.0.0.1:8000/console/          # 观察视图联动
# 终端② 执行下列各步命令（cwd = 项目根）
```

---

## 步骤 1：加载 S5 基线场景，取证失败降级

```bash
python scenarios/load_scenario.py S5 --reset-observations --full
```

| 项 | 内容 |
|---|---|
| run_id | `BB03-RUN-001`（S5 · reality · completed） |
| 预期现象 | T1 **unknown**（P3/L 最新证据 CF-S5-001 取证失败，无有效签收凭证，送达与否无法判断）；T4 **pending_verification**（部分签收闭环正常 + CF-S5-001 无法核验）；T2/T3 established；T5 established（**degraded** 降级置信标注）；**无 blocker** |
| console 视图变化 | 流程条：T1 灰「未知」、T4 琥珀「待验证」、T2/T3/T5 绿；证据视图：CF-S5-001 状态「取证失败/不可用」、接收量空、备注含失败原因；缺口视图：无 blocker；拓扑：P3 节点琥珀（R 满足 + L 未知混合） |
| 判定解释 | 取证失败确认不是 `received:false`：判 unknown 而非 unsatisfied；gap_check 双证据硬性规则不满足 → 不生成缺口；全链路无 NOT_RECEIVED 输出 |

## 步骤 2：发起复核流程（待复核单）

```bash
curl -X POST http://127.0.0.1:8000/api/reviews \
  -H "Content-Type: application/json" \
  -d '{"dispatch_id":"DSP-S5-001","service_id":"P3","material_id":"L","trigger_run_id":"BB03-RUN-001"}'
```

| 项 | 内容 |
|---|---|
| 预期输出 | `{"review_id":"BB03-REV-001","created":true}` |
| 证据变化 | 待复核单落库 review_requests（pending）；小程序「待复核列表」同步出现该单（P3/L，投送单 DSP-S5-001） |
| 说明 | 同三元组已存在 pending 单时幂等返回原 review_id（不重复建单） |

## 步骤 3：现场补取证，回填合法签收凭证

```bash
curl -X POST http://127.0.0.1:8000/api/reviews/BB03-REV-001/receipt \
  -H "Content-Type: application/json" \
  -d '{"receipt_id":"RCT-S5-DEMO-001","dispatch_id":"DSP-S5-001","service_id":"P3","material_id":"L","received_quantity":8,"observed_at":"2026-11-15T12:30:00","source_type":"DEMO","note":"现场补拍签收凭证"}'
```

| 项 | 内容 |
|---|---|
| 预期输出 | `receipt_id` + `review_id` + `review_status:"completed"` + **`new_run_id:"BB03-RUN-002"`** + `execute`（T1~T5 结果） |
| 后端动作 | 同一事务：复核单置 completed → 签收写入 receipt_records_v2 → 备注写入 evidence_v2 → **自动生成新 reality run**（快照包含刚写入的签收）→ 自动执行完整 T1~T5 |
| run_id | `BB03-RUN-002`（S5 · reality · completed，parent 指向 RUN-001） |
| 预期现象 | T1 **established**（回填签收 8 ≥ 需求 8）；T2~T5 全 established |
| console 视图变化 | 约 30 秒内自动刷新（或点「刷新」）：run 列表新增 RUN-002 全绿；选中 RUN-002 流程条五节点全绿；证据视图出现 RCT-S5-DEMO-001（status ok）与 note 证据；对比视图 RUN-001（T1 未知）vs RUN-002（T1 成立）并排 |

## 步骤 4：验证历史 run 不被修改（只增不改）

| 检查点 | 预期 |
|---|---|
| RUN-001 判定 | 仍为 T1 unknown / T4 pending_verification（completed 快照冻结，触发器拒改） |
| CF-S5-001 证据 | 仍为 unavailable（取证失败事实不覆盖） |
| 新证据 | RCT-S5-DEMO-001（ok）为新增行，回填不抹除历史 |

在 console 选中 RUN-001：流程条仍显示 T1 灰「未知」；选中 RUN-002：全绿——**降级 → 复核 → 回填 → 恢复**的完整证据链可一路下钻（追溯链路面板：T1 → T1-D1 证据 → 签收记录）。

## 等价 CLI 路径（无复核 API 时的现场回填）

```bash
# 用 acquisition_status overrides 直接回填取证状态（不改场景文件）
python scenarios/load_scenario.py S5 --overrides '{"acquisition_status":[
  {"service_id":"P3","material_id":"L","status":"ok","received_quantity":8}]}' --full
# 或使用 S5 冻结的 refill 变体
python scenarios/load_scenario.py S5 --variant refill --full
```

## 完整流程速览（干净环境）

| 步骤 | 动作 | run_id | T1 | T4 |
|---|---|---|---|---|
| 1 | `S5 --reset-observations --full` | RUN-001 | unknown | pending_verification |
| 2 | POST /api/reviews（复核单） | —（BB03-REV-001） | — | — |
| 3 | POST .../receipt（回填签收 8 件） | RUN-002 | **established** | established |
| 4 | 验证历史 RUN-001 判定不变 | — | unknown（不变） | pending_verification（不变） |
