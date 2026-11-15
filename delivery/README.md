# SupplyGuard 应急物资保障系统

> 校园极端天气演练的「保供值班员」：不是告诉你仓库里有多少箱水，是告诉你**每个关键服务点的保障任务到底成不成立**。
>
> 核心语义：**签收取证失败 ≠ 未送达** —— 投送单派出去、现场却没能留下签收凭证，系统判「待验证」，不武断算缺口。

## 1. 项目介绍

| 能力 | 说明 |
|---|---|
| T1~T5 五级保障判定 | 需求保障 / 运输可达 / 响应时效 / 签收闭环 / 证据一致性，逐任务独立落库可追溯 |
| 缺口识别与候选方案 | 双证据齐全且 gap>0 才生成缺口（blocker）；固定三套调拨方案（PLAN-A/B/C）可行性判定 |
| 接收复核闭环 | 小程序待复核单 → 签收提交 → 幂等回执 → 自动生成新 run 复测 |
| reality / what-if 双轨 | 假设推演独立落库，父基线六表 sha256 指纹防污染校验 |
| 证据体系 | evidence_v2 统一证据层：签收/调配直连证据 + 天气/RAG/ML/工具轨迹/LLM 解释辅助证据，异常一律降级不阻断判定 |
| 场景冻结与重放 | `scenarios/` S1~S5 冻结基线，现场变量调整统一走 `input_overrides` / `assumptions`，绝不改场景文件 |
| 前端工作台 | 四视图（任务总览/缺口依赖/证据运行/方案对比）+ Three.js 拓扑关系视图 + run trace 一路下钻 |

**教学场景规模（固定）：** 储备点 R1/R2 · 服务点 P1/P2/P3 · 物资 W(水)/L(照明)/R(急救包) · 车辆 V1/V2。

## 2. 环境依赖

- Python 3.8+（开发验证版本 3.8.10）
- 依赖安装：`pip install -r requirements.txt`
  （fastapi / uvicorn / paho-mqtt / pandas；国内可加 `-i https://mirrors.aliyun.com/pypi/simple`）
- MQTT broker（**可选**，仅实时模拟器需要）：如 mosquitto，默认 `127.0.0.1:1883`
- 前端无构建依赖：`frontend/console.html` 纯静态页面，Three.js 已本地化（`frontend/vendor/`），离线可用

## 3. 启动步骤（clone → install → env 配置 → start）

```bash
# ① clone
git clone <repo-url> supplyguard
cd supplyguard

# ② install
pip install -r requirements.txt

# ③ env 配置（可选）
# 本项目的环境变量为可选配置：不配置也能完整运行（使用内置默认值）。
# 需要接入远程 LLM 或自定义 MQTT 时，参考模板：
#   cp delivery/.env.example .env
#   # 编辑 .env 后按需 export（详见第 8 节：环境变量说明）
# 注意：.env.example 只是模板，不含任何真实密钥，也请不要把真实密钥提交进仓库。

# ④ start 后端
cd backend
uvicorn main:app --port 8000
# 浏览器打开 http://127.0.0.1:8000/console/  （四视图 + 拓扑工作台）

# ⑤ （可选）start 实时模拟器：另开终端，先启动 mosquitto 再运行
python simulator/simulator.py --interval 10
```

> 提示：后端无 `--reload` 时，修改代码后需手动重启 uvicorn（Windows PowerShell 下 Ctrl+C 后重新运行）。

## 4. 加载场景演示流程（S1~S5）

场景文件冻结于 `scenarios/s1.json` ~ `s5.json`，**禁止直接修改**；现场变量调整统一走 `input_overrides`（reality）/ `assumptions`（what-if）。

```bash
# 种基线（幂等，source_type='scenario_<id>'，不影响 MQTT 实时数据）
python scenarios/load_scenario.py S1

# 完整重放：种基线 → 建 run → T1~T5 → 缺口 → 候选方案 → 方案判定
python scenarios/load_scenario.py S3 --full

# 只解析不写库（预览基线/overrides 结果）
python scenarios/load_scenario.py S5 --dry-run
```

### S1~S5 演示指引

| 场景 | 演示内容 | 命令 | 预期（console 查看） |
|---|---|---|---|
| **S1** | 标准基线 | `python scenarios/load_scenario.py S1 --full` | T1 blocked（8/9 需求缺签收），T2~T5 established |
| **S2** | 缺口识别 | `python scenarios/load_scenario.py S2 --full` | blockers：P1/W gap=2、P2/L gap=3；无签收的组合不武断算缺口 |
| **S3** | 候选方案判定 | `python scenarios/load_scenario.py S3 --full` | T1 blocked（签收3<需求7）；PLAN-A 可执行，PLAN-B（V2 FAULT）/PLAN-C（路线超时）不可执行 |
| **S4** | what-if 推演 | `python scenarios/load_scenario.py S4 --whatif A --parent <S3基线run>` | 推演A：V2修复 → PLAN-B 转可执行；推演B：时限放宽 → PLAN-C 可执行；推演C：库存削减 → 全部不可执行；父基线指纹无污染 |
| **S5** | 取证失败降级 | `python scenarios/load_scenario.py S5 --full` | T1 unknown、T4 pending_verification、T5 established(degraded)；无 blocker；无 NOT_RECEIVED |
| **S5 回填** | 证据回填复测 | `python scenarios/load_scenario.py S5 --variant refill --full` | 补合法签收 8 件后 T1 恢复 established；旧 run 判定不动 |

演示时在 console 顶栏选择对应 run：总览页 KPI 与 T1→T5 通关流程条、缺口矩阵、证据链、方案对比、拓扑视图（储备点/服务点/投送连线状态色）全部联动。

## 5. 现场 Modify → Run → Verify 操作说明

「现场变量调整」不改场景文件，而是以入参覆盖冻结基线：

```bash
# Modify（reality 现场调整）：给 R1 补 W 库存到 5，追加/覆盖签收等
python scenarios/load_scenario.py S3 \
  --overrides '{"stocks":[{"reserve_id":"R1","material_id":"W","quantity":5}],
                "remaining_time":45}' --full

# Modify（what-if 假设）：V2 修复推演
python scenarios/load_scenario.py S4 --whatif '{"transport.V2.availability":"AVAILABLE"}' \
  --parent BB03-RUN-003
```

- **overrides 语法**（reality）：`stocks` / `demands` / `transports` / `receipts` / `routes` 列表按自然键 upsert（stocks: reserve_id+material_id；demands: service_id+material_id；transports: transport_id；receipts: receipt_id；routes: route_id），`remaining_time` 标量替换；未出现的键追加。
- **assumptions 语法**（what-if）：`transport.<id>.availability|capacity`、`stock.<reserve>.<material>`、`remaining_minutes`。
- **Verify**：浏览器打开 `http://127.0.0.1:8000/console/`，选中新 run 查看 T1~T5 通关状态、缺口、证据与方案判定；或用 `GET /api/runs/{run_id}/trace` 一次性拉取全链路。

## 6. 回归测试命令

```bash
cd backend

# 板块二回归套件：正向 5 案例（固定 run_id）+ 失败回归 4 类
python regression_suite.py                    # 全部
python regression_suite.py --positive-only    # 仅正向
python regression_suite.py --failures-only    # 仅失败回归
python regression_suite.py --report-file reg_report.json   # 输出 JSON 报告

# 板块一回归：场景冻结 + run_trace（55 用例，含阶段07 回归）
python board1_scenario_test.py
# 板块一前端回归：四视图 + 拓扑 + 下钻（Playwright，需先启动后端）
python board1_frontend_test.py http://127.0.0.1:8000

# 阶段07 历史用例（独立回归）
python whatif_run_test.py        # S4 what-if 推演 + 基线无污染
python s5_degrade_test.py        # S5 取证失败降级 + 证据回填复测
```

回归案例清单与预期输出见 [REGRESSION_CASES.md](REGRESSION_CASES.md)；板块二回归报告见 [PHASE08_BOARD2_REPORT.md](PHASE08_BOARD2_REPORT.md)。

## 7. 项目结构

```
supplyguard/
├── backend/                 # FastAPI 后端 + 判定/存储层
│   ├── main.py              # REST/WS 接口（含 /api/runs/{id}/trace、/console 静态挂载）
│   ├── t1_check.py ~ t5_check.py   # T1~T5 纯判定（只读快照）
│   ├── gap_check.py / plan_store.py / plan_judge.py   # 缺口与候选方案
│   ├── whatif_run.py        # what-if 推演编排 + 六表指纹无污染校验
│   ├── run_trace.py         # run 追溯链路聚合（只读）
│   ├── regression_suite.py  # 板块二回归套件
│   ├── board1_*_test.py     # 板块一自测
│   └── bb03.db              # SQLite（runs/判定/证据全部落库于此）
├── scenarios/               # S1~S5 冻结场景 + load_scenario.py 加载器
├── frontend/                # console.html 四视图+拓扑工作台（vendor/ 本地 Three.js）
├── simulator/               # MQTT 实时模拟器（储备/需求/运输三类消息）
├── miniprogram/             # 微信小程序（待复核列表 + 签收提交）
├── analysis/                # 辅助证据：天气 / RAG / ML / LLM 解释 / 报告
├── rag_docs/                # 保障知识库文档（RAG 检索源）
├── delivery/                # 工程交付文件（本文档 / .env.example / 回归清单与报告）
├── docs/                    # 测试记录、截图与 AI 对话存档
└── requirements.txt
```

## 8. 环境变量说明

`.env.example` 为模板（不含真实密钥）。当前代码读取情况：

| 变量 | 是否被代码读取 | 默认值 / 说明 |
|---|---|---|
| `SUPPLYGUARD_LLM_URL` / `SUPPLYGUARD_LLM_API_KEY` / `SUPPLYGUARD_LLM_MODEL` | ✅ 读取（analysis/llm_explainer.py） | 远程 LLM 为可选：未配置走本地模板解释；配置错误自动降级 unavailable |
| `SUPPLYGUARD_LLM_DISABLED` | ✅ 读取 | `1` 强制禁用 LLM（故障注入/回归测试用），降级 status=unavailable |
| `SUPPLYGUARD_MQTT_BROKER` / `SUPPLYGUARD_MQTT_PORT` | ❌ 模板（当前代码常量） | 当前 mqtt_client.py 使用默认 `127.0.0.1:1883` |
| `SUPPLYGUARD_API_HOST` / `SUPPLYGUARD_API_PORT` | ❌ 模板 | uvicorn 启动参数控制（如 `--port 8000`） |
| `SUPPLYGUARD_DB_PATH` | ❌ 模板（当前代码常量） | 当前默认 `backend/bb03.db`；回归测试用代码级重定向 |

使用示例（PowerShell）：

```powershell
$env:SUPPLYGUARD_LLM_URL="https://your-llm-gateway/v1/chat/completions"
$env:SUPPLYGUARD_LLM_API_KEY="sk-...（真实密钥勿提交仓库）"
$env:SUPPLYGUARD_LLM_MODEL="gpt-4o-mini"
cd backend; uvicorn main:app --port 8000
```

## 9. 已知边界（如实记录）

- 缺口/候选方案/方案判定三个环节目前以 CLI（load_scenario.py / gap_check.py / plan_store.py / plan_judge.py）驱动；`POST /api/runs/{id}/execute` 只执行 T1~T5。
- MQTT 模拟器未生成 dispatch/receipt 消息（签收回执走小程序/API 复核链路）。
- 无 JSON1 扩展的 SQLite 环境已兼容（证据检索采用 Python 侧解析）。
- 项目历史偏差记录见仓库根 `LIMITATIONS.md`。
