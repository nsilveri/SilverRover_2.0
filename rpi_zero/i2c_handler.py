import time
import threading
import logging
import struct
import json
from collections import deque

from smbus2 import SMBus
import paho.mqtt.client as mqtt

# =========================
# CONFIG
# =========================

I2C_BUS = 1

MOTOR_UNIT_ADDRESS = 0x45
SOLAR_UNIT_ADDRESS = 0x47
POWER_UNIT_ADDRESS = 0x49

FIXED_ORDER = [
    MOTOR_UNIT_ADDRESS,
    SOLAR_UNIT_ADDRESS
]

MQTT_BROKER = "localhost"
MQTT_PORT   = 1883

PRIORITY_MODE   = True
I2C_LOOP_DELAY  = 0.03
POWER_POLL_TIME = 0.5

# =========================
# POWER REGISTERS
# =========================

POWER_REGS = [
    ("INA226_1",     0x01),
    ("INA226_2",     0x02),
    ("INA3221_CH1",  0x03),
    ("INA3221_CH2",  0x04),
    ("INA3221_CH3",  0x05),
]

POWER_BASE_TOPIC = "rover/power"

# =========================
# LOGGING
# =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# =========================
# STATE (WRITE ONLY)
# =========================

state_lock = threading.Lock()

STATE = {
    MOTOR_UNIT_ADDRESS: {
        "stateful": {"packet": None, "reg": None}
    },
    SOLAR_UNIT_ADDRESS: {
        "stateful": {"packet": None, "reg": None},
        "oneshot":  {"packet": None, "reg": None, "dirty": False}
    }
}

# =========================
# PRIORITY QUEUE
# =========================

priority_queue = deque()
priority_set   = set()

def enqueue_priority(addr):
    if addr not in priority_set:
        priority_queue.appendleft(addr)
        priority_set.add(addr)

# =========================
# PACKET BUILDER
# =========================

def build_packet(reg, payload_str):
    payload = payload_str.encode("utf-8")
    return [reg, len(payload)] + list(payload) + [0x01, 0xFF]

# =========================
# MQTT CALLBACKS
# =========================

def on_connect(client, userdata, flags, rc):
    logging.info("MQTT connected")
    client.subscribe("rover/#")


def on_message(client, userdata, msg):
    try:
        topic = msg.topic
        payload_txt = msg.payload.decode().strip()

        # ---------- MOTOR (0x45) ----------
        if topic.startswith("rover/accel") or topic.startswith("rover/steering"):
            reg = int(payload_txt.split(",")[0])
            packet = build_packet(reg, payload_txt)

            with state_lock:
                STATE[MOTOR_UNIT_ADDRESS]["stateful"].update({
                    "packet": packet,
                    "reg": reg
                })

            if PRIORITY_MODE:
                enqueue_priority(MOTOR_UNIT_ADDRESS)

        # ---------- SOLAR CAMERA (stateful) ----------
        elif topic in ("rover/camera", "rover/camera_continuous"):
            reg = int(payload_txt.split(",")[0])
            packet = build_packet(reg, payload_txt)

            with state_lock:
                STATE[SOLAR_UNIT_ADDRESS]["stateful"].update({
                    "packet": packet,
                    "reg": reg
                })

            if PRIORITY_MODE:
                enqueue_priority(SOLAR_UNIT_ADDRESS)

        # ---------- SOLAR PANEL (one-shot) ----------
        elif topic == "rover/panel":
            reg = int(payload_txt.split(",")[0])
            packet = build_packet(reg, payload_txt)

            with state_lock:
                STATE[SOLAR_UNIT_ADDRESS]["oneshot"].update({
                    "packet": packet,
                    "reg": reg,
                    "dirty": True
                })

            if PRIORITY_MODE:
                enqueue_priority(SOLAR_UNIT_ADDRESS)

    except Exception as e:
        logging.error("MQTT error: %s", e)

# =========================
# I2C WRITE LOOP
# =========================

def i2c_write_loop():
    fixed_idx = 0

    while True:

        if PRIORITY_MODE and priority_queue:
            addr = priority_queue.popleft()
            priority_set.discard(addr)
        else:
            addr = FIXED_ORDER[fixed_idx]
            fixed_idx = (fixed_idx + 1) % len(FIXED_ORDER)

        try:
            with SMBus(I2C_BUS) as bus:
                with state_lock:
                    unit = STATE[addr]

                    # --- ONE SHOT ---
                    if "oneshot" in unit:
                        o = unit["oneshot"]
                        if o["dirty"] and o["packet"]:
                            bus.write_i2c_block_data(addr, o["reg"], o["packet"][1:])
                            logging.info("One-shot sent to 0x%02X", addr)

                            o["packet"] = None
                            o["reg"]    = None
                            o["dirty"]  = False

                    # --- STATEFUL ---
                    s = unit["stateful"]
                    if s["packet"]:
                        bus.write_i2c_block_data(addr, s["reg"], s["packet"][1:])

        except Exception as e:
            logging.warning("I2C write error 0x%02X: %s", addr, e)

        time.sleep(I2C_LOOP_DELAY)

# =========================
# POWER READ LOOP (0x49)
# =========================

def power_read_loop(client):
    last_values = {}

    while True:
        for name, reg in POWER_REGS:
            try:
                with SMBus(I2C_BUS) as bus:
                    bus.write_i2c_block_data(POWER_UNIT_ADDRESS, 0, [reg])
                    time.sleep(0.05)
                    data = bus.read_i2c_block_data(POWER_UNIT_ADDRESS, 0, 9)

                payload = bytes(data[1:9])
                voltage, current = struct.unpack("<ff", payload)

                voltage = round(voltage, 2)
                current = round(current, 2)

                if last_values.get(name) != (voltage, current):
                    topic = f"{POWER_BASE_TOPIC}/{name}"
                    msg = {
                        "voltage": voltage,
                        "current": current
                    }
                    client.publish(topic, json.dumps(msg))
                    last_values[name] = (voltage, current)

            except Exception as e:
                logging.warning("Power read error %s: %s", name, e)

        time.sleep(POWER_POLL_TIME)

# =========================
# MAIN
# =========================

def main():
    mqtt_client = mqtt.Client()
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message

    mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)

    threading.Thread(target=i2c_write_loop, daemon=True).start()
    threading.Thread(target=power_read_loop, args=(mqtt_client,), daemon=True).start()

    mqtt_client.loop_forever()


if __name__ == "__main__":
    main()
