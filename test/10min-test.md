# 10 分钟快速复现实测记录

> 本次实测按 03_README.md 第 3 节「路线 A：完整模式」执行：**GitHub 真实克隆 → 全新 venv 装依赖 → 启动 → 场景判定 → 辅助证据 → 实时链路**，全程逐步计时。

## 实测环境

| 项 | 内容 |
|---|---|
| 测试人 | 本人（开发机，AI 辅助执行与计时） |
| 机器 | 12th Gen Intel Core i7-1260P / 32GB / Windows 11 Home 10.0.22000 |
| Python | 3.8.10（系统）；全新 venv 亦为 3.8.10 |
| 网络 | 家庭宽带；GitHub 与 PyPI 实测可达 |
| 测试日期 | 2026-11-15 23:17 ~ 23:23 |
| 前置已具备 | mosquitto 2.1.2 已在运行（路线 A 步骤③复用，本机服务） |

## 分步计时

| # | 步骤 | 命令 | 开始 | 结束 | 耗时 | 结果 |
|---|---|---|---|---|---|---|
| 1 | 克隆仓库 | `git clone https://github.com/yang-shi-man070316/supplyguard.git` | 23:17:42 | 23:20:39 | 2 分 57 秒 | 15MB（含 8 张截图）；`.env.example` 在根目录 ✓ |
| 2 | 建虚拟环境 | `python -m venv sgvenv` | 23:20:51 | 23:20:54 | 3.3 秒 | 创建成功 |
| 3 | 安装依赖 | `pip install -i https://mirrors.aliyun.com/pypi/simple -r requirements.txt` | 23:20:54 | 23:21:17 | 22.8 秒 | fastapi 0.124.4 / uvicorn 0.33.0 / paho-mqtt 2.1.0 / pandas 2.0.3 / numpy 1.24.4 |
| 4 | 启动后端 | `uvicorn main:app --host 0.0.0.0 --port 8124` | 23:21:54 | 23:22:02 | 8 秒（含探活等待） | `/health` 200；`/api/runs` 空表 |
| 5 | 场景判定 S3 | `python scenarios/load_scenario.py S3 --full` | 23:22:14 | 23:22:14 | 0.16 秒 | RUN-001：T1 blocked、T2~T5 established、PLAN-A 可执行 |
| 6 | run trace | `GET /api/runs/BB03-RUN-001/trace` | 23:22:15 | 23:22:15 | 不足 1 秒 | 汇总与步骤 5 一致 |
| 7 | 场景判定 S5 | `python scenarios/load_scenario.py S5 --full` | 23:22:20 | 23:22:20 | 0.13 秒 | RUN-002：T1 unknown、T4 pending_verification、无 blocker |
| 8 | 天气证据 | `GET /api/weather` | 23:22:23 | 23:22:23 | 约 1 秒 | wttr.in 实时返回（北京 8°C） |
| 9 | 实时链路 | `python simulator/simulator.py --rounds 3 --interval 2` | 23:22:25 | 23:22:29 | 6 秒 | 3 轮发布后 `/state` 有 6 库存 / 9 需求 / 2 运输 |
| 10 | 前端页面 | `GET /console/` | 23:22:02 | — | 不足 1 秒 | HTTP 200（页面渲染由 board1_frontend_test 34/34 与 Playwright E2E 16/16 覆盖） |

**总耗时：4 分 48 秒**（23:17:42 → 23:22:30），10 分钟内完成。

## 验收核对（03_README.md 第 7 节）

- [x] 服务启动，终端无报错 → `/health` 返回 `{"status":"ok"}`
- [x] 打开首页 → `/console/` HTTP 200（首屏渲染见 docs/screenshots/01-overview.png）
- [x] 核心功能 1（场景判定）→ S3：T1 受阻、PLAN-A 可执行
- [x] 核心功能 7（取证失败降级）→ S5：T1 未知、T4 待验证、无 blocker
- [x] 实时链路 → 模拟器消息进入 `/state` 快照（MQTT → 后端 → WebSocket 推送链路通）
- [x] 数据留下记录 → `/api/runs` 出现 RUN-001 / RUN-002，判定落库可追溯
- [ ] 未在本次复测的功能（此前已实测通过）：投送签收闭环、复核回填、what-if 推演、缺口/证据/方案/拓扑视图交互 —— 见 docs/screenshots/ 与 delivery/demo/ 两套演示脚本

## 备注（如实记录）

1. **端口**：本机 8000 被开发实例占用，克隆实例使用 8124。评委环境 8000 空闲，按 README 命令直接跑即可。
2. **依赖安装**：本次 22.8 秒是 pip 缓存命中的结果；**首次无缓存安装实测 2 分 18 秒**（同镜像，当次实测）。默认 PyPI 源当日一度超时（11 分 46 秒未完成），国内网络建议直接用阿里云镜像。
3. **克隆耗时**取决于评委侧到 GitHub 的网速，本机实测 2 分 57 秒（仓库 15MB，含截图）。
4. **mosquitto**：本机已有服务在跑（路线 A 步骤③未重装）。路线 B（无 broker）此前已实测：后端启动降级提示 + 场景判定完整可用。
5. 本次克隆内容为 GitHub 最新提交（f2208c8，2026-11-15），含全部 6 项 MQTT 修复、场景校验与前端回归预置脚本。
