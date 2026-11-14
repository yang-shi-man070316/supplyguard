"""阶段04板块3: S3 方案可行性判定 (plan_judge) — 生成 plan_results.

判定规则 (全部满足才 executable, 任意一条不满足 → non_executable):
    1. 始发仓库的物资库存 ≥ transfer_qty (快照 stocks)
    2. 车辆状态 available 且车辆 capacity ≥ transfer_qty (快照 transports)
    3. 路线状态 available 且路线耗时 ≤ remaining_time (快照 routes +
       remaining_time, 单位分钟)

数据来源 (全部只读, 不硬编码任何固定值):
    - plan_definitions: 当前 run 的候选方案 (板块2 落库)
    - runs.input_snapshot_json: stocks / transports / routes /
      remaining_time (判定所需资源状态, 逐条从快照匹配读取)
    - blockers 不参与判定 (transfer_qty 已随方案落库)
    快照缺失对应数据 → 该条判定不满足 (保守判定), reason 写明缺失项。

业务红线 (文档第7步强制要求):
    本模块绝不修改 T1 状态——不触碰 task_results / dependency_checks /
    runs.run_status; 判定为 executable 只代表方案可行, 不解除 T1 阻塞;
    只有产生真实投送与新的接收证据后 T1 才会解除阻塞 (另有流程处理)。

幂等: 重复执行先 DELETE 该 run 的 plan_results 旧记录, 再重新计算
写入, 避免旧结果残留 (与表 UNIQUE(run_id, plan_id) 双保险)。

CLI: python plan_judge.py <run_id>
"""

import json
import sqlite3

from db import close_conn, get_conn


def _judge_plan(plan, snapshot) -> dict:
    """单条方案可行性判定 (纯计算, 只读快照)。

    返回 {"plan_id", "plan_name", "status", "reason_json"}。
    reason_json = {summary, checks: [{rule, passed, detail} x3]}
    """
    transfer_qty = plan["transfer_qty"]
    reserve_id = plan["reserve_id"]
    vehicle_id = plan["vehicle_id"]
    route_id = plan["route_id"]
    material_id = plan["material_id"]
    checks = []

    # ---- 规则1: 始发仓库库存 ≥ transfer_qty ----
    stock = next((s for s in (snapshot.get("stocks") or [])
                  if s.get("reserve_id") == reserve_id
                  and s.get("material_id") == material_id), None)
    if stock is None:
        checks.append({"rule": "reserve_stock", "passed": False,
                       "detail": "快照无 %s/%s 库存观测, 无法确认库存充足"
                                 % (reserve_id, material_id)})
    else:
        quantity = stock.get("quantity")
        if quantity is None or quantity < transfer_qty:
            checks.append({"rule": "reserve_stock", "passed": False,
                           "detail": "仓库库存不足: %s/%s 库存%s < 调拨量%d"
                                     % (reserve_id, material_id, quantity,
                                        transfer_qty)})
        else:
            checks.append({"rule": "reserve_stock", "passed": True,
                           "detail": "%s/%s 库存%d ≥ 调拨量%d, 满足"
                                     % (reserve_id, material_id, quantity,
                                        transfer_qty)})

    # ---- 规则2: 车辆 available 且 capacity ≥ transfer_qty ----
    vehicle = next((t for t in (snapshot.get("transports") or [])
                    if t.get("transport_id") == vehicle_id), None)
    if vehicle is None:
        checks.append({"rule": "vehicle", "passed": False,
                       "detail": "快照无车辆 %s 观测, 无法确认车辆可用"
                                 % vehicle_id})
    else:
        availability = vehicle.get("availability")
        capacity = vehicle.get("capacity")
        avail_ok = (availability or "").upper() == "AVAILABLE"
        cap_ok = capacity is not None and capacity >= transfer_qty
        if not avail_ok:
            checks.append({"rule": "vehicle", "passed": False,
                           "detail": "车辆不可用: %s 状态 %s"
                                     % (vehicle_id, availability)})
        elif not cap_ok:
            checks.append({"rule": "vehicle", "passed": False,
                           "detail": "车辆运力不足: %s capacity %s < 调拨量%d"
                                     % (vehicle_id, capacity, transfer_qty)})
        else:
            checks.append({"rule": "vehicle", "passed": True,
                           "detail": "车辆可用: %s AVAILABLE, capacity %d ≥ 调拨量%d"
                                     % (vehicle_id, capacity, transfer_qty)})

    # ---- 规则3: 路线 available 且耗时 ≤ remaining_time ----
    remaining_time = snapshot.get("remaining_time")
    if remaining_time is None:
        checks.append({"rule": "route", "passed": False,
                       "detail": "快照无 remaining_time, 无法判定路线时限"})
    else:
        route = next((r for r in (snapshot.get("routes") or [])
                      if r.get("route_id") == route_id), None)
        if route is None:
            checks.append({"rule": "route", "passed": False,
                           "detail": "快照无路线 %s 信息, 无法判定路线可用"
                                     % route_id})
        else:
            status_ok = (route.get("status") or "").lower() == "available"
            duration = route.get("duration_min")
            time_ok = duration is not None and duration <= remaining_time
            if not status_ok:
                checks.append({"rule": "route", "passed": False,
                               "detail": "路线不可用: %s 状态 %s"
                                         % (route_id, route.get("status"))})
            elif not time_ok:
                checks.append({"rule": "route", "passed": False,
                               "detail": "路线超时: %s 耗时%smin > remaining_time %smin"
                                         % (route_id, duration, remaining_time)})
            else:
                checks.append({"rule": "route", "passed": True,
                               "detail": "路线可用: %s 耗时%dmin ≤ remaining_time %dmin"
                                         % (route_id, duration, remaining_time)})

    failed = [c for c in checks if not c["passed"]]
    if not failed:
        status, summary = "executable", "全部判定通过, 可执行"
    else:
        status = "non_executable"
        summary = "；".join(c["detail"] for c in failed) + "，不可执行"
    return {
        "plan_id": plan["plan_id"],
        "plan_name": plan["plan_name"],
        "status": status,
        "reason_json": {"summary": summary, "checks": checks},
    }


def _result_to_dict(row) -> dict:
    d = dict(row)
    if d.get("reason_json"):
        try:
            d["reason_json"] = json.loads(d["reason_json"])
        except json.JSONDecodeError:
            pass  # 防御脏数据: 保留原文
    return d


def run_plan_judge(run_id: str) -> tuple:
    """读取 run 的候选方案逐条做可行性判定, 写入 plan_results (幂等)。

    - 只读 plan_definitions + runs.input_snapshot_json, 不读任何实时表
    - 幂等: 先 DELETE 该 run 旧 plan_results, 再重新计算写入 (同事务)
    - run 不存在 → 404; 快照损坏 → 500; 无候选方案 → 清旧结果并友好提示
    - 业务红线: 不触碰 task_results / dependency_checks / runs 任何状态
    返回 (ok, http_status, msg, data); data =
    {run_id, run_status, plan_count, executable_count,
     non_executable_count, results: [...]}。
    """
    try:
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                return False, 404, "run %s not found" % run_id, None
            try:
                snapshot = json.loads(row["input_snapshot_json"])
            except (TypeError, ValueError):
                return False, 500, (
                    "run %s input_snapshot_json invalid" % run_id), None
            if not isinstance(snapshot, dict):
                return False, 500, (
                    "run %s input_snapshot_json invalid" % run_id), None

            plans = conn.execute(
                "SELECT * FROM plan_definitions WHERE run_id=? "
                "ORDER BY plan_id",
                (run_id,),
            ).fetchall()

            # 幂等: 先清除该 run 旧判定结果, 避免残留
            conn.execute("DELETE FROM plan_results WHERE run_id=?", (run_id,))
            if not plans:
                conn.commit()
                return True, 200, (
                    "run %s 无候选调拨方案, 未写入判定结果 (请先执行 "
                    "python plan_store.py %s), 已清理该 run 旧 plan_results"
                    % (run_id, run_id)), {
                    "run_id": run_id,
                    "run_status": row["run_status"],
                    "plan_count": 0,
                    "executable_count": 0,
                    "non_executable_count": 0,
                    "results": [],
                }

            verdicts = [_judge_plan(p, snapshot) for p in plans]
            for v in verdicts:
                conn.execute(
                    "INSERT INTO plan_results (run_id, plan_id, status, "
                    "reason_json) VALUES (?,?,?,?)",
                    (run_id, v["plan_id"], v["status"],
                     json.dumps(v["reason_json"], ensure_ascii=False)),
                )
            conn.commit()

            rows = conn.execute(
                "SELECT * FROM plan_results WHERE run_id=? ORDER BY plan_id",
                (run_id,),
            ).fetchall()
        finally:
            close_conn(conn)
    except sqlite3.Error as exc:
        return False, 500, "db error: %s" % exc, None

    results = []
    for v in verdicts:
        r = next((x for x in rows if x["plan_id"] == v["plan_id"]), None)
        entry = dict(v)
        entry["created_at"] = r["created_at"] if r is not None else None
        results.append(entry)
    exe_n = sum(1 for v in verdicts if v["status"] == "executable")
    return True, 200, (
        "判定完成: %d 套方案, %d 可执行, %d 不可执行"
        % (len(verdicts), exe_n, len(verdicts) - exe_n)), {
        "run_id": run_id,
        "run_status": row["run_status"],
        "plan_count": len(results),
        "executable_count": exe_n,
        "non_executable_count": len(results) - exe_n,
        "results": results,
    }


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("usage: python plan_judge.py <run_id>", file=sys.stderr)
        sys.exit(2)
    ok, http_status, msg, data = run_plan_judge(sys.argv[1])
    print(json.dumps({"ok": ok, "http_status": http_status, "msg": msg,
                      "data": data}, ensure_ascii=False, indent=2))
    sys.exit(0 if ok else 1)
