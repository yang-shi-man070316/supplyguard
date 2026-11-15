"""阶段08板块3: 最终验收 —— 重新 clone 部署 + 完整复现演示流程 (E2E-CLONE).

模拟「整机断电/关闭所有进程后重新 clone」:
    ① 把当前工作区 (含全部未提交文件) 提交进临时 bare 仓库
       (不动用户真实仓库的任何历史/分支)
    ② git clone 该 bare 仓库到全新目录 (清空环境: 无 bb03.db)
    ③ 按 delivery/README.md 步骤启动后端, 依次复现:

       [成功演示] demo_success.md
         S1 --full → BB03-RUN-001 (T1 blocked)
         S2 --full → BB03-RUN-002 (blockers P1/W=2, P2/L=3)
         S3 --full → BB03-RUN-003 (PLAN-A 可执行, T1 blocked)
         模拟投送+新增签收 (overrides) → BB03-RUN-004 (T1 established)

       [失败演示] demo_failure.md
         S5 --full → BB03-RUN-005 (T1 unknown, T4 pending_verification)
         POST /api/reviews → 复核单
         POST /api/reviews/{id}/receipt → BB03-RUN-006 (T1 established),
         RUN-005 判定保持降级不变

       [终验附加]
         /console/ 前端可访问; 多 run 列表区分 reality;
         /api/runs/{id}/trace 溯源链路可用

    ④ 收尾: 停止后端, 删除临时仓库/副本

依赖: git 可用 (临时仓库使用 local user 配置, 不影响全局).

用法:
    python e2e_clone_test.py [--port 8124]
退出码: 0 = 通过。
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_IGNORE = shutil.ignore_patterns("*.db", "__pycache__", ".git", "*.pyc",
                                 ".venv", "venv", "node_modules", ".env",
                                 "docs", ".tmp-shot")
_PORT = None

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


def http_json(url, payload=None, timeout=15):
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
        try:
            return resp.status, json.loads(body)
        except json.JSONDecodeError:
            return resp.status, body


def wait_http(url, tries=30, delay=1.0):
    for _ in range(tries):
        try:
            return http_json(url, timeout=5)
        except Exception:
            time.sleep(delay)
    return None, None


def git(cwd, *args):
    return subprocess.run(["git", "-C", cwd] + list(args),
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120)


def run_cli(cwd, script, *args, timeout=240):
    return subprocess.run(
        [sys.executable, script] + list(args), cwd=cwd,
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout,
        env=dict(os.environ, PYTHONIOENCODING="utf-8"))


def task_summary(base, run_id):
    st, trace = http_json(base + "/api/runs/%s/trace" % run_id)
    if st != 200:
        return None
    return trace.get("summary")


def blockers_view(base, run_id):
    st, trace = http_json(base + "/api/runs/%s/trace" % run_id)
    return ([(b["service_id"], b["material_id"], b["gap"])
             for b in trace["blockers"]], trace["run"]) if st == 200 else None


def main():
    global _PORT
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8124)
    args = parser.parse_args()
    _PORT = args.port
    base = "http://127.0.0.1:%d" % _PORT

    tmp = tempfile.mkdtemp(prefix="supplyguard_clone_")
    bare = os.path.join(tmp, "supplyguard.git")
    staging = os.path.join(tmp, "staging")
    fresh = os.path.join(tmp, "fresh")
    uvicorn_proc = None
    try:
        # ---- ① 工作区 → 临时 bare 仓库 (完整内容含未提交文件) ----
        os.makedirs(bare)
        git(tmp, "init", "--bare", bare)
        shutil.copytree(PROJECT_ROOT, staging, ignore=_IGNORE)
        git(staging, "init")
        git(staging, "config", "user.email", "e2e@supplyguard.local")
        git(staging, "config", "user.name", "E2E Clone Test")
        git(staging, "add", "-A")
        r = git(staging, "commit", "-m", "e2e clone snapshot (临时验收仓库)")
        check("E2E-C01 临时仓库快照提交 (含全部未提交文件)",
              r.returncode == 0, "exit=%r %s" % (r.returncode, r.stderr[-200:]))
        git(staging, "remote", "add", "origin", bare)
        r = git(staging, "push", "origin", "HEAD")
        check("E2E-C02 push 到临时 bare 仓库", r.returncode == 0,
              "exit=%r %s" % (r.returncode, r.stderr[-200:]))

        # ---- ② 重新 clone (清空环境) ----
        r = git(tmp, "clone", bare, fresh)
        check("E2E-C03 git clone 全新副本 (无 bb03.db)",
              r.returncode == 0 and not os.path.exists(
                  os.path.join(fresh, "backend", "bb03.db")),
              "exit=%r %s" % (r.returncode, r.stderr[-200:]))

        # ---- ③ 按 README 启动后端 ----
        uvicorn_proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app", "--port", str(_PORT)],
            cwd=os.path.join(fresh, "backend"),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        st, _ = wait_http(base + "/health")
        check("E2E-C04 clone 副本 uvicorn 启动 (/health 200)", st == 200,
              "st=%r" % st)

        # ---- ④ 成功演示流程 (demo_success.md) ----
        sc = os.path.join(fresh, "scenarios", "load_scenario.py")
        r = run_cli(fresh, sc, "S1", "--full")
        t1 = task_summary(base, "BB03-RUN-001")
        check("E2E-C05 S1 --full → RUN-001 (T1 blocked, T2~T5 established)",
              r.returncode == 0 and t1 == {"T1": "blocked", "T2": "established",
                                           "T3": "established",
                                           "T4": "established",
                                           "T5": "established"},
              "exit=%r tasks=%r" % (r.returncode, t1))
        r = run_cli(fresh, sc, "S2", "--full")
        blk, run2 = blockers_view(base, "BB03-RUN-002") or (None, None)
        check("E2E-C06 S2 --full → RUN-002 (blockers P1/W=2, P2/L=3)",
              r.returncode == 0
              and blk == [("P1", "W", 2), ("P2", "L", 3)],
              "exit=%r blk=%r" % (r.returncode, blk))
        r = run_cli(fresh, sc, "S3", "--full")
        st, trace3 = http_json(base + "/api/runs/BB03-RUN-003/trace")
        plans3 = {p["plan_name"]: p["status"] for p in trace3["plans"]}
        t3 = trace3["summary"]
        check("E2E-C07 S3 --full → RUN-003 (PLAN-A 可执行, T1 仍 blocked)",
              r.returncode == 0 and plans3 == {"PLAN-A": "executable",
                                               "PLAN-B": "non_executable",
                                               "PLAN-C": "non_executable"}
              and t3["T1"] == "blocked",
              "exit=%r plans=%r t1=%r" % (r.returncode, plans3, t3.get("T1")))
        overrides = ("{\"receipts\":[{\"receipt_id\":\"RCT-S3-DEMO-001\","
                     "\"dispatch_id\":\"DSP-S3-DEMO-001\",\"service_id\":\"P1\","
                     "\"material_id\":\"W\",\"received_quantity\":7,"
                     "\"observed_at\":\"2026-11-15T11:00:00\","
                     "\"created_at\":\"2026-11-15 11:00:00\"}]}")
        r = run_cli(fresh, sc, "S3", "--overrides", overrides, "--full")
        t4 = task_summary(base, "BB03-RUN-004")
        blk4, _ = blockers_view(base, "BB03-RUN-004") or (None, None)
        check("E2E-C08 模拟投送+新增签收 → RUN-004 (T1 全 established, 无缺口)",
              r.returncode == 0 and t4 == {t: "established" for t in
                                           ("T1", "T2", "T3", "T4", "T5")}
              and blk4 == [],
              "exit=%r tasks=%r blk=%r" % (r.returncode, t4, blk4))

        # ---- ⑤ 失败演示流程 (demo_failure.md) ----
        # 现场复位: 清空此前 S1~S3 的观测残留, 使现场口径 = S5 场景
        r = run_cli(fresh, sc, "S5", "--reset-observations", "--full")
        t5 = task_summary(base, "BB03-RUN-005")
        check("E2E-C09 S5 --full → RUN-005 (T1 unknown, T4 pending_verification)",
              r.returncode == 0
              and t5 == {"T1": "unknown", "T2": "established",
                         "T3": "established", "T4": "pending_verification",
                         "T5": "established"},
              "exit=%r tasks=%r" % (r.returncode, t5))
        st, review = http_json(base + "/api/reviews", payload={
            "dispatch_id": "DSP-S5-001", "service_id": "P3",
            "material_id": "L", "trigger_run_id": "BB03-RUN-005"})
        review_id = (review or {}).get("review_id")
        check("E2E-C10 发起复核流程 (POST /api/reviews)",
              st in (200, 201) and review_id, "st=%r review=%r" % (st, review))
        st, receipt = http_json(
            base + "/api/reviews/%s/receipt" % review_id, payload={
                "receipt_id": "RCT-S5-DEMO-001", "dispatch_id": "DSP-S5-001",
                "service_id": "P3", "material_id": "L",
                "received_quantity": 8,
                "observed_at": "2026-11-15T12:30:00", "source_type": "DEMO",
                "note": "现场补拍签收凭证"})
        new_run = (receipt or {}).get("new_run_id")
        t6 = task_summary(base, new_run) if new_run else None
        check("E2E-C11 回填合法签收 → 新 run T1 established",
              st == 200 and new_run == "BB03-RUN-006"
              and t6 == {t: "established" for t in
                         ("T1", "T2", "T3", "T4", "T5")},
              "st=%r new=%r tasks=%r" % (st, new_run, t6))
        t5_after = task_summary(base, "BB03-RUN-005")
        check("E2E-C12 历史 RUN-005 判定保持降级不变 (只增不改)",
              t5_after == t5, "t5_after=%r" % t5_after)

        # ---- ⑥ 终验附加: console / 列表 / trace ----
        st, body = http_json(base + "/console/")
        check("E2E-C13 /console/ 前端工作台可访问",
              st == 200 and "保障任务指挥台" in body, "st=%r" % st)
        st, runs = http_json(base + "/api/runs")
        kinds = {(r["run_id"], r["run_type"], r["run_status"]) for r in runs}
        check("E2E-C14 多 run 列表 (6 条 reality, 区分状态)",
              len(kinds) == 6 and all(t == "reality" for _, t, _ in kinds)
              and any(s == "pending" for _, _, s in kinds) is False,
              "runs=%r" % sorted(kinds))
        st, trace = http_json(base + "/api/runs/BB03-RUN-003/trace")
        chain_ok = (st == 200 and trace.get("tasks")
                    and trace.get("blockers") and trace.get("plans"))
        check("E2E-C15 evidence trace 溯源链路可用 (任务/缺口/方案齐备)",
              chain_ok, "st=%r keys=%r" % (st, sorted((trace or {}).keys())))
    finally:
        if uvicorn_proc is not None:
            uvicorn_proc.terminate()
            try:
                uvicorn_proc.wait(timeout=10)
            except Exception:
                uvicorn_proc.kill()
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass

    print("-" * 60)
    print("result: %d passed, %d failed" % (_PASS, _FAIL))
    print("ALL PASSED" if _FAIL == 0 else "FAILURES PRESENT")
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
