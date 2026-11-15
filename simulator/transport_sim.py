import json
import sys
import time
import paho.mqtt.client as mqtt
from scenario import load_scenario

# 数据统一放在 scenario_data.json，修改保存后本进程会在下一个 5 秒周期自动热加载
BROKER = "127.0.0.1"
PORT = 1883

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.reconnect_delay_set(min_delay=1, max_delay=30)
try:
    client.connect(BROKER, PORT, 60)
except OSError as exc:
    sys.exit(f"无法连接 MQTT broker {BROKER}:{PORT} ({exc}) —— 请先启动 broker (如 mosquitto)")
# 网络循环线程: 维持 keepalive 心跳 + broker 断线自动重连, 否则连接约 90 秒后被 broker 断开
client.loop_start()

while True:
    data = load_scenario()
    if data:
        for item in data.get("transport", []):
            msg = {
                **item,
                "observed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "source_type": "SIMULATED"
            }
            topic = f"nova/bb03/transport/{item['transport_id']}/status"
            client.publish(topic, json.dumps(msg))
    time.sleep(5)
