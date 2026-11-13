import json
import time
import paho.mqtt.client as mqtt

BROKER = "127.0.0.1"
PORT = 1883

transport_data = [
    {"transport_id":"V1","availability":"AVAILABLE","capacity":4},
    {"transport_id":"V2","availability":"AVAILABLE","capacity":4},
]

client = mqtt.Client()
client.connect(BROKER, PORT, 60)

while True:
    for item in transport_data:
        msg = {
            **item,
            "observed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "source_type": "SIMULATED"
        }
        topic = f"nova/bb03/transport/{item['transport_id']}/status"
        client.publish(topic, json.dumps(msg))
    time.sleep(5)
