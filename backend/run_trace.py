"""阶段08板块1: run 追溯链路聚合 (只读, 供 GET /api/runs/{run_id}/trace).

把一次保障任务的完整溯源链路一次拉齐, 供前端「任务 → T状态 → 缺口 →
证据 → 方案」一路下钻:

    run          runs 行 (类型/状态/父子关系)
    ├─ tasks     T1~T5 任务结果 + dependency_checks 依赖明细
    ├─ blockers  缺口记录 (gap 数值 + reason_json)
    ├─ evidences 与本 run 关联的证据 (evidence_v2 溯源链路):
    │             · 签收/调配证据: source_id ∈ 快照 receipts/dispatch ids
    │             · 工具调用轨迹: value_json.args.run_id == run_id
    │             · LLM 解释:     value_json.run_id == run_id
    └─ plans     候选方案 + 可行性判定 (plan_definitions ∪ plan_results)

约束 (阶段08板块1):
    - 纯只读聚合, 不新增任何 T1~T5 判定逻辑, 不改动任何现有判定模块;
    - 天气/RAG/ML 等无 run 关联的全局辅助证据不混入本 run 追溯链
      (前端全局证据列表走既有 GET /api/evidences)。
"""

import json
import sqlite3

from db import close_conn, get_conn


def _parse_json(raw):
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass  # 防御脏数据: 保留原文
    return raw


def _task_rows(conn, run_id):
    """task_results + dependency_checks 明细 (evidence 字段解析为对象)。"""
    results = conn.execute(
        "SELECT * FROM task_results WHERE run_id=? ORDER BY task_id",
        (run_id,)).fetchall()
    checks = conn.execute(
        "SELECT * FROM dependency_checks WHERE run_id=? ORDER BY task_id, id",
        (run_id,)).fetchall()
    checks_by_task = {}
    for c in checks:
        d = dict(c)
        d["evidence"] = _parse_json(d["evidence"])
        checks_by_task.setdefault(d["task_id"], []).append(d)
    tasks = []
    for r in results:
        d = dict(r)
        d["dependency_checks"] = checks_by_task.get(d["task_id"], [])
        tasks.append(d)
    # 防御: 有依赖明细但缺任务结果行时也保留明细
    for task_id in checks_by_task:
        if task_id not in {t["task_id"] for t in tasks}:
            tasks.append({"task_id": task_id, "result_status": None,
                          "summary_reason": None,
                          "dependency_checks": checks_by_task[task_id]})
    return tasks


def _blocker_rows(conn, run_id):
    rows = conn.execute(
        "SELECT * FROM blockers WHERE run_id=? ORDER BY blocker_id",
        (run_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["reason_json"] = _parse_json(d["reason_json"])
        out.append(d)
    return out


def _linked_evidences(conn, snapshot):
    """与 run 关联的 evidence_v2 证据 (签收/调配直连 + tool/llm 轨迹)。"""
    receipts = snapshot.get("receipts") or []
    source_ids = {r.get("receipt_id") for r in receipts if r.get("receipt_id")}
    source_ids |= {r.get("dispatch_id") for r in receipts
                   if r.get("dispatch_id")}
    if not source_ids:
        return []
    rows = conn.execute(
        "SELECT * FROM evidence_v2 WHERE source_id IN (%s) "
        "ORDER BY created_at, evidence_id"
        % ", ".join("?" * len(source_ids)),
        tuple(source_ids)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["value_json"] = _parse_json(d.get("value_json"))
        out.append(d)
    return out


def _linked_evidences_by_field(conn, run_id):
    """工具调用轨迹 / LLM 解释证据: value_json 内嵌 run_id 匹配。

    SQLite 无 JSON1 扩展时无法用 json_extract, 采用全表扫 + Python 解析
    (证据表为本地轻量数据, 与阶段07板块3 无 JSON1 兼容口径一致)。
    """
    rows = conn.execute(
        "SELECT * FROM evidence_v2 WHERE evidence_type IN ('tool','llm') "
        "ORDER BY created_at, evidence_id").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        value = _parse_json(d.get("value_json"))
        matched = False
        if isinstance(value, dict):
            if value.get("run_id") == run_id:
                matched = True
            elif isinstance(value.get("args"), dict) \
                    and value["args"].get("run_id") == run_id:
                matched = True
        if matched:
            d["value_json"] = value
            out.append(d)
    return out


def _plan_rows(conn, run_id):
    rows = conn.execute(
        "SELECT pd.plan_id, pd.plan_name, pd.reserve_id, pd.vehicle_id, "
        "pd.route_id, pd.material_id, pd.transfer_qty, pd.created_at, "
        "pr.status, pr.reason_json, pr.created_at AS verdict_created_at "
        "FROM plan_definitions pd "
        "LEFT JOIN plan_results pr "
        "ON pr.run_id = pd.run_id AND pr.plan_id = pd.plan_id "
        "WHERE pd.run_id=? ORDER BY pd.plan_id",
        (run_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["reason_json"] = _parse_json(d["reason_json"])
        out.append(d)
    return out


def get_run_trace(run_id: str) -> tuple:
    """组装 run 追溯链路 (只读)。返回 (ok, http_status, msg, data)。

    data = {run, summary, tasks, blockers, evidences, plans, snapshot}
    - summary: T1~T5 result_status 压缩视图 + 统计
    - snapshot: 快照五段 (demands/stocks/transports/receipts) +
      可选 routes/remaining_time, 供前端缺口/依赖视图直接渲染
    """
    try:
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                return False, 404, "run %s not found" % run_id, None
            run = dict(row)
            for key in ("input_snapshot_json", "assumptions_json"):
                if run.get(key):
                    run[key] = _parse_json(run[key])
            snapshot = run.get("input_snapshot_json") or {}
            if isinstance(snapshot, str):
                snapshot = {}
            tasks = _task_rows(conn, run_id)
            summary = {
                t["task_id"]: t["result_status"] for t in tasks
                if t.get("result_status") is not None
            }
            evidences = _linked_evidences(conn, snapshot)
            evidences += _linked_evidences_by_field(conn, run_id)
            # 去重 (同一证据可能同时命中 source_id 与 run_id 两条规则)
            seen, unique = set(), []
            for e in evidences:
                if e["evidence_id"] in seen:
                    continue
                seen.add(e["evidence_id"])
                unique.append(e)
            snap_out = {k: snapshot.get(k) for k in (
                "demands", "stocks", "transports", "receipts",
                "routes", "remaining_time") if k in snapshot}
            return True, 200, "ok", {
                "run_id": run_id,
                "run": {k: run[k] for k in (
                    "run_id", "scenario_id", "run_type", "parent_run_id",
                    "run_status", "created_at", "completed_at")},
                "summary": summary,
                "tasks": tasks,
                "blockers": _blocker_rows(conn, run_id),
                "evidences": unique,
                "plans": _plan_rows(conn, run_id),
                "snapshot": snap_out,
            }
        finally:
            close_conn(conn)
    except sqlite3.Error as exc:
        return False, 500, "db error: %s" % exc, None


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("usage: python run_trace.py <run_id>", file=sys.stderr)
        sys.exit(2)
    ok, http_status, msg, data = get_run_trace(sys.argv[1])
    print(json.dumps({"ok": ok, "http_status": http_status, "msg": msg,
                      "data": data}, ensure_ascii=False, indent=2))
    sys.exit(0 if ok else 1)
