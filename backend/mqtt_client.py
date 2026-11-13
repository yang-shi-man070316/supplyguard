import asyncio
import json
import paho.mqtt.client as mqtt

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

def on_message(client, userdata, msg):
    payload = json.loads(msg.payload.decode("utf-8"))
    topic = msg.topic

    if topic.startswith("nova/bb03/reserve/"):
        rid = payload["reserve_id"]
        mid = payload["material_id"]
        current_stock[(rid, mid)] = payload
    elif topic.startswith("nova/bb03/service/"):
        sid = payload["service_id"]
        mid = payload["material_id"]
        current_demand[(sid, mid)] = payload
    elif topic.startswith("nova/bb03/transport/"):
        tid = payload["transport_id"]
        current_transport[tid] = payload

    # 跨线程投递到 uvicorn 的 event loop 里执行，再广播给前端
    if _main_loop is not None:
        _main_loop.call_soon_threadsafe(_push_snapshot, current_snapshot())

mqtt_client = mqtt.Client()
mqtt_client.on_message = on_message
mqtt_client.connect(BROKER, PORT, 60)
mqtt_client.subscribe("nova/bb03/#")
mqtt_client.loop_start()
