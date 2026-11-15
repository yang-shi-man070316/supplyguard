# -*- coding: utf-8 -*-
"""board1_frontend_test.py 的回归数据准备脚本（阶段08 板块1 配套）。

board1_frontend_test.py 硬编码依赖特定 run 序列 (BB03-RUN-007 pending /
RUN-009~010 连续 S1 / RUN-011 唯一 S3 reality / RUN-016 S5), 该序列
只能从干净库按固定顺序生成。本脚本从零生成 BB03-RUN-001~016:

    RUN-001  S1 --reset-observations --full      (demo_success 步骤1)
    RUN-002  S2 --full                           (demo_success 步骤2)
    RUN-003  S1 --full                           (占位)
    RUN-004  S2 --full                           (占位; 避免提前生成 S3,
                                                 RUN-011 才能是唯一 S3 reality)
    RUN-005  S5 --reset-observations --full      (demo_failure 步骤1)
    RUN-006  复核单 + 回填签收 (REST)             (demo_failure 步骤2~3)
    RUN-007  POST /api/runs 建 run 不执行          → pending (TC-UI-01e)
             (scenario_id 用 "S2": 合法标签且不与 S3 冲突,
              RUN-011 才能是唯一 S3 reality run)
    RUN-008  S1 --full
    RUN-009  S1 --full                           (RUN-010 的上一 reality)
    RUN-010  S1 --full                           (TC-UI-10a 选中)
    RUN-011  S3 --full                           (TC-UI-06/07 选中; 唯一 S3
                                                 reality → 对比列 = 1+子运行数)
    RUN-012  S4 --whatif whatif-A --parent 011   (RUN-011 的 what-if 子运行)
    RUN-013  S4 --whatif whatif-B --parent 011
    RUN-014  S2 --full                           (占位)
    RUN-015  S5 --variant refill --full          (占位)
    RUN-016  S5 --full                           (TC-UI-08a 选中)

前置条件: 后端已启动且 runs 表为空 (干净库)。库里有任何 run 时本脚本
拒绝执行, 防止 run_id 顺延导致前端测试硬编码断言错位。

用法:
    python prepare_frontend_test_data.py [base_url]
    base_url 默认 http://127.0.0.1:8000
退出码: 0 = 数据就绪; 1 = 失败。
"""

import json
import subprocess
import sys
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOADER = PROJECT_ROOT / "scenarios" / "load_scenario.py"

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def http(method, path, body=None):
    req = urllib.request.Request(
        BASE_URL + path, method=method,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8")
        return resp.status, (json.loads(raw) if raw else {})


def load(args):
    r = subprocess.run(
        [sys.executable, str(LOADER)] + args,
        cwd=str(PROJECT_ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=180)
    if r.returncode != 0:
        print("[FAIL] load_scenario %s\n%s" % (args, r.stdout[-500:] + r.stderr[-500:]))
        return False
    return True


def check(name, cond, detail=""):
    print(("[PASS] " if cond else "[FAIL] ") + name
          + ((" :: " + str(detail)) if detail else ""))
    return cond


def main():
    # ---- 前置检查: runs 表必须为空 ----
    try:
        st, runs = http("GET", "/api/runs")
    except Exception as exc:
        print("[FAIL] 无法访问后端 %s (%s) —— 请先启动后端" % (BASE_URL, exc))
        return 1
    if runs:
        print("[FAIL] 库里已有 %d 个 run，本脚本只支持干净库（防止 run_id 顺延错位）。\n"
              "       请: 停后端 → 删除 backend/bb03.db → 起后端 → 重跑本脚本。"
              % len(runs))
        return 1

    ok = True
    # ---- 1~5: demo 场景 ----
    ok &= check("RUN-001 S1 基线", load(["S1", "--reset-observations", "--full"]))
    ok &= check("RUN-002 S2 缺口", load(["S2", "--full"]))
    ok &= check("RUN-003 S1 占位", load(["S1", "--full"]))
    ok &= check("RUN-004 S2 占位", load(["S2", "--full"]))
    ok &= check("RUN-005 S5 降级", load(["S5", "--reset-observations", "--full"]))

    # ---- 6: 复核闭环 (REST, demo_failure 步骤2~3) ----
    try:
        st, created = http("POST", "/api/reviews", {
            "dispatch_id": "DSP-S5-001", "service_id": "P3",
            "material_id": "L", "trigger_run_id": "BB03-RUN-005"})
        ok &= check("RUN-006 复核单创建", st in (200, 201) and created.get("created"),
                    created)
        st, receipt = http("POST",
                           "/api/reviews/%s/receipt" % created["review_id"], {
                               "receipt_id": "RCT-S5-DEMO-001",
                               "dispatch_id": "DSP-S5-001",
                               "service_id": "P3", "material_id": "L",
                               "received_quantity": 8,
                               "observed_at": "2026-11-15T12:30:00",
                               "source_type": "DEMO",
                               "note": "on-site re-photographed receipt"})
        ok &= check("RUN-006 回填签收", st == 200
                    and receipt.get("new_run_id") == "BB03-RUN-006",
                    receipt.get("new_run_id"))
    except Exception as exc:
        print("[FAIL] 复核闭环: %s" % exc)
        ok = False

    # ---- 7: pending run (TC-UI-01e 依赖) ----
    # scenario_id 用 "S2" (合法且非 S3): RUN-011 必须是唯一 S3 reality run,
    # 否则同场景上一 reality 对比列会破坏 TC-UI-06a 的列数断言
    try:
        st, run = http("POST", "/api/runs",
                       {"scenario_id": "S2", "run_type": "reality"})
        ok &= check("RUN-007 pending 建 run 不执行",
                    st == 201 and run.get("run_id") == "BB03-RUN-007"
                    and run.get("run_status") == "pending", run.get("run_id"))
    except Exception as exc:
        print("[FAIL] 建 pending run: %s" % exc)
        ok = False

    # ---- 8~10: 连续 S1 (TC-UI-10a 需要 RUN-010 的上一 reality = RUN-009) ----
    ok &= check("RUN-008 S1", load(["S1", "--full"]))
    ok &= check("RUN-009 S1", load(["S1", "--full"]))
    ok &= check("RUN-010 S1", load(["S1", "--full"]))

    # ---- 11~13: 唯一 S3 reality + 两个 what-if 子运行 (TC-UI-06a/06b/06c) ----
    ok &= check("RUN-011 S3 唯一 reality", load(["S3", "--full"]))
    ok &= check("RUN-012 whatif-A 子运行",
                load(["S4", "--whatif", "whatif-A", "--parent", "BB03-RUN-011"]))
    ok &= check("RUN-013 whatif-B 子运行",
                load(["S4", "--whatif", "whatif-B", "--parent", "BB03-RUN-011"]))

    # ---- 14~16: 占位 + RUN-016 S5 (TC-UI-08a) ----
    ok &= check("RUN-014 S2 占位", load(["S2", "--full"]))
    ok &= check("RUN-015 S5 refill 占位", load(["S5", "--variant", "refill", "--full"]))
    ok &= check("RUN-016 S5", load(["S5", "--full"]))

    # ---- 终检: run 序列与关键属性 ----
    _, runs = http("GET", "/api/runs")
    ids = [r["run_id"] for r in runs]
    p007 = next((r for r in runs if r["run_id"] == "BB03-RUN-007"), None)
    p010 = next((r for r in runs if r["run_id"] == "BB03-RUN-010"), None)
    p011 = next((r for r in runs if r["run_id"] == "BB03-RUN-011"), None)
    ok &= check("共 16 个 run", len(runs) == 16, "count=%d" % len(runs))
    ok &= check("RUN-007 为 pending", p007 is not None
                and p007["run_status"] == "pending",
                p007 and p007["run_status"])
    ok &= check("RUN-010 为 S1 reality", p010 is not None
                and p010["scenario_id"] == "S1", p010 and p010["scenario_id"])
    ok &= check("RUN-011 为 S3 reality 且唯一", p011 is not None
                and p011["scenario_id"] == "S3"
                and sum(1 for r in runs
                        if r["scenario_id"] == "S3"
                        and r["run_type"] == "reality") == 1)

    print("\n%s 数据准备完成。下一步:" % ("ALL PASSED" if ok else "FAILURES PRESENT"))
    print("  python board1_frontend_test.py %s" % BASE_URL)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
