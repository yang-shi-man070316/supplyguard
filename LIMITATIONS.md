```
# 项目历史偏差说明  SupplyGuard
> 本文件用于记录开发演进过程中的早期理解偏差，符合传智杯证据如实记录要求。
## 历史背景
Commit #1‑#22：早期开发阶段，对业务模型理解出现偏差：
1. T1任务最初实现基于`stock_observations`库存观测做判断，**未依据receipt_records接收回执**。
2. 前期缺少表：`dispatch_records`、`receipt_records`、`review_requests`、`plan_definitions`、`plan_results`、`blockers`。
3. 缺失统一`evidence`业务证据层；缺失缺口计算、候选调配方案引擎、接收复核补证链路。
4. what‑if假设推演全套模块（板块1‑4）在该阶段完整实现，代码稳定可用。

从commit #23开始，向后迭代修复上述偏差。
旧提交#1‑#22完整保留，作为真实开发探索记录，未做任何篡改、回滚。

## 当前版本状态
-  T1~T5：重构，T1以receipt_records接收回执为核心判断依据
-  dispatch / receipt 调配‑接收回执完整表与业务逻辑
-  evidence统一证据层
-  gap缺口计算 + plan候选调配方案可行性判断
-  review_requests接收复核链路，提交回执自动生成新reality run
-  原有全部what‑if（视图合成、临时推演、方案管理、快照对比）保持兼容可用

## 仍然未实现
- MQTT模拟器dispatch/receipt消息生成（后续可扩展）
- 前端工作台、小程序、Three.js可视化
- RAG/LLM智能分析模块
```
