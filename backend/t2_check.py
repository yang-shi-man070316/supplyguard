"""阶段03板块4: T2 运输链路可达保障 (纯判断, 与 T1/T3/T4/T5 互相隔离).

规则 (对齐 task_definitions.T2 rule_notes):
    T2-D1 物资存在可用运输通道: 至少存在一个最新状态为
    AVAILABLE/IN_TRANSIT 的运输资源; 全部 FAULT 判 unsatisfied;
    无运输观测判 unknown。

数据来源: 只读快照 snapshot["transports"] (每个运输资源最新一条)。
状态体系复用: 依赖 satisfied/unsatisfied/unknown; 任务
established/blocked/pending_verification/unknown。
"""

T2_TASK_ID = "T2"
T2_DEP_KEY = "T2-D1"
T2_DEP_DESC = "物资存在可用运输通道"

_USABLE = ("AVAILABLE", "IN_TRANSIT")


def _aggregate(checks):
    """任务级聚合 (通用规则):
    无检查 → unknown; 任一 unsatisfied → blocked; 全部 satisfied → established;
    全部 unknown (无有效观测) → unknown; 混合 satisfied+unknown → pending_verification。
    """
    if not checks:
        return "unknown", "无依赖检查, 无法判断"
    statuses = [c["status"] for c in checks]
    if any(s == "unsatisfied" for s in statuses):
        return "blocked", "; ".join(
            "%s:%s" % (c["dep_key"], c["status"]) for c in checks)
    if all(s == "satisfied" for s in statuses):
        return "established", "存在可用运输通道, 运输链路可达"
    if all(s == "unknown" for s in statuses):
        return "unknown", "无有效运输观测, 无法判断"
    return "pending_verification", "部分依赖无法判断, 待验证"


def check_t2(snapshot: dict) -> dict:
    """T2 纯判断 (只读快照, 不访问数据库)。

    返回 {"task_id", "checks":[{dep_key,dep_desc,status,evidence,reason}],
          "task_status", "summary_reason"}。
    """
    transports = snapshot.get("transports") or []

    if not transports:
        checks = [{
            "dep_key": T2_DEP_KEY, "dep_desc": T2_DEP_DESC,
            "status": "unknown", "evidence": {"transport_total": 0},
            "reason": "无运输资源观测记录, 无法判断",
        }]
        task_status, task_reason = "unknown", "无运输资源观测记录, 无法判断"
    else:
        usable = [t["transport_id"] for t in transports
                  if t.get("availability") in _USABLE]
        faulty = [t["transport_id"] for t in transports
                  if t.get("availability") == "FAULT"]
        others = [t["transport_id"] for t in transports
                  if t.get("availability") not in _USABLE + ("FAULT",)]
        if usable:
            status, reason = "satisfied", "存在可用运输通道: %s" % ", ".join(usable)
            if faulty:
                reason += "; 故障: %s" % ", ".join(faulty)
        elif faulty:
            status, reason = "unsatisfied", (
                "全部运输资源均 FAULT, 无可用运输通道: %s" % ", ".join(faulty))
        else:
            status, reason = "unknown", (
                "运输资源状态值无法识别: %s" % ", ".join(others))
        checks = [{
            "dep_key": T2_DEP_KEY, "dep_desc": T2_DEP_DESC,
            "status": status, "reason": reason,
            "evidence": {
                "transport_total": len(transports),
                "usable": usable, "faulty": faulty, "unrecognized": others,
            },
        }]
        task_status, task_reason = _aggregate(checks)

    return {
        "task_id": T2_TASK_ID,
        "checks": checks,
        "task_status": task_status,
        "summary_reason": task_reason,
    }
