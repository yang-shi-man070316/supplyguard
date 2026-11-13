"""B-B03 独立任务: run 记录保护.

run 存在状态字段 (pending / completed)。核心规则:
    - 查询: 任意状态的 run 均可读
    - 更新: 仅 pending 允许; completed 的 run 拒绝任何修改, 数据库不改动
    - 新增: 不受限制 (默认 pending)

防竞态要点: 更新/完成语句自带 status='pending' 条件, 状态校验与写库在
同一原子 UPDATE 内完成——即使"先查"与"写"之间 run 被 complete, rowcount=0
同样拒绝, 不存在"查了 pending 却覆盖 completed"的窗口。
"""

import json
import sqlite3
from datetime import datetime

from db import close_conn, get_conn


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _row_to_dict(row) -> dict:
    d = dict(row)
    if d.get("payload"):
        try:
            d["payload"] = json.loads(d["payload"])
        except json.JSONDecodeError:
            pass  # 保留原文
    return d


def create_run(run_id: str, payload: dict = None) -> tuple:
    """新增 run (status=pending)。run_id 已存在则拒绝, 不覆盖。"""
    try:
        conn = get_conn()
        try:
            conn.execute(
                "INSERT INTO runs (run_id, status, payload, created_at) VALUES (?,?,?,?)",
                (run_id, "pending",
                 json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                 _now()),
            )
            conn.commit()
        finally:
            close_conn(conn)
    except sqlite3.IntegrityError:
        return False, "run %s already exists" % run_id
    except sqlite3.Error as exc:
        return False, "db error: %s" % exc
    return True, "created"


def get_run(run_id: str):
    """查询单个 run (任意状态均可读)。不存在返回 None。"""
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    finally:
        close_conn(conn)
    return _row_to_dict(row) if row is not None else None


def list_runs() -> list:
    """全部 run 列表 (任意状态均可读), 按 created_at 倒序 (新在前)。

    同秒创建的 run 用 rowid 倒序兜底, 保证结果稳定。
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY created_at DESC, rowid DESC"
        ).fetchall()
    finally:
        close_conn(conn)
    return [_row_to_dict(r) for r in rows]


def update_run(run_id: str, payload: dict) -> tuple:
    """更新 run 内容: 仅 pending 允许; completed 拒绝且不改动数据库。"""
    try:
        conn = get_conn()
        try:
            row = conn.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                return False, "run %s not found" % run_id
            if row["status"] == "completed":
                return False, "run %s is completed, not modifiable" % run_id
            cur = conn.execute(
                "UPDATE runs SET payload=?, updated_at=? WHERE run_id=? AND status='pending'",
                (json.dumps(payload, ensure_ascii=False), _now(), run_id),
            )
            if cur.rowcount == 0:  # 查与写之间被 complete 的竞态兜底
                return False, "run %s is completed, not modifiable" % run_id
            conn.commit()
        finally:
            close_conn(conn)
    except sqlite3.Error as exc:
        return False, "db error: %s" % exc
    return True, "updated"


def complete_run(run_id: str) -> tuple:
    """pending → completed 状态转换; 已 completed 或不存在均拒绝。"""
    try:
        conn = get_conn()
        try:
            cur = conn.execute(
                "UPDATE runs SET status='completed', updated_at=? "
                "WHERE run_id=? AND status='pending'",
                (_now(), run_id),
            )
            conn.commit()
        finally:
            close_conn(conn)
    except sqlite3.Error as exc:
        return False, "db error: %s" % exc
    if cur.rowcount == 0:
        if get_run(run_id) is None:
            return False, "run %s not found" % run_id
        return False, "run %s is completed, not modifiable" % run_id
    return True, "completed"


# ============================================================
# B-B03 阶段03板块3: 通用依赖校验框架 + T1 (仅 T1, 禁 T2~T5)
# 框架: seed 元数据 → 读证据快照 → 逐依赖判断 → 写 dependency_checks
#       → 汇总写 task_results。依赖判断函数只实现 T1-D1。
# ============================================================

T1_TASK_ID = "T1"
T1_TASK_NAME = "关键服务点最低需求保障"
T1_TASK_DESC = "全部关键服务点当期最低需求均可被当前库存保障"
T1_DEPS = (
    {
        "dep_id": "T1-D1",
        "dep_name": "关键节点库存≥当期最低需求",
        "dep_desc": "每个服务点每种物资的当期最低需求 ≤ 当前全网该物资库存观测总量",
    },
)


def seed_t1_meta() -> tuple:
    """插入 T1 任务与依赖定义。幂等: 重复执行不重复插入、不覆盖已有行。"""
    try:
        conn = get_conn()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO task_definitions "
                "(task_id, task_name, task_desc, created_at) VALUES (?,?,?,?)",
                (T1_TASK_ID, T1_TASK_NAME, T1_TASK_DESC, _now()),
            )
            for dep in T1_DEPS:
                conn.execute(
                    "INSERT OR IGNORE INTO dependency_definitions "
                    "(dep_id, task_id, dep_name, dep_desc, created_at) VALUES (?,?,?,?,?)",
                    (dep["dep_id"], T1_TASK_ID, dep["dep_name"], dep["dep_desc"], _now()),
                )
            conn.commit()
        finally:
            close_conn(conn)
    except sqlite3.Error as exc:
        return False, "db error: %s" % exc
    return True, "seeded"


def _latest_demands(conn) -> list:
    """每个 (service_id, material_id) 的最新一条需求观测 (按最大 demand_id)。"""
    rows = conn.execute(
        "SELECT service_id, material_id, required_quantity, observed_at "
        "FROM demand_records d "
        "WHERE d.demand_id = ("
        "  SELECT MAX(d2.demand_id) FROM demand_records d2 "
        "  WHERE d2.service_id = d.service_id AND d2.material_id = d.material_id)"
    ).fetchall()
    return [dict(r) for r in rows]


def _latest_stock_totals(conn) -> dict:
    """每个 (reserve_id, material_id) 最新库存观测, 按物资全网求和。"""
    rows = conn.execute(
        "SELECT material_id, quantity FROM stock_observations s "
        "WHERE s.stock_id = ("
        "  SELECT MAX(s2.stock_id) FROM stock_observations s2 "
        "  WHERE s2.reserve_id = s.reserve_id AND s2.material_id = s.material_id)"
    ).fetchall()
    totals = {}
    for r in rows:
        totals[r["material_id"]] = totals.get(r["material_id"], 0) + r["quantity"]
    return totals


def _check_dep_t1d1(conn) -> tuple:
    """T1-D1 判断: 每个服务点每种物资的当期需求 ≤ 全网该物资库存。

    返回 (status, msg), status ∈ satisfied / unsatisfied / unknown。
    无任何需求观测判 unknown; 某物资无库存观测按库存 0 计 (与阶段02
    缺口语义一致: 缺口 = 需求 − 全网该物资库存)。
    """
    demands = _latest_demands(conn)
    if not demands:
        return "unknown", "无服务点需求观测记录, 无法判断"
    stocks = _latest_stock_totals(conn)
    shortfalls = []
    ok_count = 0
    for d in demands:
        total = stocks.get(d["material_id"], 0)
        if total >= d["required_quantity"]:
            ok_count += 1
        else:
            shortfalls.append(
                "%s %s 缺口%d (需求%d, 库存%d)"
                % (d["service_id"], d["material_id"],
                   d["required_quantity"] - total, d["required_quantity"], total)
            )
    if shortfalls:
        return "unsatisfied", "; ".join(shortfalls)
    return "satisfied", "全部%d项需求均满足 (关键节点库存≥当期最低需求)" % ok_count


# 依赖校验分发表: dep_id → 判断函数。T2~T5 的依赖不在此实现。
_T1_DEP_CHECKERS = {
    "T1-D1": _check_dep_t1d1,
}


def run_t1_check(run_id: str) -> tuple:
    """执行 T1 全部依赖校验, 结果落库 dependency_checks + task_results。

    - 仅 status=pending 的 run 允许执行; completed / 不存在直接拒绝。
    - 落库用 INSERT...SELECT FROM runs WHERE status='pending' 原子控制:
      预检与写库之间 run 被 complete 时 rowcount=0, 整体 rollback, 不写任何数据。
    - 同一 run 重复执行时覆盖旧结果 (每 run 每依赖/每任务只保留最新一次)。

    返回 (ok, msg, data); data = {task_id, task_status, deps:[{dep_id,status,check_msg}]}
    """
    seed_t1_meta()  # 幂等保底: 元数据缺失时补种
    try:
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT status FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                return False, "run %s not found" % run_id, None
            if row["status"] != "pending":
                return False, "run %s is completed, not modifiable" % run_id, None

            deps = conn.execute(
                "SELECT dep_id FROM dependency_definitions WHERE task_id=? ORDER BY dep_id",
                (T1_TASK_ID,),
            ).fetchall()
            if not deps:
                return False, "task %s metadata missing" % T1_TASK_ID, None

            dep_results = []
            for d in deps:
                dep_id = d["dep_id"]
                checker = _T1_DEP_CHECKERS.get(dep_id)
                if checker is None:
                    dep_results.append({
                        "dep_id": dep_id, "status": "unknown",
                        "check_msg": "依赖 %s 的判断逻辑未实现" % dep_id,
                    })
                else:
                    status, check_msg = checker(conn)
                    dep_results.append(
                        {"dep_id": dep_id, "status": status, "check_msg": check_msg}
                    )

            if any(r["status"] == "unsatisfied" for r in dep_results):
                task_status = "blocked"
            elif all(r["status"] == "satisfied" for r in dep_results):
                task_status = "established"
            else:
                task_status = "unknown"
            task_msg = "; ".join(
                "%s:%s" % (r["dep_id"], r["status"]) for r in dep_results
            )

            # ---- 原子落库: 写库瞬间要求 run 仍为 pending, 否则整体回滚 ----
            new_check_ids = []  # (dep_id, 新行 check_id), 用于覆盖旧结果
            for r in dep_results:
                cur = conn.execute(
                    "INSERT INTO dependency_checks "
                    "(run_id, dep_id, status, check_msg, created_at) "
                    "SELECT ?,?,?,?,? FROM runs WHERE run_id=? AND status='pending'",
                    (run_id, r["dep_id"], r["status"], r["check_msg"], _now(), run_id),
                )
                if cur.rowcount == 0:
                    conn.rollback()
                    return False, "run %s is completed, not modifiable" % run_id, None
                new_check_ids.append((r["dep_id"], cur.lastrowid))
            cur_results = conn.execute(
                "INSERT INTO task_results (run_id, task_id, status, result_msg, created_at) "
                "SELECT ?,?,?,?,? FROM runs WHERE run_id=? AND status='pending'",
                (run_id, T1_TASK_ID, task_status, task_msg, _now(), run_id),
            )
            if cur_results.rowcount == 0:
                conn.rollback()
                return False, "run %s is completed, not modifiable" % run_id, None
            # 覆盖旧结果: 同一 run 的同一依赖/任务只保留本次新写入的行
            for dep_id, check_id in new_check_ids:
                conn.execute(
                    "DELETE FROM dependency_checks "
                    "WHERE run_id=? AND dep_id=? AND check_id<?",
                    (run_id, dep_id, check_id),
                )
            conn.execute(
                "DELETE FROM task_results WHERE run_id=? AND task_id=? AND result_id<?",
                (run_id, T1_TASK_ID, cur_results.lastrowid),
            )
            conn.commit()
        finally:
            close_conn(conn)
    except sqlite3.Error as exc:
        return False, "db error: %s" % exc, None
    return True, "checked", {
        "run_id": run_id,
        "task_id": T1_TASK_ID,
        "task_status": task_status,
        "deps": dep_results,
    }
