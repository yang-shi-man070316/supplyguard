"""阶段04板块3/4 测试辅助: 向指定 run 注入 S3 参考文档条件快照.

用法:
    python inject_s3_snapshot.py <run_id> FAULT
    python inject_s3_snapshot.py <run_id> AVAILABLE

把 run 的 input_snapshot_json 中的 stocks / transports 替换为参考文档
固定判定条件, 并追加 routes 与 remaining_time (当前快照抓取本身不含
路线数据, 判定所需数据由本脚本注入, 仅用于板块3/4 自测; demands /
receipts 保持原快照不动, T1 判定口径不受影响):

    stocks:          R1/W = 6, R2/W = 5
    transports:      V1 AVAILABLE capacity 4; V2 状态由参数控制
                     (FAULT = 不可用 / AVAILABLE = 可用), capacity 4
    routes:          route-A available 20min / route-B available 20min
                     / route-C available 40min
    remaining_time:  30 (分钟)

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


def inject(run_id: str, v2_state: str) -> tuple:
    """注入快照, 返回 (ok, http_status, msg, data)。"""
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


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: python inject_s3_snapshot.py <run_id> "
              "FAULT|AVAILABLE", file=sys.stderr)
        sys.exit(2)
    try:
        ok, http_status, msg, data = inject(sys.argv[1], sys.argv[2])
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
    print(json.dumps({"ok": ok, "http_status": http_status, "msg": msg,
                      "data": data}, ensure_ascii=False, indent=2))
    sys.exit(0 if ok else 1)
