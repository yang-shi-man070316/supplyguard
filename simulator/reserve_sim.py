import json
import time
import paho.mqtt.client as mqtt

BROKER = "127.0.0.1"
PORT = 1883

stock_data = [
    {"reserve_id":"R1","material_id":"W","quantity":6},
    {"reserve_id":"R1","material_id":"L","quantity":2},
    {"reserve_id":"R1","material_id":"R","quantity":4},
    {"reserve_id":"R2","material_id":"W","quantity":5},
    {"reserve_id":"R2","material_id":"L","quantity":3},
    {"reserve_id":"R2","material_id":"R","quantity":3},
]

client = mqtt.Client()
client.connect(BROKER, PORT, 60)

while True:
    for item in stock_data:
        msg = {
            **item,
            "observed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "source_type": "SIMULATED"
        }
        topic = f"nova/bb03/reserve/{item['reserve_id']}/stock"
        client.publish(topic, json.dumps(msg))
    time.sleep(5)
