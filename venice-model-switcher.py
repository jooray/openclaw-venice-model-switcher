#!/usr/bin/env python3

import json
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
STATE_DIR = (
    Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    / "venice-model-switcher"
)
LOG_FILE = Path(
    os.environ.get("VENICE_SWITCH_LOG_FILE", str(STATE_DIR / "switcher.log"))
).expanduser()
STATE_FILE = Path(
    os.environ.get("VENICE_SWITCH_STATE_FILE", str(STATE_DIR / "state.json"))
).expanduser()
DEFAULT_BALANCE_SCRIPT = SCRIPT_DIR / "venice_balance.py"
DEFAULT_OPENCLAW_BIN = "openclaw"
DEFAULT_ENV_FILE = SCRIPT_DIR / ".env"

MODELS = {
    "sonnet": "venice/claude-sonnet-4-6",
    "grok420": "venice/grok-4-20-beta",
    "flash": "venice/gemini-3-flash-preview",
    "grok41fast": "venice/grok-41-fast",
}

MODEL_TIERS = [
    (0, "sonnet"),
    (35, "grok420"),
    (60, "flash"),
    (90, "grok41fast"),
]

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


def fail(message: str, exit_code: int = 1) -> "NoReturn":
    log.error(message)
    raise SystemExit(exit_code)


def load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def parse_json_from_output(stdout: str) -> object:
    for marker in ("{", "["):
        idx = stdout.find(marker)
        if idx >= 0:
            return json.loads(stdout[idx:])
    raise ValueError("no JSON payload found in stdout")


def run_command(command: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "NO_COLOR": "1"},
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        stdout = result.stdout.strip()
        fail(
            f"command failed ({result.returncode}): {' '.join(command)} | "
            f"stdout={stdout[:300]!r} stderr={stderr[:300]!r}"
        )
    return result


def openclaw_bin() -> str:
    return os.environ.get("OPENCLAW_BIN", str(DEFAULT_OPENCLAW_BIN))


def balance_script_path() -> str:
    return os.environ.get("VENICE_BALANCE_SCRIPT", str(DEFAULT_BALANCE_SCRIPT))


def parse_pinned_sessions() -> dict[str, str]:
    raw_value = os.environ.get("VENICE_SWITCH_PINNED_SESSIONS", "").strip()
    if not raw_value:
        return {}

    pinned: dict[str, str] = {}
    for item in raw_value.split(","):
        entry = item.strip()
        if not entry:
            continue
        if "=" not in entry:
            fail(
                "invalid VENICE_SWITCH_PINNED_SESSIONS entry: "
                f"{entry!r} (expected session-key=model-name)"
            )
        key, model = entry.split("=", 1)
        key = key.strip()
        model = model.strip().removeprefix("venice/")
        if not key or not model:
            fail(
                "invalid VENICE_SWITCH_PINNED_SESSIONS entry: "
                f"{entry!r} (expected session-key=model-name)"
            )
        pinned[key] = model

    return pinned


def get_diem_spent_pct() -> float:
    result = run_command(
        [sys.executable, balance_script_path(), "--percentage"], timeout=30
    )
    return float(result.stdout.strip())


def get_target_tier(spent_pct: float) -> str:
    target = MODEL_TIERS[0][1]
    for threshold, tier in MODEL_TIERS:
        if spent_pct >= threshold:
            target = tier
    return target


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"current_tier": None, "last_spent_pct": 0.0}
    return json.loads(STATE_FILE.read_text())


def save_state(tier: str, spent_pct: float) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(
            {
                "current_tier": tier,
                "last_spent_pct": spent_pct,
                "updated_at": datetime.now().isoformat(),
            },
            indent=2,
        )
    )


def gateway_call(method: str, params: dict, timeout_ms: int = 10000) -> object:
    result = run_command(
        [
            openclaw_bin(),
            "--no-color",
            "gateway",
            "call",
            method,
            "--json",
            "--timeout",
            str(timeout_ms),
            "--params",
            json.dumps(params, separators=(",", ":")),
        ],
        timeout=max(60, int(timeout_ms / 1000) + 10),
    )
    return parse_json_from_output(result.stdout)


def set_default_model(model: str) -> None:
    run_command([openclaw_bin(), "--no-color", "models", "set", model], timeout=60)


def list_sessions() -> list[dict]:
    payload = gateway_call("sessions.list", {}, timeout_ms=5000)
    sessions = payload.get("sessions")
    if not isinstance(sessions, list):
        fail("sessions.list did not return a sessions array")
    return sessions


def patch_session_model(key: str, model: str) -> dict:
    payload = gateway_call(
        "sessions.patch", {"key": key, "model": model}, timeout_ms=10000
    )
    if not payload.get("ok"):
        fail(f"sessions.patch failed for {key}")
    return payload


def normalize_model_name(session: dict) -> str:
    model = session.get("modelOverride") or session.get("model") or ""
    return str(model).strip()


def is_venice_session(session: dict) -> bool:
    provider = str(
        session.get("providerOverride") or session.get("modelProvider") or ""
    ).strip()
    if provider == "venice":
        return True
    model = normalize_model_name(session)
    return model.startswith("venice/") or model in {
        "claude-opus-4-6",
        "claude-sonnet-4-6",
        "grok-4-20-beta",
        "gemini-3-flash-preview",
        "grok-41-fast",
    }


def session_target_model(
    session: dict, tier_key: str, pinned_sessions: dict[str, str]
) -> str | None:
    key = str(session.get("key") or "")
    current_model = normalize_model_name(session)

    keep_model = pinned_sessions.get(key)
    if keep_model:
        if current_model.removeprefix("venice/") == keep_model:
            return None
        return f"venice/{keep_model}"

    if not is_venice_session(session):
        return None

    return MODELS[tier_key]


def select_session_patches(
    sessions: list[dict],
    tier_key: str,
    pinned_sessions: dict[str, str],
) -> list[tuple[str, str, str]]:
    patches: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    for session in sessions:
        key = str(session.get("key") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)

        target_model = session_target_model(session, tier_key, pinned_sessions)
        if not target_model:
            continue

        current_model = normalize_model_name(session)
        normalized_current = current_model.removeprefix("venice/")
        normalized_target = target_model.removeprefix("venice/")
        if normalized_current == normalized_target:
            continue

        patches.append((key, current_model or "(unset)", target_model))

    return patches


def detect_diem_reset(previous_spent_pct: float, spent_pct: float) -> bool:
    return previous_spent_pct > 30.0 and spent_pct < 10.0


def verify_openclaw() -> None:
    payload = gateway_call("sessions.list", {}, timeout_ms=5000)
    if not isinstance(payload.get("sessions"), list):
        fail("OpenClaw verification failed: sessions.list unavailable")


def main() -> int:
    env_file = Path(os.environ.get("VENICE_SWITCH_ENV_FILE", str(DEFAULT_ENV_FILE)))
    load_env_file(env_file)
    verify_openclaw()

    spent_pct = get_diem_spent_pct()
    state = load_state()
    pinned_sessions = parse_pinned_sessions()
    target_tier = get_target_tier(spent_pct)

    if detect_diem_reset(float(state.get("last_spent_pct", 0.0)), spent_pct):
        log.info(
            "DIEM reset detected (%.1f%% -> %.1f%%); forcing sonnet",
            float(state.get("last_spent_pct", 0.0)),
            spent_pct,
        )
        target_tier = "sonnet"

    target_model = MODELS[target_tier]
    log.info(
        "DIEM %.1f%% spent | current tier=%s | target tier=%s | target model=%s",
        spent_pct,
        state.get("current_tier"),
        target_tier,
        target_model,
    )

    set_default_model(target_model)
    sessions = list_sessions()
    patches = select_session_patches(sessions, target_tier, pinned_sessions)

    if not patches:
        log.info("No session patches needed")
    else:
        for key, current_model, desired_model in patches:
            log.info("Patching %s: %s -> %s", key, current_model, desired_model)
            patch_session_model(key, desired_model)

    save_state(target_tier, spent_pct)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
