"""阶段04板块3/4 测试辅助: 向指定 run 注入 S3 参考文档条件快照.

用法:
    python inject_s3_snapshot.py <run_id> FAULT
    python inject_s3_snapshot.py <run_id> AVAILABLE
    python inject_s3_snapshot.py <run_id> FAULT full   (阶段07板块1: 完整文档快照)

inject() (阶段04 原行为, 不变): 把 run 的 input_snapshot_json 中的
stocks / transports 替换为参考文档固定判定条件, 并追加 routes 与
remaining_time; demands / receipts 保持原快照不动, T1 判定口径不受影响。

inject_full_doc() (阶段07板块1 新增): 在 inject 基础上把 demands /
receipts 也替换为 S3 参考文档条件 (P1/W 需求 7 / 签收 3), 使缺口识别
产生 W 物资 blocker (gap = 7 - 3 = 4), 从而 plan_store 的调拨量
transfer_qty = 4, 方案判定链路完整成立:

    stocks:          R1/W = 6, R2/W = 5
    transports:      V1 AVAILABLE capacity 4; V2 状态由参数控制
                     (FAULT = 不可用 / AVAILABLE = 可用), capacity 4
    routes:          route-A available 20min / route-B available 20min
                     / route-C available 40min
    remaining_time:  30 (分钟)
    demands:         P1/W required_quantity = 7 (08:00)
    receipts:        P1/W received_quantity = 3 (09:00)
    → blocker:       P1/W gap = 4 → 三套候选方案 transfer_qty = 4

要求 run 仍为 pending (completed 快照被触发器冻结, 注入会失败 409)。
"""

import json
import sys

from db import close_conn, get_conn

DOC_STOCKS = [
    {"reserve_id": "R1", "material_id": "W", "quantity": 6},
    {"reserve_id": "R2", "material_id": "W", "quantity": 5},
]
DOC_ROUTES = [
    {"route_id": "route-A", "status": "available", "duration_min": 20},
    {"route_id": "route-B", "status": "available", "duration_min": 20},
    {"route_id": "route-C", "status": "available", "duration_min": 40},
]
DOC_REMAINING_TIME = 30

# 阶段07板块1: S3 参考文档需求/签收条件 (构造 W 缺口 = 4)
# 时间口径: 需求 08:00 < 签收 09:00 < 库存/运输 10:00, 观测时间不晚于
# 落库时间, T3 响应性 / T5 时间线校验均成立。
DOC_DEMANDS = [
    {"service_id": "P1", "material_id": "W", "required_quantity": 7,
     "observed_at": "2026-11-15T08:00:00",
     "created_at": "2026-11-15 08:00:00"},
]
DOC_RECEIPTS = [
    {"receipt_id": "RCT-S3-001", "dispatch_id": "DSP-S3-001",
     "service_id": "P1", "material_id": "W", "received_quantity": 3,
     "observed_at": "2026-11-15T09:00:00", "source_type": "DOC",
     "created_at": "2026-11-15 09:00:00"},
]
DOC_STOCKS_FULL = [
    dict(s, observed_at="2026-11-15T10:00:00",
         created_at="2026-11-15 10:00:00") for s in DOC_STOCKS
]
DOC_TRANSPORTS_FULL = [
    {"transport_id": "V1", "availability": "AVAILABLE", "capacity": 4,
     "observed_at": "2026-11-15T10:00:00",
     "created_at": "2026-11-15 10:00:00"},
    {"transport_id": "V2", "availability": None, "capacity": 4,
     "observed_at": "2026-11-15T10:00:00",
     "created_at": "2026-11-15 10:00:00"},
]


def inject(run_id: str, v2_state: str) -> tuple:
    """注入快照 (阶段04 原行为), 返回 (ok, http_status, msg, data)。"""
    v2 = (v2_state or "").upper()
    if v2 not in ("FAULT", "AVAILABLE"):
        raise ValueError("v2_state must be FAULT or AVAILABLE")
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT run_id, run_status, input_snapshot_json "
            "FROM runs WHERE run_id=?", (run_id,),
        ).fetchone()
        if row is None:
            return False, 404, "run %s not found" % run_id, None
        if row["run_status"] == "completed":
            return False, 409, (
                "run %s is completed, snapshot immutable" % run_id), None
        try:
            snap = json.loads(row["input_snapshot_json"])
        except (TypeError, ValueError):
            return False, 500, (
                "run %s input_snapshot_json invalid" % run_id), None
        if not isinstance(snap, dict):
            return False, 500, (
                "run %s input_snapshot_json invalid" % run_id), None
        snap["stocks"] = DOC_STOCKS
        snap["transports"] = [
            {"transport_id": "V1", "availability": "AVAILABLE",
             "capacity": 4},
            {"transport_id": "V2", "availability": v2, "capacity": 4},
        ]
        snap["routes"] = DOC_ROUTES
        snap["remaining_time"] = DOC_REMAINING_TIME
        conn.execute(
            "UPDATE runs SET input_snapshot_json=? WHERE run_id=?",
            (json.dumps(snap, ensure_ascii=False), run_id),
        )
        conn.commit()
    finally:
        close_conn(conn)
    return True, 200, "snapshot injected (V2=%s)" % v2, {
        "run_id": run_id,
        "v2_state": v2,
        "remaining_time": DOC_REMAINING_TIME,
    }


def inject_full_doc(run_id: str, v2_state: str) -> tuple:
    """注入完整 S3 参考文档快照 (阶段07板块1, 含 demands/receipts)。

    与 inject 的区别: demands / receipts 同样替换为文档条件
    (P1/W 需求 7 / 签收 3 → W 缺口 blocker gap=4), 使 gap_check →
    plan_store (transfer_qty=4) → plan_judge 全链路成立; 库存/运输/
    签收/需求条目均携带 observed_at / created_at, T5 时间线校验合法。
    返回 (ok, http_status, msg, data)。
    """
    v2 = (v2_state or "").upper()
    if v2 not in ("FAULT", "AVAILABLE"):
        raise ValueError("v2_state must be FAULT or AVAILABLE")
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT run_id, run_status, input_snapshot_json "
            "FROM runs WHERE run_id=?", (run_id,),
        ).fetchone()
        if row is None:
            return False, 404, "run %s not found" % run_id, None
        if row["run_status"] == "completed":
            return False, 409, (
                "run %s is completed, snapshot immutable" % run_id), None
        try:
            snap = json.loads(row["input_snapshot_json"])
        except (TypeError, ValueError):
            return False, 500, (
                "run %s input_snapshot_json invalid" % run_id), None
        if not isinstance(snap, dict):
            return False, 500, (
                "run %s input_snapshot_json invalid" % run_id), None
        snap["stocks"] = DOC_STOCKS_FULL
        transports = [dict(t) for t in DOC_TRANSPORTS_FULL]
        for t in transports:
            if t["transport_id"] == "V2":
                t["availability"] = v2
        snap["transports"] = transports
        snap["routes"] = DOC_ROUTES
        snap["remaining_time"] = DOC_REMAINING_TIME
        snap["demands"] = DOC_DEMANDS
        snap["receipts"] = DOC_RECEIPTS
        conn.execute(
            "UPDATE runs SET input_snapshot_json=? WHERE run_id=?",
            (json.dumps(snap, ensure_ascii=False), run_id),
        )
        conn.commit()
    finally:
        close_conn(conn)
    return True, 200, "full S3 doc snapshot injected (V2=%s)" % v2, {
        "run_id": run_id,
        "v2_state": v2,
        "remaining_time": DOC_REMAINING_TIME,
        "demand": DOC_DEMANDS,
        "receipts": DOC_RECEIPTS,
        "expected_w_gap": 4,
    }


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        print("usage: python inject_s3_snapshot.py <run_id> "
              "FAULT|AVAILABLE [full]", file=sys.stderr)
        sys.exit(2)
    try:
        if len(sys.argv) == 4 and sys.argv[3] == "full":
            ok, http_status, msg, data = inject_full_doc(
                sys.argv[1], sys.argv[2])
        else:
            ok, http_status, msg, data = inject(sys.argv[1], sys.argv[2])
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
    print(json.dumps({"ok": ok, "http_status": http_status, "msg": msg,
                      "data": data}, ensure_ascii=False, indent=2))
    sys.exit(0 if ok else 1)
