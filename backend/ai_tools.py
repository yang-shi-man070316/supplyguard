"""阶段06板块4: 4 个只读查询工具 + 调用轨迹 (tool trace) 自动落 evidence_v2.

工具 (全部只读 SELECT, 禁止修改任何业务数据/判定结果):
    get_current_demand(service_id=None)
        当前服务点需求: 每个 (服务点, 物资) 最新一条需求观测
        (口径与 T1 校验的 _latest_demands 一致)
    get_current_stock(reserve_id=None, material_id=None)
        当前储备点库存: 每个 (储备点, 物资) 最新一条库存观测
    get_task_result(run_id, task_id='T1')
        T1~T5 任务结果 + 依赖检查明细 (复用 t1_check.get_task_result, 只读)
    get_plan_results(run_id)
        候选方案 + 可行性判定 (复用 plan_store.get_plans_with_results, 只读)

调用轨迹 (板块红线之一):
    每次工具调用 (无论成功/失败/未找到) 自动追加一条 evidence_v2 轨迹:
        evidence_type='tool'
        source_id='tool:<工具名>'
        status='ok'   (工具执行完成, 含 404 未找到这类正常应答)
             |'error' (数据库异常)
        value_json = {tool, args, http_status, msg, payload}
    轨迹写入失败只打印不抛异常 —— 工具查询结果不受轨迹影响。
"""

import json
import sqlite3
import traceback
from datetime import datetime

from db import close_conn, get_conn
from plan_store import get_plans_with_results
from run_store import save_evidence
from t1_check import get_task_result as _query_task_result

TOOL_DEMAND = "get_current_demand"
TOOL_STOCK = "get_current_stock"
TOOL_TASK = "get_task_result"
TOOL_PLANS = "get_plan_results"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _write_trace(tool_name: str, args: dict, http_status: int, msg: str,
                 payload, status: str = "ok",
                 error_message: str = "") -> None:
    """写入一条 tool trace 到 evidence_v2 (只增不改)。

    轨迹写库失败绝不外抛: 工具查询本身的结果不受轨迹影响
    (辅助证据机制不阻塞主查询)。
    """
    try:
        save_evidence(
            evidence_type="tool",
            source_id="tool:%s" % tool_name,
            status=status,
            value_json=json.dumps({
                "tool": tool_name,
                "args": args,
                "http_status": http_status,
                "msg": msg,
                "payload": payload,
            }, ensure_ascii=False, default=str),
            observed_at=_now(),
            error_message=error_message,
        )
    except (ValueError, sqlite3.Error):
        print("[ai_tools] tool trace write failed for %s" % tool_name)
        traceback.print_exc()


def get_current_demand(service_id: str = None) -> tuple:
    """当前服务点需求查询 (只读): 每 (服务点, 物资) 最新一条需求观测。

    service_id 为空 → 全部服务点; 无匹配 → 空列表 (HTTP 200)。
    返回 (ok, http_status, msg, data);
    data = {tool, args, demand_count, demands:[...]}。
    """
    args = {"service_id": service_id}
    tool_name = TOOL_DEMAND
    try:
        conn = get_conn()
        try:
            if service_id:
                rows = conn.execute(
                    "SELECT service_id, material_id, required_quantity, "
                    "observed_at, source_type FROM demand_records d "
                    "WHERE d.demand_id = ("
                    "  SELECT MAX(d2.demand_id) FROM demand_records d2 "
                    "  WHERE d2.service_id=d.service_id "
                    "    AND d2.material_id=d.material_id) "
                    "AND d.service_id=? ORDER BY service_id, material_id",
                    (service_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT service_id, material_id, required_quantity, "
                    "observed_at, source_type FROM demand_records d "
                    "WHERE d.demand_id = ("
                    "  SELECT MAX(d2.demand_id) FROM demand_records d2 "
                    "  WHERE d2.service_id=d.service_id "
                    "    AND d2.material_id=d.material_id) "
                    "ORDER BY service_id, material_id",
                ).fetchall()
        finally:
            close_conn(conn)
    except sqlite3.Error as exc:
        _write_trace(tool_name, args, 500, "db error: %s" % exc, None,
                     status="error", error_message=str(exc))
        return False, 500, "db error: %s" % exc, None
    data = {"tool": tool_name, "args": args, "demand_count": len(rows),
            "demands": [dict(r) for r in rows]}
    _write_trace(tool_name, args, 200, "ok", data)
    return True, 200, "ok", data


def get_current_stock(reserve_id: str = None, material_id: str = None) -> tuple:
    """当前储备点库存查询 (只读): 每 (储备点, 物资) 最新一条库存观测。

    reserve_id / material_id 均可选, 可组合过滤; 无匹配 → 空列表。
    返回 (ok, http_status, msg, data);
    data = {tool, args, stock_count, stocks:[...]}。
    """
    args = {"reserve_id": reserve_id, "material_id": material_id}
    tool_name = TOOL_STOCK
    conds, params = [], []
    if reserve_id:
        conds.append("s.reserve_id=?")
        params.append(reserve_id)
    if material_id:
        conds.append("s.material_id=?")
        params.append(material_id)
    where = (" AND " + " AND ".join(conds)) if conds else ""
    try:
        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT reserve_id, material_id, quantity, observed_at "
                "FROM stock_observations s "
                "WHERE s.stock_id = ("
                "  SELECT MAX(s2.stock_id) FROM stock_observations s2 "
                "  WHERE s2.reserve_id=s.reserve_id "
                "    AND s2.material_id=s.material_id)" + where +
                " ORDER BY reserve_id, material_id",
                params,
            ).fetchall()
        finally:
            close_conn(conn)
    except sqlite3.Error as exc:
        _write_trace(tool_name, args, 500, "db error: %s" % exc, None,
                     status="error", error_message=str(exc))
        return False, 500, "db error: %s" % exc, None
    data = {"tool": tool_name, "args": args, "stock_count": len(rows),
            "stocks": [dict(r) for r in rows]}
    _write_trace(tool_name, args, 200, "ok", data)
    return True, 200, "ok", data


def get_task_result(run_id: str, task_id: str = "T1") -> tuple:
    """T1~T5 任务结果查询 (只读): 复用 t1_check.get_task_result 口径。

    run 不存在 → 404; 无结果 → task_result=None, dependency_checks=[]。
    返回 (ok, http_status, msg, data); data 含 tool/args 与查询结果。
    """
    args = {"run_id": run_id, "task_id": task_id}
    tool_name = TOOL_TASK
    ok, http_status, msg, data = _query_task_result(run_id, task_id)
    if not ok:
        _write_trace(tool_name, args, http_status, msg, None,
                     status="ok" if http_status == 404 else "error",
                     error_message="" if http_status == 404 else msg)
        return ok, http_status, msg, None
    out = {"tool": tool_name, "args": args}
    out.update(data)
    _write_trace(tool_name, args, 200, msg, data)
    return True, 200, msg, out


def get_plan_results(run_id: str) -> tuple:
    """候选方案+可行性判定查询 (只读): 复用 plan_store.get_plans_with_results。

    run 不存在 → 404; 返回 (ok, http_status, msg, data)。
    """
    args = {"run_id": run_id}
    tool_name = TOOL_PLANS
    ok, http_status, msg, data = get_plans_with_results(run_id)
    if not ok:
        _write_trace(tool_name, args, http_status, msg, None,
                     status="ok" if http_status == 404 else "error",
                     error_message="" if http_status == 404 else msg)
        return ok, http_status, msg, None
    out = {"tool": tool_name, "args": args}
    out.update(data)
    _write_trace(tool_name, args, 200, msg, data)
    return True, 200, msg, out


if __name__ == "__main__":
    import sys as _sys

    usage = ("usage:\n"
             "  python ai_tools.py demand [service_id]\n"
             "  python ai_tools.py stock [reserve_id] [material_id]\n"
             "  python ai_tools.py task <run_id> [task_id]\n"
             "  python ai_tools.py plans <run_id>\n")
    argv = _sys.argv[1:]
    if not argv:
        print(usage, file=_sys.stderr)
        _sys.exit(2)
    cmd = argv[0]
    if cmd == "demand":
        ok, http_status, msg, data = get_current_demand(
            argv[1] if len(argv) > 1 else None)
    elif cmd == "stock":
        ok, http_status, msg, data = get_current_stock(
            argv[1] if len(argv) > 1 else None,
            argv[2] if len(argv) > 2 else None)
    elif cmd == "task":
        if len(argv) < 2:
            print(usage, file=_sys.stderr)
            _sys.exit(2)
        ok, http_status, msg, data = get_task_result(
            argv[1], argv[2] if len(argv) > 2 else "T1")
    elif cmd == "plans":
        if len(argv) < 2:
            print(usage, file=_sys.stderr)
            _sys.exit(2)
        ok, http_status, msg, data = get_plan_results(argv[1])
    else:
        print(usage, file=_sys.stderr)
        _sys.exit(2)
    print(json.dumps({"ok": ok, "http_status": http_status, "msg": msg,
                      "data": data}, ensure_ascii=False, indent=2))
    _sys.exit(0 if ok else 1)
