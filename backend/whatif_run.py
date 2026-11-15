"""阶段07板块1: what-if 假设推演编排 + 基线无污染指纹验证.

前置约束 (严格执行):
    1. run 分 reality / what-if 两类; what-if 以 parent_run_id 关联基线 run。
    2. what-if 假设修改仅作用于自身 run 上下文, 绝不污染 parent 基线:
       创建时由 run_store_v2.create_run 深拷贝 parent 快照并在副本上
       叠加假设, 对 parent 行零写入 (只读 SELECT)。
    3. 所有 run 记录只增不改; runs / task_results / dependency_checks
       三表隔离规则不变: 每次 what-if 推演的判定结果均以自身 run_id
       独立落库, 不触碰 parent 的任何判定行。

run_whatif_pipeline 完整链路:
    ① create_run(run_type='what-if')     父快照副本 + 假设叠加
    ② run_execute_all                     T1~T5 完整判定 (独立落库)
    ③ run_gap_check                       缺口识别 (独立落库)
    ④ save_candidate_plans                3 套候选方案 (独立落库)
    ⑤ run_plan_judge                      方案可行性判定 (独立落库)

capture_fingerprint / verify_fingerprint:
    对基线 run 在 runs / task_results / dependency_checks / blockers /
    plan_definitions / plan_results 六张表的所有行做 sha256 指纹,
    推演前后对比, 证明基线数据与判定结果完全不变。

CLI:
    python whatif_run.py <parent_run_id> '<assumptions-json>' [scenario_id]
    示例:
    python whatif_run.py BB03-RUN-011 '{"transport.V2.availability":"AVAILABLE"}'
"""

import hashlib
import json
import sys

from constants import RUN_TYPE_WHATIF
from db import close_conn, get_conn
from gap_check import run_gap_check
from plan_judge import run_plan_judge
from plan_store import save_candidate_plans
from run_store_v2 import create_run
from task_execute import run_execute_all

# 基线指纹覆盖表: (表名, 行排序主键列); 全部按 run_id 过滤
_FINGERPRINT_TABLES = (
    ("runs", "run_id"),
    ("task_results", "id"),
    ("dependency_checks", "id"),
    ("blockers", "blocker_id"),
    ("plan_definitions", "plan_id"),
    ("plan_results", "plan_result_id"),
)


def capture_fingerprint(run_id: str) -> dict:
    """抓取 run 相关全部落库数据的 sha256 指纹 (只读, 不修改任何表)。

    覆盖六张表: runs 行 / task_results / dependency_checks / blockers /
    plan_definitions / plan_results, 全部按 run_id 过滤。
    返回 {表名: sha256hex}。
    """
    conn = get_conn()
    try:
        fp = {}
        for table, pk in _FINGERPRINT_TABLES:
            rows = conn.execute(
                "SELECT * FROM %s WHERE run_id=? ORDER BY %s"
                % (table, pk), (run_id,)).fetchall()
            rows = [dict(r) for r in rows]
            payload = json.dumps(rows, ensure_ascii=False,
                                 sort_keys=True).encode("utf-8")
            fp[table] = hashlib.sha256(payload).hexdigest()
    finally:
        close_conn(conn)
    return fp


def verify_fingerprint(before: dict, after: dict) -> tuple:
    """对比推演前后指纹, 返回 (intact, changed_tables)。

    intact=True 表示六张表全部一致 (基线无污染); changed_tables 列出
    before 中缺失的表 (防御: 覆盖范围缩小也视为不一致)。
    """
    changed = []
    for table in before:
        if table not in after or after[table] != before[table]:
            changed.append(table)
    for table in after:
        if table not in before:
            changed.append(table)
    return (not changed), sorted(set(changed))


def run_whatif_pipeline(parent_run_id: str, assumptions: dict,
                        scenario_id: str = None) -> tuple:
    """what-if 全链路推演: 创建 → T1~T5 → 缺口 → 方案 → 方案判定。

    - parent_run_id 必填 (reality 基线); scenario_id 缺省取 parent 的
      scenario_id (创建时读取);
    - 每步结果独立落库 (run_id 隔离), parent 行零写入;
    - 返回 (ok, http_status, msg, data); data = {
        parent_run_id, scenario_id, assumptions, run, execute, gaps,
        plans, plan_results}
    """
    if parent_run_id is None or not str(parent_run_id).strip():
        raise ValueError("parent_run_id required")
    parent_run_id = str(parent_run_id).strip()
    if scenario_id is None:
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT scenario_id FROM runs WHERE run_id=?",
                (parent_run_id,)).fetchone()
        finally:
            close_conn(conn)
        if row is None:
            return False, 404, "parent run %s not found" % parent_run_id, None
        scenario_id = row["scenario_id"]

    try:
        ok_c, st_c, msg_c, run = create_run(
            scenario_id, parent_run_id, RUN_TYPE_WHATIF, assumptions)
    except ValueError as exc:
        return False, 400, str(exc), None
    if not ok_c:
        return False, st_c, msg_c, None
    run_id = run["run_id"]

    ok_e, st_e, msg_e, exec_data = run_execute_all(run_id)
    if not ok_e:
        return False, st_e, msg_e, {
            "parent_run_id": parent_run_id, "run_id": run_id,
            "run": run, "execute_error": msg_e}
    ok_g, st_g, msg_g, gap_data = run_gap_check(run_id)
    if not ok_g:
        return False, st_g, msg_g, {
            "parent_run_id": parent_run_id, "run_id": run_id,
            "run": run, "execute": exec_data, "gaps_error": msg_g}
    ok_p, st_p, msg_p, plan_data = save_candidate_plans(run_id)
    if not ok_p:
        return False, st_p, msg_p, {
            "parent_run_id": parent_run_id, "run_id": run_id,
            "run": run, "execute": exec_data, "gaps": gap_data,
            "plans_error": msg_p}
    ok_j, st_j, msg_j, judge_data = run_plan_judge(run_id)
    if not ok_j:
        return False, st_j, msg_j, {
            "parent_run_id": parent_run_id, "run_id": run_id,
            "run": run, "execute": exec_data, "gaps": gap_data,
            "plans": plan_data, "plan_results_error": msg_j}
    return True, 200, (
        "what-if run %s executed (parent %s): T1~T5 + gap + plans + "
        "plan judge done" % (run_id, parent_run_id)), {
        "parent_run_id": parent_run_id,
        "scenario_id": scenario_id,
        "run_id": run_id,
        "assumptions": assumptions,
        "run": run,
        "execute": exec_data,
        "gaps": gap_data,
        "plans": plan_data,
        "plan_results": judge_data,
    }


def _plan_verdicts(judge_data) -> dict:
    """方案判定结果压缩为 {plan_name: status}, 便于日志/文档展示。"""
    verdicts = {}
    for r in (judge_data or {}).get("results") or []:
        verdicts[r.get("plan_name")] = r.get("status")
    return verdicts


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        print("usage: python whatif_run.py <parent_run_id> "
              "'<assumptions-json>' [scenario_id]", file=sys.stderr)
        sys.exit(2)
    parent_run_id = sys.argv[1]
    scenario_id = sys.argv[3] if len(sys.argv) == 4 else None
    try:
        assumptions = json.loads(sys.argv[2])
    except json.JSONDecodeError as exc:
        print("assumptions must be valid JSON: %s" % exc, file=sys.stderr)
        sys.exit(2)

    before = capture_fingerprint(parent_run_id)
    ok, http_status, msg, data = run_whatif_pipeline(
        parent_run_id, assumptions, scenario_id)
    after = capture_fingerprint(parent_run_id)
    intact, changed = verify_fingerprint(before, after)
    out = {
        "ok": ok,
        "http_status": http_status,
        "msg": msg,
        "data": data,
        "parent_baseline": {
            "parent_run_id": parent_run_id,
            "fingerprint_before": before,
            "fingerprint_after": after,
            "baseline_intact": intact,
            "changed_tables": changed,
        },
    }
    if ok:
        out["plan_verdicts"] = _plan_verdicts(
            (data or {}).get("plan_results"))
    print(json.dumps(out, ensure_ascii=False, indent=2))
    sys.exit(0 if (ok and intact) else 1)
