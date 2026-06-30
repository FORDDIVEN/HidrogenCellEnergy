import time
import random
import threading
import logging
from datetime import datetime
from pymodbus.client.tcp import ModbusTcpClient
import traceback

from database import (
    save_reading,
    raise_alarm,
    clear_alarm,
    log_alarm_event,
    save_controller_state,
    get_controller_state,
    get_system_config,
    save_system_config,
    finish_startup_run
)
from startup_procedure import (
    STEPS,
    format_flow,
    step_temperature_target,
    validate_steps
)

logger = logging.getLogger(__name__)

_CONFIG = get_system_config()

#config del dataloger
SIMULATION_MODE = _CONFIG["simulation_mode"]

#datos tcp/ip de datalogger configurador de manera manual
#IP = "192.168.0.15"
IP = _CONFIG["modbus_ip"]
PORT_TCP = _CONFIG["modbus_port"]
SLAVE_ID = _CONFIG["modbus_slave_id"]

#registros de las termcuplas en la direciones de memoria en el dataloger
TC1_REGISTER = 3
TC2_REGISTER = 4

#lo mismo pero para los relés
RL1_REG = 30
RL2_REG = 31
HIL_CONTROL_REG = 40
HIL_STATE_STOPPED = 0
HIL_STATE_RUNNING = 1
HIL_STATE_PAUSED = 2

ESCALA_TC = 1.0
HYSTERESIS = 2
SAMPLE_TIME = _CONFIG["sample_time"]
DEFAULT_LOG_INTERVAL_S = 1
HIGH_TEMP_MARGIN = 15
MAX_SAFE_TEMP = 700
MIN_VALID_TEMP = -20
MAX_VALID_TEMP = 800
SETPOINT_DEVIATION_MARGIN = 30
SETPOINT_DEVIATION_TIME = 15 * 60

controller_running = False
controller_paused = False
controller_lock = threading.Lock()
next_step_requested = False
logging_interval_s = DEFAULT_LOG_INTERVAL_S
controller_status = {
    "status": "STOPPED",
    "current_run_id": None,
    "current_step": 0,
    "step_started_at": None,
    "updated_at": None
}

#funciones para calcular la duracion de un paso, el setpoint actual, y serializar un paso para enviarlo al front para mostrarlo en la interfaz
def step_duration_s(step):

    if step["mode"] == "hold":
        if step["duration_s"] is None:
            return float("inf")

        return step["duration_s"]

    return (step["t_end"] - step["t_start"]) / step["slope"]


def step_setpoint(step, t_in_step_s):

    if step["mode"] == "hold":
        return step["t_set"]

    sp = step["t_start"] + step["slope"] * t_in_step_s

    return min(sp, step["t_end"])


def serialize_step(step, index, elapsed_s=0):

    duration_s = step_duration_s(step)
    remaining_s = None

    if duration_s != float("inf"):
        remaining_s = max(0, duration_s - elapsed_s)

    data = {
        "index": index,
        "name": step["name"],
        "phase": step["phase"],
        "step": step["step"],
        "mode": step["mode"],
        "target_temp": step_temperature_target(step),
        "duration_s": None if duration_s == float("inf") else duration_s,
        "elapsed_s": elapsed_s,
        "remaining_s": remaining_s,
        "anode_flow": step["anode_flow"],
        "cathode_flow": step["cathode_flow"],
        "anode_flow_text": format_flow(step["anode_flow"]),
        "cathode_flow_text": format_flow(step["cathode_flow"])
    }

    if step["mode"] == "ramp":
        data.update({
            "t_start": step["t_start"],
            "t_end": step["t_end"],
            "slope": step["slope"]
        })
    else:
        data.update({
            "t_set": step["t_set"]
        })

    return data


def update_controller_status(
    status=None,
    current_step=None,
    step_started_at=None,
    current_run_id=None,
    clear_run=False
):

    with controller_lock:
        if status is not None:
            controller_status["status"] = status

        if clear_run:
            controller_status["current_run_id"] = None
        elif current_run_id is not None:
            controller_status["current_run_id"] = current_run_id

        if current_step is not None:
            controller_status["current_step"] = current_step

        if step_started_at is not None:
            controller_status["step_started_at"] = step_started_at

        controller_status["updated_at"] = datetime.now().isoformat()

        stored_status = controller_status["status"]
        stored_run_id = controller_status["current_run_id"]
        stored_step = controller_status["current_step"]
        stored_started_at = controller_status["step_started_at"]
        stored_logging_interval_s = logging_interval_s

    try:
        save_controller_state(
            stored_status,
            stored_step,
            stored_started_at,
            stored_run_id,
            stored_logging_interval_s
        )
    except Exception:
        logger.exception("No se pudo persistir el estado del controlador")


def get_system_status():

    global logging_interval_s

    with controller_lock:
        status = dict(controller_status)
        status["logging_interval_s"] = logging_interval_s
        status["logging_interval_text"] = format_interval(logging_interval_s)

    status["modbus_config"] = get_modbus_config()

    try:
        persisted = get_controller_state()

        if status["updated_at"] is None:
            status.update(persisted)

        persisted_interval = persisted.get("logging_interval_s")

        if persisted_interval:
            with controller_lock:
                logging_interval_s = persisted_interval
                status["logging_interval_s"] = logging_interval_s
                status["logging_interval_text"] = format_interval(logging_interval_s)
    except Exception:
        logger.exception("No se pudo leer el estado persistido del controlador")

    current_step = status.get("current_step") or 0
    current_step = min(max(current_step, 0), len(STEPS) - 1)
    step_started_at = status.get("step_started_at")
    elapsed_s = 0

    if step_started_at:
        elapsed_s = max(0, time.time() - step_started_at)

    status["procedure_step"] = serialize_step(
        STEPS[current_step],
        current_step,
        elapsed_s
    )

    return status


def get_modbus_config():

    with controller_lock:
        current = {
            "modbus_ip": IP,
            "modbus_port": PORT_TCP,
            "modbus_slave_id": SLAVE_ID,
            "simulation_mode": SIMULATION_MODE,
            "sample_time": SAMPLE_TIME
        }

    current["mode_label"] = get_system_config().get("mode_label", "")

    return current


def apply_modbus_config(config):

    global IP
    global PORT_TCP
    global SLAVE_ID
    global SIMULATION_MODE
    global SAMPLE_TIME

    modbus_ip = str(config.get("modbus_ip", IP)).strip()
    mode_label = str(config.get("mode_label", "")).strip()
    modbus_port = int(config.get("modbus_port", PORT_TCP))
    modbus_slave_id = int(config.get("modbus_slave_id", SLAVE_ID))
    sample_time = int(config.get("sample_time", SAMPLE_TIME))
    simulation_mode = config.get("simulation_mode", SIMULATION_MODE)

    if isinstance(simulation_mode, str):
        simulation_mode = simulation_mode.strip().lower() in ("1", "true", "yes", "on")

    if not modbus_ip:
        raise ValueError("La IP Modbus no puede estar vacía")

    if not 1 <= modbus_port <= 65535:
        raise ValueError("El puerto Modbus debe estar entre 1 y 65535")

    if not (1 <= modbus_slave_id <= 247 or modbus_slave_id == 255):
        raise ValueError("El slave/device id debe estar entre 1 y 247, o 255")

    if sample_time < 1:
        raise ValueError("El sample time mínimo es 1 segundo")

    saved = save_system_config({
        "modbus_ip": modbus_ip,
        "modbus_port": modbus_port,
        "modbus_slave_id": modbus_slave_id,
        "simulation_mode": simulation_mode,
        "sample_time": sample_time,
        "mode_label": mode_label
    })

    with controller_lock:
        IP = saved["modbus_ip"]
        PORT_TCP = saved["modbus_port"]
        SLAVE_ID = saved["modbus_slave_id"]
        SIMULATION_MODE = saved["simulation_mode"]
        SAMPLE_TIME = saved["sample_time"]

    return get_modbus_config()


def test_modbus_connection(config=None):

    if config:
        host = str(config.get("modbus_ip", IP)).strip()
        port = int(config.get("modbus_port", PORT_TCP))
        slave_id = int(config.get("modbus_slave_id", SLAVE_ID))
        simulation = config.get("simulation_mode", SIMULATION_MODE)

        if isinstance(simulation, str):
            simulation = simulation.strip().lower() in ("1", "true", "yes", "on")
    else:
        with controller_lock:
            host = IP
            port = PORT_TCP
            slave_id = SLAVE_ID
            simulation = SIMULATION_MODE

    if simulation:
        return {
            "ok": True,
            "message": "Modo simulación interno activo; no se requiere conexión Modbus"
        }

    client = ModbusTcpClient(
        host=host,
        port=port,
        timeout=2
    )

    try:
        if not client.connect():
            return {
                "ok": False,
                "message": f"No se pudo conectar a {host}:{port}"
            }

        rr = read_holding_compat(client, TC1_REGISTER, 1, slave_id)

        if rr is None or rr.isError():
            return {
                "ok": False,
                "message": "Conectó, pero no pudo leer TC1"
            }

        return {
            "ok": True,
            "message": f"Conexión Modbus OK. TC1={s16(rr.registers[0]) / ESCALA_TC:.1f}"
        }
    finally:
        client.close()


def parse_interval(value):

    if value is None:
        raise ValueError("Intervalo vacío")

    raw = str(value).strip().lower()

    if not raw:
        raise ValueError("Intervalo vacío")

    unit = "s"
    number = raw

    if raw[-1].isalpha():
        unit = raw[-1]
        number = raw[:-1].strip()

    try:
        amount = float(number)
    except ValueError as exc:
        raise ValueError("Formato inválido. Usa ejemplos como 5s, 5m o 1h") from exc

    if amount <= 0:
        raise ValueError("El intervalo debe ser mayor que 0")

    multipliers = {
        "s": 1,
        "m": 60,
        "h": 3600
    }

    if unit not in multipliers:
        raise ValueError("Unidad inválida. Usa s, m o h")

    interval = int(amount * multipliers[unit])

    if interval < 1:
        raise ValueError("El intervalo mínimo es 1 segundo")

    return interval


def format_interval(seconds):

    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"

    if seconds % 60 == 0:
        return f"{seconds // 60}m"

    return f"{seconds}s"


def set_logging_interval(value):

    global logging_interval_s

    interval = parse_interval(value)

    with controller_lock:
        logging_interval_s = interval
        stored_status = controller_status["status"]
        stored_step = controller_status["current_step"]
        stored_started_at = controller_status["step_started_at"]
        stored_run_id = controller_status["current_run_id"]

    try:
        save_controller_state(
            stored_status,
            stored_step,
            stored_started_at,
            stored_run_id,
            interval
        )
    except Exception:
        logger.exception("No se pudo persistir el intervalo de logging")

    return interval


def get_logging_interval():

    with controller_lock:
        interval = logging_interval_s

    return {
        "seconds": interval,
        "text": format_interval(interval)
    }


def request_next_step():

    global next_step_requested

    with controller_lock:
        if not controller_running:
            return False

        next_step_requested = True

    return True


def finish_current_run(stop_reason):

    try:
        run_id = get_controller_state().get("current_run_id")
        finish_startup_run(run_id, "controller", stop_reason)
    except Exception:
        logger.exception("No se pudo cerrar la corrida actual")


def recover_controller_state_on_boot():

    try:
        state = get_controller_state()
    except Exception:
        logger.exception("No se pudo recuperar el estado del controlador al iniciar")
        return

    if state.get("status") not in ("RUNNING", "STARTING", "PAUSED", "MODBUS_ERROR", "MODBUS_DISCONNECTED"):
        return

    try:
        finish_startup_run(
            state.get("current_run_id"),
            "system",
            "APP_BOOT_RECOVERY"
        )
    except Exception:
        logger.exception("No se pudo cerrar la corrida obsoleta durante la recuperación")

    update_controller_status(
        status="STOPPED",
        current_step=state.get("current_step") or 0,
        step_started_at=None,
        clear_run=True
    )


def start_controller(run_id=None):
    global controller_running
    global controller_paused
    global next_step_requested
    global logging_interval_s

    try:
        persisted_interval = get_controller_state().get("logging_interval_s")

        if persisted_interval:
            logging_interval_s = persisted_interval
    except Exception:
        logger.exception("No se pudo cargar el intervalo persistido al iniciar el controlador")

    with controller_lock:
        controller_running = True
        controller_paused = False
        next_step_requested = False

    update_controller_status(
        status="STARTING",
        current_step=0,
        step_started_at=time.time(),
        current_run_id=run_id
    )

def stop_controller():
    global controller_running
    global controller_paused
    global next_step_requested

    with controller_lock:
        controller_running = False
        controller_paused = False
        next_step_requested = False

    update_controller_status(
        status="STOPPED",
        clear_run=True
    )

def pause_controller():
    global controller_paused
    controller_paused = True
    update_controller_status(status="PAUSED")

def resume_controller():
    global controller_paused
    controller_paused = False
    update_controller_status(status="RUNNING")

def s16(raw: int) -> int:
    return raw - 0x10000 if raw >= 0x8000 else raw

def read_holding_compat(client, address, count, slave_id):
    try:
        #Metodo 1: pymodbus v3.13+
        return client.read_holding_registers(
            address,
            count=count,
            device_id=slave_id
        )
    except Exception:
        try:
            #Metodo 2: Establecer slave primero (pymodbus v3 antiguo)
            client.set_slave(slave_id)
            return client.read_holding_registers(address, count)
        except Exception:
            try:
                #Metodo 3: Con parametro slave como keyword (pymodbus v2)
                return client.read_holding_registers(address=address, count=count, slave=slave_id)
            except Exception:
                try:
                    #Metodo 4: Sin slave (fallback)
                    return client.read_holding_registers(address, count)
                except Exception:
                    return None

def write_register_compat(client, address, value, slave_id):
    try:
        # Método 1: pymodbus v3.13+
        return client.write_register(
            address,
            value,
            device_id=slave_id
        )
    except Exception:
        try:
            #Metodo 2: Establecer slave primero (pymodbus v3 antiguo)
            client.set_slave(slave_id)
            return client.write_register(address, value)
        except Exception:
            try:
                #Metodo 3: Con parámetro slave como keyword (pymodbus v2)
                return client.write_register(address=address, value=value, slave=slave_id)
            except Exception:
                try:
                    #Metodo 4: Sin slave (fallback)
                    return client.write_register(address, value)
                except Exception:
                    return None

def leer_temp(client, addr):

    rr = read_holding_compat(client, addr, 1, SLAVE_ID)

    if rr is None or rr.isError():
        return None

    raw = s16(rr.registers[0])

    return raw / ESCALA_TC

def set_rele(client, reg, on: bool):

    if SIMULATION_MODE:
        return True

    val = 1 if on else 0

    rr = write_register_compat(client, reg, val, SLAVE_ID)

    return (rr is not None) and (not rr.isError())


def set_hil_control_state(client, state):

    if SIMULATION_MODE or client is None:
        return True

    rr = write_register_compat(client, HIL_CONTROL_REG, state, SLAVE_ID)

    return (rr is not None) and (not rr.isError())


def alarm_raise(socketio, code, severity, message, run_id=None):

    try:
        created, alarm_id = raise_alarm(code, severity, message, run_id)

        if not created:
            return
        
        try:
            log_alarm_event(
                alarm_id,
                code,
                "raised",
                f"{severity}: {message}",
                run_id
            )
        except Exception:
            logger.exception("No se pudo registrar evento de alarma levantada")
        
        socketio.emit("alarm_update", {
            "action": "raised",
            "code": code,
            "severity": severity,
            "message": message
        })
    except Exception:
        logger.exception("No se pudo levantar o emitir la alarma %s", code)


def alarm_clear(socketio, code, run_id=None):

    try:
        cleared = clear_alarm(code)

        if not cleared:
            return
        
        try:
            log_alarm_event(
                None,
                code,
                "cleared",
                None,
                run_id
            )
        except Exception:
            logger.exception("No se pudo registrar evento de alarma despejada o resuelta")
        
        socketio.emit("alarm_update", {
            "action": "cleared",
            "code": code
        })
    except Exception:
        logger.exception("No se pudo despejar/resolver o emitir la alarma %s", code)


def monitor_temperature_alarms(socketio, tc1, tc2, sp, step, t_in_step, now, state, run_id=None):

    for label, temp in (("TC1", tc1), ("TC2", tc2)):

        if temp is None:
            alarm_raise(
                socketio,
                f"SENSOR_{label}_NO_DATA",
                "critical",
                f"{label} sin lectura",
                run_id
            )
            continue

        alarm_clear(socketio, f"SENSOR_{label}_NO_DATA", run_id)

        if temp < MIN_VALID_TEMP or temp > MAX_VALID_TEMP:
            alarm_raise(
                socketio,
                f"SENSOR_{label}_OUT_OF_RANGE",
                "critical",
                f"{label} fuera de rango ({temp:.1f} °C)",
                run_id
            )
        else:
            alarm_clear(socketio, f"SENSOR_{label}_OUT_OF_RANGE", run_id)

        if temp > MAX_SAFE_TEMP:
            alarm_raise(
                socketio,
                f"MAX_SAFE_TEMP_{label}",
                "critical",
                f"{label} excede temperatura segura ({temp:.1f} > {MAX_SAFE_TEMP:.1f})",
                run_id
            )
        else:
            alarm_clear(socketio, f"MAX_SAFE_TEMP_{label}", run_id)

        if temp > sp + HIGH_TEMP_MARGIN:
            alarm_raise(
                socketio,
                f"HIGH_TEMP_{label}",
                "critical",
                f"{label} excede el setpoint ({temp:.1f} > {sp + HIGH_TEMP_MARGIN:.1f})",
                run_id
            )
        else:
            alarm_clear(socketio, f"HIGH_TEMP_{label}", run_id)

        deviation_key = f"{label.lower()}_setpoint_deviation_since"

        if step["mode"] == "hold" and temp < (sp - SETPOINT_DEVIATION_MARGIN):
            if state.get(deviation_key) is None:
                state[deviation_key] = now

            if now - state[deviation_key] >= SETPOINT_DEVIATION_TIME:
                alarm_raise(
                    socketio,
                    f"SETPOINT_DEVIATION_{label}",
                    "warning",
                    f"{label} bajo setpoint por tiempo prolongado ({temp:.1f} < {sp - SETPOINT_DEVIATION_MARGIN:.1f})",
                    run_id
                )
        else:
            state[deviation_key] = None
            alarm_clear(socketio, f"SETPOINT_DEVIATION_{label}", run_id)

#cuandon no se dispone de HIL o controlador real se simula aqui
def simulated_temperatures(tc1_prev, tc2_prev, sp):

    if tc1_prev is None:
        tc1_prev = 20

    if tc2_prev is None:
        tc2_prev = 20

    tc1 = tc1_prev + (sp - tc1_prev) * 0.05
    tc2 = tc2_prev + (sp - tc2_prev) * 0.05

    tc1 += random.uniform(-0.5, 0.5)
    tc2 += random.uniform(-0.5, 0.5)

    return tc1, tc2

# ================= MAIN LOOP =================

def controller_loop(socketio):

    global controller_running
    global controller_paused
    global next_step_requested

    client = None
    modbus_connected = False
    RECONNECT_INTERVAL = 5
    state = {
        "tc1_setpoint_deviation_since": None,
        "tc2_setpoint_deviation_since": None
    }

    try:
        validate_steps()
    except ValueError as exc:
        finish_current_run("PROCEDURE_INVALID")
        update_controller_status(status="PROCEDURE_INVALID")
        socketio.emit("system_status", {
            "status": "PROCEDURE_INVALID",
            "detail": str(exc)
        })
        print(exc)
        return

    if not SIMULATION_MODE:

        client = ModbusTcpClient(
            host=IP,
            port=PORT_TCP,
            timeout=2
        )


        if not client.connect():

            print("No se pudo conectar al FieldLogger, iniciando reintentos")

            socketio.emit("system_status", {"status": "MODBUS_ERROR"})
            socketio.emit("system_status", {"status": "MODBUS_DISCONNECTED"})

            #muestra alarma si el dataloger se desconecta
            alarm_raise(
                socketio,
                "MODBUS_DISCONNECTED",
                "critical",
                "FieldLogger no conectado al inicio"
            )

            #reconexion constante
            while controller_running:
                if client.connect():
                    modbus_connected = True
                    break

                time.sleep(RECONNECT_INTERVAL)

            if not modbus_connected:
                finish_current_run("MODBUS_CONNECT_FAILED")
                update_controller_status(
                    status="STOPPED",
                    clear_run=True
                )
                return

            socketio.emit("system_status", {"status": "MODBUS_RECONNECTED"})

            #limpiar alarma
            alarm_clear(socketio, "MODBUS_DISCONNECTED")

            print("FieldLogger reconectado")

        else:

            modbus_connected = True

            print("FieldLogger conectado")

    else:

        print("SIMULATION MODE ENABLED")

    saved_state = get_controller_state()
    current_run_id = saved_state.get("current_run_id")
    current_step = saved_state.get("current_step") or 0
    current_step = min(max(current_step, 0), len(STEPS) - 1)
    step_start_time = saved_state.get("step_started_at") or time.time()

    tc1 = 20
    tc2 = 20
    last_log_time = 0
    pause_start_time = None
    hil_control_state = None

    if client is not None:
        set_hil_control_state(client, HIL_STATE_RUNNING)
        hil_control_state = HIL_STATE_RUNNING

    controller_running = True
    update_controller_status(
        status="RUNNING",
        current_step=current_step,
        step_started_at=step_start_time,
        current_run_id=current_run_id
    )

    try:

        while controller_running:

            try:

                if controller_paused:
                    if client is not None and hil_control_state != HIL_STATE_PAUSED:
                        set_rele(client, RL1_REG, True)
                        set_rele(client, RL2_REG, True)
                        set_hil_control_state(client, HIL_STATE_PAUSED)
                        hil_control_state = HIL_STATE_PAUSED

                    if pause_start_time is None:
                        pause_start_time = time.time()

                    update_controller_status(
                        status="PAUSED",
                        current_step=current_step,
                        step_started_at=step_start_time,
                        current_run_id=current_run_id
                    )

                    time.sleep(0.5)

                    continue

                if pause_start_time is not None:
                    paused_for_s = time.time() - pause_start_time
                    step_start_time += paused_for_s
                    pause_start_time = None

                if client is not None and hil_control_state != HIL_STATE_RUNNING:
                    set_hil_control_state(client, HIL_STATE_RUNNING)
                    hil_control_state = HIL_STATE_RUNNING

                now = time.time()

                step = STEPS[current_step]

                t_in_step = now - step_start_time

                dur = step_duration_s(step)

                with controller_lock:
                    advance_requested = next_step_requested
                    next_step_requested = False

                if advance_requested:
                    if current_step < len(STEPS) - 1:
                        current_step += 1
                        step_start_time = time.time()
                        step = STEPS[current_step]
                        t_in_step = 0
                        dur = step_duration_s(step)

                        update_controller_status(
                            status="RUNNING",
                            current_step=current_step,
                            step_started_at=step_start_time,
                            current_run_id=current_run_id
                        )

                        socketio.emit("system_status", {
                            "status": "RUNNING",
                            "step": step["name"]
                        })
                    else:
                        alarm_raise(
                            socketio,
                            "PROCEDURE_END_REACHED",
                            "info",
                            "No hay más pasos disponibles",
                            current_run_id
                        )

                if t_in_step >= dur:

                    if current_step < len(STEPS) - 1:

                        current_step += 1

                        step_start_time = time.time()

                        update_controller_status(
                            status="RUNNING",
                            current_step=current_step,
                            step_started_at=step_start_time,
                            current_run_id=current_run_id
                        )

                        continue

                sp = step_setpoint(step, t_in_step)

                #metodo real y simulacion

                if SIMULATION_MODE:

                    tc1, tc2 = simulated_temperatures(
                        tc1,
                        tc2,
                        sp
                    )

                else:

                    #intentar leer si da errores continuos registar
                    tc1 = leer_temp(client, TC1_REGISTER)
                    tc2 = leer_temp(client, TC2_REGISTER)


                    if tc1 is None or tc2 is None:

                        print("Error lectura en las termocuplas: desconexión Modbus detectada")

                        socketio.emit("system_status", {"status": "MODBUS_ERROR"})

                        #marcar como desconectado y emitir alarma
                        modbus_connected = False
                        socketio.emit("system_status", {"status": "MODBUS_DISCONNECTED"})

                        # raise alarm
                        alarm_raise(
                            socketio,
                            "MODBUS_DISCONNECTED",
                            "critical",
                            "Desconexión durante lecturas",
                            current_run_id
                        )

                        monitor_temperature_alarms(
                            socketio,
                            tc1,
                            tc2,
                            0,
                            step,
                            t_in_step,
                            now,
                            state,
                            current_run_id
                        )

                        #inetntar reconexion constantemente
                        reconnect_attempts = 0

                        while controller_running and not modbus_connected:

                            reconnect_attempts += 1

                            try:
                                print(f"Reintentando conexión Modbus (intento {reconnect_attempts})")

                                #cerrar cliente anterior si existe y abrir uno nuevo para inicio limpio
                                try:
                                    client.close()
                                except Exception:
                                    logger.exception("No se pudo cerrar cliente Modbus antes de reconectar")

                                client = ModbusTcpClient(
                                    host=IP,
                                    port=PORT_TCP,
                                    timeout=2
                                )

                                if client.connect():
                                    modbus_connected = True
                                    socketio.emit("system_status", {"status": "MODBUS_RECONNECTED"})

                                    # clear alarm
                                    alarm_clear(socketio, "MODBUS_DISCONNECTED", current_run_id)

                                    print("Reconexion Modbus exitosa")
                                    break

                            except Exception:
                                logger.exception("Error durante reintento Modbus")

                            time.sleep(RECONNECT_INTERVAL)

                        #si sigue sin conectar esperar siguiente loop del main para intentar nuevamente
                        if not modbus_connected:
                            time.sleep(SAMPLE_TIME)
                            continue

                #si puede leer proceder con el control de relés y alarmas
                if tc1 is None or tc2 is None:
                    # si no puede entonces no hace nada
                    time.sleep(SAMPLE_TIME)
                    continue

                monitor_temperature_alarms(
                    socketio,
                    tc1,
                    tc2,
                    sp,
                    step,
                    t_in_step,
                    now,
                    state,
                    current_run_id
                )

                rl1_state = 0
                rl2_state = 0

                #control relé1
                if tc1 < (sp - HYSTERESIS):

                    set_rele(client, RL1_REG, False)

                    rl1_state = 1

                else:

                    set_rele(client, RL1_REG, True)

                #control relé2
                if tc2 < (sp - HYSTERESIS):

                    set_rele(client, RL2_REG, False)

                    rl2_state = 1

                else:

                    set_rele(client, RL2_REG, True)

                data = {
                    "timestamp": datetime.now().strftime("%H:%M:%S"),
                    "tc1": tc1,
                    "tc2": tc2,
                    "sp": sp,
                    "rl1": rl1_state,
                    "rl2": rl2_state,
                    "run_id": current_run_id,
                    "step": step["name"],
                    "procedure_step": serialize_step(
                        step,
                        current_step,
                        t_in_step
                    )
                }

                with controller_lock:
                    current_logging_interval_s = logging_interval_s

                if now - last_log_time >= current_logging_interval_s:
                    save_reading(
                        tc1,
                        tc2,
                        sp,
                        rl1_state,
                        rl2_state,
                        step["name"],
                        current_run_id
                    )

                    last_log_time = now

                socketio.emit(
                    "sensor_update",
                    data,
                    namespace="/"
                )

                print(data)

                update_controller_status(
                    status="RUNNING",
                    current_step=current_step,
                    step_started_at=step_start_time,
                    current_run_id=current_run_id
                )

                time.sleep(SAMPLE_TIME)

            except Exception:

                #watchdog caputra y emite alarmas y errores
                err = traceback.format_exc()
                print("Controller loop exception:\n", err)

                try:
                    socketio.emit("system_status", {"status": "CONTROLLER_ERROR", "detail": str(err)})
                except Exception:
                    logger.exception("No se pudo emitir estado CONTROLLER_ERROR")

                #espera unos segundos antes de continuar para evitar bucles de error muy rapido
                time.sleep(5)

                continue

    except KeyboardInterrupt:

        print("Detenido")

    finally:

        # se asegurar de apagar los reles cuando se cumpla la regla de seguridad y se detenga el controlador
        try:
            if client is not None:

                set_rele(client, RL1_REG, True)
                set_rele(client, RL2_REG, True)
                set_hil_control_state(client, HIL_STATE_STOPPED)

                client.close()
        except Exception:
            print("Error during cleanup:\n", traceback.format_exc())

        print("Sistema detenido")
        finish_current_run("CONTROLLER_EXIT")
        update_controller_status(
            status="STOPPED",
            clear_run=True
        )
