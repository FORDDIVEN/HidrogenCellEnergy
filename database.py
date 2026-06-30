import sqlite3
import bcrypt
import os
from datetime import datetime

from config import DEFAULT_CONFIG

DB_NAME = "data/horno.db"
VALID_ROLES = ("viewer", "operator", "admin")
DEFAULT_ADMIN_USERNAME = "admin"

#base datos inicial

def _ensure_db_dir():
    db_dir = os.path.dirname(DB_NAME)

    if db_dir:
        os.makedirs(db_dir, exist_ok=True)


def connect_db():
    _ensure_db_dir()
    return sqlite3.connect(DB_NAME)


def init_db():

    conn = connect_db()

    #lecturas
    conn.execute("""
    CREATE TABLE IF NOT EXISTS readings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER,
        timestamp TEXT,
        tc1 REAL,
        tc2 REAL,
        sp REAL,
        rl1 INTEGER,
        rl2 INTEGER,
        step TEXT
    )
    """)

    #usuarios
    conn.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        password TEXT,
        role TEXT,
        created_at TEXT
    )
    """)

    #logs de seguridad
    conn.execute("""
    CREATE TABLE IF NOT EXISTS security_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT,
        username TEXT,
        action TEXT,
        ip TEXT
    )
    """)

    #alarmas
    conn.execute("""
    CREATE TABLE IF NOT EXISTS alarms (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER,
        timestamp TEXT,
        code TEXT,
        severity TEXT,
        message TEXT,
        active INTEGER,
        acknowledged INTEGER,
        acknowledged_at TEXT,
        cleared_at TEXT
    )
    """)

    #eventos de alarmas
    conn.execute("""
    CREATE TABLE IF NOT EXISTS alarm_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER,
        timestamp TEXT,
        alarm_id INTEGER,
        alarm_code TEXT,
        event_type TEXT,
        details TEXT,
        FOREIGN KEY (alarm_id) REFERENCES alarms(id)
    )
    """)

    #inicio de ejecucion
    conn.execute("""
    CREATE TABLE IF NOT EXISTS startup_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT,
        ended_at TEXT,
        status TEXT,
        started_by TEXT,
        stopped_by TEXT,
        stop_reason TEXT,
        simulation_mode INTEGER
    )
    """)

    #cofiguracion del sistema, HIL o simulacion

    conn.execute("""
    CREATE TABLE IF NOT EXISTS system_config (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)

    #estado del contrlador
    conn.execute("""
    CREATE TABLE IF NOT EXISTS controller_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        status TEXT,
        current_run_id INTEGER,
        current_step INTEGER,
        step_started_at REAL,
        logging_interval_s INTEGER,
        updated_at TEXT
    )
    """)

    _ensure_column(conn, "readings", "run_id", "INTEGER")
    _ensure_column(conn, "alarms", "run_id", "INTEGER")
    _ensure_column(conn, "alarm_events", "run_id", "INTEGER")
    _ensure_column(conn, "controller_state", "current_run_id", "INTEGER")
    _ensure_column(conn, "controller_state", "logging_interval_s", "INTEGER")

    for key, value in DEFAULT_CONFIG.items():
        conn.execute("""
        INSERT OR IGNORE INTO system_config (
            key,
            value
        ) VALUES (?, ?)
        """, (
            key,
            str(value)
        ))

    conn.execute("""
    INSERT OR IGNORE INTO controller_state (
        id,
        status,
        current_run_id,
        current_step,
        step_started_at,
        logging_interval_s,
        updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        1,
        "STOPPED",
        None,
        0,
        None,
        1,
        datetime.now().isoformat()
    ))

    #admin inicial opcional si no existe la base

    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM users")

    user_count = cursor.fetchone()[0]

    if user_count == 0:

        bootstrap_username = os.getenv(
            "SCADA_BOOTSTRAP_ADMIN_USERNAME",
            DEFAULT_ADMIN_USERNAME
        ).strip() or DEFAULT_ADMIN_USERNAME

        bootstrap_password = os.getenv("SCADA_BOOTSTRAP_ADMIN_PASSWORD")

        if not bootstrap_password:
            print(
                "No hay usuarios iniciales. Crea el primer administrador "
                "desde /setup o define SCADA_BOOTSTRAP_ADMIN_PASSWORD."
            )
            conn.commit()
            conn.close()
            return

        hashed_password = bcrypt.hashpw(
            bootstrap_password.encode(),
            bcrypt.gensalt()
        ).decode()

        conn.execute("""
        INSERT INTO users (
            username,
            password,
            role,
            created_at
        ) VALUES (?, ?, ?, ?)
        """, (
            bootstrap_username,
            hashed_password,
            "admin",
            datetime.now().isoformat()
        ))

        print(
            f"Usuario admin inicial creado: {bootstrap_username}"
        )

    conn.commit()
    conn.close()


def has_users():

    conn = connect_db()

    cursor = conn.cursor()

    cursor.execute("SELECT 1 FROM users LIMIT 1")

    exists = cursor.fetchone() is not None

    conn.close()

    return exists


def _ensure_column(conn, table, column, column_type):

    cursor = conn.cursor()

    cursor.execute(f"PRAGMA table_info({table})")

    columns = [row[1] for row in cursor.fetchall()]

    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


#datos del contrlador y si configuracion
def _coerce_config(key, value):

    if key in ("modbus_port", "modbus_slave_id", "sample_time"):
        return int(value)

    if key == "simulation_mode":
        return str(value).strip().lower() in ("1", "true", "yes", "on")

    return value


def get_system_config():

    conn = connect_db()

    cursor = conn.cursor()

    try:
        cursor.execute("""
        SELECT key, value
        FROM system_config
        """)
    except sqlite3.OperationalError:
        conn.close()
        return dict(DEFAULT_CONFIG)

    rows = cursor.fetchall()

    conn.close()

    config = dict(DEFAULT_CONFIG)

    for key, value in rows:
        config[key] = _coerce_config(key, value)

    return config


def save_system_config(config):

    allowed = set(DEFAULT_CONFIG.keys())

    conn = connect_db()

    for key, value in config.items():
        if key not in allowed:
            continue

        conn.execute("""
        INSERT INTO system_config (
            key,
            value
        ) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value
        """, (
            key,
            str(value)
        ))

    conn.commit()
    conn.close()

    return get_system_config()

#guardar datos del sensor
def save_reading(tc1, tc2, sp, rl1, rl2, step, run_id=None):

    conn = connect_db()

    conn.execute("""
    INSERT INTO readings (
        run_id,
        timestamp,
        tc1,
        tc2,
        sp,
        rl1,
        rl2,
        step
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        run_id,
        datetime.now().isoformat(),
        tc1,
        tc2,
        sp,
        rl1,
        rl2,
        step
    ))

    conn.commit()
    conn.close()


#guardar estao del colntrlador
def save_controller_state(
    status,
    current_step=0,
    step_started_at=None,
    current_run_id=None,
    logging_interval_s=None
):

    conn = connect_db()

    conn.execute("""
    INSERT INTO controller_state (
        id,
        status,
        current_run_id,
        current_step,
        step_started_at,
        logging_interval_s,
        updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(id) DO UPDATE SET
        status = excluded.status,
        current_run_id = excluded.current_run_id,
        current_step = excluded.current_step,
        step_started_at = excluded.step_started_at,
        logging_interval_s = COALESCE(excluded.logging_interval_s, controller_state.logging_interval_s),
        updated_at = excluded.updated_at
    """, (
        1,
        status,
        current_run_id,
        current_step,
        step_started_at,
        logging_interval_s,
        datetime.now().isoformat()
    ))

    conn.commit()
    conn.close()


def get_controller_state():

    conn = connect_db()

    cursor = conn.cursor()

    cursor.execute("""
    SELECT status, current_run_id, current_step, step_started_at, logging_interval_s, updated_at
    FROM controller_state
    WHERE id = 1
    """)

    row = cursor.fetchone()

    conn.close()

    if not row:
        return {
            "status": "STOPPED",
            "current_run_id": None,
            "current_step": 0,
            "step_started_at": None,
            "logging_interval_s": 1,
            "updated_at": None
        }

    return {
        "status": row[0],
        "current_run_id": row[1],
        "current_step": row[2],
        "step_started_at": row[3],
        "logging_interval_s": row[4] or 1,
        "updated_at": row[5]
    }


#inicio del procedimiento
def create_startup_run(started_by, simulation_mode):

    conn = connect_db()

    cursor = conn.execute("""
    INSERT INTO startup_runs (
        started_at,
        ended_at,
        status,
        started_by,
        stopped_by,
        stop_reason,
        simulation_mode
    ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.now().isoformat(),
        None,
        "RUNNING",
        started_by,
        None,
        None,
        1 if simulation_mode else 0
    ))

    run_id = cursor.lastrowid

    conn.commit()
    conn.close()

    return run_id


def finish_startup_run(run_id, stopped_by, stop_reason="STOP_SYSTEM"):

    if run_id is None:
        return

    conn = connect_db()

    conn.execute("""
    UPDATE startup_runs
    SET ended_at = ?,
        status = ?,
        stopped_by = ?,
        stop_reason = ?
    WHERE id = ? AND status = ?
    """, (
        datetime.now().isoformat(),
        "STOPPED",
        stopped_by,
        stop_reason,
        run_id,
        "RUNNING"
    ))

    conn.commit()
    conn.close()


def get_startup_run(run_id):

    conn = connect_db()

    cursor = conn.cursor()

    cursor.execute("""
    SELECT id, started_at, ended_at, status, started_by, stopped_by, stop_reason, simulation_mode
    FROM startup_runs
    WHERE id = ?
    """, (run_id,))

    row = cursor.fetchone()

    conn.close()

    return row


def get_latest_startup_runs(limit=50):

    conn = connect_db()

    cursor = conn.cursor()

    cursor.execute("""
    SELECT id, started_at, ended_at, status, started_by, stopped_by, stop_reason, simulation_mode
    FROM startup_runs
    ORDER BY id DESC
    LIMIT ?
    """, (limit,))

    rows = cursor.fetchall()

    conn.close()

    return rows

#users
def get_user(username):

    conn = connect_db()

    cursor = conn.cursor()

    cursor.execute("""
    SELECT id, username, password, role
    FROM users
    WHERE username = ?
    """, (username,))

    user = cursor.fetchone()

    conn.close()

    return user


def get_user_by_id(user_id):

    conn = connect_db()

    cursor = conn.cursor()

    cursor.execute("""
    SELECT id, username, role
    FROM users
    WHERE id = ?
    """, (user_id,))

    user = cursor.fetchone()

    conn.close()

    return user

def get_all_users():

    conn = connect_db()

    cursor = conn.cursor()

    cursor.execute("""
    SELECT id, username, role, created_at
    FROM users
    """)

    users = cursor.fetchall()

    conn.close()

    return users

def create_user(username, password, role):

    username = username.strip()

    if role not in VALID_ROLES:
        raise ValueError("Rol inválido")

    conn = connect_db()

    hashed_password = bcrypt.hashpw(
        password.encode(),
        bcrypt.gensalt()
    ).decode()

    conn.execute("""
    INSERT INTO users (
        username,
        password,
        role,
        created_at
    ) VALUES (?, ?, ?, ?)
    """, (
        username,
        hashed_password,
        role,
        datetime.now().isoformat()
    ))

    conn.commit()
    conn.close()

def delete_user(user_id):

    conn = connect_db()

    conn.execute("""
    DELETE FROM users
    WHERE id = ?
    """, (user_id,))

    conn.commit()
    conn.close()

def update_user(user_id, username, password, role):

    username = username.strip()
    password = (password or "").strip()

    if role not in VALID_ROLES:
        raise ValueError("Rol inválido")

    conn = connect_db()

    if password:
        hashed_password = bcrypt.hashpw(
            password.encode(),
            bcrypt.gensalt()
        ).decode()

        conn.execute("""
        UPDATE users
        SET username = ?,
            password = ?,
            role = ?
        WHERE id = ?
        """, (
            username,
            hashed_password,
            role,
            user_id
        ))
    else:
        conn.execute("""
        UPDATE users
        SET username = ?,
            role = ?
        WHERE id = ?
        """, (
            username,
            role,
            user_id
        ))

    conn.commit()
    conn.close()

#logs de segridad

def log_security_event(username, action, ip):

    conn = connect_db()

    conn.execute("""
    INSERT INTO security_logs (
        timestamp,
        username,
        action,
        ip
    ) VALUES (?, ?, ?, ?)
    """, (
        datetime.now().isoformat(),
        username,
        action,
        ip
    ))

    conn.commit()
    conn.close()


#alarm helper 
def get_active_alarm_by_code(code):

    conn = connect_db()

    cursor = conn.cursor()

    cursor.execute("""
    SELECT id, timestamp, code, severity, message, acknowledged, acknowledged_at
    FROM alarms
    WHERE code = ? AND active = 1
    ORDER BY id DESC
    LIMIT 1
    """, (code,))

    alarm = cursor.fetchone()

    conn.close()

    return alarm


def raise_alarm(code, severity, message, run_id=None):

    conn = connect_db()

    active_alarm = get_active_alarm_by_code(code)

    if active_alarm:

        conn.execute("""
        UPDATE alarms
        SET timestamp = ?,
            run_id = COALESCE(?, run_id),
            severity = ?,
            message = ?
        WHERE id = ?
        """, (
            datetime.now().isoformat(),
            run_id,
            severity,
            message,
            active_alarm[0]
        ))

        alarm_id = active_alarm[0]
        created = False

    else:

        cursor = conn.execute("""
        INSERT INTO alarms (
            run_id,
            timestamp,
            code,
            severity,
            message,
            active,
            acknowledged,
            acknowledged_at,
            cleared_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            run_id,
            datetime.now().isoformat(),
            code,
            severity,
            message,
            1,
            0,
            None,
            None
        ))

        alarm_id = cursor.lastrowid
        created = True

    conn.commit()
    conn.close()

    return created, alarm_id


def clear_alarm(code):

    conn = connect_db()

    cursor = conn.execute("""
    UPDATE alarms
    SET active = 0,
        cleared_at = ?
    WHERE code = ? AND active = 1
    """, (
        datetime.now().isoformat(),
        code
    ))

    cleared = cursor.rowcount > 0

    conn.commit()
    conn.close()

    return cleared


def acknowledge_alarm(alarm_id):

    conn = connect_db()

    cursor = conn.execute("""
    UPDATE alarms
    SET acknowledged = 1,
        acknowledged_at = ?
    WHERE id = ? AND active = 1
    """, (
        datetime.now().isoformat(),
        alarm_id
    ))

    acknowledged = cursor.rowcount > 0

    conn.commit()
    conn.close()

    return acknowledged


def get_active_alarms():

    conn = connect_db()

    cursor = conn.cursor()

    cursor.execute("""
    SELECT id, timestamp, code, severity, message, active, acknowledged, acknowledged_at, cleared_at, run_id
    FROM alarms
    WHERE active = 1
    ORDER BY id DESC
    """)

    rows = cursor.fetchall()

    conn.close()

    return rows

#alarme event
def log_alarm_event(alarm_id, alarm_code, event_type, details=None, run_id=None):
    """
    Log an alarm event: raised, cleared, acknowledged
    """
    conn = connect_db()

    conn.execute("""
    INSERT INTO alarm_events (
        run_id,
        timestamp,
        alarm_id,
        alarm_code,
        event_type,
        details
    ) VALUES (?, ?, ?, ?, ?, ?)
    """, (
        run_id,
        datetime.now().isoformat(),
        alarm_id,
        alarm_code,
        event_type,
        details
    ))

    conn.commit()
    conn.close()


def get_alarm_events(limit=500):
    """
    Get alarm events history
    """
    conn = connect_db()

    cursor = conn.cursor()

    cursor.execute("""
    SELECT 
        id,
        timestamp,
        alarm_id,
        alarm_code,
        event_type,
        details
    FROM alarm_events
    ORDER BY id DESC
    LIMIT ?
    """, (limit,))

    rows = cursor.fetchall()

    conn.close()

    return rows

def get_alarm_history(limit=200):

    conn = connect_db()

    cursor = conn.cursor()

    cursor.execute("""
    SELECT id, timestamp, code, severity, message, active, acknowledged, acknowledged_at, cleared_at
    FROM alarms
    ORDER BY id DESC
    LIMIT ?
    """, (limit,))

    rows = cursor.fetchall()

    conn.close()

    return rows

def get_security_logs():

    conn = connect_db()

    cursor = conn.cursor()

    cursor.execute("""
    SELECT
        timestamp,
        username,
        action,
        ip
    FROM security_logs
    ORDER BY id DESC
    LIMIT 200
    """)

    logs = cursor.fetchall()

    conn.close()

    return logs
