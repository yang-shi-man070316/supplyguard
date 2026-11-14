"""阶段04板块2: 候选调拨方案落库 (plan_definitions).

固定 3 套候选调拨方案 (均调拨物资 W):
    PLAN-A: R1 + V1 + route-A
    PLAN-B: R2 + V2 + route-B
    PLAN-C: R1 + V1 + route-C

规则 (严格按阶段04板块2规格):
    1. transfer_qty 统一取自当前 run 的 blockers: material_id='W' 的
       blocker 缺口 gap 之和 (三套方案互为候选, 调拨量一致; 多服务点
       缺口时按总量补齐)。
    2. 方案基础信息从 run.input_snapshot_json 读取 (只读):
       仓库余量 (stocks 中 reserve_id+material_id 条目)、
       车辆信息 (transports 中 vehicle_id 条目)、
       路线信息与 remaining_time (快照 routes 字段; 当前快照无路线
       数据, 防御性读取, 读不到返回 None, 不臆造数值)。
       表 plan_definitions 无对应列, 这些信息随返回值/CLI 输出展示,
       供板块3 可行性判断参考。
    3. 只做方案入库, 不做可行性判断, 不写 plan_results (板块3 内容)。
    4. 幂等: 同一 run 同一 plan_name 已存在则跳过, 重复执行不产生
       重复方案数据 (应用层存在性检查; 不改表结构/迁移)。
    5. 数据来源: 只读 blockers 表 + runs.input_snapshot_json, 不读
       demand_records / stocks 等实时观测表; run 无 W 物资 blocker
       时跳过写入, 友好提示, 不脏写数据库。
    6. 不新增对外 API 接口 (CLI: python plan_store.py <run_id>)。

与板块1 的关系: 只读 blockers (由 gap_check 生成), 不改动 gap_check.py
及其写入逻辑。
"""

import json
import sqlite3

from db import close_conn, get_conn

PLAN_MATERIAL_ID = "W"

# 固定 3 套候选方案定义 (任务规格给定, 均为 W 物资)
PLAN_TEMPLATES = (
    {"plan_name": "PLAN-A", "reserve_id": "R1",
     "vehicle_id": "V1", "route_id": "route-A"},
    {"plan_name": "PLAN-B", "reserve_id": "R2",
     "vehicle_id": "V2", "route_id": "route-B"},
    {"plan_name": "PLAN-C", "reserve_id": "R1",
     "vehicle_id": "V1", "route_id": "route-C"},
)


def _w_gap_total(conn, run_id):
    """当前 run blockers 中 W 物资的缺口合计; 无 W blocker 返回 None。

    blockers 只存 gap>0 的行 (板块1 口径), SUM 为空按 0 处理。
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(gap), 0) FROM blockers "
        "WHERE run_id=? AND material_id=?",
        (run_id, PLAN_MATERIAL_ID),
    ).fetchone()
    total = row[0]
    return total if total > 0 else None


def _snapshot_context(snapshot, reserve_id, vehicle_id, material_id, route_id):
    """从快照读取方案基础信息 (只读, 不落库, 供展示/板块3 参考)。

    - 仓库余量: stocks 中 (reserve_id, material_id) 匹配条目
      (快照 stocks 本身即每组合最新一条观测)
    - 车辆信息: transports 中 vehicle_id 匹配条目
      (快照 transports 本身即每资源最新一条观测)
    - 路线信息 / remaining_time: 快照 routes 字段防御性读取;
      当前快照无路线数据 → route=None, remaining_time=None (不臆造)
    """
    stocks = snapshot.get("stocks") or []
    transports = snapshot.get("transports") or []
    reserve_stock = next((s for s in stocks
                          if s.get("reserve_id") == reserve_id
                          and s.get("material_id") == material_id), None)
    vehicle = next((t for t in transports
                    if t.get("transport_id") == vehicle_id), None)
    route = next((r for r in (snapshot.get("routes") or [])
                  if r.get("route_id") == route_id), None)
    return {
        "reserve_stock": reserve_stock,
        "vehicle": vehicle,
        "route": route,
        "remaining_time": (route or {}).get("remaining_time"),
    }


def _plan_to_dict(row) -> dict:
    return dict(row) if row is not None else None


def save_candidate_plans(run_id: str) -> tuple:
    """将 3 套固定候选调拨方案写入 plan_definitions (幂等, 同事务)。

    - run 不存在 → (False, 404); 快照缺失/损坏 → (False, 500)
    - 无 W 物资 blocker → 不写任何行, 友好提示 (True, 200)
    - 同 run 同 plan_name 已存在 → 跳过 (重复执行不产生重复方案)
    - run 状态不限 (pending/completed 均可; 快照自创建起冻结)
    返回 (ok, http_status, msg, data); data =
    {run_id, run_status, transfer_qty, plan_count,
     inserted: [新增方案详情], skipped: [已存在跳过的方案],
     plans: [该 run 现存全部方案]}。
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

            transfer_qty = _w_gap_total(conn, run_id)
            if transfer_qty is None:
                total_blockers = conn.execute(
                    "SELECT COUNT(*) FROM blockers WHERE run_id=?",
                    (run_id,),
                ).fetchone()[0]
                if total_blockers == 0:
                    msg = ("run %s 无 blocker 缺口记录, 未写入调拨方案 "
                           "(请先执行 python gap_check.py %s)" % (run_id, run_id))
                else:
                    msg = ("run %s 的 blockers 无 material_id='W' 缺口, "
                           "无需 W 物资调拨方案, 未写入任何数据" % run_id)
                return True, 200, msg, {
                    "run_id": run_id,
                    "run_status": row["run_status"],
                    "transfer_qty": None,
                    "plan_count": 0,
                    "plans": [],
                    "inserted": [],
                    "skipped": [],
                }

            existing = {r[0] for r in conn.execute(
                "SELECT plan_name FROM plan_definitions WHERE run_id=?",
                (run_id,),
            ).fetchall()}
            inserted, skipped = [], []
            for tpl in PLAN_TEMPLATES:
                plan_name = tpl["plan_name"]
                detail = dict(tpl, material_id=PLAN_MATERIAL_ID,
                              transfer_qty=transfer_qty)
                if plan_name in existing:
                    skipped.append(detail)
                    continue
                cur = conn.execute(
                    "INSERT INTO plan_definitions "
                    "(run_id, plan_name, reserve_id, vehicle_id, route_id, "
                    "material_id, transfer_qty) VALUES (?,?,?,?,?,?,?)",
                    (run_id, plan_name, tpl["reserve_id"], tpl["vehicle_id"],
                     tpl["route_id"], PLAN_MATERIAL_ID, transfer_qty),
                )
                detail["plan_id"] = cur.lastrowid
                detail["context"] = _snapshot_context(
                    snapshot, tpl["reserve_id"], tpl["vehicle_id"],
                    PLAN_MATERIAL_ID, tpl["route_id"])
                inserted.append(detail)
            conn.commit()

            plans = conn.execute(
                "SELECT * FROM plan_definitions WHERE run_id=? "
                "ORDER BY plan_id",
                (run_id,),
            ).fetchall()
        finally:
            close_conn(conn)
    except sqlite3.Error as exc:
        return False, 500, "db error: %s" % exc, None

    if not inserted:
        msg = ("run %s 的 3 套候选方案已存在, 跳过写入 (幂等), "
               "未产生重复数据" % run_id)
    elif not skipped:
        msg = ("run %s 写入 3 套候选调拨方案 "
               "(transfer_qty=%d)" % (run_id, transfer_qty))
    else:
        msg = ("run %s 写入 %d 套方案, 跳过已存在 %d 套"
               % (run_id, len(inserted), len(skipped)))
    return True, 200, msg, {
        "run_id": run_id,
        "run_status": row["run_status"],
        "transfer_qty": transfer_qty,
        "plan_count": len(plans),
        "plans": [_plan_to_dict(r) for r in plans],
        "inserted": inserted,
        "skipped": skipped,
    }


def get_plans(run_id: str) -> tuple:
    """查询某 run 的全部候选方案记录 (只读)。run 不存在 → 404。"""
    try:
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                return False, 404, "run %s not found" % run_id, None
            rows = conn.execute(
                "SELECT * FROM plan_definitions WHERE run_id=? "
                "ORDER BY plan_id",
                (run_id,),
            ).fetchall()
        finally:
            close_conn(conn)
    except sqlite3.Error as exc:
        return False, 500, "db error: %s" % exc, None
    return True, 200, "ok", {
        "run_id": run_id,
        "run_status": row["run_status"],
        "plan_count": len(rows),
        "plans": [_plan_to_dict(r) for r in rows],
    }


def get_plans_with_results(run_id: str) -> tuple:
    """合并查询候选方案 + 可行性判定结果 (阶段04板块4 只读接口用)。

    plan_definitions LEFT JOIN plan_results: 每条方案携带 status
    (executable / non_executable, 未判定为 None) 与结构化 reason_json。
    run 不存在 → 404。
    """
    try:
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT run_id, run_status FROM runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if row is None:
                return False, 404, "run %s not found" % run_id, None
            rows = conn.execute(
                "SELECT pd.plan_id, pd.plan_name, pd.reserve_id, pd.vehicle_id, "
                "pd.route_id, pd.material_id, pd.transfer_qty, pd.created_at, "
                "pr.status, pr.reason_json, pr.created_at AS verdict_created_at "
                "FROM plan_definitions pd "
                "LEFT JOIN plan_results pr "
                "ON pr.run_id = pd.run_id AND pr.plan_id = pd.plan_id "
                "WHERE pd.run_id=? ORDER BY pd.plan_id",
                (run_id,),
            ).fetchall()
        finally:
            close_conn(conn)
    except sqlite3.Error as exc:
        return False, 500, "db error: %s" % exc, None

    plans = []
    for r in rows:
        reason = r["reason_json"]
        if reason:
            try:
                reason = json.loads(reason)
            except json.JSONDecodeError:
                pass  # 防御脏数据: 保留原文
        plans.append({
            "plan_id": r["plan_id"],
            "plan_name": r["plan_name"],
            "reserve_id": r["reserve_id"],
            "vehicle_id": r["vehicle_id"],
            "route_id": r["route_id"],
            "material_id": r["material_id"],
            "transfer_qty": r["transfer_qty"],
            "status": r["status"],
            "reason_json": reason,
            "created_at": r["created_at"],
            "verdict_created_at": r["verdict_created_at"],
        })
    return True, 200, "ok", {
        "run_id": run_id,
        "run_status": row["run_status"],
        "plan_count": len(plans),
        "plans": plans,
    }


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("usage: python plan_store.py <run_id>", file=sys.stderr)
        sys.exit(2)
    ok, http_status, msg, data = save_candidate_plans(sys.argv[1])
    print(json.dumps({"ok": ok, "http_status": http_status, "msg": msg,
                      "data": data}, ensure_ascii=False, indent=2))
    sys.exit(0 if ok else 1)
