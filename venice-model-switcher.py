#!/usr/bin/env python3

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import NoReturn


SCRIPT_DIR = Path(__file__).resolve().parent
STATE_DIR = Path.home() / ".openclaw"
LOG_FILE = Path(
    os.environ.get("VENICE_SWITCH_LOG_FILE", str(STATE_DIR / "diem-switch.log"))
).expanduser()
STATE_FILE = Path(
    os.environ.get("VENICE_SWITCH_STATE_FILE", str(STATE_DIR / "diem-switch-state.json"))
).expanduser()
DEFAULT_BALANCE_SCRIPT = SCRIPT_DIR / "venice_balance.py"
DEFAULT_OPENCLAW_BIN = Path.home() / ".npm-global" / "bin" / "openclaw"
DEFAULT_ENV_FILE = SCRIPT_DIR / ".env"

MODELS = {
    "sonnet": "venice/claude-sonnet-4-6",
    "grok420": "venice/grok-4-20-beta",
    "gemini": "venice/gemini-3-flash-preview",
    "grok41fast": "venice/grok-41-fast",
}

MODEL_TIERS = [
    (0, "sonnet"),
    (35, "grok420"),
    (65, "gemini"),
    (90, "grok41fast"),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Switch the default OpenClaw Venice model based on DIEM usage, and "
            "optionally reconcile existing sessions."
        )
    )
    parser.add_argument(
        "--query-sessions",
        action="store_true",
        help=(
            "Always query and patch sessions even when the target tier did not change."
        ),
    )
    return parser.parse_args()

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


def decode_process_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def run_command(command: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "NO_COLOR": "1"},
        )
    except subprocess.TimeoutExpired as exc:
        stdout = (exc.stdout or "")[:300]
        stderr = (exc.stderr or "")[:300]
        fail(
            f"command timed out after {timeout}s: {' '.join(command)} | "
            f"stdout={stdout!r} stderr={stderr!r}"
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
    configured = os.environ.get("OPENCLAW_BIN")
    if configured:
        return configured
    discovered = shutil.which("openclaw")
    if discovered:
        return discovered
    return str(DEFAULT_OPENCLAW_BIN)


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


def load_state() -> dict[str, str | float | None]:
    if not STATE_FILE.exists():
        return {
            "last_default_tier": None,
            "last_session_patch_tier": None,
            "last_spent_pct": 0.0,
        }

    raw_state = json.loads(STATE_FILE.read_text())
    if not isinstance(raw_state, dict):
        fail(f"invalid state payload in {STATE_FILE}")

    current_tier = raw_state.get("current_tier")
    return {
        "last_default_tier": raw_state.get("last_default_tier", current_tier),
        "last_session_patch_tier": raw_state.get(
            "last_session_patch_tier", current_tier
        ),
        "last_spent_pct": float(raw_state.get("last_spent_pct", 0.0)),
    }


def save_state(default_tier: str | None, session_patch_tier: str | None, spent_pct: float) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(
            {
                "current_tier": default_tier,
                "last_default_tier": default_tier,
                "last_session_patch_tier": session_patch_tier,
                "last_spent_pct": spent_pct,
                "updated_at": datetime.now().isoformat(),
            },
            indent=2,
        )
    )


def gateway_call(method: str, params: dict, timeout_ms: int = 60000) -> dict:
    command = [
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
    ]
    timeout = max(120, int(timeout_ms / 1000) + 30)
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "NO_COLOR": "1"},
        )
        stdout = result.stdout
        stderr = result.stderr
    except subprocess.TimeoutExpired as exc:
        stdout = decode_process_output(exc.stdout)
        stderr = decode_process_output(exc.stderr)
        try:
            payload = parse_json_from_output(stdout)
        except ValueError:
            fail(
                f"command timed out after {timeout}s: {' '.join(command)} | "
                f"stdout={stdout[:300]!r} stderr={stderr[:300]!r}"
            )
        if not isinstance(payload, dict):
            fail(f"{method} did not return a JSON object")
        log.warning(
            "%s timed out after %ss but returned JSON; using partial output",
            method,
            timeout,
        )
        return payload

    if result.returncode != 0:
        fail(
            f"command failed ({result.returncode}): {' '.join(command)} | "
            f"stdout={stdout[:300]!r} stderr={stderr[:300]!r}"
        )

    payload = parse_json_from_output(stdout)
    if not isinstance(payload, dict):
        fail(f"{method} did not return a JSON object")
    return payload


def set_default_model(model: str) -> None:
    run_command([openclaw_bin(), "--no-color", "models", "set", model], timeout=120)


def list_sessions() -> list[dict]:
    payload = gateway_call("sessions.list", {}, timeout_ms=60000)
    sessions = payload.get("sessions")
    if not isinstance(sessions, list):
        fail("sessions.list did not return a sessions array")
    return sessions


def patch_session_model(key: str, model: str) -> dict:
    payload = gateway_call(
        "sessions.patch", {"key": key, "model": model}, timeout_ms=60000
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
        "kimi-k2-5",
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


def state_float(state: dict[str, str | float | None], key: str, default: float = 0.0) -> float:
    value = state.get(key)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        return float(value)
    return default


def state_tier(state: dict[str, str | float | None], key: str) -> str | None:
    value = state.get(key)
    if isinstance(value, str) and value.strip():
        return value
    return None


def main() -> int:
    args = parse_args()
    env_file = Path(os.environ.get("VENICE_SWITCH_ENV_FILE", str(DEFAULT_ENV_FILE)))
    load_env_file(env_file)

    spent_pct = get_diem_spent_pct()
    state = load_state()
    pinned_sessions = parse_pinned_sessions()
    target_tier = get_target_tier(spent_pct)

    last_spent_pct = state_float(state, "last_spent_pct")
    if detect_diem_reset(last_spent_pct, spent_pct):
        log.info(
            "DIEM reset detected (%.1f%% -> %.1f%%); forcing sonnet",
            last_spent_pct,
            spent_pct,
        )
        target_tier = "sonnet"

    target_model = MODELS[target_tier]
    log.info(
        "DIEM %.1f%% spent | default tier=%s | session tier=%s | target tier=%s | target model=%s",
        spent_pct,
        state_tier(state, "last_default_tier"),
        state_tier(state, "last_session_patch_tier"),
        target_tier,
        target_model,
    )

    current_default_tier = state_tier(state, "last_default_tier")
    current_session_patch_tier = state_tier(state, "last_session_patch_tier")

    if current_default_tier != target_tier:
        log.info("Switching default model to %s", target_model)
        set_default_model(target_model)
        current_default_tier = target_tier
    else:
        log.info("Target tier unchanged; skipping default model switch")

    should_query_sessions = (
        args.query_sessions or current_session_patch_tier != current_default_tier
    )
    if should_query_sessions:
        if args.query_sessions and current_session_patch_tier == current_default_tier:
            log.info("Forced session reconciliation enabled via --query-sessions")
        else:
            log.info(
                "Reconciling sessions for tier %s",
                current_default_tier,
            )
        try:
            sessions = list_sessions()
            patches = select_session_patches(sessions, target_tier, pinned_sessions)

            if not patches:
                log.info("No session patches needed")
            else:
                if current_default_tier is None:
                    fail("cannot reconcile sessions without a resolved default tier")
                target_for_patches = MODELS[current_default_tier]
                for key, current_model, desired_model in patches:
                    pinned_model = pinned_sessions.get(key)
                    if pinned_model:
                        desired_model = f"venice/{pinned_model}"
                    else:
                        desired_model = target_for_patches
                    log.info("Patching %s: %s -> %s", key, current_model, desired_model)
                    patch_session_model(key, desired_model)
        except SystemExit:
            save_state(current_default_tier, current_session_patch_tier, spent_pct)
            raise

        current_session_patch_tier = current_default_tier
    else:
        log.info("Target tier unchanged; skipping session query")

    save_state(current_default_tier, current_session_patch_tier, spent_pct)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
