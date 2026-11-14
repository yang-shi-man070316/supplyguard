import asyncio
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from constants import RUN_TYPE
from db import close_conn, get_conn
from gap_check import get_blockers
from mqtt_client import ws_connections, bind_loop, current_snapshot
from plan_store import get_plans_with_results
from run_store_v2 import create_run, get_run, list_runs
from task_execute import run_execute_all
from t1_check import get_task_result

app = FastAPI(title="SupplyGuard‑阶段01 实时数据流")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
async def health():
    return {"status":"ok","stage":"01‑memory‑only"}

@app.get("/state")
async def get_state():
    """调试查看接口：返回当前内存中全部库存、需求、运输状态"""
    return current_snapshot()

@app.get("/api/history/demands")
def history_demands(service_id: str = None):
    """阶段02: 服务点需求历史 (只读, 直接透传 demand_records 原始字段)。

    使用示例:
        GET /api/history/demands              -> 全量, 按 observed_at 倒序
        GET /api/history/demands?service_id=P1 -> 仅 P1 的需求历史
    查询参数全部可选, 无匹配返回 []。"""
    conn = get_conn()
    try:
        if service_id:
            rows = conn.execute(
                "SELECT * FROM demand_records WHERE service_id=? "
                "ORDER BY observed_at DESC, demand_id DESC",
                (service_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM demand_records "
                "ORDER BY observed_at DESC, demand_id DESC"
            ).fetchall()
    finally:
        close_conn(conn)
    return [dict(r) for r in rows]

@app.get("/api/history/stocks")
def history_stocks(reserve_id: str = None):
    """阶段02: 储备点库存观测历史 (只读, 直接透传 stock_observations 原始字段)。

    使用示例:
        GET /api/history/stocks             -> 全量, 按 observed_at 倒序
        GET /api/history/stocks?reserve_id=R1 -> 仅 R1 的库存观测历史
    查询参数全部可选, 无匹配返回 []。"""
    conn = get_conn()
    try:
        if reserve_id:
            rows = conn.execute(
                "SELECT * FROM stock_observations WHERE reserve_id=? "
                "ORDER BY observed_at DESC, stock_id DESC",
                (reserve_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM stock_observations "
                "ORDER BY observed_at DESC, stock_id DESC"
            ).fetchall()
    finally:
        close_conn(conn)
    return [dict(r) for r in rows]

@app.get("/api/history/transports")
def history_transports(transport_id: str = None):
    """阶段02: 运输状态观测历史 (只读, 直接透传 transport_observations 原始字段;
    transport_observations 表无 service_id 列, 过滤参数为 transport_id)。

    使用示例:
        GET /api/history/transports               -> 全量, 按 observed_at 倒序
        GET /api/history/transports?transport_id=V1 -> 仅 V1 的运输状态历史
    查询参数全部可选, 无匹配返回 []。"""
    conn = get_conn()
    try:
        if transport_id:
            rows = conn.execute(
                "SELECT * FROM transport_observations WHERE transport_id=? "
                "ORDER BY observed_at DESC, observation_id DESC",
                (transport_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM transport_observations "
                "ORDER BY observed_at DESC, observation_id DESC"
            ).fetchall()
    finally:
        close_conn(conn)
    return [dict(r) for r in rows]

@app.get("/api/history/dispatches")
def history_dispatches(from_reserve_id: str = None, to_service_id: str = None):
    """阶段02: 调配记录历史 (只读, 直接透传 dispatch_records 原始字段;
    dispatch_records 表的事件时间列为 dispatched_at, 按它倒序)。

    使用示例:
        GET /api/history/dispatches            -> 全量, 按 dispatched_at 倒序
        GET /api/history/dispatches?from_reserve_id=R1 -> 仅 R1 发出的调配
        GET /api/history/dispatches?from_reserve_id=R1&to_service_id=P1 -> 双条件过滤
    查询参数全部可选, 可单独或组合使用, 无匹配返回 []。"""
    conn = get_conn()
    try:
        conds, params = [], []
        if from_reserve_id:
            conds.append("from_reserve_id=?")
            params.append(from_reserve_id)
        if to_service_id:
            conds.append("to_service_id=?")
            params.append(to_service_id)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        rows = conn.execute(
            "SELECT * FROM dispatch_records" + where +
            " ORDER BY dispatched_at DESC, dispatch_id DESC",
            params,
        ).fetchall()
    finally:
        close_conn(conn)
    return [dict(r) for r in rows]

@app.get("/api/history/receipts")
def history_receipts(service_id: str = None):
    """阶段02: 接收记录历史 (只读, 直接透传 receipt_records_v2 原始字段;
    旧 receipt_records 表原样保留不动)。

    使用示例:
        GET /api/history/receipts              -> 全量, 按 observed_at 倒序
        GET /api/history/receipts?service_id=P1 -> 仅 P1 的接收记录历史
    查询参数全部可选, 无匹配返回 []。"""
    conn = get_conn()
    try:
        if service_id:
            rows = conn.execute(
                "SELECT * FROM receipt_records_v2 WHERE service_id=? "
                "ORDER BY observed_at DESC, id DESC",
                (service_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM receipt_records_v2 ORDER BY observed_at DESC, id DESC"
            ).fetchall()
    finally:
        close_conn(conn)
    return [dict(r) for r in rows]

@app.get("/api/evidences")
def list_evidences(evidence_type: str = None, source_id: str = None):
    """阶段02: 统一证据表查询 (只读, 直接透传 evidence_v2 原始字段;
    旧 evidence 表为历史遗留, 原样保留不动, 本接口不读取)。

    使用示例:
        GET /api/evidences                          -> 全量证据, 按 observed_at 倒序
        GET /api/evidences?evidence_type=dispatch   -> 仅调配类证据
        GET /api/evidences?source_id=RCT-2026-001   -> 仅该来源业务记录的证据
    查询参数全部可选, 可单独或组合使用, 无匹配返回 []。"""
    conn = get_conn()
    try:
        conds, params = [], []
        if evidence_type:
            conds.append("evidence_type=?")
            params.append(evidence_type)
        if source_id:
            conds.append("source_id=?")
            params.append(source_id)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        rows = conn.execute(
            "SELECT * FROM evidence_v2" + where +
            " ORDER BY observed_at DESC, created_at DESC, evidence_id DESC",
            params,
        ).fetchall()
    finally:
        close_conn(conn)
    return [dict(r) for r in rows]

# ============================================================
# 阶段03板块2: run 生命周期接口 (reality run + 快照冻结)
# 只做 run 创建/查询与快照抓取冻结, 不做任何 T1~T5 验算逻辑
# (execute 验算接口由板块3 提供, 见下方)。
# ============================================================

class RunCreate(BaseModel):
    """POST /api/runs 入参。"""
    scenario_id: str
    parent_run_id: str = None
    run_type: str = RUN_TYPE


@app.post("/api/runs", status_code=201)
def api_create_run(body: RunCreate):
    """创建 S1 reality run。

    - run_id 自动生成 (BB03-RUN-001/002/... 自增)
    - 抓取当前时刻服务点/物资/需求/接收数据冻结进 input_snapshot_json
    - assumptions_json 初始为空 JSON 对象 {}
    - run_status=pending; parent_run_id 默认 null; created_at 自动写入
    """
    if not body.scenario_id.strip():
        raise HTTPException(status_code=400, detail="scenario_id required")
    if body.run_type != RUN_TYPE:
        raise HTTPException(status_code=400,
                            detail="only run_type=%r supported" % RUN_TYPE)
    ok, msg, run = create_run(body.scenario_id, body.parent_run_id, body.run_type)
    if not ok:
        raise HTTPException(status_code=500, detail=msg)
    return run


@app.get("/api/runs")
def api_list_runs():
    """run 列表: run_id / scenario_id / run_type / parent_run_id /
    run_status / created_at / completed_at (不含快照列)。"""
    return list_runs()


@app.get("/api/runs/{run_id}")
def api_get_run(run_id: str):
    """单 run 完整详情 (含 input_snapshot_json / assumptions_json);
    run_id 不存在返回 404。"""
    run = get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run %s not found" % run_id)
    return run


# ============================================================
# 阶段03板块3: T1 execute 验算接口 + 任务结果查询
# 只实现 T1 校验闭环, 不处理 T2~T5。
# ============================================================

@app.post("/api/runs/{run_id}/execute")
def api_execute_run(run_id: str):
    """执行 T1~T5 全套验算 (板块4): 读 run 快照 → 串行执行 T1、T2、T3、T4、T5
    → 每个任务独立写 dependency_checks + task_results
    → 全部完成后 run 置 completed (写入 completed_at)。

    - run_id 不存在 → 404; 已完成(completed)的 run → 409 拒绝执行
    - 整套执行同一事务; 完成后 run 快照冻结, 禁止任何修改 (触发器兜底)
    """
    ok, http_status, msg, data = run_execute_all(run_id)
    if not ok:
        raise HTTPException(status_code=http_status, detail=msg)
    return data


@app.get("/api/runs/{run_id}/task-result")
def api_get_task_result(run_id: str, task_id: str = "T1"):
    """查询任务结果: T1 任务结果 + 该 run 下全部依赖检查明细
    (含 evidence / reason)。未执行时 task_result 为 null、明细为空数组。

    - run_id 不存在 → 404; 板块3 只支持 task_id=T1 (其余任务板块4 实现)
    """
    ok, http_status, msg, data = get_task_result(run_id, task_id)
    if not ok:
        raise HTTPException(status_code=http_status, detail=msg)
    return data


# ============================================================
# 阶段04板块4: Blockers 缺口查询 + 候选方案评估查询 (只读接口)
# - blockers 来自板块1 gap_check 落库 (含 gap 数值与缺口描述文本)
# - plans 合并板块2 plan_definitions 基础信息 + 板块3 plan_results
#   判定结果; 未判定方案 status 为 null
# ============================================================

@app.get("/api/runs/{run_id}/blockers")
def api_get_blockers(run_id: str):
    """当前 run 全部 blocker 缺口记录: gap 数值 + 缺口描述文本
    (reason_json.summary)。run 不存在 → 404。"""
    ok, http_status, msg, data = get_blockers(run_id)
    if not ok:
        raise HTTPException(status_code=http_status, detail=msg)
    return data


@app.get("/api/runs/{run_id}/plans")
def api_get_plans(run_id: str):
    """合并返回候选方案基础信息 + 可行性判定结果: 每条方案携带方案标识
    (plan_name)、status (executable / non_executable, 未判定 null)、
    结构化 reason_json。run 不存在 → 404。"""
    ok, http_status, msg, data = get_plans_with_results(run_id)
    if not ok:
        raise HTTPException(status_code=http_status, detail=msg)
    return data


@app.websocket("/ws/guarantee")
async def ws_guarantee(websocket: WebSocket):
    bind_loop(asyncio.get_running_loop())
    await websocket.accept()
    ws_connections.add(websocket)
    # 新连接首推当前快照，不用等下一条MQTT消息
    snapshot = current_snapshot()
    if snapshot["stock"] or snapshot["demand"] or snapshot["transport"]:
        try:
            await websocket.send_json(snapshot)
        except Exception:
            pass
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        ws_connections.discard(websocket)
