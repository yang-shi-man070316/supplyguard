"""阶段03板块3: T1 校验判断 (板块4 起只负责 T1 纯判断, 落库由 task_execute 编排).

T1 最小判断逻辑 (严格按阶段03文档):
    for each critical service point
        for each required material
            latest valid demand exists?
            latest valid receipt exists?
            received >= required?

实现口径:
    - 数据来源: 只读 input_snapshot_json 快照, 不读实时观测表
    - 检查对象: 快照 demands 中每条 (服务点, 物资) 需求
    - 依赖状态: satisfied / unsatisfied / unknown
    - 任务状态: 任一 unsatisfied → blocked; 全部 satisfied → established;
      全部 unknown → unknown (阶段07板块2 取证失败降级);
      mixed satisfied+unknown → pending_verification;
      快照无需求观测 → unknown

板块4 起本模块导出 check_t1(snapshot) 纯判断函数, 与 t2~t5_check 并行;
T1 判断逻辑自板块3 起保持不变。
"""

import json
import sqlite3

from db import close_conn, get_conn

T1_TASK_ID = "T1"
T1_DEP_KEY = "T1-D1"
T1_DEP_DESC = "最新有效签收接收量 ≥ 当期最低需求"

# 阶段07板块2 取证失败降级语义 (与板块3文档对齐后的 T1 静态描述):
#   签收条目 status='unavailable' 表示「接收确认取证失败」——取证失败 ≠
#   业务判定失败: 该组合依赖判 unknown (禁止输出未收到/NOT_RECEIVED 结论);
#   若更早存在有效签收 (status≠unavailable 且数量有效), 旧回执仍沿用判定。
T1_ALIGNED_DESCRIPTION = "全部关键服务点当期最低需求均可被当前签收接收保障"
T1_ALIGNED_RULE_NOTES = (
    "T1-D1 最新有效签收接收量≥当期最低需求: 对每个服务点每种物资, "
    "取最新有效需求与最新签收证据, 接收量≥需求量判 satisfied; "
    "无签收或接收不足判 unsatisfied; 需求或签收数据无效判 unknown; "
    "最新签收证据为取证失败 (status='unavailable') 时: 更早有有效签收则"
    "沿用旧回执判定, 否则判 unknown (取证失败≠未收到, 禁止输出未收到结论)。"
    "任一 unsatisfied → blocked; 全部 satisfied → established; "
    "全部 unknown → unknown; 无 unsatisfied 但有 mixed → "
    "pending_verification; 快照无需求观测 → unknown"
)


def align_t1_meta(conn):
    """幂等对齐 task_definitions 的 T1 行为签收口径 (只更新 T1 行数据)。

    板块1 种子沿用旧口径文本 (库存≥需求), 板块3 文档的 T1 判断以签收
    接收为核心依据, 此处将静态描述/规则文本对齐。不动表结构、不动常量。
    """
    conn.execute(
        "UPDATE task_definitions SET description=?, rule_notes=? WHERE task_id=?",
        (T1_ALIGNED_DESCRIPTION, T1_ALIGNED_RULE_NOTES, T1_TASK_ID),
    )


def _latest_receipt_for(receipts, service_id, material_id):
    """该 (服务点, 物资) 的最新一条签收证据 (含取证失败确认)。receipts 已按
    (observed_at, id) 升序, 取最后一条匹配项即最新; 无匹配返回 None。"""
    latest = None
    for r in receipts:
        if (r.get("service_id") == service_id
                and r.get("material_id") == material_id):
            latest = r
    return latest


def _is_failed_confirmation(receipt) -> bool:
    """该签收证据是否为「取证失败确认」 (阶段07板块2)。

    取证失败确认: status='unavailable' 且无有效接收数量 (received_quantity
    为 null/无效), 表示「现场无法留下签收凭证, 送达与否未知」——不是
    received:false, 绝不代表确认未收到。
    """
    return receipt.get("status") == "unavailable"


def _is_valid_receipt(receipt) -> bool:
    """该签收证据是否为「有效签收回执」: 非取证失败确认且数量有效。"""
    return (not _is_failed_confirmation(receipt)
            and _is_valid_quantity(receipt.get("received_quantity")))


def _latest_valid_receipt_for(receipts, service_id, material_id,
                              before=None) -> dict:
    """该组合最新一条有效签收回执 (可选: 只取 before 之前的更早条目)。

    阶段07板块2 边界语义: 取证失败确认不使历史回执失效——旧回执若满足
    有效性规则 (非 unavailable 且数量有效), 仍可沿用为有效证据。
    """
    latest = None
    for r in receipts:
        if before is not None and r is before:
            break
        if (r.get("service_id") == service_id
                and r.get("material_id") == material_id
                and _is_valid_receipt(r)):
            latest = r
    return latest


def _is_valid_quantity(value) -> bool:
    """有效数量: 非 bool 的 int 且 >= 0。"""
    return (isinstance(value, int) and not isinstance(value, bool)
            and value >= 0)


def _judge_demand(demand, receipts) -> dict:
    """单条需求判断 → {dep_key, dep_desc, status, evidence(dict), reason}。"""
    service = demand.get("service_id")
    material = demand.get("material_id")
    required = demand.get("required_quantity")
    evidence = {
        "service_id": service,
        "material_id": material,
        "required_quantity": required,
        "demand_observed_at": demand.get("observed_at"),
    }

    # 1) latest valid demand exists?
    if not service or not material or not _is_valid_quantity(required):
        return {
            "dep_key": T1_DEP_KEY, "dep_desc": T1_DEP_DESC, "status": "unknown",
            "evidence": evidence,
            "reason": "需求记录无效 (服务点/物资缺失或需求量缺失/为负), 无法判断",
        }

    # 2) latest receipt evidence exists?
    receipt = _latest_receipt_for(receipts, service, material)
    if receipt is None:
        return {
            "dep_key": T1_DEP_KEY, "dep_desc": T1_DEP_DESC, "status": "unsatisfied",
            "evidence": evidence,
            "reason": "%s %s 无签收记录, 无法证明接收到位" % (service, material),
        }

    # 阶段07板块2: 最新证据为取证失败确认 (status='unavailable') →
    # 取证失败 ≠ 未收到: 有更早有效回执则沿用判定, 否则判 unknown
    # (绝不输出未收到/NOT_RECEIVED 确定结论)。
    if _is_failed_confirmation(receipt):
        evidence["failed_confirmation"] = {
            "receipt_id": receipt.get("receipt_id"),
            "status": receipt.get("status"),
            "received_quantity": receipt.get("received_quantity"),
            "observed_at": receipt.get("observed_at"),
            "error_message": receipt.get("error_message"),
        }
        older = _latest_valid_receipt_for(receipts, service, material,
                                          before=receipt)
        if older is not None:
            evidence["latest_receipt"] = {
                "receipt_id": older.get("receipt_id"),
                "received_quantity": older.get("received_quantity"),
                "observed_at": older.get("observed_at"),
            }
            received = older.get("received_quantity")
            inherited = "沿用历史有效回执 %s (%d件)" % (
                older.get("receipt_id"), received)
            if received >= required:
                return {
                    "dep_key": T1_DEP_KEY, "dep_desc": T1_DEP_DESC,
                    "status": "satisfied",
                    "evidence": evidence,
                    "reason": ("本次接收确认取证失败(unavailable), %s; "
                               "签收%d件 ≥ 需求%d件, 满足" %
                               (inherited, received, required)),
                }
            return {
                "dep_key": T1_DEP_KEY, "dep_desc": T1_DEP_DESC,
                "status": "unsatisfied",
                "evidence": evidence,
                "reason": ("本次接收确认取证失败(unavailable), %s; "
                           "签收%d件 < 需求%d件, 缺口%d件" %
                           (inherited, received, required, required - received)),
            }
        return {
            "dep_key": T1_DEP_KEY, "dep_desc": T1_DEP_DESC, "status": "unknown",
            "evidence": evidence,
            "reason": ("%s %s 接收确认取证失败 (status=unavailable, 无有效签收"
                       "凭证), 送达与否无法判断, 不判定未收到" % (service, material)),
        }

    evidence["latest_receipt"] = {
        "receipt_id": receipt.get("receipt_id"),
        "received_quantity": receipt.get("received_quantity"),
        "observed_at": receipt.get("observed_at"),
    }
    received = receipt.get("received_quantity")
    if not _is_valid_quantity(received):
        return {
            "dep_key": T1_DEP_KEY, "dep_desc": T1_DEP_DESC, "status": "unknown",
            "evidence": evidence,
            "reason": "签收记录接收量无效 (缺失或为负), 无法判断",
        }

    # 3) received >= required?
    if received >= required:
        return {
            "dep_key": T1_DEP_KEY, "dep_desc": T1_DEP_DESC, "status": "satisfied",
            "evidence": evidence,
            "reason": "签收%d件 ≥ 需求%d件, 满足" % (received, required),
        }
    return {
        "dep_key": T1_DEP_KEY, "dep_desc": T1_DEP_DESC, "status": "unsatisfied",
        "evidence": evidence,
        "reason": "签收%d件 < 需求%d件, 缺口%d件"
                  % (received, required, required - received),
    }


def _aggregate_task_status(checks) -> tuple:
    """任务级状态聚合 (status, summary_reason)。

    checks 为空 → unknown (快照无需求观测);
    任一 unsatisfied → blocked; 全部 satisfied → established;
    全部 unknown → unknown (阶段07板块2: 取证失败降级语义——
    全部依赖均无法判断时任务整体 unknown, 不落入待验证);
    mixed satisfied+unknown → pending_verification。
    """
    if not checks:
        return "unknown", "快照中无需求观测, 无法判断"
    statuses = [c["status"] for c in checks]
    if any(s == "unsatisfied" for s in statuses):
        bad = [c for c in checks if c["status"] == "unsatisfied"]
        head = "; ".join(
            "%s %s: %s" % (c["evidence"].get("service_id"),
                           c["evidence"].get("material_id"), c["reason"])
            for c in bad[:5]
        )
        more = "" if len(bad) <= 5 else "; ... 共%d项不满足" % len(bad)
        return "blocked", "%d/%d 项依赖不满足 (blocked): %s%s" % (
            len(bad), len(checks), head, more)
    if all(s == "satisfied" for s in statuses):
        return "established", "全部%d项需求均有有效签收且接收量满足" % len(checks)
    if all(s == "unknown" for s in statuses):
        return "unknown", (
            "全部%d项依赖均无法判断 (取证失败/证据缺失), 任务结果未知"
            % len(checks))
    unknown_n = sum(1 for s in statuses if s == "unknown")
    return "pending_verification", (
        "无 unsatisfied 依赖, 但有%d项依赖无法判断, 待验证" % unknown_n)


def check_t1(snapshot: dict) -> dict:
    """T1 纯判断 (只读快照, 不访问数据库)。

    返回 {"task_id", "checks":[{dep_key,dep_desc,status,evidence,reason}],
          "task_status", "summary_reason"}。落库由 task_execute 编排。
    """
    demands = snapshot.get("demands") or []
    receipts = snapshot.get("receipts") or []

    # ---- T1 逐条判断 ----
    check_rows = [_judge_demand(d, receipts) for d in demands]
    if not check_rows:
        # 快照无需求观测: 写一条占位检查, 任务结果直接 unknown
        # (不走聚合, 避免落入 pending_verification)
        check_rows = [{
            "dep_key": T1_DEP_KEY, "dep_desc": T1_DEP_DESC,
            "status": "unknown", "evidence": {},
            "reason": "快照中无需求观测, 无法判断",
        }]
        task_status, task_reason = "unknown", "快照中无需求观测, 无法判断"
    else:
        task_status, task_reason = _aggregate_task_status(check_rows)
    return {
        "task_id": T1_TASK_ID,
        "checks": check_rows,
        "task_status": task_status,
        "summary_reason": task_reason,
    }


def _check_row_to_dict(row) -> dict:
    d = dict(row)
    if d.get("evidence"):
        try:
            d["evidence"] = json.loads(d["evidence"])
        except json.JSONDecodeError:
            pass  # 防御脏数据: 保留原文
    return d


def _task_row_to_dict(row) -> dict:
    return dict(row) if row is not None else None


def get_task_result(run_id: str, task_id: str = T1_TASK_ID) -> tuple:
    """查询某 run 的某任务结果 + 该 run 下全部依赖检查明细 (只读)。

    返回 (ok, http_status, msg, data);
    run 不存在 → 404; 无结果时 task_result=None, dependency_checks=[]。
    """
    try:
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                return False, 404, "run %s not found" % run_id, None
            result = conn.execute(
                "SELECT * FROM task_results WHERE run_id=? AND task_id=? "
                "ORDER BY id DESC LIMIT 1",
                (run_id, task_id),
            ).fetchone()
            checks = conn.execute(
                "SELECT * FROM dependency_checks WHERE run_id=? AND task_id=? "
                "ORDER BY id",
                (run_id, task_id),
            ).fetchall()
        finally:
            close_conn(conn)
    except sqlite3.Error as exc:
        return False, 500, "db error: %s" % exc, None
    return True, 200, "ok", {
        "run_id": run_id,
        "task_id": task_id,
        "scenario_id": row["scenario_id"],
        "run_type": row["run_type"],
        "run_status": row["run_status"],
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
        "task_result": _task_row_to_dict(result),
        "dependency_checks": [_check_row_to_dict(c) for c in checks],
    }
