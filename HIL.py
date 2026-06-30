import asyncio
import threading
import time

from pymodbus.datastore import ModbusServerContext
from pymodbus.datastore.simulator import ModbusSimulatorContext
from pymodbus.server import StartTcpServer


HOST = "127.0.0.1"
PORT = 6666
DEVICE_ID = 255

TC1_REGISTER = 3
TC2_REGISTER = 4
RL1_REG = 30
RL2_REG = 31
HIL_CONTROL_REG = 40
HIL_STOPPED = 0
HIL_RUNNING = 1
HIL_PAUSED = 2

AMBIENT_TEMP = 20.0
HEATING_RATE = 0.35
COOLING_RATE = 0.08
NOISE = 0.08


def build_context():
    config = {
        "setup": {
            "di size": 0,
            "co size": 0,
            "ir size": 0,
            "hr size": 80,
            "shared blocks": False,
            "type exception": False,
            "defaults": {
                "value": {
                    "bits": 0,
                    "uint16": 0,
                    "uint32": 0,
                    "float32": 0.0,
                    "string": " "
                },
                "action": {
                    "bits": None,
                    "uint16": None,
                    "uint32": None,
                    "float32": None,
                    "string": None
                }
            }
        },
        "invalid": [],
        "write": [
            [0, 79]
        ],
        "bits": [],
        "uint16": [
            {"addr": [0, 79], "value": 0}
        ],
        "uint32": [],
        "float32": [],
        "string": [],
        "repeat": []
    }

    device = ModbusSimulatorContext(config, custom_actions=None)
    context = ModbusServerContext(devices={DEVICE_ID: device}, single=False)
    return context, device


async def set_hr(device, address, value):
    await device.async_OLD_setValues(3, address, [int(value)])


async def get_hr(device, address):
    values = await device.async_OLD_getValues(3, address, 1)
    return int(values[0])


def set_initial_hr(device, address, value):
    device.registers[address].value = int(value)
    device.registers[address].count_write = 0


def dashboard_has_started(device):
    if device.registers[HIL_CONTROL_REG].count_write > 0:
        return False

    return (
        device.registers[RL1_REG].count_write > 0
        or device.registers[RL2_REG].count_write > 0
    )


def signed_u16(value):
    value = int(round(value))
    if value < 0:
        return 0x10000 + value

    return value


def plant_loop(device):
    tc1 = AMBIENT_TEMP
    tc2 = AMBIENT_TEMP
    tick = 0
    started = False
    wait_ticks = 0

    set_initial_hr(device, TC1_REGISTER, signed_u16(tc1))
    set_initial_hr(device, TC2_REGISTER, signed_u16(tc2))
    set_initial_hr(device, RL1_REG, 1)
    set_initial_hr(device, RL2_REG, 1)
    set_initial_hr(device, HIL_CONTROL_REG, HIL_STOPPED)

    while True:
        rl1 = asyncio.run(get_hr(device, RL1_REG))
        rl2 = asyncio.run(get_hr(device, RL2_REG))
        hil_state = asyncio.run(get_hr(device, HIL_CONTROL_REG))

        if not started:
            if hil_state == HIL_RUNNING or dashboard_has_started(device):
                started = True
                print("HIL received dashboard command. Plant model started.")
            else:
                set_initial_hr(device, TC1_REGISTER, signed_u16(tc1))
                set_initial_hr(device, TC2_REGISTER, signed_u16(tc2))

                if wait_ticks % 5 == 0:
                    print("HIL waiting for dashboard Start command...")

                wait_ticks += 1
                time.sleep(1)
                continue

        if hil_state == HIL_STOPPED:
            started = False
            tc1 = max(AMBIENT_TEMP, tc1 - COOLING_RATE)
            tc2 = max(AMBIENT_TEMP, tc2 - COOLING_RATE)
            asyncio.run(set_hr(device, TC1_REGISTER, signed_u16(tc1)))
            asyncio.run(set_hr(device, TC2_REGISTER, signed_u16(tc2)))

            if wait_ticks % 5 == 0:
                print(
                    f"HIL stopped. TC1={tc1:.1f} TC2={tc2:.1f}. "
                    "Waiting for dashboard Start command..."
                )

            wait_ticks += 1
            time.sleep(1)
            continue

        if hil_state == HIL_PAUSED:
            asyncio.run(set_hr(device, TC1_REGISTER, signed_u16(tc1)))
            asyncio.run(set_hr(device, TC2_REGISTER, signed_u16(tc2)))

            if wait_ticks % 5 == 0:
                print(f"HIL paused. TC1={tc1:.1f} TC2={tc2:.1f}")

            wait_ticks += 1
            time.sleep(1)
            continue

        tc1 += HEATING_RATE if rl1 == 0 else -COOLING_RATE
        tc2 += HEATING_RATE if rl2 == 0 else -COOLING_RATE

        tc1 += ((tick % 5) - 2) * NOISE
        tc2 += (((tick + 2) % 5) - 2) * NOISE

        tc1 = max(AMBIENT_TEMP, min(tc1, 750.0))
        tc2 = max(AMBIENT_TEMP, min(tc2, 750.0))

        asyncio.run(set_hr(device, TC1_REGISTER, signed_u16(tc1)))
        asyncio.run(set_hr(device, TC2_REGISTER, signed_u16(tc2)))

        print(
            f"HIL TC1={tc1:.1f} TC2={tc2:.1f} "
            f"RL1={'HEAT' if rl1 == 0 else 'OFF'} "
            f"RL2={'HEAT' if rl2 == 0 else 'OFF'}"
        )

        tick += 1
        time.sleep(1)


if __name__ == "__main__":
    context, device = build_context()

    threading.Thread(
        target=plant_loop,
        args=(device,),
        daemon=True
    ).start()

    print(f"HIL Modbus TCP listening on {HOST}:{PORT}, device_id={DEVICE_ID}")

    StartTcpServer(
        context,
        address=(HOST, PORT)
    )
