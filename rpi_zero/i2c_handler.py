# pip install paho-mqtt smbus2

import time
import threading
import struct
import json
import logging
import traceback

from smbus2 import SMBus
import paho.mqtt.client as mqtt
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

# =======================
# I2C CONFIG
# =======================
I2C_BUS = 1
MAX_CHARS = 32

MOTOR_UNIT_ADDRESS = 0x45
SOLAR_UNIT_ADDRESS = 0x47
POWER_UNIT_ADDRESS = 0x49

# =======================
# MQTT CONFIG
# =======================
MQTT_BROKER = 'localhost'
MQTT_PORT = 1883

I2C_MAP = {
    'rover/accel':               (MOTOR_UNIT_ADDRESS, 1),
    'rover/steering_all':        (MOTOR_UNIT_ADDRESS, 2),
    'rover/steering_each':       (MOTOR_UNIT_ADDRESS, 5),
    'rover/steering_continuous': (MOTOR_UNIT_ADDRESS, 6),
    'rover/camera':              (SOLAR_UNIT_ADDRESS, 3),
    'rover/camera_continuous':   (SOLAR_UNIT_ADDRESS, 5),
    'rover/panel':               (SOLAR_UNIT_ADDRESS, 4),
}

MQTT_TOPICS = list(I2C_MAP.keys())

# =======================
# POWER SUPPLY CONFIG
# =======================
BASE_TOPIC = 'rover/power'

REQ_ARRAY_TOPICS = [
    "INA226_1", "INA226_2",
    "INA3221_CH1", "INA3221_CH2", "INA3221_CH3"
]

REQ_ARRAY = [0x01, 0x02, 0x03, 0x04, 0x05]
REQ_ARRAY_INDEX = 0
POLL_DELAY = 0.5

# =======================
# SHARED STATE
# =======================
STATE = {
    MOTOR_UNIT_ADDRESS: {"packet": None, "reg": None, "dirty": False, "oneshot": False},
    SOLAR_UNIT_ADDRESS: {"packet": None, "reg": None, "dirty": False, "oneshot": False},
    POWER_UNIT_ADDRESS: {"packet": None, "reg": None, "dirty": False, "oneshot": False}
}

state_lock = threading.Lock()

# =======================
# HELPERS
# =======================
def build_packet(reg, payload_str):
    payload = payload_str.encode('utf-8')
    if len(payload) > MAX_CHARS:
        raise ValueError("Payload troppo lungo")

    return [reg, len(payload)] + list(payload) + [0x01, 0xFF]


def publish_measurement(client, reg, voltage, current):
    topic = f"{BASE_TOPIC}/{reg}"
    payload = {
        "reg": reg,
        "voltage": round(voltage, 2),
        "current": round(current, 2)
    }
    client.publish(topic, json.dumps(payload), qos=0)
    logging.info("Published %s -> %s", topic, payload)

# =======================
# MQTT CALLBACKS
# =======================
def on_connect(client, userdata, flags, rc):
    logging.info("MQTT connected")
    for topic in MQTT_TOPICS:
        client.subscribe(topic)


def on_message(client, userdata, msg):
    try:
        payload_txt = msg.payload.decode().strip()
        mapping = I2C_MAP.get(msg.topic)
        if not mapping:
            return

        slave_addr, reg = mapping

        # ---- PAYLOAD BUILDING ----
        if msg.topic in ('rover/camera', 'rover/camera_continuous', 'rover/panel'):
            values = [int(x) for x in payload_txt.split(',')]
            values += [0] * (3 - len(values))
            payload_str = f"{reg},{values[0]},{values[1]},{values[2]},0,0"

        elif msg.topic == 'rover/steering_each':
            parts = payload_txt.split(',')
            values = [int(p) if p else 0 for p in parts[:4]]
            values += [0] * (4 - len(values))
            payload_str = f"{reg},{values[0]},{values[1]},{values[2]},{values[3]},0"

        elif msg.topic == 'rover/steering_continuous':
            parts = payload_txt.split(',')
            front = int(parts[0]) if len(parts) > 0 and parts[0] else 0
            rear  = int(parts[1]) if len(parts) > 1 and parts[1] else 0
            payload_str = f"{reg},{front},{rear},0,0,0"

        else:
            value = int(payload_txt.split(',')[0]) if payload_txt else 0
            payload_str = f"{reg},{value},0,0,0,0"

        packet = build_packet(reg, payload_str)

        with state_lock:
            STATE[slave_addr]["packet"] = packet
            STATE[slave_addr]["reg"] = reg
            STATE[slave_addr]["dirty"] = True
            STATE[slave_addr]["oneshot"] = True # one-time command

    except Exception:
        traceback.print_exc()

# =======================
# HTTP RELAY
# =======================
class RelayHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/ir_relay":
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode().strip().lower()

        if body not in ("on", "off"):
            self.send_response(400)
            self.end_headers()
            return

        reg = 0x07 if body == "on" else 0x08
        packet = [0, 1, reg, 0x01, 0xFF]

        with state_lock:
            STATE[POWER_UNIT_ADDRESS]["packet"] = packet
            STATE[POWER_UNIT_ADDRESS]["reg"] = 0
            STATE[POWER_UNIT_ADDRESS]["dirty"] = True
            STATE[POWER_UNIT_ADDRESS]["oneshot"] = True

        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass

def start_http():
    server = HTTPServer(("0.0.0.0", 8000), RelayHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

# =======================
# I2C MAIN LOOP
# =======================
def i2c_loop(client):
    global REQ_ARRAY_INDEX
    sequence = [MOTOR_UNIT_ADDRESS, SOLAR_UNIT_ADDRESS, POWER_UNIT_ADDRESS]

    while True:
        for addr in sequence:

            # ===== SEND COMMANDS =====
            with state_lock:
                entry = STATE.get(addr, {})
                packet = entry.get("packet")
                reg = entry.get("reg")
                dirty = entry.get("dirty", False)
                oneshot = entry.get("oneshot", False)

            if packet and reg is not None and dirty:
                try:
                    with SMBus(I2C_BUS) as bus:
                        bus.write_i2c_block_data(addr, reg, packet[1:])
                    with state_lock:
                        STATE[addr]["dirty"] = False
                        if oneshot:
                            STATE[addr]["packet"] = None
                            STATE[addr]["reg"] = None
                            STATE[addr]["oneshot"] = False
                except Exception as e:
                    logging.warning("I2C write error %02X: %s", addr, e)

            # ===== POWER POLLING =====
            if addr == POWER_UNIT_ADDRESS:
                reg = REQ_ARRAY[REQ_ARRAY_INDEX]
                topic = REQ_ARRAY_TOPICS[REQ_ARRAY_INDEX]

                try:
                    with SMBus(I2C_BUS) as bus:
                        bus.write_i2c_block_data(addr, 0, [reg])
                        time.sleep(0.1)

                        t0 = time.time()
                        status = None
                        while time.time() - t0 < 0.2:
                            status = bus.read_i2c_block_data(addr, 0, 1)[0]
                            if status == 0xAA:
                                break
                            time.sleep(0.01)

                        if status == 0xAA:
                            data = bus.read_i2c_block_data(addr, 0, 9)
                            voltage, current = struct.unpack('<ff', bytes(data[1:9]))
                            publish_measurement(client, topic, voltage, current)

                except Exception as e:
                    logging.warning("Power poll error: %s", e)

                REQ_ARRAY_INDEX = (REQ_ARRAY_INDEX + 1) % len(REQ_ARRAY)

            time.sleep(0.02)

# =======================
# MAIN
# =======================
def main():
    logging.basicConfig(level=logging.INFO)

    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_BROKER, MQTT_PORT, 60)
    client.loop_start()

    start_http()

    threading.Thread(target=i2c_loop, args=(client,), daemon=True).start()

    while True:
        time.sleep(1)

if __name__ == "__main__":
    main()
