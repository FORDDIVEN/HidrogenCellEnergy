from flask import (
    Flask,
    render_template,
    jsonify,
    send_file,
    request,
    redirect,
    session,
    abort
)

from flask_socketio import SocketIO, disconnect

from flask_login import (
    LoginManager,
    UserMixin,
    login_user,
    login_required,
    logout_user,
    current_user
)

import bcrypt
import io
import os
import secrets
import threading
import sqlite3
import csv
import time
import pandas as pd

from functools import wraps
from datetime import timedelta

from controller import (
    controller_loop,
    start_controller,
    stop_controller,
    pause_controller,
    resume_controller,
    get_system_status,
    request_next_step,
    serialize_step,
    get_logging_interval,
    set_logging_interval,
    get_modbus_config,
    apply_modbus_config,
    test_modbus_connection,
    SIMULATION_MODE,
    recover_controller_state_on_boot
)
from startup_procedure import STEPS, validate_steps

from database import (
    init_db,
    get_user,
    get_user_by_id,
    get_all_users,
    create_user,
    delete_user,
    update_user,
    log_security_event,
    get_security_logs,
    create_startup_run,
    finish_startup_run,
    get_latest_startup_runs,
    get_startup_run,
    get_active_alarms,
    acknowledge_alarm,
    get_alarm_history,
    get_alarm_events,
    log_alarm_event,
    has_users,
    connect_db,
    VALID_ROLES
)

validate_steps()

app = Flask(__name__)

# seguridad web

secret_key = os.getenv("SCADA_SECRET_KEY")

if not secret_key:
    print(
        "WARNING: SCADA_SECRET_KEY no definido. "
        "Usa este modo solo en desarrollo; en producción define una clave segura."
    )
    secret_key = secrets.token_urlsafe(32)

app.secret_key = secret_key

app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

app.permanent_session_lifetime = timedelta(
    minutes=30
)


#seguridad Cross-Site Request Forgery o falsificacion de peticiones en sitios cruzados
def get_csrf_token():

    token = session.get("csrf_token")

    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token

    return token


@app.context_processor
def inject_csrf_token():

    return {
        "csrf_token": get_csrf_token()
    }


def get_form_value(name, default=""):

    return str(request.form.get(name, default) or "").strip()


def get_data_value(data, name, default=""):

    value = data.get(name, default)

    if isinstance(value, str):
        return value.strip()

    return value


def parse_bool(value):

    if isinstance(value, bool):
        return value

    if value is None:
        return False

    return str(value).strip().lower() in ("1", "true", "yes", "on")


@app.before_request
def csrf_protect():

    if request.method != "POST":
        return

    expected = session.get("csrf_token")
    provided = (
        request.form.get("csrf_token")
        or request.headers.get("X-CSRFToken")
        or request.headers.get("X-CSRF-Token")
        or request.headers.get("X-XSRF-TOKEN")
    )

    if not expected or not provided or not secrets.compare_digest(expected, provided):
        abort(400, "CSRF token inválido")

#security headers de aqui hasta abajo
@app.after_request
def security_headers(response):

    response.headers[
        "X-Frame-Options"
    ] = "DENY"

    response.headers[
        "X-Content-Type-Options"
    ] = "nosniff"

    response.headers[
        "Referrer-Policy"
    ] = "no-referrer"

    response.headers[
        "Content-Security-Policy"
    ] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'"
    )

    return response

#socket.io para crear conexion
socketio = SocketIO(
    app,
    async_mode="threading",
    cors_allowed_origins=os.getenv("SCADA_SOCKETIO_ORIGINS")
)


@socketio.on("connect")
def socket_connect():

    if not current_user.is_authenticated:
        disconnect()
        return False

    return True

#aqui se maneja el login
login_manager = LoginManager()

login_manager.init_app(app)

login_manager.login_view = "login"

#variables globales
controller_thread = None

paused = False

failed_logins = {}
app_initialized = False
LOGIN_MAX_FAILED_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 15 * 60


def initialize_app_once():

    global app_initialized

    if app_initialized:
        return

    init_db()
    recover_controller_state_on_boot()
    app_initialized = True


@app.before_request
def ensure_initialized():

    initialize_app_once()

#clase usuario

class User(UserMixin):

    def __init__(self, id, username, role):

        self.id = id
        self.username = username
        self.role = role

#se carga el usuario
@login_manager.user_loader
def load_user(user_id):

    user = get_user_by_id(user_id)

    if user:

        return User(
            user[0],
            user[1],
            user[2]
        )

    return None

#donde se ve el rol
def role_required(roles):

    def decorator(f):

        @wraps(f)
        def wrapped(*args, **kwargs):

            if current_user.role not in roles:
                return "Acceso denegado", 403

            return f(*args, **kwargs)

        return wrapped

    return decorator


def login_attempt_key(username, ip):

    return (
        (username or "unknown").strip().lower(),
        ip or "unknown"
    )


def get_login_attempt(username, ip):

    key = login_attempt_key(username, ip)
    attempt = failed_logins.get(key)

    if not attempt:
        return key, {
            "count": 0,
            "first_failed_at": None,
            "locked_until": None
        }

    now = time.time()
    locked_until = attempt.get("locked_until")
    first_failed_at = attempt.get("first_failed_at")

    if locked_until and locked_until <= now:
        failed_logins.pop(key, None)
        return key, {
            "count": 0,
            "first_failed_at": None,
            "locked_until": None
        }

    if first_failed_at and now - first_failed_at > LOGIN_LOCKOUT_SECONDS:
        failed_logins.pop(key, None)
        return key, {
            "count": 0,
            "first_failed_at": None,
            "locked_until": None
        }

    return key, attempt


def register_failed_login(username, ip):

    key, attempt = get_login_attempt(username, ip)
    now = time.time()

    if not attempt.get("first_failed_at"):
        attempt["first_failed_at"] = now

    attempt["count"] = attempt.get("count", 0) + 1

    if attempt["count"] >= LOGIN_MAX_FAILED_ATTEMPTS:
        attempt["locked_until"] = now + LOGIN_LOCKOUT_SECONDS

    failed_logins[key] = attempt

    return attempt


def clear_failed_logins(username, ip):

    failed_logins.pop(login_attempt_key(username, ip), None)


def is_login_locked(attempt):

    locked_until = attempt.get("locked_until")

    return locked_until is not None and locked_until > time.time()


#configuracion inicial
@app.route("/setup", methods=["GET", "POST"])
def setup_admin():

    if has_users():
        return redirect("/login")

    if request.method == "POST":

        username = get_form_value("username")
        password = get_form_value("password")
        confirm_password = get_form_value("confirm_password")

        if not username:
            return "Usuario requiere un nombre válido", 400

        if len(password) < 6:
            return "Password demasiado corta", 400

        if password != confirm_password:
            return "Las contraseñas no coinciden", 400

        try:
            create_user(
                username,
                password,
                "admin"
            )
        except sqlite3.IntegrityError:
            return "El usuario ya existe", 409

        log_security_event(
            username,
            "BOOTSTRAP_ADMIN_CREATED",
            request.remote_addr
        )

        user = get_user(username)

        login_user(
            User(
                user[0],
                user[1],
                user[3]
            )
        )

        return redirect("/")

    return render_template("setup.html")


#login
@app.route("/login", methods=["GET", "POST"])
def login():

    if not has_users():
        return redirect("/setup")

    if request.method == "POST":

        username = get_form_value("username")
        password = get_form_value("password")

        ip = request.remote_addr
        _, attempt = get_login_attempt(username, ip)

        if is_login_locked(attempt):
            log_security_event(
                username or "unknown",
                "ACCOUNT_LOCKED",
                ip
            )
            return "Demasiados intentos", 429

        if not username or not password:
            attempt = register_failed_login(username, ip)
            log_security_event(
                username or "unknown",
                "LOGIN_FAILED",
                ip
            )

            if is_login_locked(attempt):
                log_security_event(
                    username or "unknown",
                    "ACCOUNT_LOCKED",
                    ip
                )
                return "Demasiados intentos", 429

            return "Login inválido", 401

        user = get_user(username)

        if user:

            user_id = user[0]
            db_username = user[1]
            db_password = user[2]
            db_role = user[3]

            if bcrypt.checkpw(
                password.encode(),
                db_password.encode()
            ):

                clear_failed_logins(db_username, ip)

                login_user(
                    User(
                        user_id,
                        db_username,
                        db_role
                    )
                )

                log_security_event(
                    db_username,
                    "LOGIN",
                    ip
                )

                return redirect("/")

        #Intento fallido
        attempt = register_failed_login(username, ip)

        #se registrael intento fallico
        log_security_event(
            username or "unknown",
            "LOGIN_FAILED",
            ip
        )

        #si falla mucho se bloquea la cuenta pro ip
        if is_login_locked(attempt):

            log_security_event(
                username or "unknown",
                "ACCOUNT_LOCKED",
                ip
            )

            return "Demasiados intentos", 429

        return "Login inválido", 401

    return render_template("login.html")

#para desloguearse
@app.route("/logout", methods=["POST"])
@login_required
def logout():

    log_security_event(
        current_user.username,
        "LOGOUT",
        request.remote_addr
    )

    logout_user()

    return redirect("/login")

#ruta home
@app.route("/")
@login_required
def index():

    return render_template(
        "index.html",
        role=current_user.role,
        username=current_user.username
    )

#historial ejecuciones
@app.route("/history")
@login_required
def history():

    conn = connect_db()

    cursor = conn.cursor()

    run_id = request.args.get("run_id", type=int)

    query = """
    SELECT
        run_id,
        timestamp,
        tc1,
        tc2,
        sp,
        rl1,
        rl2,
        step
    FROM readings
    """

    params = ()

    if run_id is not None:
        query += " WHERE run_id = ?"
        params = (run_id,)

    query += """
    ORDER BY id DESC
    LIMIT 100
    """

    cursor.execute(query, params)

    rows = cursor.fetchall()

    conn.close()

    data = []

    for r in rows:

        data.append({
            "run_id": r[0],
            "timestamp": r[1],
            "tc1": r[2],
            "tc2": r[3],
            "sp": r[4],
            "rl1": r[5],
            "rl2": r[6],
            "step": r[7]
        })

    return jsonify(data)

#exportar datos a csv
def requested_time_range():

    start = (request.args.get("start") or "").strip()
    end = (request.args.get("end") or "").strip()

    if len(start) == 16:
        start = f"{start}:00"

    if len(end) == 16:
        end = f"{end}:59.999999"

    return start, end


@app.route("/export_csv")
@login_required
def export_csv():

    conn = connect_db()

    cursor = conn.cursor()

    run_id = request.args.get("run_id", type=int)
    start, end = requested_time_range()

    query = """
    SELECT id, run_id, timestamp, tc1, tc2, sp, rl1, rl2, step
    FROM readings
    """

    filters = []
    params = []

    if run_id is not None:
        filters.append("run_id = ?")
        params.append(run_id)

    if start:
        filters.append("timestamp >= ?")
        params.append(start)

    if end:
        filters.append("timestamp <= ?")
        params.append(end)

    if filters:
        query += " WHERE " + " AND ".join(filters)

    query += " ORDER BY id ASC"

    cursor.execute(query, params)

    rows = cursor.fetchall()

    conn.close()

    csv_buffer = io.StringIO()

    writer = csv.writer(csv_buffer)

    writer.writerow([
        "sample",
        "run_id",
        "timestamp",
        "tc1",
        "tc2",
        "sp",
        "rl1",
        "rl2",
        "step"
    ])

    for sample, row in enumerate(rows, start=1):
        writer.writerow([sample, *row[1:]])

    output = io.BytesIO(csv_buffer.getvalue().encode("utf-8"))
    output.seek(0)

    return send_file(
        output,
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"export_run_{run_id}.csv" if run_id else "export.csv"
    )

#exportar a excel
@app.route("/export_excel")
@login_required
def export_excel():

    conn = connect_db()

    run_id = request.args.get("run_id", type=int)
    start, end = requested_time_range()
    params = []
    filters = []
    query = "SELECT id, run_id, timestamp, tc1, tc2, sp, rl1, rl2, step FROM readings"

    if run_id is not None:
        filters.append("run_id = ?")
        params.append(run_id)

    if start:
        filters.append("timestamp >= ?")
        params.append(start)

    if end:
        filters.append("timestamp <= ?")
        params.append(end)

    if filters:
        query += " WHERE " + " AND ".join(filters)

    query += " ORDER BY id ASC"

    df = pd.read_sql_query(
        query,
        conn,
        params=params or None
    )

    conn.close()

    if "id" in df.columns:
        df = df.drop(columns=["id"])

    df.insert(0, "sample", range(1, len(df) + 1))

    output = io.BytesIO()

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(
            writer,
            index=False
        )

    output.seek(0)

    return send_file(
        output,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=f"export_run_{run_id}.xlsx" if run_id else "export.xlsx"
    )

#contorla el thread del controlador
def start_controller_thread():
    controller_loop(socketio)

#da inicio el controlador de la celda
@app.route("/start", methods=["POST"])
@login_required
@role_required(["operator", "admin"])
def start():

    global controller_thread
    global paused

    paused = False

    if controller_thread is None or not controller_thread.is_alive():

        run_id = create_startup_run(
            current_user.username,
            get_modbus_config()["simulation_mode"]
        )

        start_controller(run_id)

        controller_thread = threading.Thread(
            target=start_controller_thread
        )

        controller_thread.daemon = True

        controller_thread.start()

        socketio.emit("reset_chart")

        socketio.emit("system_status", {
            "status": "RUNNING",
            "current_run_id": run_id
        })

        log_security_event(
            current_user.username,
            f"START_SYSTEM:RUN:{run_id}",
            request.remote_addr
        )

        return f"Controller iniciado. Run #{run_id}"

    return "Controller ya está corriendo"

#manda señal de stop
@app.route("/stop", methods=["POST"])
@login_required
@role_required(["operator", "admin"])
def stop():

    status = get_system_status()
    run_id = status.get("current_run_id")
    reason = get_form_value("reason", "STOP_SYSTEM")

    stop_controller()

    finish_startup_run(
        run_id,
        current_user.username,
        reason
    )

    socketio.emit("system_status", {
        "status": "STOPPED",
        "current_run_id": None
    })

    log_security_event(
        current_user.username,
        f"STOP_SYSTEM:RUN:{run_id}",
        request.remote_addr
    )

    return "Controller detenido"

#manda señal de pausar el procedimiento
@app.route("/pause", methods=["POST"])
@login_required
@role_required(["operator", "admin"])
def pause():

    global paused

    if not paused:

        pause_controller()

        paused = True

        socketio.emit("system_status", {
            "status": "PAUSED"
        })

        log_security_event(
            current_user.username,
            "PAUSE_SYSTEM",
            request.remote_addr
        )

        return "Sistema pausado"

    else:

        resume_controller()

        paused = False

        socketio.emit("system_status", {
            "status": "RUNNING"
        })

        log_security_event(
            current_user.username,
            "RESUME_SYSTEM",
            request.remote_addr
        )

        return "Sistema reanudado"


#muestra el estado del procedimiento (status)

@app.route("/api/system/status")
@login_required
def api_system_status():

    return jsonify(get_system_status())


#aqui se maneja la config previa a iniciar la celda
@app.route("/admin/config")
@login_required
@role_required(["admin"])
def admin_config():

    return render_template(
        "config.html",
        config=get_modbus_config()
    )


@app.route("/api/config")
@login_required
@role_required(["admin"])
def api_get_config():

    return jsonify(get_modbus_config())


def request_config_payload():

    if request.is_json:
        data = request.get_json(silent=True) or {}
    else:
        data = request.form

    return {
        "modbus_ip": get_data_value(data, "modbus_ip"),
        "modbus_port": get_data_value(data, "modbus_port"),
        "modbus_slave_id": get_data_value(data, "modbus_slave_id"),
        "simulation_mode": parse_bool(get_data_value(data, "simulation_mode")),
        "sample_time": get_data_value(data, "sample_time"),
        "mode_label": get_data_value(data, "mode_label")
    }


def summarize_config(config):

    public_keys = (
        "mode_label",
        "modbus_ip",
        "modbus_port",
        "modbus_slave_id",
        "simulation_mode",
        "sample_time"
    )

    return ",".join(
        f"{key}={config.get(key)}"
        for key in public_keys
    )


@app.route("/api/config", methods=["POST"])
@login_required
@role_required(["admin"])
def api_set_config():

    if get_system_status().get("status") != "STOPPED":
        return "Deten el sistema antes de cambiar la configuracion", 409

    old_config = get_modbus_config()

    try:
        config = apply_modbus_config(request_config_payload())
    except (TypeError, ValueError) as exc:
        return str(exc), 400

    log_security_event(
        current_user.username,
        f"UPDATE_SYSTEM_CONFIG:{summarize_config(old_config)}->{summarize_config(config)}",
        request.remote_addr
    )

    socketio.emit("system_status", get_system_status())

    return jsonify(config)


@app.route("/api/config/test_modbus", methods=["POST"])
@login_required
@role_required(["admin"])
def api_test_modbus():

    try:
        result = test_modbus_connection(request_config_payload())
    except (TypeError, ValueError) as exc:
        return jsonify({
            "ok": False,
            "message": str(exc)
        }), 400

    log_security_event(
        current_user.username,
        "TEST_MODBUS_CONFIG",
        request.remote_addr
    )

    status_code = 200 if result["ok"] else 400

    return jsonify(result), status_code


@app.route("/api/logging_interval")
@login_required
def api_get_logging_interval():

    return jsonify(get_logging_interval())


@app.route("/api/logging_interval", methods=["POST"])
@login_required
@role_required(["operator", "admin"])
def api_set_logging_interval():

    if request.is_json:
        data = request.get_json(silent=True) or {}
        value = get_data_value(data, "interval")
    else:
        value = get_form_value("interval")

    try:
        interval = set_logging_interval(value)
    except ValueError as exc:
        return str(exc), 400

    log_security_event(
        current_user.username,
        f"SET_LOG_INTERVAL:{interval}s",
        request.remote_addr
    )

    socketio.emit("system_status", get_system_status())

    return jsonify(get_logging_interval())


#empieza a guardar datos cuando se inicia la celda
def serialize_run(row):

    return {
        "id": row[0],
        "started_at": row[1],
        "ended_at": row[2],
        "status": row[3],
        "started_by": row[4],
        "stopped_by": row[5],
        "stop_reason": row[6],
        "simulation_mode": bool(row[7])
    }


@app.route("/api/runs")
@login_required
def api_runs():

    return jsonify([
        serialize_run(row)
        for row in get_latest_startup_runs()
    ])


@app.route("/api/runs/<int:run_id>/readings")
@login_required
def api_run_readings(run_id):

    if not get_startup_run(run_id):
        abort(404)

    conn = connect_db()

    cursor = conn.cursor()

    start = (request.args.get("start") or "").strip()
    end = (request.args.get("end") or "").strip()

    if len(start) == 16:
        start = f"{start}:00"

    if len(end) == 16:
        end = f"{end}:59.999999"

    query = """
    SELECT timestamp, tc1, tc2, sp, rl1, rl2, step
    FROM readings
    WHERE run_id = ?
    """

    params = [run_id]

    if start:
        query += " AND timestamp >= ?"
        params.append(start)

    if end:
        query += " AND timestamp <= ?"
        params.append(end)

    query += " ORDER BY id ASC"

    cursor.execute(query, params)

    rows = cursor.fetchall()

    conn.close()

    return jsonify([
        {
            "sample": index,
            "timestamp": row[0],
            "tc1": row[1],
            "tc2": row[2],
            "sp": row[3],
            "rl1": bool(row[4]),
            "rl2": bool(row[5]),
            "step": row[6]
        }
        for index, row in enumerate(rows, start=1)
    ])


@app.route("/runs")
@login_required
def runs_page():

    return render_template(
        "runs.html",
        runs=[
            serialize_run(row)
            for row in get_latest_startup_runs()
        ],
        role=current_user.role
    )


@app.route("/runs/<int:run_id>/chart")
@login_required
def run_chart_page(run_id):

    row = get_startup_run(run_id)

    if not row:
        abort(404)

    return render_template(
        "run_chart.html",
        run=serialize_run(row)
    )


#procedimiento
@app.route("/api/procedure")
@login_required
def api_procedure():

    return jsonify([
        serialize_step(step, index)
        for index, step in enumerate(STEPS)
    ])


@app.route("/procedure")
@login_required
def procedure_page():

    return render_template(
        "procedure.html",
        steps=[
            serialize_step(step, index)
            for index, step in enumerate(STEPS)
        ],
        role=current_user.role
    )


@app.route("/api/procedure/next", methods=["POST"])
@login_required
@role_required(["admin"])
def api_next_procedure_step():

    advanced = request_next_step()

    if not advanced:
        return "Controller no está corriendo", 409

    log_security_event(
        current_user.username,
        "PROCEDURE_NEXT_STEP",
        request.remote_addr
    )

    return "OK"

#Permiso solo para admins o acceso especial

@app.route("/admin/users")
@login_required
@role_required(["admin"])
def admin_users():

    users = get_all_users()

    return render_template(
        "admin_users.html",
        users=users
    )

#crear nuevo usuario
@app.route("/admin/create_user", methods=["POST"])
@login_required
@role_required(["admin"])
def admin_create_user():

    username = get_form_value("username")
    password = get_form_value("password")
    role = get_form_value("role")

    if not username:
        return "Usuario requiere un nombre válido", 400

    if len(password) < 6:
        return "Password demasiado corta", 400

    if role not in VALID_ROLES:
        return "Rol inválido", 400

    try:
        create_user(
            username,
            password,
            role
        )
    except sqlite3.IntegrityError:
        return "El usuario ya existe", 409
    except ValueError as exc:
        return str(exc), 400

    log_security_event(
        current_user.username,
        f"CREATE_USER:{username}",
        request.remote_addr
    )

    return redirect("/admin/users")

#para borrar usuario
@app.route("/admin/delete_user/<int:user_id>", methods=["POST"])
@login_required
@role_required(["admin"])
def admin_delete_user(user_id):

    # prevent deleting self
    if current_user.id == user_id:
        return "No se puede eliminar el usuario actual", 400

    target = next(
        (user for user in get_all_users() if user[0] == user_id),
        None
    )

    if target is None:
        return "Usuario no encontrado", 404

    if target[2] == "admin":
        admin_count = sum(
            1 for user in get_all_users()
            if user[2] == "admin"
        )

        if admin_count <= 1:
            return "No se puede eliminar el último admin", 400

    delete_user(user_id)

    log_security_event(
        current_user.username,
        f"DELETE_USER:{user_id}",
        request.remote_addr
    )

    return redirect("/admin/users")

#actualizar usuario
@app.route("/admin/update_user/<int:user_id>", methods=["POST"])
@login_required
@role_required(["admin"])
def admin_update_user(user_id):

    username = get_form_value("username")
    password = get_form_value("password")
    role = get_form_value("role")

    if not username:
        return "Usuario requiere un nombre válido", 400

    if password and len(password) < 6:
        return "Password demasiado corta", 400

    if role not in VALID_ROLES:
        return "Rol inválido", 400

    if current_user.id == user_id and role != "admin":
        return "No se puede quitar el rol admin al usuario actual", 400

    try:
        update_user(
            user_id,
            username,
            password,
            role
        )
    except sqlite3.IntegrityError:
        return "El usuario ya existe", 409
    except ValueError as exc:
        return str(exc), 400

    log_security_event(
        current_user.username,
        f"UPDATE_USER:{user_id}",
        request.remote_addr
    )

    return redirect("/admin/users")

#logs de seguridad
@app.route("/admin/logs")
@login_required
@role_required(["admin"])
def admin_logs():

    logs = get_security_logs()

    return render_template(
        "security_logs.html",
        logs=logs
    )


#alarmas
@app.route("/api/alarms/active")
@login_required
def api_active_alarms():

    alarms = get_active_alarms()

    data = []

    for a in alarms:
        data.append({
            "id": a[0],
            "timestamp": a[1],
            "code": a[2],
            "severity": a[3],
            "message": a[4],
            "acknowledged": a[6],
            "acknowledged_at": a[7],
            "run_id": a[9]
        })

    return jsonify(data)


@app.route("/api/alarms/ack/<int:alarm_id>", methods=["POST"])
@login_required
@role_required(["operator", "admin"])
def api_ack_alarm(alarm_id):

    alarms = get_active_alarms()
    alarm = None

    for a in alarms:
        if a[0] == alarm_id:
            alarm = a
            break

    if alarm is None:
        return "Alarma no encontrada o no activa", 404

    acknowledged = acknowledge_alarm(alarm_id)

    if not acknowledged:
        return "No se pudo confirmar la alarma", 409

    log_alarm_event(
        alarm_id,
        alarm[2],
        "acknowledged",
        f"Acked by {current_user.username}",
        alarm[9]
    )

    log_security_event(
        current_user.username,
        f"ACK_ALARM:{alarm_id}",
        request.remote_addr
    )

    return "OK"


#alarmas como evento
@app.route("/api/alarm_events")
@login_required
@role_required(["operator", "admin"])
def api_alarm_events():

    events = get_alarm_events()

    data = []

    for e in events:
        data.append({
            "id": e[0],
            "timestamp": e[1],
            "alarm_id": e[2],
            "alarm_code": e[3],
            "event_type": e[4],
            "details": e[5]
        })

    return jsonify(data)


#pagina donde ver las alarmas y su historial
@app.route("/alarm_events")
@login_required
@role_required(["operator", "admin"])
def alarm_events_page():

    events = get_alarm_events(limit=500)

    return render_template(
        "alarm_events.html",
        events=events
    )


#pagina donde verlas
@app.route("/alarms")
@login_required
@role_required(["operator", "admin"])
def alarms_page():

    active = get_active_alarms()
    history = get_alarm_history()
    active_count = len(active)
    unacked_count = sum(1 for a in active if a[6] == 0)
    show_filter = request.args.get("filter", "all")

    if show_filter == "active":
        history = active

    severity_order = {"critical": 3, "warning": 2, "info": 1}
    filter_severity = "info"
    if active:
        filter_severity = max(
            (a[3] for a in active if a[3] in severity_order),
            key=lambda s: severity_order.get(s, 0),
            default="info"
        )

    return render_template(
        "alarms.html",
        active=active,
        history=history,
        active_count=active_count,
        unacked_count=unacked_count,
        show_filter=show_filter,
        filter_severity=filter_severity
    )

if __name__ == "__main__":

    init_db()
    validate_steps()

    socketio.run(
    app,
    host="0.0.0.0",
    port=5000,
    debug=False,
    use_reloader=False,
    allow_unsafe_werkzeug=True,
    ssl_context=(
        "certificados/cert.pem",
        "certificados/key.pem"
    )
)
