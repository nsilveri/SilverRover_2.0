import time
import threading
import logging
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
    SOLAR_UNIT_ADDRESS,
    POWER_UNIT_ADDRESS
]

MQTT_BROKER = "localhost"
MQTT_PORT   = 1883

PRIORITY_MODE = True
I2C_LOOP_DELAY = 0.03

# =========================
# TOPIC → (ADDR, MODE)
# =========================

STATEFUL_TOPICS = {
    "rover/accel":               MOTOR_UNIT_ADDRESS,
    "rover/steering_all":        MOTOR_UNIT_ADDRESS,
    "rover/steering_each":       MOTOR_UNIT_ADDRESS,
    "rover/steering_continuous": MOTOR_UNIT_ADDRESS,
    "rover/camera":              SOLAR_UNIT_ADDRESS,
    "rover/camera_continuous":   SOLAR_UNIT_ADDRESS,
}

ONESHOT_TOPICS = {
    "rover/panel": SOLAR_UNIT_ADDRESS
}

# =========================
# LOGGING
# =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# =========================
# STATE
# =========================

state_lock = threading.Lock()

STATE = {
    MOTOR_UNIT_ADDRESS: {
        "stateful": {"packet": None, "reg": None}
    },
    SOLAR_UNIT_ADDRESS: {
        "stateful": {"packet": None, "reg": None},
        "oneshot":  {"packet": None, "reg": None, "dirty": False}
    },
    POWER_UNIT_ADDRESS: {
        "stateful": {"packet": None, "reg": None}
    }
}

# =========================
# PRIORITY QUEUE
# =========================

priority_queue = deque()
priority_set = set()

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

        # -------------------------
        # STATEFUL COMMANDS
        # -------------------------
        if topic in STATEFUL_TOPICS:
            addr = STATEFUL_TOPICS[topic]
            reg = int(payload_txt.split(",")[0])
            packet = build_packet(reg, payload_txt)

            with state_lock:
                STATE[addr]["stateful"]["packet"] = packet
                STATE[addr]["stateful"]["reg"] = reg

            if PRIORITY_MODE:
                enqueue_priority(addr)

        # -------------------------
        # ONE SHOT (PANELS)
        # -------------------------
        elif topic in ONESHOT_TOPICS:
            addr = ONESHOT_TOPICS[topic]
            reg = int(payload_txt.split(",")[0])
            packet = build_packet(reg, payload_txt)

            with state_lock:
                STATE[addr]["oneshot"].update({
                    "packet": packet,
                    "reg": reg,
                    "dirty": True
                })

            if PRIORITY_MODE:
                enqueue_priority(addr)

    except Exception as e:
        logging.error("MQTT error: %s", e)

# =========================
# I2C LOOP
# =========================

def i2c_loop():
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

                    # ---- ONE SHOT ----
                    if "oneshot" in unit:
                        o = unit["oneshot"]
                        if o["dirty"] and o["packet"]:
                            bus.write_i2c_block_data(addr, o["reg"], o["packet"][1:])
                            logging.info("One-shot sent to 0x%02X", addr)

                            o["packet"] = None
                            o["reg"] = None
                            o["dirty"] = False

                    # ---- STATEFUL ----
                    s = unit["stateful"]
                    if s["packet"]:
                        bus.write_i2c_block_data(addr, s["reg"], s["packet"][1:])

        except Exception as e:
            logging.warning("I2C error 0x%02X: %s", addr, e)

        time.sleep(I2C_LOOP_DELAY)

# =========================
# MAIN
# =========================

def main():
    mqtt_client = mqtt.Client()
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message

    mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)

    threading.Thread(target=i2c_loop, daemon=True).start()

    mqtt_client.loop_forever()


if __name__ == "__main__":
    main()
