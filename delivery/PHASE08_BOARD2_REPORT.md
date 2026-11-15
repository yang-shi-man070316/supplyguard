# 回归报告

> 回归案例库 + 失败回归 + 工程文档补齐 · 报告日期 2026-11-15
> 回归脚本：`backend/regression_suite.py` / `backend/e2e_boot_test.py`
> 隔离策略：正向案例与失败回归均在临时库执行，真实 `bb03.db` 零写入。

## 交付产物清单

| 产物               | 位置                                | 说明                                                                                                                |
| ------------------ | ----------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| 正向+失败回归套件  | `backend/regression_suite.py`       | 正向 5 案例（固定 run_id + 预期输出比对）+ 失败 4 类（异常注入与降级验证），支持 `--report-file` JSON 报告          |
| 清空环境端到端自测 | `backend/e2e_boot_test.py`          | 完整代码副本（无任何 .db）→ uvicorn 从零拉起 → 真实 CLI 加载场景 → REST 验证                                        |
| 工程 README        | `delivery/README.md`                | 项目介绍 / 依赖 / clone→install→env→start / 场景演示流程 / S1~S5 指引 / 回归命令 / Modify→Run→Verify / 环境变量说明 |
| 环境变量模板       | `delivery/.env.example`             | 模板变量（LLM 实际生效项 + MQTT/端口/DB 模板项），不含真实密钥                                                      |
| 回归用例清单       | `delivery/REGRESSION_CASES.md`      | 案例表 + 注入方式 + 预期降级行为 + 与历史回归关系                                                                   |
| 本报告             | `delivery/PHASE08_BOARD2_REPORT.md` | 验收对照与实测结果                                                                                                  |

## 验收标准对照（全部实测通过）

### ✅ 验收 1：regression_suite.py 正向 5 案例全部 Pass

```
[PASS] REG-01 S1 全成立 (现场补签收 Modify→Run→Verify) (run_id=BB03-RUN-001)
[PASS] REG-02 S2 T1 blocked + 缺口识别 (run_id=BB03-RUN-002)
[PASS] REG-03 S3 PLAN-A 可执行但 T1 blocked (run_id=BB03-RUN-003)
[PASS] REG-04 S4 what-if 隔离 (基线无污染) (run_id=BB03-RUN-004/005/006)
[PASS] REG-05 S5 T1 unknown + T4 pending_verification (run_id=BB03-RUN-007)
```

- 每个案例**绑定固定 run_id**（隔离临时库按固定顺序执行，自增序列确定），
  逐项比对 T1~T5 状态、blockers 数值、方案判定、指纹无污染，任一漂移即 FAIL。
- REG-01 演示「现场 Modify → Run → Verify」闭环：S1 基线补 8 条现场签收
  后 T1 由 blocked 转 established。

### ✅ 验收 2：4 类失败场景回归验证，降级行为符合预期

```
[PASS] FR-01a 库存接口 DB 异常不崩溃且返回 (False,500,db error)
[PASS] FR-01b 需求接口同样降级 (不崩溃)
[PASS] FR-02a 取证失败不使历史有效回执失效 (T1 established 且沿用旧回执)
[PASS] FR-02b 无旧回执 → T1 unknown (禁止 NOT_RECEIVED)
[PASS] FR-02c 取证失败不武断算缺口 (无 blocker)
[PASS] FR-02d T4 取证失败确认判 unknown
[PASS] FR-03a RAG 无匹配 → 固定文本「无可靠依据」(组件正常, evidence=ok)
[PASS] FR-03b LLM 故障注入 → status=unavailable 降级 (不崩溃)
[PASS] FR-03c weather 网络异常 → status=unavailable 且证据落库
[PASS] FR-04a 重启前: 建 run + 完整判定落库
[PASS] FR-04b 重启后: run 记录与判定完整保留
```

- FR-01 库存接口：DB 不可打开 → `(False, 500, "db error ...")` 结构化降级，
  无异常外抛（工具轨迹写入失败同样不外抛）。
- FR-02 取证失败：覆盖「有旧回执沿用判定 / 无旧回执 unknown / 不生成
  blocker / T4 判 unknown」四条降级语义，全链路无 NOT_RECEIVED。
- FR-03 组件异常：LLM 用 `SUPPLYGUARD_LLM_DISABLED=1` 故障注入开关、
  weather monkeypatch 网络异常、RAG 零重叠查询——三者均按契约降级且
  不阻断 T1~T5 判定。
- FR-04 重启：两阶段子进程模拟进程重启，runs / 5 条 task_results /
  blocker / 3 套方案与判定 / 快照 JSON 全部完整保留。

### ✅ 验收 3：清空环境，跟着 README 步骤，可从头拉起整套系统

`backend/e2e_boot_test.py`（8/8 通过）：

```
[PASS] E2E-01a 清空环境副本创建 (无数据库文件)
[PASS] E2E-01b 空环境 uvicorn 启动 (/health 200)
[PASS] E2E-01c 全新空库 run 列表为空
[PASS] E2E-01d load_scenario.py S3 --full 执行成功
[PASS] E2E-01e /api/runs 出现 BB03-RUN-001 (S3, completed)
[PASS] E2E-01f trace: T1 blocked + PLAN-A 可执行
[PASS] E2E-01g /console/ 前端工作台可访问
[PASS] E2E-01h S5 --full → BB03-RUN-002 降级语义 (T1 unknown, T4 pending_verification)
```

流程与 README 第 3/4/5 节完全一致：完整代码副本（无 .db）→
`uvicorn main:app` → `load_scenario.py S3 --full` → REST/trace/console
验证 → 第二场景 S5 复验。

### ✅ 验收 4：阶段 07 所有历史用例回归全部通过

| 用例套件                                                 | 结果           |
| -------------------------------------------------------- | -------------- |
| `whatif_run_test.py`（S4 what-if 推演 + 六表指纹无污染） | **46/46 PASS** |
| `s5_degrade_test.py`（S5 取证失败降级 + 证据回填复测）   | **33/33 PASS** |

## 回归汇总

| 套件                            | 用例数  | 结果             |
| ------------------------------- | ------- | ---------------- |
| regression_suite.py 正向案例    | 5       | 5/5 PASS         |
| regression_suite.py 失败回归    | 11      | 11/11 PASS       |
| e2e_boot_test.py 清空环境端到端 | 8       | 8/8 PASS         |
| whatif_run_test.py（阶段07）    | 46      | 46/46 PASS       |
| s5_degrade_test.py（阶段07）    | 33      | 33/33 PASS       |
| **合计**                        | **103** | **103/103 PASS** |

## 执行与复现

```bash
cd backend
python regression_suite.py --report-file reg_report.json   # 正向+失败 (16 用例)
python e2e_boot_test.py                                     # 清空环境端到端 (8 用例)
python whatif_run_test.py && python s5_degrade_test.py      # 阶段07 历史回归
```

全部测试隔离执行：真实数据库 `bb03.db`、运行中的后端服务（8000 端口）均不受影响。
