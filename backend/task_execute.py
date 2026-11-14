"""阶段03板块4: execute 编排器 — T1~T5 串行执行 + 统一落库 + run 完成.

继承板块3 的 execute 保护逻辑 (404/409/快照解析/原子完成闸门), 泛化到
T1~T5: 读取 task_definitions 静态定义 → 依次串行执行各任务纯判断
(只读快照) → 每个任务独立写 dependency_checks + task_results →
全部完成后 run 置 completed。整套执行在同一事务内。

约束:
    - 不新增表/不改表结构/常量/触发器 (复用板块1 表与状态体系)
    - 校验只读 input_snapshot_json, 不读实时观测表
    - T1 判断逻辑 (t1_check.check_t1) 保持板块3 不变
"""

import json
import sqlite3
from datetime import datetime

from db import close_conn, get_conn
from t1_check import align_t1_meta, check_t1
from t2_check import check_t2
from t3_check import check_t3
from t4_check import check_t4
from t5_check import check_t5

# 执行顺序固定 T1 → T2 → T3 → T4 → T5
_CHECKERS = (check_t1, check_t2, check_t3, check_t4, check_t5)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _check_row_to_dict(row) -> dict:
    d = dict(row)
    if d.get("evidence"):
        try:
            d["evidence"] = json.loads(d["evidence"])
        except json.JSONDecodeError:
            pass  # 防御脏数据: 保留原文
    return d


def run_execute_all(run_id: str) -> tuple:
    """执行 T1~T5 全套校验并落库, 完成后 run 置 completed。

    流程: 读 runs 行 (不存在 → 404; 非 pending → 409) → 解析快照
    (损坏 → 500) → 读 task_definitions 静态定义 → 串行执行 T1~T5
    → 同事务写全部 dependency_checks + task_results
    → UPDATE runs ... WHERE run_status='pending' 原子置 completed
    (rowcount=0 → 整体回滚 409, 与触发器一起保证 completed 后修改被拒)。

    返回 (ok, http_status, msg, data); data =
    {run_id, scenario_id, run_type, run_status, created_at, completed_at,
     tasks: [{task_id, task_name, result_status, summary_reason,
              dependency_checks: [...]}]}
    """
    try:
        conn = get_conn()
        try:
            align_t1_meta(conn)  # 板块3 行为保留: T1 静态描述对齐
            row = conn.execute(
                "SELECT * FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                return False, 404, "run %s not found" % run_id, None
            if row["run_status"] != "pending":
                return False, 409, (
                    "run %s is completed, not modifiable" % run_id), None

            try:
                snapshot = json.loads(row["input_snapshot_json"])
            except (TypeError, ValueError):
                return False, 500, "run %s input_snapshot_json invalid" % run_id, None
            if not isinstance(snapshot, dict):
                return False, 500, "run %s input_snapshot_json invalid" % run_id, None

            # 读取 task_definitions 静态定义 (任务编号/名称; 静态配置, 非实时数据)
            def_names = {
                r["task_id"]: r["task_name"] for r in conn.execute(
                    "SELECT task_id, task_name FROM task_definitions"
                ).fetchall()
            }

            # ---- 串行执行 T1~T5 (纯判断, 只读快照) + 逐任务落库 (同一事务) ----
            results = []
            for checker in _CHECKERS:
                res = checker(snapshot)
                task_id = res["task_id"]
                for c in res["checks"]:
                    conn.execute(
                        "INSERT INTO dependency_checks "
                        "(run_id, task_id, dep_key, dep_desc, status, evidence, reason) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (run_id, task_id, c["dep_key"], c["dep_desc"], c["status"],
                         json.dumps(c["evidence"], ensure_ascii=False), c["reason"]),
                    )
                conn.execute(
                    "INSERT INTO task_results "
                    "(run_id, task_id, result_status, summary_reason) "
                    "VALUES (?,?,?,?)",
                    (run_id, task_id, res["task_status"], res["summary_reason"]),
                )
                results.append(res)

            # ---- 原子完成闸门: 全部任务落库后一次性置 completed ----
            completed_at = _now()
            cur_run = conn.execute(
                "UPDATE runs SET run_status='completed', completed_at=? "
                "WHERE run_id=? AND run_status='pending'",
                (completed_at, run_id),
            )
            if cur_run.rowcount == 0:
                conn.rollback()
                return False, 409, (
                    "run %s is completed, not modifiable" % run_id), None
            conn.commit()

            # ---- 组装响应 (读回本事务落库结果) ----
            tasks_out = []
            for res in results:
                checks = conn.execute(
                    "SELECT * FROM dependency_checks "
                    "WHERE run_id=? AND task_id=? ORDER BY id",
                    (run_id, res["task_id"]),
                ).fetchall()
                tasks_out.append({
                    "task_id": res["task_id"],
                    "task_name": def_names.get(res["task_id"], res["task_id"]),
                    "result_status": res["task_status"],
                    "summary_reason": res["summary_reason"],
                    "dependency_checks": [_check_row_to_dict(c) for c in checks],
                })
            return True, 200, "executed", {
                "run_id": run_id,
                "scenario_id": row["scenario_id"],
                "run_type": row["run_type"],
                "run_status": "completed",
                "created_at": row["created_at"],
                "completed_at": completed_at,
                "tasks": tasks_out,
            }
        finally:
            close_conn(conn)
    except sqlite3.Error as exc:
        return False, 500, "db error: %s" % exc, None
