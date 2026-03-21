# OpenClaw Venice Model Switcher

Small helper scripts for OpenClaw users on Venice who want to stretch the daily DIEM budget automatically.

Venice gives DIEM token holders about $1/day of inference. This repo checks how much of that daily allocation is already spent, then moves OpenClaw to cheaper or faster Venice models as the day goes on so you do not burn the budget too early.

## How it works

- `venice_balance.py` calls the Venice billing endpoint and reads `VENICE_ADMIN_KEY`.
- `venice-model-switcher.py` maps DIEM usage to a model tier, sets the default OpenClaw model, and patches active Venice sessions to the same target model.
- At the start of a fresh daily DIEM window it drops back to the cheapest default tier automatically.

## Model tiers

The default thresholds live in `venice-model-switcher.py`:

- `0%+` -> `venice/claude-sonnet-4-6`
- `35%+` -> `venice/grok-4-20-beta`
- `60%+` -> `venice/gemini-3-flash-preview`
- `90%+` -> `venice/grok-41-fast`

Edit `MODELS` and `MODEL_TIERS` if you want different tradeoffs.

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

## Environment variables

- `VENICE_ADMIN_KEY`: required Venice Admin API key.
- `OPENCLAW_BIN`: optional path to the OpenClaw binary.
- `VENICE_SWITCH_ENV_FILE`: optional env file path for `venice-model-switcher.py`.
- `VENICE_BALANCE_ENV_FILE`: optional env file path for `venice_balance.py`.
- `VENICE_BALANCE_SCRIPT`: optional path to `venice_balance.py` if you split the files up.
- `VENICE_SWITCH_PINNED_SESSIONS`: optional comma-separated session pins like `session-key=grok-41-fast`.

By default both scripts look for a `.env` file next to the script itself.

## Scheduling

Run the switcher every 5-10 minutes. That is enough for the default tiering.

### cron

```cron
*/10 * * * * /usr/bin/python3 /path/to/venice-model-switcher.py >> /tmp/venice-model-switcher.log 2>&1
```

### systemd

Example unit files are included in `systemd/venice-model-switcher.service` and `systemd/venice-model-switcher.timer`.

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
- Private deployment notes and local API notes are intentionally gitignored in this repo.
