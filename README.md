# OpenClaw Venice Model Switcher

Small helper scripts for OpenClaw users on Venice who want to stretch the daily DIEM budget automatically.

Venice gives DIEM token holders about $1/day of inference. This repo checks how much of that daily allocation is already spent, then moves OpenClaw to cheaper or faster Venice models as the day goes on so you do not burn the budget too early.

## How it works

- `venice_balance.py` calls the Venice billing endpoint and reads `VENICE_ADMIN_KEY`.
- `venice-model-switcher.py` maps DIEM usage to a model tier, sets the default OpenClaw model, and patches active Venice sessions to the same target model when needed.
- At the start of a fresh daily DIEM window it drops back to the cheapest default tier automatically.

## Model tiers

The default thresholds live in `venice-model-switcher.py`:

- `0%+` -> `venice/claude-sonnet-4-6`
- `35%+` -> `venice/grok-4-20-beta`
- `65%+` -> `venice/gemini-3-flash-preview`
- `90%+` -> `venice/grok-41-fast`
- `100%` -> `ollama/qwen3.5:397b-cloud` (configurable via `VENICE_SWITCH_EXHAUSTED_MODEL`)

When DIEM is fully exhausted (100%), the switcher moves to a non-Venice provider so the agent can still respond. The default exhausted model is `ollama/qwen3.5:397b-cloud` (Ollama Cloud). Set `VENICE_SWITCH_EXHAUSTED_MODEL` to use a different fallback provider/model.

Edit `MODELS` and `MODEL_TIERS` if you want different tradeoffs.

We intentionally do not use `venice/kimi-k2-5` in the default ladder right now because of upstream tool-calling reliability problems in OpenClaw: [openclaw/openclaw#40742](https://github.com/openclaw/openclaw/issues/40742).

## Setup

1. Make sure `python3` is installed.
2. Make sure `openclaw` is on your `PATH`, or set `OPENCLAW_BIN`.
3. Copy `.env.example` to `.env`.
4. Put your Venice admin key in `.env`:

```bash
VENICE_ADMIN_KEY=...
```

5. Run it manually once:

```bash
python3 venice-model-switcher.py
```

## Scripts

### `venice_balance.py`

Use this when you want the raw Venice DIEM balance without changing any OpenClaw settings.

Default output is compact JSON:

```bash
python3 venice_balance.py
```

Example fields:

- `used`
- `remaining`
- `total`
- `percentage_used`
- `usd_balance`

Print only the used percentage:

```bash
python3 venice_balance.py --percentage
```

Read the key from a different environment variable:

```bash
python3 venice_balance.py --key-env MY_VENICE_ADMIN_KEY
```

`venice-model-switcher.py` uses this script internally, but it is also handy for cron checks, dashboards, or debugging.

### `venice-model-switcher.py`

Default behavior is intentionally cheap on steady-state runs:

- it reads the DIEM percentage
- computes the target tier
- skips `sessions.list` when the target tier is unchanged and no session reconciliation is pending
- queries and patches sessions only when the tier changes, or when a previous patch attempt did not finish successfully

Force the old always-reconcile behavior:

```bash
python3 venice-model-switcher.py --query-sessions
```

This is useful if you want to re-apply pinned sessions or reconcile existing sessions on demand.

The switcher keeps a small state file so it remembers:

- the last default tier it applied
- the last session patch tier it completed
- the last DIEM percentage it saw

By default the state file is stored at `~/.openclaw/diem-switch-state.json` and the log file at `~/.openclaw/diem-switch.log`.

## Environment variables

- `VENICE_ADMIN_KEY`: required Venice Admin API key.
- `OPENCLAW_BIN`: optional path to the OpenClaw binary.
- `VENICE_SWITCH_ENV_FILE`: optional env file path for `venice-model-switcher.py`.
- `VENICE_BALANCE_ENV_FILE`: optional env file path for `venice_balance.py`.
- `VENICE_BALANCE_SCRIPT`: optional path to `venice_balance.py` if you split the files up.
- `VENICE_SWITCH_PINNED_SESSIONS`: optional comma-separated session pins like `session-key=grok-41-fast`.
- `VENICE_SWITCH_LOG_FILE`: optional path to the switcher log file.
- `VENICE_SWITCH_STATE_FILE`: optional path to the switcher state file.
- `VENICE_SWITCH_EXHAUSTED_MODEL`: optional model to use when DIEM is fully exhausted (default: `ollama/qwen3.5:397b-cloud`).

By default both scripts look for a `.env` file next to the script itself.

## Scheduling

Run the switcher every 5-10 minutes. That is enough for the default tiering.

### cron

```cron
*/10 * * * * /usr/bin/python3 /path/to/venice-model-switcher.py >> /tmp/venice-model-switcher.log 2>&1
```

### systemd

Example unit files are included in `systemd/venice-model-switcher.service` and `systemd/venice-model-switcher.timer`.

They are user units, so on unattended Linux hosts you may also want:

```bash
loginctl enable-linger "$USER"
```

Service:

```ini
[Unit]
Description=OpenClaw Venice model switcher

[Service]
Type=oneshot
WorkingDirectory=/path/to/repo
ExecStart=/usr/bin/python3 /path/to/repo/venice-model-switcher.py
```

Timer:

```ini
[Unit]
Description=Run Venice model switcher every 10 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=10min
Persistent=true

[Install]
WantedBy=timers.target
```

Enable it with:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/venice-model-switcher.service ~/.config/systemd/user/
cp systemd/venice-model-switcher.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now venice-model-switcher.timer
```

## Notes

- `VENICE_ADMIN_KEY` needs Admin scope because the billing endpoint requires it.
- Pinned session overrides are optional and should be configured via environment, not hard-coded into the repo.

## Related reading

- [Full OpenClaw setup write-up](https://juraj.bednar.io/en/blog-en/2026/03/21/openclaw-the-cypherpunk-ish-way/)
