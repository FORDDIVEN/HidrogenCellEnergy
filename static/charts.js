const socket = io(
    window.location.origin
);

socket.on("connect", () => {
    console.log("WebSocket conectado");
});

socket.on("disconnect", () => {
    console.log("WebSocket desconectado");
});

let paused = false;
let currentStatus = "STOPPED";

function csrfHeaders() {
    return {
        "X-CSRFToken": window.CSRF_TOKEN || ""
    };
}

const ctx = document.getElementById('tempChart');

const chart = new Chart(ctx, {

    type: 'line',

    data: {
        labels: [],
        datasets: [
            {
                label: 'Termocupla 1',
                data: [],
                borderWidth: 2,
                borderColor: '#5fd3b3',
                backgroundColor: 'rgba(95, 211, 179, 0.12)',
                pointRadius: 0,
                tension: 0.25
            },
            {
                label: 'Termocupla 2',
                data: [],
                borderWidth: 2,
                borderColor: '#f3c74f',
                backgroundColor: 'rgba(243, 199, 79, 0.12)',
                pointRadius: 0,
                tension: 0.25
            },
            {
                label: 'SetPoint',
                data: [],
                borderWidth: 2,
                borderColor: '#ff0000',
                backgroundColor: 'rgba(255, 115, 115, 0.12)',
                pointRadius: 0,
                tension: 0.25
            }
        ]
    },

    options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        plugins: {
            legend: {
                labels: {
                    color: '#d8e3e7',
                    boxWidth: 12
                }
            }
        },
        scales: {
            x: {
                ticks: {
                    color: '#9aa8ad',
                    maxRotation: 0
                },
                grid: {
                    color: 'rgba(255, 255, 255, 0.06)'
                }
            },
            y: {
                ticks: {
                    color: '#9aa8ad'
                },
                grid: {
                    color: 'rgba(255, 255, 255, 0.08)'
                }
            }
        }
    }
});

socket.on("sensor_update", function(data) {

    console.log("DATA:", data);

    document.getElementById("tc1").innerText =
        data.tc1.toFixed(1) + " °C";

    document.getElementById("tc2").innerText =
        data.tc2.toFixed(1) + " °C";

    document.getElementById("sp").innerText =
        data.sp.toFixed(1) + " °C";

    document.getElementById("rl1").innerText =
        data.rl1 ? "ON" : "OFF";

    document.getElementById("rl2").innerText =
        data.rl2 ? "ON" : "OFF";

    renderRelayState("rl1Card", data.rl1);
    renderRelayState("rl2Card", data.rl2);

    const stepName = document.getElementById("step");

    if (stepName) {
        stepName.innerText = displayStepName(data.step);
    }

    renderRun(data.run_id);

    if (data.procedure_step) {
        renderProcedureStep(data.procedure_step);
    }

    chart.data.labels.push(data.timestamp);

    chart.data.datasets[0].data.push(data.tc1);
    chart.data.datasets[1].data.push(data.tc2);
    chart.data.datasets[2].data.push(data.sp);

    if (chart.data.labels.length > 50) {

        chart.data.labels.shift();

        chart.data.datasets.forEach(ds => ds.data.shift());
    }

    chart.update();
});

function resetChart() {

    chart.data.labels = [];

    chart.data.datasets.forEach(ds => {
        ds.data = [];
    });

    chart.update();
}

socket.on("reset_chart", () => {

    resetChart();

});

socket.on("system_status", (data) => {
    currentStatus = data.status || currentStatus;

    renderStatus(data.status);

    if (data.procedure_step) {
        renderProcedureStep(data.procedure_step);
    }

    renderRun(data.current_run_id);
    renderLoggingInterval(data.logging_interval_text);
    renderConnectionMode(data.modbus_config);
    renderControls(currentStatus);

});

function formatTime(seconds) {

    if (seconds === null || seconds === undefined) {
        return "indefinido";
    }

    seconds = Math.max(0, Math.floor(seconds));

    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const secs = seconds % 60;

    return `${hours}h ${minutes}m ${secs}s`;
}

function displayStepMode(mode) {
    const normalized = String(mode || "").toLowerCase();

    if (normalized === "ramp") {
        return "Rampa";
    }

    if (normalized === "hold") {
        return "Mantener";
    }

    return mode || "---";
}

function displayPhase(phase) {
    return String(phase || "---")
        .replace(/\bPhase\b/g, "Fase")
        .replace(/\bNominal operation\b/g, "Operación nominal");
}

function displayStepName(name) {
    return String(name || "---")
        .replace(/\bPHASE\b/g, "FASE")
        .replace(/\bSTEP\b/g, "PASO");
}

function renderProcedureStep(step) {

    const phase = document.getElementById("phase");
    const mode = document.getElementById("stepMode");
    const remaining = document.getElementById("remainingTime");
    const flows = document.getElementById("flows");
    const stepLabel = document.getElementById("step");

    if (phase) {
        phase.innerText = `${displayPhase(step.phase)} / Paso ${step.step}`;
    }

    if (mode) {
        mode.innerText = displayStepMode(step.mode);
    }

    if (remaining) {
        remaining.innerText = formatTime(step.remaining_s);
    }

    if (flows) {
        flows.innerText = `Ánodo: ${step.anode_flow_text} | Cátodo: ${step.cathode_flow_text}`;
    }

    if (stepLabel) {
        stepLabel.innerText = displayStepName(step.name);
    }
}

function loadSystemStatus() {

    fetch("/api/system/status")
    .then(r => r.json())
    .then(data => {
        currentStatus = data.status || currentStatus;

        renderStatus(data.status);

        renderRun(data.current_run_id);
        renderLoggingInterval(data.logging_interval_text);
        renderConnectionMode(data.modbus_config);

        if (data.procedure_step) {
            renderProcedureStep(data.procedure_step);
        }

        paused = data.status === "PAUSED";

        const pauseBtn = document.getElementById("pauseBtn");

        if (pauseBtn) {
            pauseBtn.innerText = paused ? "Resume" : "Pause";
        }

        renderControls(currentStatus);
    });
}

function renderStatus(status) {

    const card = document.getElementById("systemStatus");
    const text = document.getElementById("systemStatusText");

    if (!card || !text) {
        return;
    }

    const normalized = String(status || "STOPPED").toUpperCase();

    card.classList.remove(
        "status-running",
        "status-paused",
        "status-stopped",
        "status-error"
    );

    if (normalized === "RUNNING" || normalized === "STARTING") {
        card.classList.add("status-running");
    } else if (normalized === "PAUSED") {
        card.classList.add("status-paused");
    } else if (
        normalized.includes("ERROR")
        || normalized.includes("DISCONNECTED")
        || normalized.includes("INVALID")
    ) {
        card.classList.add("status-error");
    } else {
        card.classList.add("status-stopped");
    }

    text.innerText = `STATUS: ${normalized}`;
}

function renderRelayState(cardId, isOn) {

    const card = document.getElementById(cardId);

    if (!card) {
        return;
    }

    card.classList.toggle("relay-on", Boolean(isOn));
}

function renderControls(status) {

    const startBtn = document.getElementById("startBtn");
    const stopBtn = document.getElementById("stopBtn");
    const pauseBtn = document.getElementById("pauseBtn");
    const nextStepBtn = document.getElementById("nextStepBtn");
    const isRunning = status === "RUNNING";
    const isPaused = status === "PAUSED";
    const isStopped = status === "STOPPED";

    if (startBtn) {
        startBtn.disabled = !isStopped;
    }

    if (stopBtn) {
        stopBtn.disabled = isStopped;
    }

    if (pauseBtn) {
        pauseBtn.disabled = !(isRunning || isPaused);
        pauseBtn.innerText = isPaused ? "Continuar" : "Pausar";
    }

    if (nextStepBtn) {
        nextStepBtn.disabled = !(isRunning || isPaused);
    }
}

function renderRun(runId) {

    const run = document.getElementById("runId");

    if (!run) {
        return;
    }

    run.innerText = runId ? `#${runId}` : "---";
}

function renderLoggingInterval(intervalText) {

    const input = document.getElementById("loggingInterval");
    const status = document.getElementById("loggingIntervalStatus");

    if (!intervalText) {
        return;
    }

    if (input && document.activeElement !== input) {
        input.value = intervalText;
    }

    if (status) {
        status.innerText = `Intervalo actual: ${intervalText}`;
    }
}

function renderConnectionMode(config) {

    const target = document.getElementById("connectionMode");

    if (!target || !config) {
        return;
    }

    const mode = config.simulation_mode
        ? "Simulacion"
        : `${config.modbus_ip}:${config.modbus_port} / ID ${config.modbus_slave_id}`;

    target.innerText = `${config.mode_label || "Sin etiqueta"} - ${mode}`;
}

function loadLoggingInterval() {

    fetch("/api/logging_interval")
    .then(r => r.json())
    .then(data => {
        renderLoggingInterval(data.text);
    });
}

function saveLoggingInterval() {

    const input = document.getElementById("loggingInterval");
    const status = document.getElementById("loggingIntervalStatus");

    if (!input) {
        return;
    }

    fetch("/api/logging_interval", {
        method: "POST",
        headers: csrfHeaders(),
        body: new URLSearchParams({
            interval: input.value
        })
    })
    .then(res => {
        if (!res.ok) {
            return res.text().then(text => Promise.reject(text));
        }

        return res.json();
    })
    .then(data => {
        renderLoggingInterval(data.text);
    })
    .catch(err => {
        if (status) {
            status.innerText = err;
        } else {
            alert(err);
        }
    });
}

// Alarm handling
function severityClass(severity) {
    switch ((severity || '').toLowerCase()) {
        case 'critical':
            return 'alert-danger';
        case 'warning':
            return 'alert-warning';
        default:
            return 'alert-info';
    }
}

function highestSeverity(list) {
    const order = { critical: 3, warning: 2, info: 1 };
    return list.reduce((top, item) => {
        const score = order[item.severity?.toLowerCase()] || 0;
        const topScore = order[top.severity?.toLowerCase()] || 0;
        return score > topScore ? item : top;
    }, list[0]);
}

function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, char => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;'
    })[char]);
}

function hiddenAlarmIds() {
    try {
        return new Set(
            JSON.parse(localStorage.getItem("hiddenAlarmIds") || "[]")
            .map(id => Number(id))
        );
    } catch (error) {
        return new Set();
    }
}

function saveHiddenAlarmIds(ids) {
    localStorage.setItem(
        "hiddenAlarmIds",
        JSON.stringify([...ids])
    );
}

function hideAcknowledgedAlarms() {
    const hidden = hiddenAlarmIds();

    currentAlarms
        .filter(alarm => alarm.acknowledged)
        .forEach(alarm => hidden.add(Number(alarm.id)));

    saveHiddenAlarmIds(hidden);
    renderAlarms(currentAlarms);
}

function showHiddenAlarms() {
    localStorage.removeItem("hiddenAlarmIds");
    renderAlarms(currentAlarms);
}

let currentAlarms = [];

function alarmPageLink(extraClass = "btn-outline-dark") {
    if (!window.CAN_VIEW_ALARMS) {
        return "";
    }

    return `<a href="/alarms" class="btn btn-sm ${extraClass}">Alarmas</a>`;
}

function renderAlarms(list) {

    const container = document.getElementById('alarmsBanner');

    if (!container) return;

    currentAlarms = list || [];

    if (!list || list.length === 0) {
        container.innerHTML = `
            <div class="alert alert-success">
                <div class="d-flex justify-content-between align-items-center flex-wrap gap-2">
                    <div>No hay alarmas activas.</div>
                    ${alarmPageLink()}
                </div>
            </div>
        `;
        return;
    }

    const hidden = hiddenAlarmIds();
    const visible = list.filter(a => !hidden.has(Number(a.id)));
    const hiddenCount = list.length - visible.length;
    const ackedVisible = visible.filter(a => a.acknowledged).length;

    if (visible.length === 0) {
        container.innerHTML = `
            <div class="alert alert-secondary">
                <div class="d-flex justify-content-between align-items-center flex-wrap gap-2">
                    <div>Alarmas activas ocultas en esta vista: ${hiddenCount}</div>
                    <div class="d-flex flex-wrap gap-2 align-items-center">
                        ${alarmPageLink()}
                        <button class="btn btn-sm btn-light" onclick="showHiddenAlarms()">Mostrar</button>
                    </div>
                </div>
            </div>
        `;
        return;
    }

    const topAlarm = highestSeverity(visible);
    const count = visible.length;
    const unacked = visible.filter(a => !a.acknowledged).length;
    const alertClass = severityClass(topAlarm.severity);

    let html = `<div class="alert ${alertClass}">`;
    html += `<div class="d-flex justify-content-between align-items-center flex-wrap gap-2 mb-2">`;
    html += `<div><strong>${count} alarma${count === 1 ? '' : 's'} activas</strong> - ${unacked} no ACK</div>`;
    html += `<div class="d-flex flex-wrap gap-2 align-items-center">`;
    html += `<span class="badge bg-dark">Severidad: ${escapeHtml(topAlarm.severity)}</span>`;
    html += alarmPageLink("btn-light");

    if (ackedVisible > 0) {
        html += `<button class="btn btn-sm btn-light" onclick="hideAcknowledgedAlarms()">Ocultar</button>`;
    }

    if (hiddenCount > 0) {
        html += `<button class="btn btn-sm btn-outline-light" onclick="showHiddenAlarms()">Mostrar ocultas (${hiddenCount})</button>`;
    }

    html += `</div>`;
    html += `</div>`;

    visible.forEach(a => {
        const alarmId = Number(a.id);
        const ackButton = Number.isInteger(alarmId)
            ? `<button class="btn btn-sm btn-light ms-2" onclick="ack(${alarmId})">ACK</button>`
            : '';

        html += `<div class="d-flex justify-content-between align-items-center mb-2">`;
        html += `<div><strong>${escapeHtml(a.code)}</strong> - ${escapeHtml(a.message)} <small class="text-muted">${escapeHtml(a.timestamp)}</small></div>`;
        if (!a.acknowledged) {
            html += ackButton;
        } else {
            html += `<span class="badge bg-success">ACK</span>`;
        }
        html += `</div>`;
    });

    html += '</div>';

    container.innerHTML = html;
}

function ack(id) {
    if (!confirm('¿Confirmar ACK de esta alarma?')) {
        return;
    }

    fetch(`/api/alarms/ack/${id}`, {
        method: 'POST',
        headers: csrfHeaders()
    })
    .then(res => {
        if (res.ok) {
            loadAlarms();
        }
    })
}

function loadAlarms() {
    fetch('/api/alarms/active')
    .then(r => r.json())
    .then(data => {
        renderAlarms(data);
    })
}

socket.on('alarm_update', (payload) => {
    console.log('Alarm update', payload);
    // reload active alarms
    loadAlarms();
});

// initial load
loadAlarms();
loadSystemStatus();
loadLoggingInterval();

function controlSystem(url) {

    if (url === "/start" && currentStatus !== "STOPPED") {
        alert("El sistema ya esta en ejecucion o no está listo para iniciar.");
        return;
    }

    if (url === "/stop" && currentStatus === "STOPPED") {
        alert("El sistema ya esta detenido.");
        return;
    }

    if (url === "/start" && !confirm("Iniciar nueva ejecución")) {
        return;
    }

    const options = {
        method: "POST",
        headers: csrfHeaders()
    };

    if (url === "/stop") {
        const reason = prompt("Motivo de detención", "STOP_SYSTEM");

        if (reason === null) {
            return;
        }

        options.body = new URLSearchParams({
            reason: reason || "STOP_SYSTEM"
        });
    }

    fetch(url, {
        method: options.method,
        headers: options.headers,
        body: options.body
    })
    .then(res => {
        if (!res.ok) {
            return res.text().then(text => Promise.reject(text));
        }

        return res.text();
    })
    .then(data => {
        console.log(data);
        loadSystemStatus();
    })
    .catch(err => alert(err));
}

function togglePause() {

    if (!(currentStatus === "RUNNING" || currentStatus === "PAUSED")) {
        alert("Solo se puede pausar o reanudar con el sistema corriendo.");
        return;
    }

    fetch("/pause", {
        method: "POST",
        headers: csrfHeaders()
    })
    .then(res => {
        if (!res.ok) {
            return res.text().then(text => Promise.reject(text));
        }

        return res.text();
    })
    .then(data => {

        paused = !paused;

        document.getElementById(
            "pauseBtn"
        ).innerText =
            paused ? "Continuar" : "Pausar";

        console.log(data);
        loadSystemStatus();
    })
    .catch(err => alert(err));
}

function nextProcedureStep() {

    if (!confirm("¿Saltar al siguiente paso del procedimiento?")) {
        return;
    }

    fetch("/api/procedure/next", {
        method: "POST",
        headers: csrfHeaders()
    })
    .then(res => {
        if (!res.ok) {
            return res.text().then(text => Promise.reject(text));
        }

        return res.text();
    })
    .then(data => {
        console.log(data);
        loadSystemStatus();
    })
    .catch(err => alert(err));
}
