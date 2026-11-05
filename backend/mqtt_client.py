"""B-B03 阶段01: MQTT 订阅客户端.

订阅模拟器发布的三类 topic, 解析 payload 后把每个对象的最新状态
保存在内存字典 CURRENT_STATE 中。只维护 current, 不持久化、不保存历史。

topic 约定 (与 simulator/simulator.py 发布的一致):
    nova/bb03/reserve/{reserve_id}/stock       储备点库存
    nova/bb03/service/{service_id}/demand      服务点需求
    nova/bb03/transport/{transport_id}/status  运输资源状态
"""

import json
import logging
from datetime import datetime

from paho.mqtt import client as mqtt

logger = logging.getLogger("mqtt")

# 用 + 通配符订阅所有对象实例
TOPICS = [
    ("nova/bb03/reserve/+/stock", 1),
    ("nova/bb03/service/+/demand", 1),
    ("nova/bb03/transport/+/status", 1),
]

# 每个对象只保留最新一条状态, 不保存历史
CURRENT_STATE = {
    "reserves": {},     # reserve_id -> {"materials": {material_id: payload}}
    "services": {},     # service_id -> {"materials": {material_id: payload}}
    "transports": {},   # transport_id -> payload
    "updated_at": None,  # 最后收到消息的时间
}

_client = None


def start_mqtt(host: str = "127.0.0.1", port: int = 1883):
    """启动时连接 broker。连接失败只打警告, 由 paho 按策略自动重试, 不崩溃。"""
    global _client
    _client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    _client.on_connect = _on_connect
    _client.on_connect_fail = _on_connect_fail
    _client.on_message = _on_message
    _client.reconnect_delay_set(min_delay=2, max_delay=30)
    _client.loop_start()  # 先起循环, connect 失败时走 on_connect_fail 而非抛异常
    try:
        _client.connect(host, port, keepalive=60)
    except OSError:
        logger.warning("MQTT broker %s:%s unreachable, will retry in background (API keeps running)", host, port)


def stop_mqtt():
    global _client
    if _client is None:
        return
    _client.loop_stop()
    _client.disconnect()
    _client = None


def _on_connect(client, userdata, flags, reason_code, properties):
    logger.info("MQTT connected, subscribed %s", [t for t, _ in TOPICS])
    client.subscribe(TOPICS)


def _on_connect_fail(client, userdata):
    logger.warning("MQTT connect failed, retrying automatically")


def _on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("discarding unparsable message %s: %s", msg.topic, exc)
        return

    parts = msg.topic.split("/")
    kind, obj_id = parts[2], parts[3]
    if kind == "reserve":
        _update_material_state(CURRENT_STATE["reserves"], obj_id, payload)
    elif kind == "service":
        _update_material_state(CURRENT_STATE["services"], obj_id, payload)
    elif kind == "transport":
        CURRENT_STATE["transports"][obj_id] = payload
    else:
        logger.warning("unknown topic kind %s, ignored", msg.topic)
    CURRENT_STATE["updated_at"] = datetime.now().isoformat(timespec="seconds")


def _update_material_state(bucket: dict, obj_id: str, payload: dict):
    """库存/需求按 (对象, 物资) 维度维护最新值, 同类消息后到覆盖先到。"""
    material_id = payload.get("material_id", "?")
    node = bucket.setdefault(obj_id, {"materials": {}})
    node["materials"][material_id] = payload
