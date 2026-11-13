import asyncio
import json
import traceback

import paho.mqtt.client as mqtt

import db  # 阶段02: 需求观测持久化 (只新增写入, 不改动阶段01内存逻辑)

BROKER = "127.0.0.1"
PORT = 1883

# ========= 阶段01：纯内存全局状态，不操作数据库 =========
current_stock = dict()    # key:(reserve_id,material_id) → dict
current_demand = dict()   # key:(service_id,material_id) → dict
current_transport = dict()# key:transport_id → dict

# 保存全部websocket连接，用来广播更新
ws_connections = set()

# uvicorn 的 event loop，由 main.py 注入（paho 回调跑在网络线程，不能直接 create_task）
_main_loop = None

def bind_loop(loop):
    global _main_loop
    _main_loop = loop

def current_snapshot():
    """组装完整状态快照，供广播和新连接首推复用"""
    return {
        "stock": list(current_stock.values()),
        "demand": list(current_demand.values()),
        "transport": list(current_transport.values())
    }

def _push_snapshot(snapshot):
    """在 event loop 线程内执行：给所有 websocket 客户端发快照"""
    for ws in list(ws_connections):
        try:
            asyncio.create_task(ws.send_json(snapshot))
        except Exception:
            ws_connections.discard(ws)

def _persist_demand(payload):
    """阶段02: 每条 demand 消息追加一行 demand_records, 只增不改、禁止覆盖历史。

    在 paho 网络线程内执行; get_conn 每次新建连接, 不存在跨线程共享连接问题。
    任何异常只打印不向外传播——保证阶段01的内存更新与 WebSocket 广播不受影响。
    """
    try:
        conn = db.get_conn()
        try:
            conn.execute(
                "INSERT INTO demand_records "
                "(service_id, material_id, required_quantity, observed_at, source_type) "
                "VALUES (?,?,?,?,?)",
                (
                    payload["service_id"],
                    payload["material_id"],
                    payload["required_quantity"],
                    payload["observed_at"],
                    payload.get("source_type", "MQTT"),
                ),
            )
            conn.commit()
        finally:
            db.close_conn(conn)
    except Exception:
        print("demand_records insert failed: "
              + json.dumps(payload, ensure_ascii=False))
        traceback.print_exc()


def _persist_stock(payload):
    """阶段02: 每条 stock 消息追加一行 stock_observations, 只增不改、禁止覆盖历史。

    异常只打印不向外传播 (同 _persist_demand)。
    """
    try:
        conn = db.get_conn()
        try:
            conn.execute(
                "INSERT INTO stock_observations "
                "(reserve_id, material_id, quantity, observed_at, source_type) "
                "VALUES (?,?,?,?,?)",
                (
                    payload["reserve_id"],
                    payload["material_id"],
                    payload["quantity"],
                    payload["observed_at"],
                    payload.get("source_type", "MQTT"),
                ),
            )
            conn.commit()
        finally:
            db.close_conn(conn)
    except Exception:
        print("stock_observations insert failed: "
              + json.dumps(payload, ensure_ascii=False))
        traceback.print_exc()


def _persist_transport(payload):
    """阶段02: 每条 transport 消息追加一行 transport_observations, 只增不改、禁止覆盖历史。

    异常只打印不向外传播 (同 _persist_demand)。capacity 可为空 (表列允许 NULL)。
    """
    try:
        conn = db.get_conn()
        try:
            conn.execute(
                "INSERT INTO transport_observations "
                "(transport_id, availability, capacity, observed_at, source_type) "
                "VALUES (?,?,?,?,?)",
                (
                    payload["transport_id"],
                    payload["availability"],
                    payload.get("capacity"),
                    payload["observed_at"],
                    payload.get("source_type", "MQTT"),
                ),
            )
            conn.commit()
        finally:
            db.close_conn(conn)
    except Exception:
        print("transport_observations insert failed: "
              + json.dumps(payload, ensure_ascii=False))
        traceback.print_exc()


def on_message(client, userdata, msg):
    payload = json.loads(msg.payload.decode("utf-8"))
    topic = msg.topic

    if topic.startswith("nova/bb03/reserve/"):
        rid = payload["reserve_id"]
        mid = payload["material_id"]
        current_stock[(rid, mid)] = payload
        _persist_stock(payload)  # 阶段02: 原内存更新之后追加落库
    elif topic.startswith("nova/bb03/service/"):
        sid = payload["service_id"]
        mid = payload["material_id"]
        current_demand[(sid, mid)] = payload
        _persist_demand(payload)  # 阶段02: 原内存更新之后追加落库
    elif topic.startswith("nova/bb03/transport/"):
        tid = payload["transport_id"]
        current_transport[tid] = payload
        _persist_transport(payload)  # 阶段02: 原内存更新之后追加落库

    # 跨线程投递到 uvicorn 的 event loop 里执行，再广播给前端
    if _main_loop is not None:
        _main_loop.call_soon_threadsafe(_push_snapshot, current_snapshot())

mqtt_client = mqtt.Client()
mqtt_client.on_message = on_message
mqtt_client.connect(BROKER, PORT, 60)
mqtt_client.subscribe("nova/bb03/#")
mqtt_client.loop_start()
