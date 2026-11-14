"""阶段03板块4: T5 证据一致性校验 (纯判断, 与 T1/T2/T3/T4 互相隔离).

规则 (对齐 task_definitions.T5 rule_notes):
    T5-D1 多源观测数据无冲突: 无负值矛盾、无非法状态值、
    观测时间不晚于落库时间 (容差内); 无任何观测证据判 unknown。

数据来源: 只读快照 snapshot["stocks"] / ["demands"] / ["transports"]
/ ["receipts"] (各条目带 created_at 落库时间, 板块4 快照扩展提供)。
状态体系复用: 依赖 satisfied/unsatisfied/unknown; 任务
established/blocked/pending_verification/unknown。
"""

from datetime import datetime, timedelta

T5_TASK_ID = "T5"
T5_DEP_KEY = "T5-D1"
T5_DEP_DESC = "多源观测数据无冲突"

_VALID_STATES = ("AVAILABLE", "IN_TRANSIT", "FAULT")
_TIME_TOLERANCE = timedelta(seconds=60)  # 观测时间晚于落库时间的允许容差


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
        return "blocked", "; ".join(c["reason"] for c in checks
                                    if c["status"] == "unsatisfied")[:400]
    if all(s == "satisfied" for s in statuses):
        return "established", "库存/需求/运输/签收观测之间无矛盾"
    if all(s == "unknown" for s in statuses):
        return "unknown", "无任何观测证据, 无法校验一致性"
    return "pending_verification", "部分依赖无法判断, 待验证"


def check_t5(snapshot: dict) -> dict:
    """T5 纯判断 (只读快照, 不访问数据库)。

    返回 {"task_id", "checks":[{dep_key,dep_desc,status,evidence,reason}],
          "task_status", "summary_reason"}。
    """
    stocks = snapshot.get("stocks") or []
    demands = snapshot.get("demands") or []
    transports = snapshot.get("transports") or []
    receipts = snapshot.get("receipts") or []

    counts = {"stock": len(stocks), "demand": len(demands),
              "transport": len(transports), "receipt": len(receipts)}
    if counts["stock"] == counts["demand"] == counts["transport"] == counts["receipt"] == 0:
        checks = [{
            "dep_key": T5_DEP_KEY, "dep_desc": T5_DEP_DESC,
            "status": "unknown", "evidence": counts,
            "reason": "无任何观测证据, 无法校验一致性",
        }]
        return {
            "task_id": T5_TASK_ID, "checks": checks,
            "task_status": "unknown", "summary_reason": "无任何观测证据, 无法校验一致性",
        }

    conflicts = []

    # 1) 负值矛盾
    negatives = []
    for s in stocks:
        if isinstance(s.get("quantity"), int) and s["quantity"] < 0:
            negatives.append("库存 %s/%s=%s" % (s.get("reserve_id"),
                                               s.get("material_id"), s["quantity"]))
    for d in demands:
        if isinstance(d.get("required_quantity"), int) and d["required_quantity"] < 0:
            negatives.append("需求 %s/%s=%s" % (d.get("service_id"),
                                               d.get("material_id"), d["required_quantity"]))
    for t in transports:
        if isinstance(t.get("capacity"), int) and t["capacity"] < 0:
            negatives.append("运输 %s 容量=%s" % (t.get("transport_id"), t["capacity"]))
    for r in receipts:
        if isinstance(r.get("received_quantity"), int) and r["received_quantity"] < 0:
            negatives.append("签收 %s 接收量=%s" % (r.get("receipt_id"),
                                                  r.get("received_quantity")))
    if negatives:
        conflicts.append("负值矛盾 %d 处: %s" % (len(negatives), "; ".join(negatives[:8])))

    # 2) 非法运输状态值
    bad_states = [t.get("availability") for t in transports
                  if t.get("availability") not in _VALID_STATES]
    if bad_states:
        conflicts.append("运输状态非法值: %s" % ", ".join(bad_states))

    # 3) 观测时间晚于落库时间 (超过容差)
    inverted = 0
    for label, rows, obs_key in (
        ("库存", stocks, "observed_at"),
        ("需求", demands, "observed_at"),
        ("运输", transports, "observed_at"),
        ("签收", receipts, "observed_at"),
    ):
        for row in rows:
            o, c = _parse_ts(row.get(obs_key)), _parse_ts(row.get("created_at"))
            if o is not None and c is not None and o > c + _TIME_TOLERANCE:
                inverted += 1
    if inverted:
        conflicts.append("观测时间晚于落库时间 %d 条 (超过%d秒容差)"
                         % (inverted, _TIME_TOLERANCE.seconds))

    if conflicts:
        status, reason = "unsatisfied", "; ".join(conflicts)
    else:
        status, reason = "satisfied", "库存/需求/运输/签收观测之间无矛盾"
    checks = [{
        "dep_key": T5_DEP_KEY, "dep_desc": T5_DEP_DESC,
        "status": status, "reason": reason,
        "evidence": {
            "observation_counts": counts,
            "negative_count": len(negatives),
            "bad_transport_states": bad_states,
            "inverted_time_count": inverted,
        },
    }]
    task_status, task_reason = _aggregate(checks)
    return {
        "task_id": T5_TASK_ID,
        "checks": checks,
        "task_status": task_status,
        "summary_reason": task_reason,
    }
