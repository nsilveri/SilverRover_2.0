import time
import json
import threading
import logging

from smbus2 import SMBus
import paho.mqtt.client as mqtt

# =========================
# CONFIG
# =========================

I2C_BUS = 1

MOTOR_UNIT_ADDRESS = 0x45
ACTUATOR_UNIT_ADDRESS = 0x46
SOLAR_UNIT_ADDRESS = 0x47

MQTT_BROKER = "localhost"
MQTT_PORT = 1883

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
        "stateful": {"packet": None, "reg": None, "dirty": False}
    },
    ACTUATOR_UNIT_ADDRESS: {
        "stateful": {"packet": None, "reg": None, "dirty": False}
    },
    SOLAR_UNIT_ADDRESS: {
        "stateful": {  # pan / tilt camera
            "packet": None,
            "reg": None,
            "dirty": False
        },
        "oneshot": {   # open / close panels
            "packet": None,
            "reg": None,
            "dirty": False
        }
    }
}

# =========================
# MQTT CALLBACKS
# =========================

def on_connect(client, userdata, flags, rc):
    logging.info("MQTT connected")
    client.subscribe("rover/#")


def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode())
        topic = msg.topic

        # =========================
        # MOTORS (0x45) - STATEFUL
        # =========================
        if topic == "rover/motors":
            packet = payload["packet"]
            reg = payload["reg"]

            with state_lock:
                STATE[MOTOR_UNIT_ADDRESS]["stateful"].update({
                    "packet": packet,
                    "reg": reg,
                    "dirty": True
                })

        # =========================
        # ACTUATORS (0x46) - STATEFUL
        # =========================
        elif topic == "rover/actuators":
            packet = payload["packet"]
            reg = payload["reg"]

            with state_lock:
                STATE[ACTUATOR_UNIT_ADDRESS]["stateful"].update({
                    "packet": packet,
                    "reg": reg,
                    "dirty": True
                })

        # =========================
        # SOLAR PAN / TILT (0x47) - STATEFUL
        # =========================
        elif topic == "rover/solar/camera":
            packet = payload["packet"]
            reg = payload["reg"]

            with state_lock:
                STATE[SOLAR_UNIT_ADDRESS]["stateful"].update({
                    "packet": packet,
                    "reg": reg,
                    "dirty": True
                })

        # =========================
        # SOLAR PANELS (0x47) - ONE SHOT
        # =========================
        elif topic == "rover/solar/panels":
            packet = payload["packet"]
            reg = payload["reg"]

            with state_lock:
                STATE[SOLAR_UNIT_ADDRESS]["oneshot"].update({
                    "packet": packet,
                    "reg": reg,
                    "dirty": True
                })

    except Exception as e:
        logging.error("MQTT message error: %s", e)

# =========================
# I2C LOOP
# =========================

def i2c_loop():
    addr_list = list(STATE.keys())
    idx = 0

    while True:
        addr = addr_list[idx]
        idx = (idx + 1) % len(addr_list)

        try:
            with SMBus(I2C_BUS) as bus:
                with state_lock:
                    unit = STATE[addr]

                    # ---------- ONE SHOT ----------
                    if "oneshot" in unit:
                        o = unit["oneshot"]
                        if o["dirty"] and o["packet"]:
                            bus.write_i2c_block_data(addr, o["reg"], o["packet"][1:])
                            logging.info("I2C one-shot sent to 0x%02X", addr)

                            # reset after single send
                            o["packet"] = None
                            o["reg"] = None
                            o["dirty"] = False

                    # ---------- STATEFUL ----------
                    s = unit["stateful"]
                    if s["packet"]:
                        bus.write_i2c_block_data(addr, s["reg"], s["packet"][1:])

        except Exception as e:
            logging.warning("I2C error addr 0x%02X: %s", addr, e)

        time.sleep(0.05)  # 20 Hz loop

# =========================
# MAIN
# =========================

def main():
    mqtt_client = mqtt.Client()
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message

    mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)

    i2c_thread = threading.Thread(target=i2c_loop, daemon=True)
    i2c_thread.start()

    mqtt_client.loop_forever()


if __name__ == "__main__":
    main()
