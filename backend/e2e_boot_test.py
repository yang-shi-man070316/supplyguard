"""阶段08板块2: 清空环境端到端拉起自测 (E2E-01).

模拟「clone 下来的全新环境」(完整代码副本, 不含任何 *.db / __pycache__),
按 delivery/README.md 的启动步骤从零拉起整套系统并加载场景生成 run:

    ① 复制项目到临时目录 (清空环境: 无数据库文件)
    ② cd backend && uvicorn main:app --port <port>    (README 第 3 节④)
    ③ python scenarios/load_scenario.py S3 --full     (README 第 4 节)
    ④ 通过 REST 验证: /api/runs 出现 BB03-RUN-001 (S3, completed);
       /api/runs/BB03-RUN-001/trace 判定 T1 blocked、方案 PLAN-A 可执行;
       /console/ 前端可访问
    ⑤ 再次调用 load_scenario.py S5 --full → BB03-RUN-002 (S5 降级语义)
    ⑥ 收尾: 停止 uvicorn、删除临时副本

真实 bb03.db 与运行中的后端零接触 (独立副本 + 独立端口)。

用法:
    python e2e_boot_test.py [--port 8123]
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
                                 ".venv", "venv", "node_modules", ".env")

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


def http_get(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
        try:
            return resp.status, json.loads(body)
        except json.JSONDecodeError:
            return resp.status, body  # 非 JSON 响应 (如 /console/ HTML)


def wait_http(url, tries=30, delay=1.0):
    for _ in range(tries):
        try:
            return http_get(url, timeout=5)
        except Exception:
            time.sleep(delay)
    return None, None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8123)
    args = parser.parse_args()
    port = args.port
    base = "http://127.0.0.1:%d" % port

    tmp = tempfile.mkdtemp(prefix="supplyguard_e2e_")
    copy_dir = os.path.join(tmp, "supplyguard")
    uvicorn_proc = None
    try:
        # ---- ① 清空环境: 完整副本, 无任何 *.db ----
        shutil.copytree(PROJECT_ROOT, copy_dir, ignore=_IGNORE)
        check("E2E-01a 清空环境副本创建 (无数据库文件)",
              not [f for f in os.listdir(os.path.join(copy_dir, "backend"))
                   if f.endswith(".db")],
              "db files found in fresh copy")

        # ---- ② README 步骤④: 启动后端 (uvicorn, 独立端口) ----
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        uvicorn_proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app",
             "--port", str(port)],
            cwd=os.path.join(copy_dir, "backend"),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        st, _ = wait_http(base + "/health")
        check("E2E-01b 空环境 uvicorn 启动 (/health 200)", st == 200,
              "st=%r" % st)
        st, _ = wait_http(base + "/api/runs")
        check("E2E-01c 全新空库 run 列表为空",
              st == 200, "st=%r" % st)

        # ---- ③ README 步骤: 加载场景 S3 --full (真实 CLI) ----
        proc = subprocess.run(
            [sys.executable, os.path.join(copy_dir, "scenarios",
                                          "load_scenario.py"),
             "S3", "--full"],
            cwd=copy_dir, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180, env=env)
        check("E2E-01d load_scenario.py S3 --full 执行成功",
              proc.returncode == 0,
              "exit=%r\n%s\n%s" % (proc.returncode,
                                   proc.stdout[-300:], proc.stderr[-200:]))

        # ---- ④ REST 验证 (README 第 5 节 Verify 口径) ----
        time.sleep(1.0)
        st, runs = http_get(base + "/api/runs")
        first = (runs or [{}])[0] if runs else None
        check("E2E-01e /api/runs 出现 BB03-RUN-001 (S3, completed)",
              st == 200 and first and first.get("run_id") == "BB03-RUN-001"
              and first.get("scenario_id") == "S3"
              and first.get("run_status") == "completed",
              "first=%r" % first)
        st, trace = http_get(base + "/api/runs/BB03-RUN-001/trace")
        tasks = (trace or {}).get("summary") or {}
        plans = {p.get("plan_name"): p.get("status")
                 for p in (trace or {}).get("plans") or []}
        check("E2E-01f trace: T1 blocked + PLAN-A 可执行",
              st == 200 and tasks.get("T1") == "blocked"
              and plans.get("PLAN-A") == "executable",
              "tasks=%r plans=%r" % (tasks, plans))
        st, body = http_get(base + "/console/")
        check("E2E-01g /console/ 前端工作台可访问",
              st == 200 and "保障任务指挥台" in body,
              "st=%r" % st)

        # ---- ⑤ S5 降级场景加载 (第二场景) ----
        proc = subprocess.run(
            [sys.executable, os.path.join(copy_dir, "scenarios",
                                          "load_scenario.py"),
             "S5", "--full"],
            cwd=copy_dir, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180, env=env)
        st, trace5 = http_get(base + "/api/runs/BB03-RUN-002/trace")
        tasks5 = (trace5 or {}).get("summary") or {}
        check("E2E-01h S5 --full → BB03-RUN-002 降级语义 (T1 unknown, "
              "T4 pending_verification)",
              proc.returncode == 0 and st == 200
              and tasks5.get("T1") == "unknown"
              and tasks5.get("T4") == "pending_verification",
              "exit=%r tasks=%r" % (proc.returncode, tasks5))
    finally:
        # ---- ⑥ 收尾 ----
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
