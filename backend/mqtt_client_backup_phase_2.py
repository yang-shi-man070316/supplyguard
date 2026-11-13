"""B-B03 阶段01: MQTT 订阅客户端.

订阅模拟器发布的三类 topic, 解析 payload 后把每个对象的最新状态
保存在内存字典 CURRENT_STATE 中。只维护 current, 不持久化、不保存历史。

topic 约定 (与 simulator/simulator.py 发布的一致):
    nova/bb03/reserve/{reserve_id}/stock       储备点库存
    nova/bb03/service/{service_id}/demand      服务点需求
    nova/bb03/transport/{transport_id}/status  运输资源状态

告警: 收到消息更新状态后同步全量重算三类告警条件, 结果保存在内存
列表 ALERTS 中 (不落库):
    low_stock:      储备点某物资库存 < 2
    demand_overrun: 服务点某物资需求缺口 > 5 (缺口 = 需求 - 全网该物资库存)
    vehicle_fault:  运输车 availability == "FAULT"
同一资源同一告警 active 期间不重复新增; 条件恢复时置 active=false, 不删除记录.

落库 (任务2-2): 每条收到的消息同时写对应观测表 (stock_observations /
demand_records / transport_observations) 和一条 status=ok 的 evidence 记录。
写库失败只记日志, 不影响内存状态更新。
"""

import json
import logging
import sqlite3
from datetime import datetime

from paho.mqtt import client as mqtt

import db

logger = logging.getLogger("mqtt")

# 用 + 通配符订阅所有对象实例
TOPICS = [
    ("nova/bb03/reserve/+/stock", 1),
    ("nova/bb03/service/+/demand", 1),
    ("nova/bb03/transport/+/status", 1),
    ("nova/bb03/reserve/+/receipt", 1),  # 任务-独立: 回执报文
]

# 每个对象只保留最新一条状态, 不保存历史
CURRENT_STATE = {
    "reserves": {},     # reserve_id -> {"materials": {material_id: payload}}
    "services": {},     # service_id -> {"materials": {material_id: payload}}
    "transports": {},   # transport_id -> payload
    "updated_at": None,  # 最后收到消息的时间
}

# ---- 告警状态 (只存内存, 不落库) ----
LOW_STOCK_THRESHOLD = 2
GAP_THRESHOLD = 5

ALERTS = []          # 全部告警记录 (含已恢复的), 按触发时间排序
_next_alert_id = 1   # alert_id 自增, 程序启动从 1 开始
_active_alerts = {}  # (type, resource_type, resource_id, material) -> alert, 活跃告警去重索引

_client = None


def start_mqtt(host: str = "127.0.0.1", port: int = 1883):
    """启动时连接 broker。连接失败只打警告, 由 paho 按策略自动重试, 不崩溃。"""
    global _client
    _ensure_alert_persistence()  # 任务2-4: 补齐告警表缺列 + 初始化 alert_id 计数器
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
    # 接收/解析异常 (任务-独立): 只落一条 status=unavailable 的 evidence,
    # 不更新内存状态、不写业务观测表、不触发告警。正常路径逻辑保持不变。
    raw_text = None
    try:
        raw_text = msg.payload.decode("utf-8")
        payload = json.loads(raw_text)
        if not isinstance(payload, dict):
            raise ValueError("payload is not a JSON object")
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("discarding unparsable message %s: %s", msg.topic, exc)
        _persist_unavailable_evidence(
            msg.topic,
            raw_text if raw_text is not None else repr(msg.payload),
            exc,
        )
        return

    parts = msg.topic.split("/")
    if len(parts) < 4:
        logger.warning("discarding message with bad topic %s", msg.topic)
        _persist_unavailable_evidence(msg.topic, raw_text, ValueError("bad topic"))
        return
    kind, obj_id = parts[2], parts[3]
    # 任务-独立: 回执 topic (5段) 先于 stock 分支分流, 原有分支逻辑不动
    if kind == "reserve" and len(parts) == 5 and parts[4] == "receipt":
        _handle_receipt(obj_id, payload)
        return
    if kind == "reserve":
        _update_material_state(CURRENT_STATE["reserves"], obj_id, payload)
    elif kind == "service":
        _update_material_state(CURRENT_STATE["services"], obj_id, payload)
    elif kind == "transport":
        CURRENT_STATE["transports"][obj_id] = payload
    else:
        logger.warning("unknown topic kind %s, ignored", msg.topic)
        _persist_unavailable_evidence(msg.topic, raw_text, ValueError("unknown topic kind"))
        return
    CURRENT_STATE["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _recompute_alerts()
    _persist_observation(kind, obj_id, payload)


def _update_material_state(bucket: dict, obj_id: str, payload: dict):
    """库存/需求按 (对象, 物资) 维度维护最新值, 同类消息后到覆盖先到。"""
    material_id = payload.get("material_id", "?")
    node = bucket.setdefault(obj_id, {"materials": {}})
    node["materials"][material_id] = payload


# ---- 回执处理 (任务-独立: 去重 + 库存变更) ----
_processed_receipt_ids = set()  # 内存去重快路径, 库 receipt_id 主键为准 (重启后仍兜底)


def _handle_receipt(reserve_id: str, payload: dict):
    """回执报文: 一条 receipt_id 只生效一次。

    INSERT OR IGNORE 原子去重: rowcount=1 (首次) 才应用库存变更;
    重复回执只记日志, 不改库存、不新增行。写库失败不应用变更 (下次重推可再插)。
    """
    receipt_id = payload.get("receipt_id")
    if not receipt_id:
        logger.warning("receipt without receipt_id, ignored")
        return
    if receipt_id in _processed_receipt_ids:
        logger.warning("duplicate receipt %s, ignored", receipt_id)
        return

    material_id = payload.get("material_id")
    quantity_change = payload.get("quantity_change", 0)
    now = datetime.now().isoformat(timespec="seconds")
    try:
        conn = db.get_conn()
        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO receipt_records "
                "(receipt_id, reserve_id, material_id, quantity_change, received_at) "
                "VALUES (?,?,?,?,?)",
                (receipt_id, reserve_id, material_id, quantity_change, now),
            )
            applied = cur.rowcount == 1
            conn.commit()
        finally:
            db.close_conn(conn)
    except sqlite3.Error as exc:
        logger.warning("persist receipt %s failed: %s", receipt_id, exc)
        return  # 无回执记录则视为未收到, 不应用变更

    _processed_receipt_ids.add(receipt_id)
    if not applied:
        logger.warning("duplicate receipt %s, ignored", receipt_id)
        return

    # 首次生效: 更新内存库存基线 (无基线时记日志, 不凭空造库存)
    item = (
        CURRENT_STATE["reserves"].get(reserve_id, {}).get("materials", {}).get(material_id)
    )
    if item is None:
        logger.warning("receipt %s recorded but no stock baseline for %s/%s",
                       receipt_id, reserve_id, material_id)
    else:
        item["quantity"] = max(item.get("quantity", 0) + quantity_change, 0)
    CURRENT_STATE["updated_at"] = now
    _recompute_alerts()  # 库存变化后重算告警 (复用现有逻辑)
    logger.info("receipt %s applied: %s/%s %+d", receipt_id, reserve_id, material_id, quantity_change)


# ---- 告警计算 ----

def _total_stock(material_id: str) -> int:
    """全网储备点某物资库存合计, 作为服务点需求缺口的参照。"""
    total = 0
    for node in CURRENT_STATE["reserves"].values():
        item = node["materials"].get(material_id)
        if item:
            total += item.get("quantity", 0)
    return total


def _set_alert(key: tuple, condition: bool, make_message):
    """条件满足且无活跃告警 → 新增; 条件恢复且告警活跃 → active=false, 记录保留。

    同步落库 (任务2-4): 新告警 INSERT; 活跃期间重复触发不新增行, 只刷新库里的
    updated_at; 恢复时置 active=0 (resolved)。写库失败只记日志, 不影响内存告警。
    """
    existing = _active_alerts.get(key)
    if condition and existing is None:
        global _next_alert_id
        alert = {
            "alert_id": _next_alert_id,
            "type": key[0],
            "resource_type": key[1],
            "resource_id": key[2],
            "material": key[3],
            "message": make_message(),
            "create_at": datetime.now().isoformat(timespec="seconds"),
            "active": True,
        }
        _next_alert_id += 1
        ALERTS.append(alert)
        _active_alerts[key] = alert
        _persist_alert_change("insert", alert)
    elif condition and existing is not None:
        _persist_alert_change("touch", existing)  # 活跃期间重复触发: 只刷时间/状态
    elif not condition and existing is not None:
        existing["active"] = False
        del _active_alerts[key]
        _persist_alert_change("resolve", existing)


def _ensure_alert_persistence():
    """任务2-4: 补齐 alert_records 缺列 (幂等) 并初始化 alert_id 计数器。

    resource_id / updated_at 为 2-1 建表后新增需求所需, 不新建表、不改 db.py。
    """
    global _next_alert_id
    try:
        conn = db.get_conn()
        try:
            for ddl in (
                "ALTER TABLE alert_records ADD COLUMN resource_id TEXT",
                "ALTER TABLE alert_records ADD COLUMN updated_at TEXT",
            ):
                try:
                    conn.execute(ddl)
                except sqlite3.OperationalError:
                    pass  # 列已存在
            conn.commit()
            row = conn.execute(
                "SELECT COALESCE(MAX(alert_id), 0) FROM alert_records"
            ).fetchone()
            _next_alert_id = row[0] + 1  # 重启后不撞库中历史 alert_id
        finally:
            db.close_conn(conn)
    except sqlite3.Error as exc:
        logger.warning("alert table setup failed: %s", exc)


def _persist_alert_change(action: str, alert: dict):
    """告警状态变化同步到 alert_records 表。写库失败只记日志。"""
    now = datetime.now().isoformat(timespec="seconds")
    try:
        conn = db.get_conn()
        try:
            if action == "insert":
                conn.execute(
                    "INSERT INTO alert_records "
                    "(alert_id, type, resource_type, resource_id, material, active, "
                    "message, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (alert["alert_id"], alert["type"], alert["resource_type"],
                     alert["resource_id"], alert["material"], 1, alert["message"],
                     alert["create_at"], now),
                )
            elif action == "touch":
                conn.execute(
                    "UPDATE alert_records SET updated_at=?, active=1 "
                    "WHERE alert_id=? AND active=1",
                    (now, alert["alert_id"]),
                )
            elif action == "resolve":
                conn.execute(
                    "UPDATE alert_records SET updated_at=?, active=0 WHERE alert_id=?",
                    (now, alert["alert_id"]),
                )
            conn.commit()
        finally:
            db.close_conn(conn)
    except sqlite3.Error as exc:
        logger.warning("persist alert %s/%s failed: %s", action, alert.get("alert_id"), exc)


def _recompute_alerts():
    """收到任何消息后全量重算三类告警条件。数据量小, 全量重算保证恢复逻辑不漏。"""
    # 储备点低库存
    for reserve_id, node in CURRENT_STATE["reserves"].items():
        for material_id, item in node["materials"].items():
            low = item.get("quantity", 0) < LOW_STOCK_THRESHOLD
            _set_alert(
                ("low_stock", "reserve", reserve_id, material_id),
                low,
                lambda r=reserve_id, i=item: "储备点 %s 物资 %s 库存过低 (当前 %s, 阈值 %s)"
                % (r, i.get("material_id", "?"), i.get("quantity"), LOW_STOCK_THRESHOLD),
            )
    # 服务点需求缺口
    for service_id, node in CURRENT_STATE["services"].items():
        for material_id, item in node["materials"].items():
            gap = max(item.get("required_quantity", 0) - _total_stock(material_id), 0)
            _set_alert(
                ("demand_overrun", "service", service_id, material_id),
                gap > GAP_THRESHOLD,
                lambda s=service_id, m=material_id, g=gap: "服务点 %s 物资 %s 需求缺口过大 (缺口 %s, 阈值 %s)"
                % (s, m, g, GAP_THRESHOLD),
            )
    # 运输车故障
    for transport_id, item in CURRENT_STATE["transports"].items():
        fault = item.get("availability") == "FAULT"
        _set_alert(
            ("vehicle_fault", "transport", transport_id, ""),
            fault,
            lambda t=transport_id: "运输车 %s 故障 (FAULT)" % t,
        )


# ---- 落库 (任务2-2) ----
# evidence_type 按任务2-2需求: stock / demand / transport
_EVIDENCE_TYPE_MAP = {"reserve": "stock", "service": "demand", "transport": "transport"}


def _persist_unavailable_evidence(topic: str, raw_text: str, exc: Exception):
    """接收/解析失败 → 只落一条 status=unavailable 的 evidence (不动内存与业务表)。

    evidence_type 尽量从 topic 推断, 推断不出用 unknown; source_id 同理, 保底用整条 topic。
    """
    parts = topic.split("/")
    kind, source_id = (parts[2], parts[3]) if len(parts) >= 4 else (None, None)
    evidence_type = _EVIDENCE_TYPE_MAP.get(kind, "unknown")
    now = datetime.now().isoformat(timespec="seconds")
    try:
        conn = db.get_conn()
        try:
            conn.execute(
                "INSERT INTO evidence "
                "(evidence_type, source_id, status, value_json, observed_at, retrieved_at, error_message) "
                "VALUES (?,?,?,?,?,?,?)",
                (evidence_type, source_id or topic, "unavailable", raw_text or "",
                 now, now, "%s: %s" % (type(exc).__name__, exc)),
            )
            conn.commit()
        finally:
            db.close_conn(conn)
    except sqlite3.Error as db_exc:
        logger.warning("persist unavailable evidence failed: %s", db_exc)

def _persist_observation(kind: str, obj_id: str, payload: dict):
    """每条消息写一条观测记录 + 一条 status=ok 的 evidence。

    写库失败只记日志, 绝不抛出——内存状态更新在此调用之前已完成。
    """
    try:
        conn = db.get_conn()
        try:
            if kind == "reserve":
                conn.execute(
                    "INSERT INTO stock_observations "
                    "(reserve_id, material_id, quantity, observed_at, source_type) "
                    "VALUES (?,?,?,?,?)",
                    (payload.get("reserve_id"), payload.get("material_id"),
                     payload.get("quantity"), payload.get("observed_at"),
                     payload.get("source_type")),
                )
            elif kind == "service":
                conn.execute(
                    "INSERT INTO demand_records "
                    "(service_id, material_id, required_quantity, observed_at, source_type) "
                    "VALUES (?,?,?,?,?)",
                    (payload.get("service_id"), payload.get("material_id"),
                     payload.get("required_quantity"), payload.get("observed_at"),
                     payload.get("source_type")),
                )
            elif kind == "transport":
                conn.execute(
                    "INSERT INTO transport_observations "
                    "(transport_id, availability, capacity, observed_at, source_type) "
                    "VALUES (?,?,?,?,?)",
                    (payload.get("transport_id"), payload.get("availability"),
                     payload.get("capacity"), payload.get("observed_at"),
                     payload.get("source_type")),
                )
            else:
                return  # 未知类型不落库

            conn.execute(
                "INSERT INTO evidence "
                "(evidence_type, source_id, status, value_json, observed_at, retrieved_at) "
                "VALUES (?,?,?,?,?,?)",
                (_EVIDENCE_TYPE_MAP[kind], obj_id, "ok", json.dumps(payload, ensure_ascii=False),
                 payload.get("observed_at"), datetime.now().isoformat(timespec="seconds")),
            )
            conn.commit()
        finally:
            db.close_conn(conn)
    except (sqlite3.Error, KeyError, TypeError) as exc:
        logger.warning("persist %s/%s failed: %s", kind, obj_id, exc)
