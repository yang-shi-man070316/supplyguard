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
    """阶段02: 服务点需求历史 (只读)。传 service_id 返回该服务点全部历史,
    不传返回整表; 均按 observed_at 升序 (demand_id 升序兜底), 无匹配返回 []。"""
    conn = get_conn()
    try:
        if service_id:
            rows = conn.execute(
                "SELECT * FROM demand_records WHERE service_id=? "
                "ORDER BY observed_at ASC, demand_id ASC",
                (service_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM demand_records ORDER BY observed_at ASC, demand_id ASC"
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
