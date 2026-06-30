HOUR = 3600

STEPS = [
    {
        "name": "PHASE1_STEP1_RAMP_20_140",
        "phase": "Phase 1",
        "step": 1,
        "mode": "ramp",
        "t_start": 20.0,
        "t_end": 140.0,
        "slope": 0.006,
        "anode_flow": {
            "air": 15.0
        },
        "cathode_flow": {
            "air": 15.0
        }
    },
    {
        "name": "PHASE1_STEP2_HOLD_140",
        "phase": "Phase 1",
        "step": 2,
        "mode": "hold",
        "t_set": 140.0,
        "duration_s": 14 * HOUR,
        "anode_flow": {
            "air": 15.0
        },
        "cathode_flow": {
            "air": 15.0
        }
    },
    {
        "name": "PHASE1_STEP3_RAMP_140_300",
        "phase": "Phase 1",
        "step": 3,
        "mode": "ramp",
        "t_start": 140.0,
        "t_end": 300.0,
        "slope": 0.005,
        "anode_flow": {
            "air": 15.0
        },
        "cathode_flow": {
            "air": 15.0
        }
    },
    {
        "name": "PHASE1_STEP4_HOLD_300",
        "phase": "Phase 1",
        "step": 4,
        "mode": "hold",
        "t_set": 300.0,
        "duration_s": 10 * HOUR,
        "anode_flow": {
            "air": 15.0
        },
        "cathode_flow": {
            "air": 15.0
        }
    },
    {
        "name": "PHASE1_STEP5_RAMP_300_520",
        "phase": "Phase 1",
        "step": 5,
        "mode": "ramp",
        "t_start": 300.0,
        "t_end": 520.0,
        "slope": 0.005,
        "anode_flow": {
            "nitrogen": 5.0
        },
        "cathode_flow": {
            "air": 5.0
        }
    },
    {
        "name": "PHASE2_STEP1_RAMP_520_550",
        "phase": "Phase 2",
        "step": 1,
        "mode": "ramp",
        "t_start": 520.0,
        "t_end": 550.0,
        "slope": 0.0085,
        "anode_flow": {
            "nitrogen": 4.0,
            "hydrogen": 1.0
        },
        "cathode_flow": {
            "air": 5.0,
            "co2": 1.0
        }
    },
    {
        "name": "PHASE2_STEP2_RAMP_550_575",
        "phase": "Phase 2",
        "step": 2,
        "mode": "ramp",
        "t_start": 550.0,
        "t_end": 575.0,
        "slope": 0.0085,
        "anode_flow": {
            "nitrogen": 3.0,
            "hydrogen": 2.0
        },
        "cathode_flow": {
            "air": 8.0,
            "co2": 2.0
        }
    },
    {
        "name": "PHASE2_STEP3_RAMP_575_600",
        "phase": "Phase 2",
        "step": 3,
        "mode": "ramp",
        "t_start": 575.0,
        "t_end": 600.0,
        "slope": 0.0085,
        "anode_flow": {
            "nitrogen": 2.0,
            "hydrogen": 3.0
        },
        "cathode_flow": {
            "air": 10.0,
            "co2": 4.0
        }
    },
    {
        "name": "PHASE2_STEP4_RAMP_600_650",
        "phase": "Phase 2",
        "step": 4,
        "mode": "ramp",
        "t_start": 600.0,
        "t_end": 650.0,
        "slope": 0.0085,
        "anode_flow": {
            "nitrogen": 1.0,
            "hydrogen": 5.0
        },
        "cathode_flow": {
            "air": 15.0,
            "co2": 6.6
        }
    },
    {
        "name": "NOMINAL_OPERATION_650",
        "phase": "Nominal operation",
        "step": 1,
        "mode": "hold",
        "t_set": 650.0,
        "duration_s": None,
        "anode_flow": {
            "nitrogen_hydrogen": 6.6
        },
        "cathode_flow": {
            "air": 15.0,
            "co2": 6.6
        }
    }
]


def validate_steps(steps=STEPS):
    errors = []

    for index, step in enumerate(steps):
        label = step.get("name") or f"step {index + 1}"
        mode = step.get("mode")

        for field in ("name", "phase", "step", "mode", "anode_flow", "cathode_flow"):
            if field not in step:
                errors.append(f"{label}: missing {field}")

        if mode == "ramp":
            for field in ("t_start", "t_end", "slope"):
                if field not in step:
                    errors.append(f"{label}: missing {field}")

            if step.get("slope", 0) <= 0:
                errors.append(f"{label}: slope must be greater than 0")

            if step.get("t_end", 0) < step.get("t_start", 0):
                errors.append(f"{label}: t_end must be greater than or equal to t_start")

        elif mode == "hold":
            if "t_set" not in step:
                errors.append(f"{label}: missing t_set")

            if "duration_s" not in step:
                errors.append(f"{label}: missing duration_s")

            if step.get("duration_s") is not None and step.get("duration_s", 0) <= 0:
                errors.append(f"{label}: duration_s must be greater than 0 or None")

        else:
            errors.append(f"{label}: invalid mode {mode}")

        for flow_name in ("anode_flow", "cathode_flow"):
            flow = step.get(flow_name, {})
            if not isinstance(flow, dict) or not flow:
                errors.append(f"{label}: {flow_name} must contain at least one gas")
                continue

            for gas, value in flow.items():
                if value < 0:
                    errors.append(f"{label}: {flow_name}.{gas} must not be negative")

    if errors:
        raise ValueError("Invalid startup procedure:\n" + "\n".join(errors))


def step_temperature_target(step):
    if step["mode"] == "hold":
        return step["t_set"]

    return step["t_end"]


def format_flow(flow):
    return ", ".join(
        f"{gas}: {value:g} NL/h"
        for gas, value in flow.items()
    )
