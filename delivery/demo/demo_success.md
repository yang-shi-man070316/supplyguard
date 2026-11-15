# 标准成功演示脚本（固定可复现流程）

> 流程：S1 基线 → S2 缺口 → S3 方案甲可执行 → 模拟投送 → 新增签收证据 → 新 reality run → T1 判定成立
> 环境：**干净环境（新 clone，无 bb03.db）** 下 run_id 固定为 BB03-RUN-001~004；
> 在已有历史库中执行时 run_id 顺序顺延，以命令输出为准。

## 前置条件

```bash
cd backend && uvicorn main:app --port 8000          # 终端① 启动后端
# 浏览器打开 http://127.0.0.1:8000/console/          # 四视图 + 拓扑工作台
# 终端② 执行下列各步命令（cwd = 项目根）
```

每步执行后**观察 console 页面**（约 30 秒内自动刷新，或点「刷新」按钮）。

---

## 步骤 1：加载 S1 基线场景，生成 reality run

```bash
python scenarios/load_scenario.py S1 --reset-observations --full
```

| 项 | 内容 |
|---|---|
| run_id | `BB03-RUN-001`（S1 · reality · completed） |
| 预期现象 | T1 **blocked**（8/9 项依赖不满足：9 条需求中仅 P3/R 有满足需求的签收）；T2/T3/T4/T5 established |
| console 视图变化 | 总览：KPI「run 总数」变 1；选中 RUN-001，**T1→T5 通关流程条 T1 红「受阻」**、T2~T5 绿；缺口视图：blockers 出现 P1/W（需求4-签收2=缺口2）、P2/L（需求5-签收2=缺口3），P3/W、P3/L 无签收**不**生成缺口（双证据硬性规则）；拓扑：P1/P2 节点红、P3 节点红（W/L 无签收）、R1/R2 绿 |
| 判定解释 | T1 口径「最新有效签收 ≥ 当期最低需求」——S1 基线只有 3 条签收，其余 6 个组合无签收证据，判不满足 |

## 步骤 2：推进到 S2，缺口识别

```bash
python scenarios/load_scenario.py S2 --full
```

| 项 | 内容 |
|---|---|
| run_id | `BB03-RUN-002`（S2 · reality · completed） |
| 预期现象 | 与 S1 同基线：T1 blocked；缺口视图 2 条 blocker（P1/W gap=2、P2/L gap=3） |
| console 视图变化 | KPI run 总数变 2；run 列表新增 RUN-002 行（T1 红）；选中 RUN-002 缺口视图可见两条 blocker 及其原因文本 |
| 判定解释 | S2 聚焦 gap_check：**双证据齐全且 gap>0 才生成缺口**；P3/W、P3/L 只有需求没有签收，取证失败≠未送达，不武断算缺口 |

## 步骤 3：推进到 S3，生成方案甲可执行

```bash
python scenarios/load_scenario.py S3 --full
```

| 项 | 内容 |
|---|---|
| run_id | `BB03-RUN-003`（S3 · reality · completed） |
| 预期现象 | T1 **blocked**（P1/W 签收 3 < 需求 7）；blocker P1/W **gap=4**；三套候选方案判定：**PLAN-A 可执行**（R1 库存 6 ≥ 调拨 4、V1 可用、route-A 20min ≤ 30min）、PLAN-B 不可执行（V2 FAULT）、PLAN-C 不可执行（route-C 40min 超时） |
| console 视图变化 | 方案对比视图：PLAN-A 绿「可执行」+ 三条判定规则 ✓；对比列（RUN-003 + 同场景上一 reality run RUN-002）可并排看到 T1 均为受阻；拓扑：P1 红、连线绿（T2 运输可达） |
| 判定解释 | 方案判定只回答「方案可行吗」，**不解除 T1 阻塞**——只有产生真实投送与新的接收证据后 T1 才会解除 |

## 步骤 4：模拟投送动作 + 新增签收证据，生成新 reality run

```bash
python scenarios/load_scenario.py S3 --overrides '{"receipts":[
  {"receipt_id":"RCT-S3-DEMO-001","dispatch_id":"DSP-S3-DEMO-001",
   "service_id":"P1","material_id":"W","received_quantity":7,
   "observed_at":"2026-11-15T11:00:00","created_at":"2026-11-15 11:00:00"}]}' --full
```

| 项 | 内容 |
|---|---|
| 动作语义 | 按 PLAN-A 执行投送（R1 装车 4 件 + R2 并车调剂 3 件），P1 现场签收 **7 件**——现场变量调整统一走 `input_overrides`，S3 场景文件未改动 |
| run_id | `BB03-RUN-004`（S3 · reality · completed，独立新 run） |
| 预期现象 | T1 **established**（最新签收 7 ≥ 需求 7）；T2~T5 established；**无 blocker**（累计接收 3+7=10 ≥ 7） |
| console 视图变化 | 总览 KPI 变 4；选中 RUN-004：**流程条 T1~T5 全绿**；缺口视图 blocker 消失；方案对比视图 RUN-003（T1 受阻）vs RUN-004（T1 成立）并排对比，改动影响一目了然；证据视图可见 RCT-S3-DEMO-001 签收记录 |
| 判定解释 | T1 解除阻塞的证据链：投送 DSP-S3-DEMO-001 → 签收 RCT-S3-DEMO-001（7 件）→ 最新签收 ≥ 需求 → satisfied；gap_check 累计口径 10 ≥ 7 → 缺口清零 |

## 步骤 5：回溯与收尾（可选）

- 在 console 追溯链路面板从 RUN-004 一路下钻：任务 T1 → 依赖 T1-D1（evidence 含 RCT-S3-DEMO-001）→ 证据视图签收记录 → 方案视图 PLAN-A 判定。
- 现场复位还原：重新执行 `python scenarios/load_scenario.py S3 --reset-observations --full` 可回到步骤 3 的状态。

## 完整流程速览（干净环境）

| 步骤 | 命令要点 | run_id | T1 | 关键变化 |
|---|---|---|---|---|
| 1 | `S1 --reset-observations --full` | RUN-001 | blocked | 基线 8/9 不满足 |
| 2 | `S2 --full` | RUN-002 | blocked | 缺口 P1/W=2、P2/L=3 |
| 3 | `S3 --full` | RUN-003 | blocked | PLAN-A 可执行 |
| 4 | `S3 --overrides receipts[...7件] --full` | RUN-004 | **established** | 投送+签收闭环，T1 成立 |
