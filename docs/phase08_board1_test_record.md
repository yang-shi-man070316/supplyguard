# 阶段08 板块一 测试记录

> 场景冻结 + 前端四视图 + Three.js 拓扑视图组装 · 测试日期 2026-11-15
> 约束遵守：不新增 T1~T5 判定、不修改后端核心判定逻辑（本次后端仅新增
> 只读聚合接口 `GET /api/runs/{run_id}/trace` 与 `/console` 静态挂载，
> 既有接口与判定模块零改动）。

## 交付产物清单

| 产物 | 位置 | 说明 |
|---|---|---|
| 场景冻结文件 S1~S5 | `scenarios/s1.json` ~ `s5.json` | 固化 demand/stock/transport/route/receipt 基线快照 + 冻结预期判定 |
| 场景加载脚本 | `scenarios/load_scenario.py` | 加载/重放/input_overrides(reality)/assumptions(what-if)/历史一致性自检 |
| run trace 聚合模块 | `backend/run_trace.py` | 只读聚合「任务→T状态→缺口→证据→方案」链路 |
| trace 接口 + 静态挂载 | `backend/main.py`（仅新增） | `GET /api/runs/{run_id}/trace`、`/console` → frontend |
| 整合版前端 | `frontend/console.html` | 四视图 + Three.js 拓扑视图，hash 路由切换 |
| Three.js 本地化 | `frontend/vendor/three.min.js`、`OrbitControls.js` | 离线可用（r128） |
| 板块一自测 | `backend/board1_scenario_test.py`（55 用例）、`backend/board1_frontend_test.py`（20 用例） | 临时库隔离 + Playwright UI 实测 |
| 截图 | `docs/phase08-board1-shots/` | 七个视图截图 |

## 验收标准对照

### 验收 1：加载 S1~S5 任意场景，基线快照和历史运行记录一致 ✅

- `python load_scenario.py --check-all`（真实库只读比对）：
  - S1 基线 vs **BB03-RUN-010**：stocks/demands/transports/receipts 五段逐条一致
  - S2 基线 vs **BB03-RUN-010**：一致（S2 与 S1 同基线，聚焦缺口）
  - S3 基线 vs **BB03-RUN-011**：一致（含 routes/remaining_time 文档条件）
  - S4 基线 vs **BB03-RUN-011** + 三组标准假设 vs **BB03-RUN-012/014/015**（apply_assumptions 叠加后逐条一致）
  - S5 降级基线 vs **BB03-RUN-016**、回填变体 vs **BB03-RUN-017**：一致
- `board1_scenario_test.py` TC-B1-01/02：5 个场景文件全部可加载，历史一致性全绿。
- 重放一致性（TC-B1-04b/06b）：临时库重放 S1/S3 后，run 快照与冻结基线
  逐条一致（`compare_baseline_to_snapshot` 零差异）。

### 验收 2：四个视图切换正常，run_trace 下钻链路完整 ✅

- `board1_frontend_test.py`（Playwright 无头 Chromium，20/20 通过）：
  - TC-UI-01：页面加载，17 条历史 run 列表 + T1~T5 状态芯片全部渲染
  - TC-UI-02：①总览→②缺口→③证据→④方案→⑤拓扑五个视图切换正常
  - TC-UI-03：选中 run 行 → 追溯链路面板（run → T1~T5 → 缺口 → 证据 → 方案
    逐段可点击下钻），链路跳转携带 run/focus 参数（`#/gaps?run=..&focus=task=T1`）
  - TC-UI-04：缺口矩阵（需求/接收/缺口）+ blockers + 依赖明细（T1-D1…T5-D1）
  - TC-UI-05：签收记录、run 关联证据、全局证据列表类型筛选、证据溯源链路
- 截图：`01-overview.png`、`02-overview-trace.png`、`03-gaps.png`、
  `04-evidence.png`、`05-plans.png`。

### 验收 3：Three.js 拓扑渲染正常，和当前 run 状态联动 ✅

- TC-UI-02d：WebGL2 画布渲染成功（本地 vendor/three.min.js，无 CDN 依赖）
- TC-UI-07a/08a：选中 run 变更 → 拓扑节点颜色与状态提示同步刷新
  （BB03-RUN-011 ↔ BB03-RUN-016 切换验证）
- TC-UI-07b：点击 P1 节点弹出简要信息（T1-D1 状态 + 缺口）
- 像素级验证（WebGL readPixels）：S3 run 下 R1/R2 节点呈绿色
  （库存可用 → established），P1 节点呈红色（T1-D1 unsatisfied → fault），
  背景与连线状态色符合图例映射
  （established 绿 / unknown 灰 / pending_verification 琥珀 / fault 红）。
- 截图：`06-topology-s3.png`、`07-topology-s5.png`。

### 验收 4：S4 what-if、S5 降级回填能力不受影响，回归测试全部通过 ✅

- `board1_scenario_test.py`（55/55 通过，临时库隔离）：
  - TC-B1-07：S4 三组标准假设推演判定与冻结预期一致（推演A PLAN-B 可执行 /
    推演B PLAN-C 可执行 / 推演C 全部不可执行），父基线六表 sha256 指纹无污染
  - TC-B1-08/09：S5 降级（T1 unknown / T4 pending_verification，无 blocker，
    无 NOT_RECEIVED）+ 回填复测（T1 恢复 established，旧 run 判定不动）
  - TC-B1-11：既有回归套件全量通过——`whatif_run_test.py` **46/46**、
    `s5_degrade_test.py` **33/33**
- 后端判定模块（t1~t5_check / gap_check / plan_store / plan_judge /
  whatif_run / run_store_v2）本次**零修改**；`main.py` 仅新增 trace 路由
  与静态挂载，已有路由行为不变。

## 使用说明

```bash
# 场景加载（真实库, 幂等种基线, source_type='scenario_<id>'）
python scenarios/load_scenario.py S1
# reality 现场变量调整（不修改场景文件）
python scenarios/load_scenario.py S3 --overrides '{"stocks":[{"reserve_id":"R1","material_id":"W","quantity":3}]}'
# 完整重放（种基线→建run→T1~T5→缺口→方案→判定）
python scenarios/load_scenario.py S3 --full
# what-if 标准推演（S4: whatif-A/B/C）
python scenarios/load_scenario.py S4 --whatif A --parent BB03-RUN-011
# 历史一致性自检
python scenarios/load_scenario.py --check-all
# 前端（需重启后端后）: http://127.0.0.1:8000/console/
```

## 测试环境与已知说明

- 后端测试实例：uvicorn 8010 端口（临时，测试后停止）；**生产 8000 端口需
  重启后端进程**才可访问新增的 `/api/runs/{run_id}/trace` 与 `/console`。
- 前端 UI 测试针对真实 bb03.db 历史数据（17 条 run）执行，全程只读。
- `console.html` 拓扑节点颜色为纯展示聚合（服务点 = T1-D1 依赖状态、
  储备点 = 库存可用性、连线 = T2 运输链路状态），不产生任何新的
  T1~T5 判定行。
- 旧页面 `index.html` / `Dashboard.html` / `Verify.html` 原样保留不动。
