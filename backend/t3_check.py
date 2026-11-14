"""阶段03板块4: T3 调度响应时效保障 (纯判断, 与 T1/T2/T4/T5 互相隔离).

规则 (对齐 task_definitions.T3 rule_notes):
    T3-D1 调度可在需求时限前送达: 每项最新需求提出后存在运输/签收活动
    响应 (库中无调度计划证据时以观测活动做代理判断);
    需求时间不可解析或需求观测缺失判 unknown。

数据来源: 只读快照 snapshot["demands"] / ["transports"] / ["receipts"]。
每个 (服务点, 物资) 需求一条 T3-D1 检查。
状态体系复用: 依赖 satisfied/unsatisfied/unknown; 任务
established/blocked/pending_verification/unknown。
"""

from datetime import datetime

T3_TASK_ID = "T3"
T3_DEP_KEY = "T3-D1"
T3_DEP_DESC = "调度可在需求时限前送达"


def _parse_ts(value):
    """解析时间字符串 (兼容 ISO 'T' 与空格分隔), 失败返回 None。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _aggregate(checks):
    """任务级聚合 (通用规则, 同 t2_check._aggregate)。"""
    if not checks:
        return "unknown", "无依赖检查, 无法判断"
    statuses = [c["status"] for c in checks]
    if any(s == "unsatisfied" for s in statuses):
        bad = [c for c in checks if c["status"] == "unsatisfied"]
        head = "; ".join(
            "%s %s: %s" % (c["evidence"].get("service_id"),
                           c["evidence"].get("material_id"), c["reason"])
            for c in bad[:5]
        )
        more = "" if len(bad) <= 5 else "; ... 共%d项" % len(bad)
        return "blocked", "%d/%d 项需求无响应 (blocked): %s%s" % (
            len(bad), len(checks), head, more)
    if all(s == "satisfied" for s in statuses):
        return "established", "全部%d项需求提出后均有运输/签收活动响应" % len(checks)
    if all(s == "unknown" for s in statuses):
        return "unknown", "无有效需求观测, 无法判断"
    return "pending_verification", "部分依赖无法判断, 待验证"


def check_t3(snapshot: dict) -> dict:
    """T3 纯判断 (只读快照, 不访问数据库)。

    返回 {"task_id", "checks":[{dep_key,dep_desc,status,evidence,reason}],
          "task_status", "summary_reason"}。
    """
    demands = snapshot.get("demands") or []

    if not demands:
        checks = [{
            "dep_key": T3_DEP_KEY, "dep_desc": T3_DEP_DESC,
            "status": "unknown", "evidence": {"demand_total": 0},
            "reason": "无服务点需求观测记录, 无法判断",
        }]
        return {
            "task_id": T3_TASK_ID, "checks": checks,
            "task_status": "unknown", "summary_reason": "无服务点需求观测记录, 无法判断",
        }

    # 全部活动时间 (运输观测 + 签收记录)
    activity_times = []
    for t in (snapshot.get("transports") or []):
        ts = _parse_ts(t.get("observed_at"))
        if ts is not None:
            activity_times.append(ts)
    for r in (snapshot.get("receipts") or []):
        ts = _parse_ts(r.get("observed_at"))
        if ts is not None:
            activity_times.append(ts)

    checks = []
    for d in demands:
        service, material = d.get("service_id"), d.get("material_id")
        evidence = {
            "service_id": service, "material_id": material,
            "demand_observed_at": d.get("observed_at"),
        }
        t = _parse_ts(d.get("observed_at"))
        if t is None:
            status, reason = "unknown", "需求时间不可解析, 无法核验时效"
        elif any(a >= t for a in activity_times):
            status, reason = "satisfied", "需求提出后存在运输/签收活动响应"
        else:
            status, reason = "unsatisfied", (
                "需求 %s 提出后无任何运输/签收活动响应" % d.get("observed_at"))
        checks.append({
            "dep_key": T3_DEP_KEY, "dep_desc": T3_DEP_DESC,
            "status": status, "evidence": evidence, "reason": reason,
        })

    task_status, task_reason = _aggregate(checks)
    return {
        "task_id": T3_TASK_ID,
        "checks": checks,
        "task_status": task_status,
        "summary_reason": task_reason,
    }
