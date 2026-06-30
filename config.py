import os


def env_bool(name, default=False):
    value = os.getenv(name)

    if value is None:
        return default

    return value.strip().lower() in ("1", "true", "yes", "on")


def env_int(name, default):
    value = os.getenv(name)

    if value is None:
        return default

    try:
        return int(value)
    except ValueError:
        return default


DEFAULT_CONFIG = {
    "modbus_ip": os.getenv("SCADA_MODBUS_IP", "10.10.50.85"),
    "modbus_port": env_int("SCADA_MODBUS_PORT", 6666),
    "modbus_slave_id": env_int("SCADA_MODBUS_SLAVE_ID", 255),
    "simulation_mode": env_bool("SCADA_SIMULATION_MODE", False),
    "sample_time": env_int("SCADA_SAMPLE_TIME", 1),
    "mode_label": os.getenv("SCADA_MODE_LABEL", "Raspberry HIL")
}
