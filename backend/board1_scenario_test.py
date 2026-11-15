"""阶段08板块1: 场景冻结 + run_trace 自测脚本 (临时库隔离).

覆盖用例 (TC-B1-*):
    TC-B1-01 场景文件: S1~S5 全部可加载, 结构校验通过
    TC-B1-02 历史一致性: 冻结基线 vs 真实库历史 run 快照逐条一致
              (只读 --check-all 口径; 依赖既有 bb03.db 历史记录)
    TC-B1-03 input_overrides (reality): 列表 upsert/追加、remaining_time
              替换、非法 key/类型 → ValueError、不修改入参基线
    TC-B1-04 S1 重放: run 快照 = 冻结基线; T1 blocked / T2~T5 established;
              blockers P1/W gap=2、P2/L gap=3
    TC-B1-05 S2 重放: 与 S1 同基线, blockers 预期一致 (S2 聚焦缺口)
    TC-B1-06 S3 重放: 快照 = S3 参考文档条件; blocker P1/W gap=4;
              方案判定 PLAN-A executable / PLAN-B、PLAN-C non_executable
    TC-B1-07 S4 what-if: 三组标准假设推演判定与冻结预期一致;
              父基线六表指纹无污染; 推演 run_id 各不相同
    TC-B1-08 S5 降级重放: T1 unknown / T2 established / T3 established /
              T4 pending_verification / T5 established; 无 blocker;
              全链路无 NOT_RECEIVED 输出
    TC-B1-09 S5 回填变体重放: T1 恢复 established (不修改旧 run)
    TC-B1-10 run_trace: 任务/依赖/缺口/方案/证据链路齐全; 签收证据按
              source_id 关联、tool/llm 证据按 value_json.run_id 关联;
              run 不存在 → 404
    TC-B1-11 回归: whatif_run_test.py / s5_degrade_test.py 全量通过
              (S4 what-if、S5 降级回填能力不受板块一改动影响)

隔离说明: 除 TC-B1-02 (只读历史一致性) 外, 全部用例把 db.DB_PATH 指向
临时库; 真实 bb03.db 只读不写。

用法 (backend 目录下):
    python board1_scenario_test.py
退出码: 0 = 全部通过; 1 = 存在失败用例。
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "scenarios"))

import db  # noqa: E402  import 即 init_db (真实库幂等建表, 启动既有行为)
import load_scenario as loader  # noqa: E402
from constants import RUN_TYPE  # noqa: E402
from run_trace import get_run_trace  # noqa: E402
from whatif_run import capture_fingerprint, verify_fingerprint  # noqa: E402

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


def task_status(run_id):
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT task_id, result_status FROM task_results WHERE run_id=?",
            (run_id,)).fetchall()
    finally:
        db.close_conn(conn)
    return {r["task_id"]: r["result_status"] for r in rows}


def blockers_of(run_id):
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT service_id, material_id, required_quantity, valid_received, "
            "gap FROM blockers WHERE run_id=? ORDER BY service_id, material_id",
            (run_id,)).fetchall()
    finally:
        db.close_conn(conn)
    return [dict(r) for r in rows]


def verdicts_of(run_id):
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


def run_snapshot(run_id):
    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT input_snapshot_json FROM runs WHERE run_id=?",
            (run_id,)).fetchone()
    finally:
        db.close_conn(conn)
    return json.loads(row["input_snapshot_json"]) if row else None


def main():
    # ---- 真实库只读历史一致性 (TC-B1-02, 先于临时库隔离) ----
    print("== TC-B1-01/02 场景文件与历史一致性 (真实库只读) ==")
    for sid in loader.SCENARIO_IDS:
        try:
            sc = loader.load_scenario(sid)
            check("TC-B1-01 场景文件可加载: %s (%s)" % (sid, sc.get("name")),
                  sc.get("scenario_id") == sid)
        except ValueError as exc:
            check("TC-B1-01 场景文件可加载: %s" % sid, False, str(exc))
    ok_all, report = loader.check_all()
    bad = [c for r in report["scenarios"] for c in r["checks"]
           if not c["consistent"]]
    check("TC-B1-02 冻结基线 vs 历史 run 快照一致 (S1~S5)",
          ok_all, "inconsistent checks: %r" % bad)

    # ---- 临时库隔离 ----
    tmp_dir = tempfile.mkdtemp(prefix="board1_scenario_test_")
    db.DB_PATH = os.path.join(tmp_dir, "test.db")
    db.init_db()
    print("test db: %s" % db.DB_PATH)

    # ================= TC-B1-03 input_overrides =================
    base = {"stocks": [{"reserve_id": "R1", "material_id": "W", "quantity": 6}],
            "demands": [], "transports": [], "receipts": [],
            "remaining_time": 30}
    out = loader.apply_input_overrides(base, {
        "stocks": [{"reserve_id": "R1", "material_id": "W", "quantity": 9}],
        "remaining_time": 40})
    check("TC-B1-03a stocks 条目按自然键 upsert 覆盖 (6→9)",
          out["stocks"][0]["quantity"] == 9 and len(out["stocks"]) == 1,
          "got %r" % out["stocks"])
    check("TC-B1-03b remaining_time 标量替换 (30→40)",
          out["remaining_time"] == 40, "got %r" % out.get("remaining_time"))
    out2 = loader.apply_input_overrides(base, {
        "stocks": [{"reserve_id": "R2", "material_id": "W", "quantity": 3}]})
    check("TC-B1-03c 新自然键条目追加",
          len(out2["stocks"]) == 2
          and any(s["reserve_id"] == "R2" for s in out2["stocks"]),
          "got %r" % out2["stocks"])
    check("TC-B1-03d 入参基线不被修改",
          base["stocks"][0]["quantity"] == 6
          and base["remaining_time"] == 30 and len(base["stocks"]) == 1,
          "mutated: %r" % base)
    expect_value_error("TC-B1-03e 未知 override key → ValueError",
                       loader.apply_input_overrides, base, {"bogus": []})
    expect_value_error("TC-B1-03f stocks override 非列表 → ValueError",
                       loader.apply_input_overrides, base, {"stocks": 1})

    # ================= TC-B1-12 acquisition_status (板块3) =================
    sc5 = loader.load_scenario("S5")
    base5 = sc5["baseline"]
    out_u = loader.apply_input_overrides(base5, {"acquisition_status": [
        {"service_id": "P2", "material_id": "L", "status": "unavailable",
         "note": "现场取证失败"}]})
    last = out_u["receipts"][-1]
    check("TC-B1-12a acquisition_status unavailable 追加取证确认条目",
          last["status"] == "unavailable"
          and last["received_quantity"] is None
          and last["service_id"] == "P2" and last["material_id"] == "L"
          and "取证失败" in last["error_message"],
          "last=%r" % last)
    check("TC-B1-12b 时间戳自动生成且晚于基线最晚观测时间",
          last["observed_at"] > "2026-11-15T11:48:37",
          "observed=%r" % last["observed_at"])
    check("TC-B1-12c 入参基线不被修改",
          len(base5["receipts"]) == 5,
          "mutated receipts=%d" % len(base5["receipts"]))
    out_ok = loader.apply_input_overrides(base5, {"acquisition_status": [
        {"service_id": "P3", "material_id": "L", "status": "ok",
         "received_quantity": 8}]})
    last_ok = out_ok["receipts"][-1]
    check("TC-B1-12d acquisition_status ok 追加回填凭证 (status=ok, qty=8)",
          last_ok["status"] == "ok" and last_ok["received_quantity"] == 8,
          "last=%r" % last_ok)
    expect_value_error("TC-B1-12e 非法 status → ValueError",
                       loader.apply_input_overrides, base5,
                       {"acquisition_status": [
                           {"service_id": "P3", "material_id": "L",
                            "status": "bogus"}]})
    expect_value_error("TC-B1-12f ok 缺数量 → ValueError",
                       loader.apply_input_overrides, base5,
                       {"acquisition_status": [
                           {"service_id": "P3", "material_id": "L",
                            "status": "ok"}]})
    expect_value_error("TC-B1-12g unavailable 带数量 → ValueError",
                       loader.apply_input_overrides, base5,
                       {"acquisition_status": [
                           {"service_id": "P3", "material_id": "L",
                            "status": "unavailable",
                            "received_quantity": 3}]})

    # ================= TC-B1-04 S1 重放 =================
    print("== TC-B1-04/05 S1/S2 重放 ==")
    ok, st, msg, data = loader.replay_scenario("S1")
    check("TC-B1-04a S1 重放全链路 ok", ok, "st=%r msg=%r" % (st, msg))
    s1_run = data["run_id"]
    sc1 = loader.load_scenario("S1")
    baseline1 = sc1["baseline"]
    snap1 = run_snapshot(s1_run)
    diffs1 = loader.compare_baseline_to_snapshot(baseline1, snap1)
    check("TC-B1-04b S1 run 快照 = 冻结基线 (五段逐条一致)",
          not diffs1, "diffs: %r" % diffs1)
    check("TC-B1-04c S1 T1~T5 与冻结预期一致",
          task_status(s1_run) == sc1["expected"]["tasks"],
          "got %r" % task_status(s1_run))
    b1 = blockers_of(s1_run)
    check("TC-B1-04d S1 blockers = 预期 (P1/W gap=2, P2/L gap=3)",
          b1 == sc1["expected"]["blockers"], "got %r" % b1)
    check("TC-B1-04e S1 种子数据源类型正确 (scenario_s1)",
          db.get_conn().execute(
              "SELECT COUNT(*) c FROM demand_records WHERE source_type=?",
              ("scenario_s1",)).fetchone()["c"] == 9,
          "")

    ok, st, msg, data = loader.replay_scenario("S2")
    check("TC-B1-05a S2 重放全链路 ok", ok, "st=%r msg=%r" % (st, msg))
    s2_run = data["run_id"]
    b2 = blockers_of(s2_run)
    check("TC-B1-05b S2 blockers = 预期 (缺口识别口径)",
          b2 == loader.load_scenario("S2")["expected"]["blockers"],
          "got %r" % b2)

    # ================= TC-B1-06 S3 重放 =================
    print("== TC-B1-06 S3 重放 ==")
    ok, st, msg, data = loader.replay_scenario("S3")
    check("TC-B1-06a S3 重放全链路 ok", ok, "st=%r msg=%r" % (st, msg))
    s3_run = data["run_id"]
    sc3 = loader.load_scenario("S3")
    diffs3 = loader.compare_baseline_to_snapshot(sc3["baseline"],
                                                 run_snapshot(s3_run))
    check("TC-B1-06b S3 run 快照 = 冻结基线 (含 routes/remaining_time)",
          not diffs3, "diffs: %r" % diffs3)
    check("TC-B1-06c S3 T1~T5 与冻结预期一致",
          task_status(s3_run) == sc3["expected"]["tasks"],
          "got %r" % task_status(s3_run))
    check("TC-B1-06d S3 blocker P1/W gap=4",
          blockers_of(s3_run) == sc3["expected"]["blockers"],
          "got %r" % blockers_of(s3_run))
    check("TC-B1-06e S3 方案判定 = 冻结预期",
          verdicts_of(s3_run) == sc3["expected"]["plan_verdicts"],
          "got %r" % verdicts_of(s3_run))

    # ================= TC-B1-07 S4 what-if =================
    print("== TC-B1-07 S4 what-if ==")
    sc4 = loader.load_scenario("S4")
    before = capture_fingerprint(s3_run)
    ids = []
    for key, meta in sc4["standard_assumptions"].items():
        ok, st, msg, wdata = loader.run_scenario_whatif(
            "S4", meta["assumptions"], None, s3_run)
        check("TC-B1-07a %s 推演全链路 ok" % key, ok,
              "st=%r msg=%r" % (st, msg))
        wid = (wdata or {}).get("pipeline", {}).get("run_id")
        ids.append(wid)
        got = verdicts_of(wid)
        check("TC-B1-07b %s 判定 = 冻结预期 (%s)"
              % (key, meta["expected_plan_verdicts"]),
              got == meta["expected_plan_verdicts"], "got %r" % got)
        check("TC-B1-07c %s 父基线指纹无污染" % key,
              (wdata or {}).get("baseline_intact") is True,
              "changed_tables=%r" % (wdata or {}).get("changed_tables"))
    check("TC-B1-07d 三组推演 run_id 各不相同且非父基线",
          len(set(ids)) == 3 and s3_run not in ids, "ids=%r" % ids)
    after = capture_fingerprint(s3_run)
    intact, changed = verify_fingerprint(before, after)
    check("TC-B1-07e 推演前后父基线六表指纹一致 (无污染)", intact,
          "changed_tables=%r" % changed)
    check("TC-B1-07f 父基线方案判定仍为冻结预期",
          verdicts_of(s3_run) == sc3["expected"]["plan_verdicts"],
          "got %r" % verdicts_of(s3_run))

    # ================= TC-B1-08/09 S5 降级 + 回填 =================
    print("== TC-B1-08/09 S5 ==")
    ok, st, msg, data = loader.replay_scenario("S5")
    check("TC-B1-08a S5 降级重放全链路 ok", ok, "st=%r msg=%r" % (st, msg))
    s5_run = data["run_id"]
    sc5 = loader.load_scenario("S5")
    exp5 = sc5["variants"]["degrade"]["expected"]
    check("TC-B1-08b S5 T1~T5 降级判定 = 冻结预期",
          task_status(s5_run) == exp5["tasks"],
          "got %r" % task_status(s5_run))
    check("TC-B1-08c S5 取证失败不生成 blocker",
          blockers_of(s5_run) == [], "got %r" % blockers_of(s5_run))
    conn = db.get_conn()
    try:
        dirty = conn.execute(
            "SELECT COUNT(*) c FROM ("
            "SELECT reason FROM dependency_checks WHERE run_id=? "
            "UNION ALL SELECT summary_reason FROM task_results WHERE run_id=?) "
            "WHERE reason LIKE '%NOT_RECEIVED%' "
            "OR reason LIKE '%not_received%'",
            (s5_run, s5_run)).fetchone()["c"]
    finally:
        db.close_conn(conn)
    check("TC-B1-08d S5 全链路无 NOT_RECEIVED 输出", dirty == 0,
          "dirty rows=%r" % dirty)

    ok, st, msg, data = loader.replay_scenario("S5", variant="refill")
    check("TC-B1-09a S5 回填变体重放全链路 ok", ok, "st=%r msg=%r" % (st, msg))
    s5r_run = data["run_id"]
    exp5r = sc5["variants"]["refill"]["expected"]
    check("TC-B1-09b S5 回填后 T1 恢复 established",
          task_status(s5r_run) == exp5r["tasks"],
          "got %r" % task_status(s5r_run))
    check("TC-B1-09c 回填不修改旧 S5 run (旧 run 判定保持降级预期)",
          task_status(s5_run) == exp5["tasks"],
          "got %r" % task_status(s5_run))

    # TC-B1-12h: acquisition_status 端到端 —— S5 + ok 回填 overrides → T1 恢复
    ok, st, msg, data = loader.replay_scenario("S5", overrides={
        "acquisition_status": [
            {"service_id": "P3", "material_id": "L", "status": "ok",
             "received_quantity": 8}]})
    check("TC-B1-12h acquisition_status ok 回填重放: T1 恢复 established",
          ok and task_status(data["run_id"])["T1"] == "established",
          "st=%r msg=%r" % (st, msg))

    # ================= TC-B1-10 run_trace =================
    print("== TC-B1-10 run_trace ==")
    conn = db.get_conn()
    try:
        conn.execute(
            "INSERT INTO evidence_v2 (evidence_id, evidence_type, source_id, "
            "status, value_json, observed_at) VALUES (?,?,?,?,?,?)",
            ("EV-TR-001", "receipt", "RCT-S3-001", "ok",
             json.dumps({"receipt_id": "RCT-S3-001"}), "2026-11-15T09:00:00"))
        conn.execute(
            "INSERT INTO evidence_v2 (evidence_id, evidence_type, source_id, "
            "status, value_json, observed_at) VALUES (?,?,?,?,?,?)",
            ("EV-TR-002", "tool", "tool:get_task_result", "ok",
             json.dumps({"tool": "get_task_result",
                         "args": {"run_id": s3_run, "task_id": "T1"}}),
             "2026-11-15T09:01:00"))
        conn.execute(
            "INSERT INTO evidence_v2 (evidence_id, evidence_type, source_id, "
            "status, value_json, observed_at) VALUES (?,?,?,?,?,?)",
            ("EV-TR-003", "llm", "llm-explain", "ok",
             json.dumps({"run_id": s3_run, "explanation": "test"}),
             "2026-11-15T09:02:00"))
        conn.execute(
            "INSERT INTO evidence_v2 (evidence_id, evidence_type, source_id, "
            "status, value_json, observed_at) VALUES (?,?,?,?,?,?)",
            ("EV-TR-004", "tool", "tool:get_task_result", "ok",
             json.dumps({"tool": "get_task_result",
                         "args": {"run_id": "BB03-RUN-999", "task_id": "T1"}}),
             "2026-11-15T09:03:00"))
        conn.commit()
    finally:
        db.close_conn(conn)
    ok, st, msg, trace = get_run_trace(s3_run)
    check("TC-B1-10a trace 组装 ok", ok, "st=%r msg=%r" % (st, msg))
    check("TC-B1-10b trace.summary = T1~T5 结果压缩视图",
          trace["summary"] == task_status(s3_run),
          "got %r" % trace["summary"])
    check("TC-B1-10c 任务携带依赖明细 (T1-D1 等)",
          all(t.get("dependency_checks") for t in trace["tasks"])
          and len(trace["tasks"]) == 5,
          "tasks=%r" % [t["task_id"] for t in trace["tasks"]])
    check("TC-B1-10d blockers 在链路中",
          [b["gap"] for b in trace["blockers"]] == [4],
          "got %r" % trace["blockers"])
    check("TC-B1-10e 方案+判定在链路中",
          {p["plan_name"]: p["status"] for p in trace["plans"]}
          == sc3["expected"]["plan_verdicts"],
          "got %r" % {p["plan_name"]: p["status"] for p in trace["plans"]})
    ev_ids = {e["evidence_id"] for e in trace["evidences"]}
    check("TC-B1-10f 签收证据按 source_id 关联 (RCT-S3-001)",
          "EV-TR-001" in ev_ids, "got %r" % ev_ids)
    check("TC-B1-10g tool 证据按 value_json.args.run_id 关联",
          "EV-TR-002" in ev_ids, "got %r" % ev_ids)
    check("TC-B1-10h llm 证据按 value_json.run_id 关联",
          "EV-TR-003" in ev_ids, "got %r" % ev_ids)
    check("TC-B1-10i 其他 run 的证据不混入 (BB03-RUN-999)",
          "EV-TR-004" not in ev_ids, "got %r" % ev_ids)
    ok, st, msg, data = get_run_trace("BB03-RUN-XXX")
    check("TC-B1-10j run 不存在 → 404 语义", ok is False and st == 404,
          "got ok=%r st=%r" % (ok, st))

    # ================= TC-B1-11 回归 =================
    print("== TC-B1-11 回归 (S4 what-if / S5 降级回填) ==")
    here = os.path.dirname(os.path.abspath(__file__))
    for script in ("whatif_run_test.py", "s5_degrade_test.py"):
        proc = subprocess.run(
            [sys.executable, os.path.join(here, script)],
            cwd=here, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300)
        ok_r = proc.returncode == 0
        check("TC-B1-11 回归 %s 全量通过" % script, ok_r,
              "exit=%r\n%s\n%s" % (proc.returncode,
                                   proc.stdout[-800:], proc.stderr[-400:]))

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
