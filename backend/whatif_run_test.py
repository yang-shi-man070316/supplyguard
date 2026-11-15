"""阶段07板块1: what-if 假设推演自测脚本 (临时库隔离).

覆盖用例 (TC-07-*):
    TC-07-01 常量: RUN_TYPE_WHATIF / RUN_TYPES 定义正确
    TC-07-02 apply_assumptions 三类假设 key 应用正确 (transport /
              stock / remaining_minutes)
    TC-07-03 apply_assumptions 非法输入: 未知 key / 非法状态值 /
              负数量 / 目标不存在 → ValueError, 且不修改入参快照
    TC-07-04 create_run reality 不接受 assumptions; what-if 缺
              parent_run_id / assumptions → ValueError
    TC-07-05 create_run what-if 字段绑定: run_type=what-if /
              parent_run_id / assumptions_json / 快照 = 父副本 + 假设
    TC-07-06 parent 不存在 → 404; parent 非 reality → 400
    TC-07-07 parent 基线零污染: 创建 what-if 前后 parent 行
              input_snapshot_json 字节级一致
    TC-07-08 what-if execute: T1~T5 判定独立落库 (dependency_checks /
              task_results 按 run_id 隔离, parent 判定行不变)
    TC-07-09 基线方案判定: V2=FAULT 时 PLAN-A 可执行 / PLAN-B、
              PLAN-C 不可执行
    TC-07-10 推演A {"transport.V2.availability":"AVAILABLE"} →
              PLAN-B 可执行
    TC-07-11 推演B {"remaining_minutes":50} → PLAN-C 可执行
    TC-07-12 推演C {"stock.R1.W":2} → PLAN-A、PLAN-C 不可执行
    TC-07-13 三组推演前后 parent 六表指纹一致 (无污染验证)
    TC-07-14 what-if run 完成后快照冻结 (触发器拒绝 UPDATE)

隔离说明: 将 db.DB_PATH 指向临时库, 全部测试数据只落在临时库,
不读写真实 bb03.db (import db 时对真实库的 init_db 为启动既有行为,
IF NOT EXISTS 幂等, 不产生/污染任何测试数据)。

用法 (backend 目录下):
    python whatif_run_test.py
退出码: 0 = 全部通过; 1 = 存在失败用例。
"""

import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db  # noqa: E402  import 即 init_db() (真实库幂等建表, 启动既有行为)
from constants import RUN_TYPE, RUN_TYPE_WHATIF, RUN_TYPES  # noqa: E402
from inject_s3_snapshot import inject_full_doc  # noqa: E402
from run_store_v2 import apply_assumptions, create_run  # noqa: E402
from whatif_run import (  # noqa: E402
    capture_fingerprint,
    run_whatif_pipeline,
    verify_fingerprint,
)

_PASS = 0
_FAIL = 0


def check(name, cond, detail=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("[PASS] %s" % name)
    else:
        _FAIL += 1
        print("[FAIL] %s %s" % (name, detail))


def expect_value_error(name, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except ValueError:
        check(name, True)
        return True
    check(name, False, "expected ValueError, none raised")
    return False


def snapshot_json(run_id):
    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT input_snapshot_json FROM runs WHERE run_id=?",
            (run_id,)).fetchone()
    finally:
        db.close_conn(conn)
    return row["input_snapshot_json"] if row else None


def verdicts(run_id):
    """该 run 方案判定压缩视图 {plan_name: status}。"""
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT pd.plan_name, pr.status FROM plan_definitions pd "
            "LEFT JOIN plan_results pr "
            "ON pr.run_id=pd.run_id AND pr.plan_id=pd.plan_id "
            "WHERE pd.run_id=? ORDER BY pd.plan_id", (run_id,)).fetchall()
    finally:
        db.close_conn(conn)
    return {r["plan_name"]: r["status"] for r in rows}


def main():
    # ---- 临时库隔离 ----
    tmp_dir = tempfile.mkdtemp(prefix="whatif_run_test_")
    db.DB_PATH = os.path.join(tmp_dir, "test.db")
    db.init_db()
    print("test db: %s" % db.DB_PATH)

    # ================= TC-07-01 常量 =================
    check("TC-07-01 常量: RUN_TYPE_WHATIF/RUN_TYPES 定义",
          RUN_TYPE_WHATIF == "what-if"
          and RUN_TYPES == (RUN_TYPE, RUN_TYPE_WHATIF),
          "RUN_TYPES=%r" % (RUN_TYPES,))

    # ================= TC-07-02/03 apply_assumptions =================
    snap = {
        "stocks": [{"reserve_id": "R1", "material_id": "W", "quantity": 6},
                   {"reserve_id": "R2", "material_id": "W", "quantity": 5}],
        "transports": [
            {"transport_id": "V1", "availability": "AVAILABLE", "capacity": 4},
            {"transport_id": "V2", "availability": "FAULT", "capacity": 4}],
        "routes": [{"route_id": "route-A", "status": "available",
                    "duration_min": 20}],
        "remaining_time": 30,
    }
    out_a = apply_assumptions(
        snap, {"transport.V2.availability": "AVAILABLE"})
    check("TC-07-02a transport 假设应用: V2 FAULT → AVAILABLE",
          out_a["transports"][1]["availability"] == "AVAILABLE",
          "got %r" % out_a["transports"])
    out_b = apply_assumptions(snap, {"remaining_minutes": 50})
    check("TC-07-02b remaining_minutes 假设应用: remaining_time 30 → 50",
          out_b["remaining_time"] == 50, "got %r" % out_b["remaining_time"])
    out_c = apply_assumptions(snap, {"stock.R1.W": 2})
    check("TC-07-02c stock 假设应用: R1/W 6 → 2",
          next(s for s in out_c["stocks"] if s["reserve_id"] == "R1")[
              "quantity"] == 2,
          "got %r" % out_c["stocks"])
    check("TC-07-02d 假设不修改入参快照 (原快照保持 FAULT/30/6)",
          snap["transports"][1]["availability"] == "FAULT"
          and snap["remaining_time"] == 30
          and next(s for s in snap["stocks"] if s["reserve_id"] == "R1")[
              "quantity"] == 6,
          "snap mutated: %r" % snap)

    expect_value_error("TC-07-03a 未知假设 key → ValueError",
                       apply_assumptions, snap, {"bogus.key": 1})
    expect_value_error("TC-07-03b 非法运输状态值 → ValueError",
                       apply_assumptions, snap,
                       {"transport.V2.availability": "BROKEN"})
    expect_value_error("TC-07-03c 库存负值 → ValueError",
                       apply_assumptions, snap, {"stock.R1.W": -1})
    expect_value_error("TC-07-03d remaining_minutes 非整数 → ValueError",
                       apply_assumptions, snap, {"remaining_minutes": "50"})
    expect_value_error("TC-07-03e 目标运输不存在 → ValueError",
                       apply_assumptions, snap,
                       {"transport.V9.availability": "AVAILABLE"})
    expect_value_error("TC-07-03f 目标库存不存在 → ValueError",
                       apply_assumptions, snap, {"stock.R9.W": 2})

    # ================= TC-07-04 create_run 入参校验 =================
    expect_value_error("TC-07-04a reality 不接受 assumptions → ValueError",
                       create_run, "S3", None, RUN_TYPE,
                       {"remaining_minutes": 50})
    expect_value_error("TC-07-04b what-if 缺 parent_run_id → ValueError",
                       create_run, "S3", None, RUN_TYPE_WHATIF,
                       {"remaining_minutes": 50})
    expect_value_error("TC-07-04c what-if 缺 assumptions → ValueError",
                       create_run, "S3", "BB03-RUN-999", RUN_TYPE_WHATIF,
                       None)
    ok, st, msg, data = create_run("S3", "BB03-RUN-999", RUN_TYPE_WHATIF,
                                   {"remaining_minutes": 50})
    check("TC-07-06a parent 不存在 → 404 语义",
          ok is False and st == 404,
          "got ok=%r st=%r msg=%r" % (ok, st, msg))

    # ---- S3 基线准备 (临时库内) ----
    ok, st, msg, base = create_run("S3", None, RUN_TYPE)
    check("TC-07-09a S3 reality run 创建", ok, "msg=%r" % msg)
    base_id = base["run_id"]
    ok, st, msg, data = inject_full_doc(base_id, "FAULT")
    check("TC-07-09b 注入 S3 参考文档快照 (V2=FAULT)",
          ok and st == 200, "msg=%r" % msg)
    from task_execute import run_execute_all
    from gap_check import run_gap_check
    from plan_store import save_candidate_plans
    from plan_judge import run_plan_judge
    ok, st, msg, _ = run_execute_all(base_id)
    check("TC-07-09c 基线 T1~T5 执行", ok, "msg=%r" % msg)
    ok, st, msg, gaps = run_gap_check(base_id)
    check("TC-07-09d 基线缺口识别: W gap=4 blocker",
          ok and gaps["blocker_count"] == 1
          and gaps["blockers"][0]["gap"] == 4,
          "msg=%r blockers=%r" % (msg, (gaps or {}).get("blockers")))
    ok, st, msg, plans = save_candidate_plans(base_id)
    check("TC-07-09e 基线候选方案写入 3 套 (transfer_qty=4)",
          ok and plans["plan_count"] == 3
          and plans["transfer_qty"] == 4,
          "msg=%r" % msg)
    ok, st, msg, judge = run_plan_judge(base_id)
    check("TC-07-09f 基线方案判定完成", ok, "msg=%r" % msg)
    base_verdicts = verdicts(base_id)
    check("TC-07-09 基线判定: PLAN-A 可执行, PLAN-B/C 不可执行",
          base_verdicts == {"PLAN-A": "executable",
                            "PLAN-B": "non_executable",
                            "PLAN-C": "non_executable"},
          "got %r" % base_verdicts)

    # ================= TC-07-05/07 创建 what-if + parent 零污染 =================
    parent_snap_before = snapshot_json(base_id)
    ok, st, msg, w = create_run("S3", base_id, RUN_TYPE_WHATIF,
                                {"transport.V2.availability": "AVAILABLE"})
    check("TC-07-05a what-if run 创建", ok, "msg=%r" % msg)
    wid = w["run_id"]
    check("TC-07-05b what-if 字段绑定: run_type/parent_run_id",
          w["run_type"] == "what-if" and w["parent_run_id"] == base_id,
          "got run_type=%r parent=%r" % (w["run_type"], w["parent_run_id"]))
    check("TC-07-05c assumptions_json 存原始假设",
          w["assumptions_json"] == {"transport.V2.availability": "AVAILABLE"},
          "got %r" % w["assumptions_json"])
    w_snap = w["input_snapshot_json"]  # _row_to_dict 已解析为对象
    check("TC-07-05d what-if 快照 = 父副本 + 假设 (V2 → AVAILABLE)",
          next(t for t in w_snap["transports"]
               if t["transport_id"] == "V2")["availability"] == "AVAILABLE",
          "got %r" % w_snap["transports"])
    check("TC-07-05e what-if 快照继承父快照其余数据 (stocks/routes/demands)",
          w_snap.get("remaining_time") == 30
          and len(w_snap.get("stocks") or []) == 2
          and len(w_snap.get("demands") or []) == 1
          and len(w_snap.get("routes") or []) == 3,
          "snapshot keys=%r" % sorted(w_snap.keys()))
    check("TC-07-07 parent 基线零污染: input_snapshot_json 字节级一致",
          snapshot_json(base_id) == parent_snap_before)
    # parent 非 reality → 400 语义
    ok, st, msg, data = create_run("S3", wid, RUN_TYPE_WHATIF,
                                   {"remaining_minutes": 50})
    check("TC-07-06b parent 非 reality (what-if) → 400 语义",
          ok is False and st == 400,
          "got ok=%r st=%r msg=%r" % (ok, st, msg))

    # ================= TC-07-08 独立落库 =================
    ok, st, msg, exec_w = run_execute_all(wid)
    check("TC-07-08a what-if T1~T5 执行", ok, "msg=%r" % msg)
    conn = db.get_conn()
    try:
        w_checks = conn.execute(
            "SELECT COUNT(*) c FROM dependency_checks WHERE run_id=?",
            (wid,)).fetchone()["c"]
        w_results = conn.execute(
            "SELECT COUNT(*) c FROM task_results WHERE run_id=?",
            (wid,)).fetchone()["c"]
        b_checks = conn.execute(
            "SELECT COUNT(*) c FROM dependency_checks WHERE run_id=?",
            (base_id,)).fetchone()["c"]
        b_results = conn.execute(
            "SELECT COUNT(*) c FROM task_results WHERE run_id=?",
            (base_id,)).fetchone()["c"]
    finally:
        db.close_conn(conn)
    check("TC-07-08b 判定结果独立落库: what-if 5 任务/依赖齐全",
          w_results == 5 and w_checks >= 5,
          "w_checks=%r w_results=%r" % (w_checks, w_results))
    check("TC-07-08c parent 判定行数不受 what-if 影响",
          b_results == 5 and b_checks >= 5,
          "b_checks=%r b_results=%r" % (b_checks, b_results))
    check("TC-07-08d what-if run 完成后置 completed",
          exec_w["run_status"] == "completed", "got %r" % exec_w["run_status"])

    # ================= TC-07-14 完成冻结 =================
    try:
        conn = db.get_conn()
        try:
            conn.execute(
                "UPDATE runs SET input_snapshot_json='{}' WHERE run_id=?",
                (wid,))
            conn.commit()
            frozen = False
        except sqlite3.IntegrityError:
            frozen = True
        finally:
            db.close_conn(conn)
    except sqlite3.Error:
        frozen = True
    check("TC-07-14 what-if 完成后快照冻结 (触发器拒绝 UPDATE)",
          frozen)

    # ================= TC-07-10/11/12/13 三组推演 + 指纹 =================
    before = capture_fingerprint(base_id)
    cases = (
        ("TC-07-10 推演A V2→AVAILABLE → PLAN-B 可执行",
         {"transport.V2.availability": "AVAILABLE"},
         {"PLAN-A": "executable", "PLAN-B": "executable",
          "PLAN-C": "non_executable"}),
        ("TC-07-11 推演B remaining_minutes=50 → PLAN-C 可执行",
         {"remaining_minutes": 50},
         {"PLAN-A": "executable", "PLAN-B": "non_executable",
          "PLAN-C": "executable"}),
        ("TC-07-12 推演C stock.R1.W=2 → PLAN-A/C 不可执行",
         {"stock.R1.W": 2},
         {"PLAN-A": "non_executable", "PLAN-B": "non_executable",
          "PLAN-C": "non_executable"}),
    )
    pipeline_ids = []
    for name, assumptions, expected in cases:
        ok, st, msg, pdata = run_whatif_pipeline(base_id, assumptions)
        check(name + " (全链路 ok)", ok, "msg=%r" % msg)
        got = (pdata or {}).get("plan_results") or {}
        got_v = {r.get("plan_name"): r.get("status")
                 for r in got.get("results") or []}
        check(name + " (判定符合预期)", got_v == expected,
              "expected %r got %r" % (expected, got_v))
        # 每步结果独立落库在自身 run_id
        pid = (pdata or {}).get("run_id")
        pipeline_ids.append(pid)
        conn = db.get_conn()
        try:
            own_checks = conn.execute(
                "SELECT COUNT(*) c FROM dependency_checks WHERE run_id=?",
                (pid,)).fetchone()["c"]
            own_results = conn.execute(
                "SELECT COUNT(*) c FROM task_results WHERE run_id=?",
                (pid,)).fetchone()["c"]
            own_plans = conn.execute(
                "SELECT COUNT(*) c FROM plan_results WHERE run_id=?",
                (pid,)).fetchone()["c"]
        finally:
            db.close_conn(conn)
        check(name + " (独立落库: 判定/方案结果归属自身 run_id)",
              own_checks >= 5 and own_results == 5 and own_plans == 3,
              "checks=%r results=%r plan_results=%r"
              % (own_checks, own_results, own_plans))
    check("TC-07-10/11/12 三组推演 run_id 各不相同 (只增不改)",
          len(set(pipeline_ids)) == 3 and all(
              i not in (base_id,) for i in pipeline_ids),
          "ids=%r" % pipeline_ids)

    after = capture_fingerprint(base_id)
    intact, changed = verify_fingerprint(before, after)
    check("TC-07-13 三组推演前后 parent 六表指纹一致 (无污染)",
          intact, "changed_tables=%r" % changed)
    check("TC-07-13b parent 方案判定结果仍为基线值",
          verdicts(base_id) == base_verdicts,
          "got %r" % verdicts(base_id))

    # ---- 清理临时库 ----
    try:
        import shutil
        db.close_conn(db.get_conn())
        shutil.rmtree(tmp_dir, ignore_errors=True)
        print("temp db cleaned: %s" % tmp_dir)
    except Exception as exc:
        print("temp db cleanup failed: %s" % exc)

    print("-" * 60)
    print("result: %d passed, %d failed" % (_PASS, _FAIL))
    print("ALL PASSED" if _FAIL == 0 else "FAILURES PRESENT")
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
