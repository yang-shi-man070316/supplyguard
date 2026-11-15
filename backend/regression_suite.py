"""阶段08板块2: 回归套件 — 正向案例库 + 失败回归 (临时库隔离).

正向案例 (固定 run_id + 预期输出, 隔离临时库按固定顺序执行, run_id 确定):
    REG-01 S1 全成立          -> BB03-RUN-001  T1~T5 全 established, 无 blocker
    REG-02 S2 T1 blocked      -> BB03-RUN-002  T1 blocked, blockers P1/W=2、P2/L=3
    REG-03 S3 PLAN-A 可执行   -> BB03-RUN-003  T1 blocked, PLAN-A executable,
                               PLAN-B/C non_executable, blocker P1/W gap=4
    REG-04 S4 what-if 隔离    -> BB03-RUN-004/005/006 (推演A/B/C),
                               父基线六表 sha256 指纹推演前后一致 (无污染)
    REG-05 S5 降级            -> BB03-RUN-007  T1 unknown, T4 pending_verification,
                               T2/T3/T5 established, 无 blocker, 无 NOT_RECEIVED

失败回归 (异常注入 → 断言不崩溃 + 既定降级语义):
    FR-01 库存接口异常失败: DB 不可打开 → get_current_stock 返回
          (False, 500, "db error ...") 不抛异常
    FR-02 receipt 取证失败: unavailable 确认不使历史有效回执失效 (沿用判定);
          无旧回执 → T1 unknown; gap_check 不生成 blocker (取证失败≠未送达)
    FR-03 weather/RAG/LLM 异常: LLM 故障注入开关 → status=unavailable;
          weather 网络异常 → status=unavailable 且 evidence 落库;
          RAG 无匹配 → 固定文本 "无可靠依据" (matched=false, status=ok)
    FR-04 后端重启数据不丢: 两阶段子进程模拟重启 —— create 阶段建 run +
          完整判定落库; verify 阶段新进程重新 init_db 后 runs/判定行/
          快照完整保留

用法 (backend 目录下):
    python regression_suite.py                 # 全部 (正向 + 失败)
    python regression_suite.py --positive-only
    python regression_suite.py --failures-only
    python regression_suite.py --report-file reg_report.json
内部参数 (子进程两阶段, 勿手动使用):
    --worker-phase create|verify|llm|weather --db <path>

退出码: 0 = 全部通过; 1 = 存在失败用例。
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "scenarios"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import db  # noqa: E402  import 即 init_db (真实库幂等建表, 启动既有行为)
import load_scenario as loader  # noqa: E402

_PASS = 0
_FAIL = 0
_RESULTS = []


def check(name, cond, detail=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("[PASS] %s" % name)
    else:
        _FAIL += 1
        print("[FAIL] %s %s" % (name, detail))
    _RESULTS.append({"name": name, "pass": bool(cond), "detail": str(detail)})


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


def no_not_received(run_id):
    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) c FROM ("
            "SELECT reason AS txt FROM dependency_checks WHERE run_id=? "
            "UNION ALL SELECT summary_reason FROM task_results WHERE run_id=?) "
            "WHERE txt LIKE '%NOT_RECEIVED%' OR txt LIKE '%not_received%'",
            (run_id, run_id)).fetchone()
    finally:
        db.close_conn(conn)
    return row["c"] == 0


# ============================================================
# 正向案例定义 (固定 run_id + 预期输出)
# ============================================================

# REG-01: S1 全成立 —— Modify: 现场补全 8 条签收 (每组合最新签收 ≥ 需求)
_REG01_RECEIPTS = [
    {"receipt_id": "RCT-REG01-%02d" % i, "dispatch_id": "DSP-REG01-%02d" % i,
     "service_id": sv, "material_id": mt, "received_quantity": qty,
     "observed_at": "2026-11-14 18:00:00", "created_at": "2026-11-14 18:00:00"}
    for i, (sv, mt, qty) in enumerate((
        ("P1", "W", 4), ("P1", "L", 1), ("P1", "R", 2),
        ("P2", "W", 5), ("P2", "L", 5), ("P2", "R", 3),
        ("P3", "W", 6), ("P3", "L", 8)), start=1)
]


def _run_reg01():
    """S1 + 现场签收补全 (Modify) → Run → 全成立。"""
    ok, st, msg, data = loader.replay_scenario(
        "S1", overrides={"receipts": _REG01_RECEIPTS})
    rid = (data or {}).get("run_id")
    expect = {"tasks": {t: "established" for t in
                        ("T1", "T2", "T3", "T4", "T5")},
              "blockers": [], "plan_count": 0}
    actual = {"run_id": rid, "tasks": task_status(rid) if rid else None,
              "blockers": blockers_of(rid) if rid else None,
              "plan_count": len(verdicts_of(rid)) if rid else None}
    ok_case = (ok and rid == "BB03-RUN-001"
               and actual["tasks"] == expect["tasks"]
               and actual["blockers"] == expect["blockers"]
               and actual["plan_count"] == 0)
    return ok_case, {"ok": ok, "msg": msg, "expect": expect, "actual": actual}


def _run_reg02():
    """S2 (S1 基线) → T1 blocked + 缺口识别。"""
    ok, st, msg, data = loader.replay_scenario("S2")
    rid = (data or {}).get("run_id")
    expect_tasks = {"T1": "blocked", "T2": "established", "T3": "established",
                    "T4": "established", "T5": "established"}
    expect_blockers = [
        {"service_id": "P1", "material_id": "W", "required_quantity": 4,
         "valid_received": 2, "gap": 2},
        {"service_id": "P2", "material_id": "L", "required_quantity": 5,
         "valid_received": 2, "gap": 3},
    ]
    actual = {"run_id": rid, "tasks": task_status(rid) if rid else None,
              "blockers": blockers_of(rid) if rid else None}
    ok_case = (ok and rid == "BB03-RUN-002"
               and actual["tasks"] == expect_tasks
               and actual["blockers"] == expect_blockers)
    return ok_case, {"ok": ok, "msg": msg,
                     "expect": {"tasks": expect_tasks,
                                "blockers": expect_blockers},
                     "actual": actual}


def _run_reg03():
    """S3 → PLAN-A 可执行但 T1 仍 blocked。"""
    ok, st, msg, data = loader.replay_scenario("S3")
    rid = (data or {}).get("run_id")
    expect_tasks = {"T1": "blocked", "T2": "established", "T3": "established",
                    "T4": "established", "T5": "established"}
    expect_verdicts = {"PLAN-A": "executable", "PLAN-B": "non_executable",
                       "PLAN-C": "non_executable"}
    expect_blockers = [{"service_id": "P1", "material_id": "W",
                        "required_quantity": 7, "valid_received": 3, "gap": 4}]
    actual = {"run_id": rid, "tasks": task_status(rid) if rid else None,
              "verdicts": verdicts_of(rid) if rid else None,
              "blockers": blockers_of(rid) if rid else None}
    ok_case = (ok and rid == "BB03-RUN-003"
               and actual["tasks"] == expect_tasks
               and actual["verdicts"] == expect_verdicts
               and actual["blockers"] == expect_blockers)
    return ok_case, {"ok": ok, "msg": msg,
                     "expect": {"tasks": expect_tasks,
                                "verdicts": expect_verdicts,
                                "blockers": expect_blockers},
                     "actual": actual}


def _run_reg04():
    """S4: 基于 REG-03 基线 (BB03-RUN-003) 三组标准推演 + 无污染验证。"""
    from whatif_run import capture_fingerprint, verify_fingerprint

    sc4 = loader.load_scenario("S4")
    parent = "BB03-RUN-003"
    expect_ids = {"whatif-A": "BB03-RUN-004", "whatif-B": "BB03-RUN-005",
                  "whatif-C": "BB03-RUN-006"}
    before = capture_fingerprint(parent)
    detail = {"assumptions": {}, "verdicts": {}, "run_ids": {},
              "baseline_intact": None}
    ok_all = True
    for key, meta in sc4["standard_assumptions"].items():
        ok, st, msg, wdata = loader.run_scenario_whatif(
            "S4", meta["assumptions"], None, parent)
        wid = (wdata or {}).get("pipeline", {}).get("run_id")
        got = verdicts_of(wid) if wid else None
        detail["assumptions"][key] = meta["assumptions"]
        detail["run_ids"][key] = wid
        detail["verdicts"][key] = got
        if not (ok and wid == expect_ids[key]
                and got == meta["expected_plan_verdicts"]
                and (wdata or {}).get("baseline_intact") is True):
            ok_all = False
            detail.setdefault("errors", []).append(
                "%s: ok=%r msg=%r wid=%r verdicts=%r" % (key, ok, msg, wid, got))
    after = capture_fingerprint(parent)
    intact, changed = verify_fingerprint(before, after)
    detail["baseline_intact"] = intact
    detail["changed_tables"] = changed
    ok_case = ok_all and intact
    return ok_case, detail


def _run_reg05():
    """S5 → T1 unknown + T4 pending_verification 降级。"""
    ok, st, msg, data = loader.replay_scenario("S5")
    rid = (data or {}).get("run_id")
    expect_tasks = {"T1": "unknown", "T2": "established",
                    "T3": "established", "T4": "pending_verification",
                    "T5": "established"}
    actual = {"run_id": rid, "tasks": task_status(rid) if rid else None,
              "blockers": blockers_of(rid) if rid else None,
              "not_received_free": no_not_received(rid) if rid else False}
    ok_case = (ok and rid == "BB03-RUN-007"
               and actual["tasks"] == expect_tasks
               and actual["blockers"] == []
               and actual["not_received_free"] is True)
    return ok_case, {"ok": ok, "msg": msg,
                     "expect": {"tasks": expect_tasks, "blockers": []},
                     "actual": actual}


POSITIVE_CASES = (
    ("REG-01", "S1 全成立 (现场补签收 Modify→Run→Verify)", "BB03-RUN-001",
     _run_reg01),
    ("REG-02", "S2 T1 blocked + 缺口识别", "BB03-RUN-002", _run_reg02),
    ("REG-03", "S3 PLAN-A 可执行但 T1 blocked", "BB03-RUN-003", _run_reg03),
    ("REG-04", "S4 what-if 隔离 (基线无污染)", "BB03-RUN-004/005/006",
     _run_reg04),
    ("REG-05", "S5 T1 unknown + T4 pending_verification",
     "BB03-RUN-007", _run_reg05),
)


def run_positive():
    print("== 正向回归案例 (固定 run_id, 隔离临时库) ==")
    tmp_dir = tempfile.mkdtemp(prefix="regression_positive_")
    db.DB_PATH = os.path.join(tmp_dir, "test.db")
    db.init_db()
    try:
        for case_id, name, rid, runner in POSITIVE_CASES:
            ok_case, detail = runner()
            check("%s %s (run_id=%s)" % (case_id, name, rid), ok_case,
                  json.dumps(detail, ensure_ascii=False)[:400])
    finally:
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass
    return tmp_dir


# ============================================================
# 失败回归案例 (异常注入 → 不崩溃 + 既定降级语义)
# ============================================================

def run_failures():
    print("== 失败回归案例 (异常注入与降级语义) ==")
    tmp_dir = tempfile.mkdtemp(prefix="regression_failures_")
    db.DB_PATH = os.path.join(tmp_dir, "test.db")
    db.init_db()

    # ---- FR-01 库存接口异常失败 ----
    from ai_tools import get_current_demand, get_current_stock

    good = db.DB_PATH
    db.DB_PATH = os.path.join(tmp_dir, "no_such_dir", "broken.db")
    crashed = False
    try:
        ok, st, msg, data = get_current_stock()
    except Exception as exc:  # 任何异常外抛 = 崩溃, 回归失败
        crashed = True
        ok, st, msg, data = False, 500, "crashed: %s" % exc, None
    check("FR-01a 库存接口 DB 异常不崩溃且返回 (False,500,db error)",
          (not crashed) and ok is False and st == 500
          and "db error" in str(msg),
          "crashed=%r ok=%r st=%r msg=%r" % (crashed, ok, st, msg))
    try:
        ok, st, msg, data = get_current_demand()
        crashed = False
    except Exception as exc:
        crashed = True
        ok, st, msg, data = False, 500, "crashed: %s" % exc, None
    check("FR-01b 需求接口同样降级 (不崩溃)",
          (not crashed) and ok is False and st == 500,
          "crashed=%r ok=%r st=%r" % (crashed, ok, st))
    db.DB_PATH = good

    # ---- FR-02 receipt 取证失败 (S5 类 unavailable) ----
    from gap_check import check_gaps
    from t1_check import check_t1
    from t4_check import check_t4

    old_receipt = {"receipt_id": "RCT-OLD-001", "dispatch_id": "DSP-OLD-001",
                   "service_id": "P3", "material_id": "L",
                   "received_quantity": 8, "status": "ok",
                   "observed_at": "2026-11-15T09:00:00",
                   "created_at": "2026-11-15 09:00:00"}
    failed_cf = {"receipt_id": "CF-FR-001", "dispatch_id": "DSP-FR-001",
                 "service_id": "P3", "material_id": "L",
                 "received_quantity": None, "status": "unavailable",
                 "observed_at": "2026-11-15T10:00:00",
                 "created_at": "2026-11-15 10:00:00",
                 "error_message": "取证失败"}
    demand = {"service_id": "P3", "material_id": "L",
              "required_quantity": 8, "observed_at": "2026-11-15T08:00:00",
              "created_at": "2026-11-15 08:00:00"}
    # 2a: 有更早有效回执 → 沿用判定 established
    res_old = check_t1({"demands": [demand],
                        "receipts": [old_receipt, failed_cf]})
    check("FR-02a 取证失败不使历史有效回执失效 (T1 established 且沿用旧回执)",
          res_old["task_status"] == "established"
          and "沿用" in (res_old["checks"][0]["reason"] or ""),
          "got %r %r" % (res_old["task_status"],
                         res_old["checks"][0]["reason"]))
    # 2b: 无旧回执 → unknown (不判未收到)
    res_new = check_t1({"demands": [demand], "receipts": [failed_cf]})
    check("FR-02b 无旧回执 → T1 unknown (禁止 NOT_RECEIVED)",
          res_new["task_status"] == "unknown"
          and "NOT_RECEIVED" not in json.dumps(res_new, ensure_ascii=False),
          "got %r" % res_new["task_status"])
    # 2c: gap_check 不生成 blocker
    gaps = check_gaps({"demands": [demand], "receipts": [failed_cf]})
    check("FR-02c 取证失败不武断算缺口 (无 blocker)",
          gaps == [], "got %r" % gaps)
    # 2d: T4 对 unavailable 确认判 unknown (不判 unsatisfied)
    res_t4 = check_t4({"demands": [demand], "receipts": [failed_cf],
                       "stocks": [{"reserve_id": "R2", "material_id": "L",
                                   "quantity": 5}],
                       "transports": []})
    check("FR-02d T4 取证失败确认判 unknown",
          res_t4["checks"][0]["status"] == "unknown",
          "got %r" % res_t4["checks"][0]["status"])

    # ---- FR-03 weather / RAG / LLM 异常降级 ----
    from analysis.rag_store import run_rag_query

    ok, st, msg, data = run_rag_query("今天股市涨了吗")  # 与保障知识库零重叠
    check("FR-03a RAG 无匹配 → 固定文本「无可靠依据」(组件正常, evidence=ok)",
          ok and (data or {}).get("matched") is False
          and (data or {}).get("answer") == "无可靠依据"
          and (data or {}).get("component_status") == "ok",
          "ok=%r data=%r" % (ok, (data or {}).get("answer")))
    here = os.path.dirname(os.path.abspath(__file__))
    # 3b: LLM 故障注入 (子进程, import 前设置 SUPPLYGUARD_LLM_DISABLED=1)
    proc = subprocess.run(
        [sys.executable, os.path.join(here, "regression_suite.py"),
         "--worker-phase", "llm", "--db", db.DB_PATH],
        cwd=here, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=120)
    llm_ok = proc.returncode == 0
    check("FR-03b LLM 故障注入 → status=unavailable 降级 (不崩溃)",
          llm_ok, "exit=%r out=%s err=%s" % (proc.returncode,
                                             proc.stdout[-300:],
                                             proc.stderr[-200:]))
    # 3c: weather 网络异常 (子进程 monkeypatch urlopen)
    proc = subprocess.run(
        [sys.executable, os.path.join(here, "regression_suite.py"),
         "--worker-phase", "weather", "--db", db.DB_PATH],
        cwd=here, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=120)
    wx_ok = proc.returncode == 0
    check("FR-03c weather 网络异常 → status=unavailable 且证据落库",
          wx_ok, "exit=%r out=%s err=%s" % (proc.returncode,
                                            proc.stdout[-300:],
                                            proc.stderr[-200:]))

    # ---- FR-04 后端重启数据不丢 (两阶段子进程) ----
    fr04_db = os.path.join(tmp_dir, "restart.db")
    proc = subprocess.run(
        [sys.executable, os.path.join(here, "regression_suite.py"),
         "--worker-phase", "create", "--db", fr04_db],
        cwd=here, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=120)
    check("FR-04a 重启前: 建 run + 完整判定落库", proc.returncode == 0,
          "exit=%r out=%s err=%s" % (proc.returncode, proc.stdout[-300:],
                                     proc.stderr[-200:]))
    proc = subprocess.run(
        [sys.executable, os.path.join(here, "regression_suite.py"),
         "--worker-phase", "verify", "--db", fr04_db],
        cwd=here, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=120)
    check("FR-04b 重启后: run 记录与判定完整保留", proc.returncode == 0,
          "exit=%r out=%s err=%s" % (proc.returncode, proc.stdout[-300:],
                                     proc.stderr[-200:]))

    try:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except Exception:
        pass


# ============================================================
# 子进程 worker (FR-03/FR-04 用; 勿手动调用)
# ============================================================

def _worker_phase(phase, db_path):
    db.DB_PATH = db_path
    db.init_db()

    if phase == "create":
        # 模拟重启前: S3 重放 (run + T1~T5 + 缺口 + 方案 + 判定 全部落库)
        ok, st, msg, data = loader.replay_scenario("S3")
        if not ok:
            print("create failed: %s" % msg)
            return 1
        conn = db.get_conn()
        try:
            n_results = conn.execute(
                "SELECT COUNT(*) c FROM task_results WHERE run_id=?",
                (data["run_id"],)).fetchone()["c"]
            n_checks = conn.execute(
                "SELECT COUNT(*) c FROM dependency_checks WHERE run_id=?",
                (data["run_id"],)).fetchone()["c"]
        finally:
            db.close_conn(conn)
        print(json.dumps({"run_id": data["run_id"], "task_results": n_results,
                          "dependency_checks": n_checks}, ensure_ascii=False))
        return 0 if n_results == 5 else 1

    if phase == "verify":
        # 模拟重启后: 新进程 init_db (幂等) 后数据必须完整保留
        conn = db.get_conn()
        try:
            runs = conn.execute(
                "SELECT run_id, scenario_id, run_type, run_status, "
                "input_snapshot_json, completed_at FROM runs "
                "ORDER BY run_id").fetchall()
            results = conn.execute(
                "SELECT run_id, task_id, result_status FROM task_results "
                "ORDER BY run_id, task_id").fetchall()
            blockers = conn.execute(
                "SELECT run_id, service_id, material_id, gap FROM blockers "
                "ORDER BY run_id").fetchall()
            plans = conn.execute(
                "SELECT run_id, plan_name, transfer_qty FROM plan_definitions "
                "ORDER BY run_id, plan_name").fetchall()
            plan_results = conn.execute(
                "SELECT run_id, status FROM plan_results ORDER BY run_id"
            ).fetchall()
        finally:
            db.close_conn(conn)
        ok_verify = (
            len(runs) == 1 and runs[0]["run_id"] == "BB03-RUN-001"
            and runs[0]["run_status"] == "completed"
            and runs[0]["input_snapshot_json"]
            and runs[0]["completed_at"]
            and len(results) == 5
            and len(blockers) == 1 and blockers[0]["gap"] == 4
            and len(plans) == 3
            and len(plan_results) == 3)
        print(json.dumps({"runs": [dict(r) for r in runs],
                          "results": [dict(r) for r in results],
                          "blockers": [dict(r) for r in blockers],
                          "plans": [dict(r) for r in plans],
                          "plan_results": [dict(r) for r in plan_results],
                          "verify_ok": ok_verify},
                         ensure_ascii=False, default=str))
        return 0 if ok_verify else 1

    if phase == "llm":
        # 必须在 import llm_explainer 之前设置故障注入开关
        os.environ["SUPPLYGUARD_LLM_DISABLED"] = "1"
        from analysis.llm_explainer import run_llm_explain

        ok, st, msg, data = loader.replay_scenario("S5")
        if not ok:
            print("llm worker: baseline run failed: %s" % msg)
            return 1
        ok_e, st_e, msg_e, data_e = run_llm_explain(data["run_id"])
        status = (data_e or {}).get("status")
        print(json.dumps({"ok": ok_e, "http_status": st_e, "msg": msg_e,
                          "status": status,
                          "error_message": (data_e or {}).get("error_message")},
                         ensure_ascii=False, default=str))
        return 0 if (ok_e and status == "unavailable") else 1

    if phase == "weather":
        import urllib.error

        from analysis import weather_evidence

        def _boom(*args, **kwargs):
            raise urllib.error.URLError("injected network failure")

        weather_evidence.urllib.request.urlopen = _boom
        ok, st, msg, data = weather_evidence.run_weather_evidence()
        conn = db.get_conn()
        try:
            row = conn.execute(
                "SELECT evidence_id, status FROM evidence_v2 "
                "WHERE evidence_type='weather' ORDER BY created_at DESC "
                "LIMIT 1").fetchone()
        finally:
            db.close_conn(conn)
        status = (data or {}).get("status")
        print(json.dumps({"ok": ok, "http_status": st, "msg": msg,
                          "status": status,
                          "evidence_row": dict(row) if row else None},
                         ensure_ascii=False, default=str))
        return 0 if (ok and status == "unavailable"
                     and row is not None and row["status"] == "unavailable") \
            else 1

    print("unknown worker phase: %s" % phase)
    return 2


# ============================================================
# 主流程
# ============================================================

def main(argv=None):
    parser = argparse.ArgumentParser(description="SupplyGuard 阶段08板块2 回归套件")
    parser.add_argument("--positive-only", action="store_true",
                        help="只跑正向案例")
    parser.add_argument("--failures-only", action="store_true",
                        help="只跑失败回归")
    parser.add_argument("--report-file", metavar="PATH",
                        help="回归报告 JSON 输出路径")
    parser.add_argument("--worker-phase", metavar="PHASE", help="内部参数")
    parser.add_argument("--db", metavar="PATH", help="内部参数: 临时库路径")
    args = parser.parse_args(argv)

    if args.worker_phase:
        if not args.db:
            print("--worker-phase requires --db", file=sys.stderr)
            return 2
        return _worker_phase(args.worker_phase, args.db)

    print("=" * 64)
    print("SupplyGuard 阶段08板块2 回归报告")
    print("=" * 64)
    if not args.failures_only:
        run_positive()
    if not args.positive_only:
        run_failures()

    print("-" * 64)
    print("result: %d passed, %d failed" % (_PASS, _FAIL))
    print("ALL PASSED" if _FAIL == 0 else "FAILURES PRESENT")
    if args.report_file:
        with open(args.report_file, "w", encoding="utf-8") as f:
            json.dump({"passed": _PASS, "failed": _FAIL,
                       "results": _RESULTS}, f, ensure_ascii=False, indent=2)
        print("report: %s" % args.report_file)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
