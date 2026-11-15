# 回归用例清单（08 板块二）

> 脚本：`backend/regression_suite.py`　执行方式：`python regression_suite.py`
> 隔离策略：正向案例在临时库按固定顺序执行（run_id 由自增序列确定，**绑定固定 run_id**）；失败回归以异常注入为主，真实库零写入。

## 一、正向回归案例（固定 run_id + 预期输出）

| 案例         | 场景                                      | 固定 run_id                       | 预期输出（逐项比对）                                                                                   |
| ---------- | --------------------------------------- | ------------------------------- | -------------------------------------------------------------------------------------------- |
| **REG-01** | S1 全成立（现场补签收 Modify→Run→Verify）         | `BB03-RUN-001`                  | T1~T5 全 `established`；blockers 为空；无候选方案（无 W 缺口）                                              |
| **REG-02** | S2 T1 blocked + 缺口识别                    | `BB03-RUN-002`                  | T1 `blocked`（8/9 依赖不满足）；T2~T5 `established`；blockers 恰为 P1/W(gap=2)、P2/L(gap=3)              |
| **REG-03** | S3 PLAN-A 可执行但 T1 blocked               | `BB03-RUN-003`                  | T1 `blocked`；PLAN-A `executable`、PLAN-B/C `non_executable`；blocker P1/W gap=4                |
| **REG-04** | S4 what-if 隔离验证                         | `BB03-RUN-004/005/006`（推演A/B/C） | 推演A：A/B 可执行、C 不可执行；推演B：A/C 可执行、B 不可执行；推演C：全不可执行；父基线（RUN-003）六表 sha256 指纹推演前后一致               |
| **REG-05** | S5 T1 unknown + T4 pending_verification | `BB03-RUN-007`                  | T1 `unknown`、T4 `pending_verification`、T2/T3/T5 `established`；无 blocker；全链路无 NOT_RECEIVED 输出 |

### 案例执行方式说明

- REG-01：`load_scenario.py S1 --overrides '{"receipts":[...8条现场补签收...]}' --full`——补签收后每个（服务点,物资）组合最新签收 ≥ 需求，T1 由 blocked 转 established；校验「现场 Modify → Run → Verify」闭环。
- REG-02：`load_scenario.py S2 --full`（S2 与 S1 同基线，聚焦缺口识别硬性规则）。
- REG-03：`load_scenario.py S3 --full`（S3 参考文档条件：V2 FAULT、路线 A/B=20min C=40min、remaining_time=30）。
- REG-04：复用 REG-03 基线 `BB03-RUN-003` 作为父 run，`load_scenario.py S4 --whatif A/B/C` 三组标准假设推演 + `capture_fingerprint` 前后比对。
- REG-05：`load_scenario.py S5 --full`（取证失败降级基线）。

## 二、失败回归案例（异常注入 → 不崩溃 + 既定降级语义）

| 案例                                       | 异常注入方式                                                                                       | 预期降级行为                                                                                                                                               |
| ---------------------------------------- | -------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| **FR-01** 库存接口异常失败                       | `db.DB_PATH` 指向不可打开路径（目录不存在）                                                                 | `get_current_stock` / `get_current_demand` 返回 `(False, 500, "db error ...")`，**不抛异常不崩溃**；轨迹写入失败同样不外抛                                                 |
| **FR-02** receipt 取证失败（S5 类 unavailable） | 快照注入 `status='unavailable'`、`received_quantity=null` 的取证确认                                   | ① 存在更早有效回执 → T1 **沿用旧回执判定**（established）；② 无旧回执 → T1 unknown，绝不输出 NOT_RECEIVED；③ gap_check 不生成 blocker；④ T4 对 unavailable 确认判 unknown 而非 unsatisfied |
| **FR-03** weather / RAG / LLM 模块异常       | LLM：`SUPPLYGUARD_LLM_DISABLED=1` 故障注入；weather：monkeypatch `urlopen` 抛 URLError；RAG：与知识库零重叠查询 | LLM → `status=unavailable`（HTTP 200 降级）；weather → `status=unavailable` 且异常证据落库 evidence_v2；RAG → 固定文本「无可靠依据」（matched=false、组件正常 evidence=ok）         |
| **FR-04** 后端重启数据不丢                       | 两阶段子进程模拟重启：create 阶段建 run + 完整判定落库；verify 阶段**新进程**重新 `init_db` 后读回                          | run 记录（含快照 JSON、completed_at）、5 条 task_results、blocker、3 套方案与 3 条方案判定**完整保留**                                                                        |

## 三、回归套件输出

```
python regression_suite.py                    # 正向 5 案例 + 失败回归 4 类
python regression_suite.py --positive-only    # 仅正向
python regression_suite.py --failures-only    # 仅失败
python regression_suite.py --report-file reg_report.json   # JSON 报告
```

退出码 0 = 全部通过。每条用例输出 `[PASS]/[FAIL]` + 差异详情；JSON 报告含逐条结果。

## 四、与历史回归的关系

| 测试                        | 覆盖                                         | 板块二验收      |
| ------------------------- | ------------------------------------------ | ---------- |
| `regression_suite.py`     | 正向 5 案例 + 失败 4 类（本清单）                      | ✅ 验收 1/2   |
| `whatif_run_test.py`      | 阶段07 S4 what-if 推演 + 六表指纹无污染（46 用例）        | ✅ 验收 4（回归） |
| `s5_degrade_test.py`      | 阶段07 S5 取证失败降级 + 回填复测（33 用例）               | ✅ 验收 4（回归） |
| `board1_scenario_test.py` | 阶段08 板块一场景冻结 + run_trace（55 用例，含上述两项子进程回归） | 板块一验收      |
| `board1_frontend_test.py` | 前端四视图 + 拓扑 + 下钻（Playwright 32 用例）          | 板块一验收      |
