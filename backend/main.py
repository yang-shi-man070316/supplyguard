import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from db import close_conn, get_conn
from mqtt_client import ws_connections, bind_loop, current_snapshot

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
