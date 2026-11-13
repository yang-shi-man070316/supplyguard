import json
import time
import paho.mqtt.client as mqtt

BROKER = "127.0.0.1"
PORT = 1883

demand_data = [
    {"service_id":"P1","material_id":"W","required_quantity":4},
    {"service_id":"P1","material_id":"L","required_quantity":1},
    {"service_id":"P1","material_id":"R","required_quantity":2},
    {"service_id":"P2","material_id":"W","required_quantity":5},
    {"service_id":"P2","material_id":"L","required_quantity":1},
    {"service_id":"P2","material_id":"R","required_quantity":3},
    {"service_id":"P3","material_id":"W","required_quantity":3},
    {"service_id":"P3","material_id":"L","required_quantity":2},
    {"service_id":"P3","material_id":"R","required_quantity":2},
]

client = mqtt.Client()
client.connect(BROKER, PORT, 60)

while True:
    for item in demand_data:
        msg = {
            **item,
            "observed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "source_type": "SIMULATED"
        }
        topic = f"nova/bb03/service/{item['service_id']}/demand"
        client.publish(topic, json.dumps(msg))
    time.sleep(5)
