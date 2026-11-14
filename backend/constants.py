"""阶段03 板块1: 全局常量枚举 (严格对齐阶段03文档).

本模块只放全局常量与静态任务定义, 不访问数据库、不包含任何业务逻辑,
可被任意层安全 import。db.py 建表时的 CHECK 约束同样引用本模块常量,
保证「常量定义 = 数据层约束」单一来源。

文档名 → 常量名对照:
    run_type           -> RUN_TYPE
    dependency_status  -> DEPENDENCY_STATUS
    task_result_status -> TASK_RESULT_STATUS
    run_status         -> RUN_STATUS
    run_id 规则        -> RUN_ID_PREFIX / format_run_id()
"""

# ---- 运行类型: 现阶段唯一取值 reality (真实运行) ----
RUN_TYPE = "reality"

# ---- 依赖检查状态枚举 (dependency_checks.status) ----
DEPENDENCY_STATUS = ["satisfied", "unsatisfied", "unknown"]

# ---- 任务结果状态枚举 (task_results.result_status) ----
TASK_RESULT_STATUS = ["established", "blocked", "pending_verification", "unknown"]

# ---- run 状态枚举 (runs.run_status) ----
RUN_STATUS = ["pending", "completed"]

# ---- run_id 生成规则: BB03-RUN-001 / BB03-RUN-002 / ... 自增 ----
RUN_ID_PREFIX = "BB03-RUN-"
RUN_ID_DIGITS = 3


def format_run_id(seq: int) -> str:
    """按规则格式化 run_id, 如 format_run_id(1) -> 'BB03-RUN-001'。"""
    return "%s%0*d" % (RUN_ID_PREFIX, RUN_ID_DIGITS, int(seq))


# ============================================================
# T1~T5 静态任务定义 (task_definitions 种子数据)
#
# 只存稳定业务结构: 任务编号/名称/描述/判定规则。
# 禁止写入实时库存、需求、运输等运行时状态——实时数据一律存观测表与
# dependency_checks / task_results, 不落本表。
# ============================================================
TASK_DEFINITIONS = (
    {
        "task_id": "T1",
        "task_name": "关键服务点最低需求保障",
        "description": "全部关键服务点当期最低需求均可被当前库存保障",
        "rule_notes": (
            "T1-D1 关键节点库存≥当期最低需求: 每个服务点每种物资的当期最低需求 "
            "≤ 当前全网该物资库存观测总量; 无需求观测判 unknown; "
            "某物资无库存观测按库存 0 计"
        ),
    },
    {
        "task_id": "T2",
        "task_name": "运输链路可达保障",
        "description": "关键物资存在可用运输通道",
        "rule_notes": (
            "T2-D1 物资存在可用运输通道: 至少存在一个最新状态为 "
            "AVAILABLE/IN_TRANSIT 的运输资源; 全部 FAULT 判 unsatisfied; "
            "无运输观测判 unknown"
        ),
    },
    {
        "task_id": "T3",
        "task_name": "调度响应时效保障",
        "description": "调度计划可以在需求时限前抵达",
        "rule_notes": (
            "T3-D1 调度可在需求时限前送达: 每项最新需求提出后存在运输/签收活动 "
            "响应 (库中无调度计划证据时以观测活动做代理判断); "
            "需求时间不可解析或需求观测缺失判 unknown"
        ),
    },
    {
        "task_id": "T4",
        "task_name": "入库签收合规保障",
        "description": "在途物资签收记录可闭环核验",
        "rule_notes": (
            "T4-D1 在途物资签收记录可闭环核验: 每条签收记录可对应到同物资的库存 "
            "观测闭环; 有在途运输时必须有签收记录; 无签收且无在途判 unknown"
        ),
    },
    {
        "task_id": "T5",
        "task_name": "证据一致性校验",
        "description": "库存、运输、签收、需求观测之间数据不自相矛盾",
        "rule_notes": (
            "T5-D1 多源观测数据无冲突: 无负值矛盾、无非法状态值、"
            "观测时间不晚于落库时间 (容差内); 无任何观测证据判 unknown"
        ),
    },
)
