"""阶段03板块4: T4 入库签收合规保障 (纯判断, 与 T1/T2/T3/T5 互相隔离).

规则 (对齐 task_definitions.T4 rule_notes):
    T4-D1 在途物资签收记录可闭环核验: 每条签收记录可对应到同物资的
    库存观测闭环; 有在途运输时必须有签收记录;
    无签收记录且无在途运输判 unknown。

数据来源: 只读快照 snapshot["receipts"] (全量) / ["stocks"] /
["transports"] (IN_TRANSIT 即"在途")。
状态体系复用: 依赖 satisfied/unsatisfied/unknown; 任务
established/blocked/pending_verification/unknown。
"""

T4_TASK_ID = "T4"
T4_DEP_KEY = "T4-D1"
T4_DEP_DESC = "在途物资签收记录可闭环核验"


def _aggregate(checks):
    """任务级聚合 (通用规则, 同 t2_check._aggregate)。"""
    if not checks:
        return "unknown", "无依赖检查, 无法判断"
    statuses = [c["status"] for c in checks]
    if any(s == "unsatisfied" for s in statuses):
        return "blocked", "; ".join(c["reason"] for c in checks
                                    if c["status"] == "unsatisfied")[:400]
    if all(s == "satisfied" for s in statuses):
        return "established", "全部签收记录均可与库存观测闭环核验"
    if all(s == "unknown" for s in statuses):
        return "unknown", "无签收记录且无在途运输, 依赖不适用"
    return "pending_verification", "部分依赖无法判断, 待验证"


def check_t4(snapshot: dict) -> dict:
    """T4 纯判断 (只读快照, 不访问数据库)。

    返回 {"task_id", "checks":[{dep_key,dep_desc,status,evidence,reason}],
          "task_status", "summary_reason"}。
    """
    receipts = snapshot.get("receipts") or []
    in_transit = [t["transport_id"] for t in (snapshot.get("transports") or [])
                  if t.get("availability") == "IN_TRANSIT"]

    if not receipts:
        if in_transit:
            status, reason = "unsatisfied", (
                "存在%d个在途运输资源 (%s) 但无任何签收记录, 无法闭环核验"
                % (len(in_transit), ", ".join(in_transit)))
        else:
            status, reason = "unknown", "无签收记录且无在途运输, 依赖不适用"
        checks = [{
            "dep_key": T4_DEP_KEY, "dep_desc": T4_DEP_DESC,
            "status": status, "reason": reason,
            "evidence": {
                "receipt_total": 0, "in_transit": in_transit,
                "closed": 0, "broken": [],
            },
        }]
        return {
            "task_id": T4_TASK_ID, "checks": checks,
            "task_status": _aggregate(checks)[0],
            "summary_reason": _aggregate(checks)[1],
        }

    # 闭环核验: 每条签收记录可对应到同物资的库存观测
    stock_materials = {s.get("material_id") for s in (snapshot.get("stocks") or [])}
    broken = [
        "%s (%s/%s)" % (r.get("receipt_id"), r.get("service_id"), r.get("material_id"))
        for r in receipts if r.get("material_id") not in stock_materials
    ]
    if broken:
        status, reason = "unsatisfied", (
            "签收记录无法与库存观测闭环核验: %s" % "; ".join(broken))
    else:
        status, reason = "satisfied", (
            "全部%d条签收记录均可与库存观测闭环核验" % len(receipts))
    checks = [{
        "dep_key": T4_DEP_KEY, "dep_desc": T4_DEP_DESC,
        "status": status, "reason": reason,
        "evidence": {
            "receipt_total": len(receipts),
            "in_transit": in_transit,
            "closed": len(receipts) - len(broken),
            "broken": broken,
        },
    }]
    task_status, task_reason = _aggregate(checks)
    return {
        "task_id": T4_TASK_ID,
        "checks": checks,
        "task_status": task_status,
        "summary_reason": task_reason,
    }
