#!/usr/bin/env python3
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


API_BASE = "https://api.venice.ai/api/v1"
DEFAULT_ENV_FILE = Path(__file__).resolve().with_name(".env")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Return Venice DIEM balance in a machine-parsable format."
    )
    parser.add_argument(
        "--percentage",
        action="store_true",
        help="Print only the used percentage as an integer, like 80.",
    )
    parser.add_argument(
        "--key-env",
        default="VENICE_ADMIN_KEY",
        help="Environment variable holding the API key to use (default: VENICE_ADMIN_KEY).",
    )
    return parser.parse_args()


def fail(message: str, exit_code: int = 1) -> "NoReturn":
    print(message, file=sys.stderr)
    raise SystemExit(exit_code)


def load_key(env_name: str) -> str:
    value = os.environ.get(env_name)
    if not value:
        fail(f"missing environment variable: {env_name}")
    return value


def load_env_file(path: Path) -> None:
    if not path.exists():
        return

    with path.open(encoding="utf-8") as handle:
        for raw_line in handle:
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


def fetch_balance(api_key: str) -> dict:
    request = urllib.request.Request(
        f"{API_BASE}/billing/balance",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "venice-balance/1.0",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {"error": body}

        error_text = payload.get("error") or f"HTTP {exc.code}"
        if exc.code == 401 and "Admin API key required" in error_text:
            fail("billing endpoint requires an Admin API key")
        fail(f"request failed: {error_text}")
    except urllib.error.URLError as exc:
        fail(f"network error: {exc.reason}")


def build_result(payload: dict) -> dict:
    balances = payload.get("balances") or {}
    remaining = balances.get("diem")
    total = payload.get("diemEpochAllocation")

    if remaining is None:
        fail("response does not include a DIEM balance")
    if total in (None, 0):
        fail("response does not include a usable DIEM total")

    used = total - remaining
    percentage_used = round((used / total) * 100)

    return {
        "used": used,
        "total": total,
        "remaining": remaining,
        "percentage_used": percentage_used,
        "can_consume": payload.get("canConsume"),
        "consumption_currency": payload.get("consumptionCurrency"),
        "usd_balance": balances.get("usd"),
    }


def main() -> int:
    args = parse_args()
    env_file = Path(
        os.environ.get("VENICE_BALANCE_ENV_FILE", str(DEFAULT_ENV_FILE))
    ).expanduser()
    load_env_file(env_file)
    api_key = load_key(args.key_env)
    payload = fetch_balance(api_key)
    result = build_result(payload)

    if args.percentage:
        print(result["percentage_used"])
    else:
        print(json.dumps(result, separators=(",", ":"), sort_keys=True))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
