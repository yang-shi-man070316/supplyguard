import json
import time
import paho.mqtt.client as mqtt
from scenario import load_scenario

# 数据统一放在 scenario_data.json，修改保存后本进程会在下一个 5 秒周期自动热加载
BROKER = "127.0.0.1"
PORT = 1883

client = mqtt.Client()
client.connect(BROKER, PORT, 60)

while True:
    data = load_scenario()
    if data:
        for item in data.get("demand", []):
            msg = {
                **item,
                "observed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "source_type": "SIMULATED"
            }
            topic = f"nova/bb03/service/{item['service_id']}/demand"
            client.publish(topic, json.dumps(msg))
    time.sleep(5)
