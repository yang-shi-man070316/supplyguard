"""B-B03 阶段01: MQTT 模拟器.

模拟三类消息并发布到 MQTT broker:

1. 储备点库存消息    nova/bb03/reserve/{reserve_id}/stock
2. 服务点需求消息    nova/bb03/service/{service_id}/demand
3. 运输资源状态消息  nova/bb03/transport/{transport_id}/status

模拟对象:
    储备点 R1、R2; 服务点 P1、P2、P3; 物资 W/L/R; 运输 V1、V2

用法:
    python simulator/simulator.py                # 每 10 秒一轮, 直到 Ctrl+C
    python simulator/simulator.py --interval 5   # 5 秒一轮
    python simulator/simulator.py --rounds 3     # 发布 3 轮后退出
    python simulator/simulator.py --seed 42      # 固定随机种子, 结果可复现

依赖: paho-mqtt>=2.0 (pip install paho-mqtt)
运行前需先启动 MQTT broker (默认 127.0.0.1:1883, 如 mosquitto).
"""

import argparse
import json
import random
import sys
import time
from datetime import datetime

from paho.mqtt import client as mqtt

# --- 模拟对象 ---
RESERVES = ["R1", "R2"]
SERVICES = ["P1", "P2", "P3"]
MATERIALS = ["W", "L", "R"]
TRANSPORTS = ["V1", "V2"]

SOURCE_TYPE = "SIMULATED"
QOS = 1


def now_iso() -> str:
    """当前时间, 与消息示例一致的 ISO 格式 (无时区)."""
    return datetime.now().isoformat(timespec="seconds")


def reserve_stock_payload(reserve_id: str, material_id: str, quantity: int) -> dict:
    return {
        "reserve_id": reserve_id,
        "material_id": material_id,
        "quantity": quantity,
        "observed_at": now_iso(),
        "source_type": SOURCE_TYPE,
    }


def service_demand_payload(service_id: str, material_id: str, required_quantity: int) -> dict:
    return {
        "service_id": service_id,
        "material_id": material_id,
        "required_quantity": required_quantity,
        "observed_at": now_iso(),
        "source_type": SOURCE_TYPE,
    }


def transport_status_payload(transport_id: str, availability: str, capacity: int) -> dict:
    return {
        "transport_id": transport_id,
        "availability": availability,
        "capacity": capacity,
        "observed_at": now_iso(),
        "source_type": SOURCE_TYPE,
    }


class Simulator:
    def __init__(self, host: str, port: int, interval: float, seed):
        self.interval = interval
        self.rng = random.Random(seed)
        # 库存世界状态: (reserve_id, material_id) -> quantity, 初始 3-8
        self.stocks = {
            (r, m): self.rng.randint(3, 8)
            for r in RESERVES
            for m in MATERIALS
        }
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        try:
            self.client.connect(host, port, keepalive=60)
        except OSError as exc:
            sys.exit(f"无法连接 MQTT broker {host}:{port} ({exc}) —— 请先启动 broker (如 mosquitto)")
        self.client.loop_start()

    def publish(self, topic: str, payload: dict):
        message = json.dumps(payload, ensure_ascii=False)
        self.client.publish(topic, message, qos=QOS)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {topic} <- {message}")

    def step_world(self):
        """库存随机游走 ±1, 限制在 0-10, 让模拟数据随时间演化."""
        for key in self.stocks:
            self.stocks[key] = max(0, min(10, self.stocks[key] + self.rng.randint(-1, 1)))

    def run_round(self):
        # ① 储备点库存: R1/R2 × W/L/R, 每轮 6 条
        for reserve_id in RESERVES:
            for material_id in MATERIALS:
                self.publish(
                    f"nova/bb03/reserve/{reserve_id}/stock",
                    reserve_stock_payload(reserve_id, material_id, self.stocks[(reserve_id, material_id)]),
                )
        # ② 服务点需求: 每个服务点每轮随机一种物资, 每轮 3 条
        for service_id in SERVICES:
            material_id = self.rng.choice(MATERIALS)
            self.publish(
                f"nova/bb03/service/{service_id}/demand",
                service_demand_payload(service_id, material_id, self.rng.randint(2, 8)),
            )
        # ③ 运输资源状态: 每轮 2 条, 多数时间 AVAILABLE, 偶尔 IN_TRANSIT
        for transport_id in TRANSPORTS:
            availability = self.rng.choices(["AVAILABLE", "IN_TRANSIT"], weights=[7, 3])[0]
            self.publish(
                f"nova/bb03/transport/{transport_id}/status",
                transport_status_payload(transport_id, availability, self.rng.randint(4, 8)),
            )
        self.step_world()

    def close(self):
        self.client.loop_stop()
        self.client.disconnect()


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows 下避免中文输出乱码

    parser = argparse.ArgumentParser(description="B-B03 阶段01 MQTT 模拟器")
    parser.add_argument("--host", default="127.0.0.1", help="MQTT broker 地址 (默认 127.0.0.1)")
    parser.add_argument("--port", type=int, default=1883, help="MQTT broker 端口 (默认 1883)")
    parser.add_argument("--interval", type=float, default=10.0, help="每轮间隔秒数 (默认 10)")
    parser.add_argument("--rounds", type=int, default=0, help="发布轮数, 0 表示无限运行 (默认 0)")
    parser.add_argument("--seed", type=int, default=None, help="随机种子, 默认每次随机")
    args = parser.parse_args()

    sim = Simulator(args.host, args.port, args.interval, args.seed)
    print(f"模拟器已启动, 目标 broker {args.host}:{args.port}, 每轮 {args.interval}s, Ctrl+C 停止")
    round_no = 0
    try:
        while args.rounds == 0 or round_no < args.rounds:
            round_no += 1
            print(f"--- 第 {round_no} 轮 ---")
            sim.run_round()
            if args.rounds == 0 or round_no < args.rounds:
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C, 停止模拟")
    finally:
        sim.close()


if __name__ == "__main__":
    main()
