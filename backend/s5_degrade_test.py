"""阶段07板块2: S5 取证失败可信降级判定自测脚本 (临时库隔离).

核心语义: 取证失败 ≠ 业务判定失败; 证据 status=unavailable 采用
unknown / pending_verification, 禁止输出 NOT_RECEIVED (不使用
received:false)。

覆盖用例 (TC-08-*):
    TC-08-01 check_t1 取证失败确认 → 依赖 unknown (不判 unsatisfied,
              reason 不含 NOT_RECEIVED)
    TC-08-02 check_t1 取证失败 + 更早有效回执 → 沿用旧回执判定
    TC-08-03 check_t1 全部 unknown 聚合 → 任务 unknown
    TC-08-04 check_t4 取证失败确认独立检查 unknown; 混合 satisfied +
              unknown → pending_verification
    TC-08-05 check_t5 unavailable 证据 → satisfied + 降级置信结论
    TC-08-06 既有快照回归: 无 status 字段的签收证据行为不变
    TC-08-07 S5 全链路: T1 unknown / T2 established / T3 established /
              T4 pending_verification / T5 established (degraded)
    TC-08-08 异常证据正常写入 evidence_v2 (status=unavailable,
              value_json=null)
    TC-08-09 gap_check 不生成 blocker (取证失败不武断算缺口)
    TC-08-10 全链路不产生 NOT_RECEIVED 输出

隔离说明: 将 db.DB_PATH 指向临时库, 全部测试数据只落在临时库,
不读写真实 bb03.db (import db 时对真实库的 init_db 为启动既有行为,
IF NOT EXISTS 幂等, 不产生/污染任何测试数据)。

用法 (backend 目录下):
    python s5_degrade_test.py
退出码: 0 = 全部通过; 1 = 存在失败用例。
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db  # noqa: E402  import 即 init_db() (真实库幂等建表, 启动既有行为)
from inject_s5_snapshot import CONFIRM_ID, inject_s5  # noqa: E402
from t1_check import check_t1  # noqa: E402
from t4_check import check_t4  # noqa: E402
from t5_check import check_t5  # noqa: E402
from run_store_v2 import create_run  # noqa: E402
from task_execute import run_execute_all  # noqa: E402
from gap_check import run_gap_check  # noqa: E402

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


def seed_s1_observations():
    """临时库种入 S1 基线实时观测 (与真实 bb03.db S1 数据同口径)。"""
    conn = db.get_conn()
    try:
        conn.executemany(
            "INSERT INTO demand_records "
            "(service_id, material_id, required_quantity, observed_at, "
            "source_type) VALUES (?,?,?,?,?)",
            [("P3", "L", 8, "2026-11-15T01:56:22", "sim"),
             ("P1", "W", 4, "2026-11-15T01:56:22", "sim"),
             ("P3", "R", 2, "2026-11-15T01:56:22", "sim")])
        conn.executemany(
            "INSERT INTO receipt_records_v2 "
            "(receipt_id, dispatch_id, service_id, material_id, "
            "received_quantity, observed_at, source_type) VALUES (?,?,?,?,?,?,?)",
            [("RCT-S1-001", "DSP-S1-001", "P1", "W", 2,
              "2026-11-14 17:56:58", "sim"),
             ("RCT-S1-002", "DSP-S1-002", "P2", "L", 2,
              "2026-11-14 17:56:58", "sim"),
             ("RCT-S1-003", "DSP-S1-003", "P3", "R", 2,
              "2026-11-14 17:56:58", "sim"),
             ("REC-A1B2C3D4", "DSP-S1-004", "P1", "W", 40,
              "2026-11-14T18:30:00", "sim")])
        conn.executemany(
            "INSERT INTO stock_observations "
            "(reserve_id, material_id, quantity, observed_at, source_type) "
            "VALUES (?,?,?,?,?)",
            [("R1", "L", 2, "2026-11-15T01:56:18", "sim"),
             ("R1", "R", 8, "2026-11-15T01:56:18", "sim"),
             ("R1", "W", 2, "2026-11-15T01:56:18", "sim"),
             ("R2", "L", 5, "2026-11-15T01:56:18", "sim"),
             ("R2", "R", 4, "2026-11-15T01:56:18", "sim"),
             ("R2", "W", 8, "2026-11-15T01:56:18", "sim")])
        conn.executemany(
            "INSERT INTO transport_observations "
            "(transport_id, availability, capacity, observed_at, source_type) "
            "VALUES (?,?,?,?,?)",
            [("V1", "AVAILABLE", 4, "2026-11-15T01:56:20", "sim"),
             ("V2", "AVAILABLE", 4, "2026-11-15T01:56:20", "sim")])
        conn.commit()
    finally:
        db.close_conn(conn)


def s5_snapshot():
    """构造 S5 场景快照 dict (P3/L 需求 + S1 历史回执 + 取证失败确认)。"""
    return {
        "demands": [
            {"service_id": "P3", "material_id": "L", "required_quantity": 8,
             "observed_at": "2026-11-15T01:56:22",
             "created_at": "2026-11-15 01:56:22"},
        ],
        "receipts": [
            {"receipt_id": "RCT-S1-003", "dispatch_id": "DSP-S1-003",
             "service_id": "P3", "material_id": "R",
             "received_quantity": 2, "observed_at": "2026-11-14 17:56:58",
             "created_at": "2026-11-14 17:56:58"},
            {"receipt_id": "RCT-S1-001", "dispatch_id": "DSP-S1-001",
             "service_id": "P1", "material_id": "W",
             "received_quantity": 2, "observed_at": "2026-11-14 17:56:58",
             "created_at": "2026-11-14 17:56:58"},
            {"receipt_id": CONFIRM_ID, "dispatch_id": "DSP-S5-001",
             "service_id": "P3", "material_id": "L",
             "received_quantity": None, "status": "unavailable",
             "observed_at": "2026-11-15T02:30:00",
             "created_at": "2026-11-15 02:30:00",
             "error_message": "接收确认取证失败: 现场无法提供有效签收凭证"},
        ],
        "stocks": [
            {"reserve_id": "R1", "material_id": "L", "quantity": 2,
             "observed_at": "2026-11-15T01:56:18",
             "created_at": "2026-11-15 01:56:18"},
            {"reserve_id": "R1", "material_id": "R", "quantity": 8,
             "observed_at": "2026-11-15T01:56:18",
             "created_at": "2026-11-15 01:56:18"},
            {"reserve_id": "R1", "material_id": "W", "quantity": 2,
             "observed_at": "2026-11-15T01:56:18",
             "created_at": "2026-11-15 01:56:18"},
            {"reserve_id": "R2", "material_id": "W", "quantity": 8,
             "observed_at": "2026-11-15T01:56:18",
             "created_at": "2026-11-15 01:56:18"},
        ],
        "transports": [
            {"transport_id": "V1", "availability": "AVAILABLE", "capacity": 4,
             "observed_at": "2026-11-15T01:56:20",
             "created_at": "2026-11-15 01:56:20"},
            {"transport_id": "V2", "availability": "AVAILABLE", "capacity": 4,
             "observed_at": "2026-11-15T01:56:20",
             "created_at": "2026-11-15 01:56:20"},
        ],
    }


def no_not_received(res) -> bool:
    """检查判定输出不含 NOT_RECEIVED 字样 (禁止输出确认未收到结论)。"""
    text = json.dumps(res, ensure_ascii=False)
    return "NOT_RECEIVED" not in text and "received:false" not in text


def main():
    tmp_dir = tempfile.mkdtemp(prefix="s5_degrade_test_")
    db.DB_PATH = os.path.join(tmp_dir, "test.db")
    db.init_db()
    seed_s1_observations()
    print("test db: %s" % db.DB_PATH)

    snap = s5_snapshot()

    # ================= TC-08-01/03 T1 取证失败 → unknown =================
    t1 = check_t1(snap)
    p3l = [c for c in t1["checks"]
           if c["evidence"].get("material_id") == "L"
           and c["evidence"].get("service_id") == "P3"]
    check("TC-08-01a T1: P3/L 取证失败确认 → 依赖 unknown",
          len(p3l) == 1 and p3l[0]["status"] == "unknown",
          "checks=%r" % [(c["evidence"].get("service_id"), c["status"])
                         for c in t1["checks"]])
    check("TC-08-01b T1: 取证失败依赖不输出确定未收到结论 (无 NOT_RECEIVED)",
          p3l and no_not_received(p3l[0])
          and "确认未收到" not in p3l[0]["reason"],
          "reason=%r" % (p3l[0]["reason"] if p3l else None))
    check("TC-08-03 T1: 全部 unknown 聚合 → 任务 unknown",
          t1["task_status"] == "unknown",
          "task_status=%r summary=%r" % (t1["task_status"], t1["summary_reason"]))

    # ================= TC-08-02 T1 旧回执沿用 =================
    snap_inherit = json.loads(json.dumps(snap))
    # P3/L 加一条更早的有效回执 (旧回执满足有效性规则)
    snap_inherit["receipts"].insert(1, {
        "receipt_id": "RCT-S1-009", "dispatch_id": "DSP-S1-009",
        "service_id": "P3", "material_id": "L", "received_quantity": 3,
        "observed_at": "2026-11-15T02:00:00",
        "created_at": "2026-11-15 02:00:00"})
    t1i = check_t1(snap_inherit)
    p3l_i = [c for c in t1i["checks"]
             if c["evidence"].get("material_id") == "L"
             and c["evidence"].get("service_id") == "P3"]
    check("TC-08-02 T1: 取证失败确认不使旧回执失效 (沿用历史有效回执判定)",
          len(p3l_i) == 1 and p3l_i[0]["status"] == "unsatisfied"
          and "沿用历史有效回执" in p3l_i[0]["reason"]
          and p3l_i[0]["evidence"].get("latest_receipt", {}).get(
              "receipt_id") == "RCT-S1-009",
          "got %r" % p3l_i)
    check("TC-08-02b T1: 沿用旧回执时也不输出 NOT_RECEIVED",
          p3l_i and no_not_received(p3l_i[0]))

    # ================= TC-08-04 T4 取证失败 → pending_verification =================
    t4 = check_t4(snap)
    statuses = [c["status"] for c in t4["checks"]]
    check("TC-08-04a T4: 每条签收证据独立检查 (历史回执 satisfied + 确认 unknown)",
          "satisfied" in statuses and "unknown" in statuses
          and "unsatisfied" not in statuses,
          "statuses=%r" % statuses)
    check("TC-08-04b T4: 混合 satisfied+unknown → 任务 pending_verification",
          t4["task_status"] == "pending_verification",
          "task_status=%r" % t4["task_status"])
    check("TC-08-04c T4: 取证失败检查不输出未收到结论",
          no_not_received(t4))

    # ================= TC-08-05 T5 degraded conclusion =================
    t5 = check_t5(snap)
    check("TC-08-05a T5: 数据无冲突 → 依赖 satisfied",
          t5["checks"][0]["status"] == "satisfied",
          "checks=%r" % t5["checks"])
    check("TC-08-05b T5: 检测到 unavailable 证据 → 降级置信结论 (degraded)",
          "降级置信" in t5["checks"][0]["reason"]
          and t5["checks"][0]["evidence"].get(
              "unavailable_receipt_count") == 1,
          "reason=%r" % t5["checks"][0]["reason"])
    check("TC-08-05c T5: 任务 established with degraded conclusion",
          t5["task_status"] == "established"
          and "降级置信" in t5["summary_reason"],
          "task_status=%r" % t5["task_status"])

    # ================= TC-08-06 既有快照回归 (无 status 字段) =================
    legacy_snap = json.loads(json.dumps(snap))
    for r in legacy_snap["receipts"]:
        r.pop("status", None)
        r.pop("error_message", None)
    # 去掉取证失败确认的 unavailable 标记 → 退化为无数量签收记录
    legacy_snap["receipts"] = [r for r in legacy_snap["receipts"]
                               if r.get("received_quantity") is not None]
    t1_legacy = check_t1(legacy_snap)
    p3l_leg = [c for c in t1_legacy["checks"]
               if c["evidence"].get("service_id") == "P3"
               and c["evidence"].get("material_id") == "L"]
    check("TC-08-06a T1 既有口径回归: 无签收 → unsatisfied (原行为)",
          len(p3l_leg) == 1 and p3l_leg[0]["status"] == "unsatisfied",
          "got %r" % p3l_leg)
    t4_legacy = check_t4(legacy_snap)
    check("TC-08-06b T4 既有口径回归: 全部闭环 → established",
          t4_legacy["task_status"] == "established",
          "task_status=%r" % t4_legacy["task_status"])
    t5_legacy = check_t5(legacy_snap)
    check("TC-08-06c T5 既有口径回归: 无降级标注",
          t5_legacy["checks"][0]["status"] == "satisfied"
          and "降级置信" not in t5_legacy["checks"][0]["reason"],
          "reason=%r" % t5_legacy["checks"][0]["reason"])

    # ================= TC-08-07/08/09/10 S5 全链路 =================
    ok, st, msg, base = create_run("S5", None)
    check("TC-08-07a S5 reality run 创建", ok, "msg=%r" % msg)
    s5_id = base["run_id"]
    ok, st, msg, inj = inject_s5(s5_id)
    check("TC-08-07b 注入 S5 取证失败快照", ok, "msg=%r" % msg)
    conn = db.get_conn()
    try:
        snap5 = json.loads(conn.execute(
            "SELECT input_snapshot_json FROM runs WHERE run_id=?",
            (s5_id,)).fetchone()["input_snapshot_json"])
    finally:
        db.close_conn(conn)
    check("TC-08-07c 快照 receipts: 历史回执 4 条保留 + 取证失败确认 1 条",
          len(snap5["receipts"]) == 5
          and sum(1 for r in snap5["receipts"]
                  if r.get("status") == "unavailable") == 1
          and all(r.get("status") != "unavailable"
                  for r in snap5["receipts"] if r["receipt_id"].startswith("RCT")
                  or r["receipt_id"].startswith("REC")),
          "receipts=%r" % [(r.get("receipt_id"), r.get("status"))
                           for r in snap5["receipts"]])
    check("TC-08-07d 取证失败确认不使用 received:false (received_quantity=null)",
          all(r.get("received_quantity") is None
              for r in snap5["receipts"] if r.get("status") == "unavailable"))

    ok, st, msg, exec5 = run_execute_all(s5_id)
    check("TC-08-07e S5 T1~T5 完整执行不崩溃", ok, "msg=%r" % msg)
    tasks = {t["task_id"]: t for t in exec5["tasks"]}
    expect_tasks = {"T1": "unknown", "T2": "established", "T3": "established",
                    "T4": "pending_verification", "T5": "established"}
    got_tasks = {tid: tasks[tid]["result_status"] for tid in expect_tasks}
    check("TC-08-07 S5 全链路约束输出: T1 unknown / T2 established / "
          "T3 established / T4 pending_verification / T5 established",
          got_tasks == expect_tasks, "got %r" % got_tasks)
    check("TC-08-07f T5 降级结论落库 (summary_reason 含 degraded)",
          "降级置信" in tasks["T5"]["summary_reason"],
          "reason=%r" % tasks["T5"]["summary_reason"])

    # evidence_v2
    conn = db.get_conn()
    try:
        ev = conn.execute(
            "SELECT * FROM evidence_v2 WHERE evidence_type='receipt' "
            "AND source_id=? ORDER BY created_at DESC LIMIT 1",
            (CONFIRM_ID,)).fetchone()
        all_checks_text = " ".join(
            r[0] for r in conn.execute(
                "SELECT reason || ' ' || COALESCE(evidence,'') "
                "FROM dependency_checks WHERE run_id=?", (s5_id,)).fetchall())
        all_results_text = " ".join(
            r[0] for r in conn.execute(
                "SELECT summary_reason FROM task_results WHERE run_id=?",
                (s5_id,)).fetchall())
    finally:
        db.close_conn(conn)
    check("TC-08-08 异常证据正常写入 evidence_v2 (unavailable + value_json=null)",
          ev is not None and ev["status"] == "unavailable"
          and ev["value_json"] is None and ev["error_message"],
          "got %r" % (dict(ev) if ev else None))
    check("TC-08-10 全链路不产生 NOT_RECEIVED / received:false 输出",
          "NOT_RECEIVED" not in all_checks_text
          and "received:false" not in all_checks_text
          and "NOT_RECEIVED" not in all_results_text)

    # gap_check: 取证失败不武断算缺口
    ok, st, msg, gaps = run_gap_check(s5_id)
    check("TC-08-09 gap_check: P3/L 取证失败不生成 blocker (不武断算缺口)",
          ok and gaps["blocker_count"] == 0,
          "msg=%r blockers=%r" % (msg, (gaps or {}).get("blocker_count")))

    # ---- 清理临时库 ----
    try:
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
