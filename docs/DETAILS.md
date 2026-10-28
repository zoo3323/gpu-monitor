# Details

**English** · [한국어](DETAILS.ko.md) · [← README](../README.md)

## AI job identification

| | |
|---|---|
| When | Once, after a process has held a GPU for 45 s. Answers are cached in `~/.cache/gpu-monitor/ai` |
| What is sent | The process's command line, working directory, open files and parent processes, plus the config and log excerpts Claude reads in that folder — through that server's Claude account |
| Cost | Usually 15–30 s, about $0.10–0.20 per job at API prices. With a subscription login it counts toward that account's usage |
| Turn off | `MONITOR_AI=off` in `monitor.env` (or `--no-ai` at install) |
| Use an API key | `MONITOR_AI_AUTH=api_key` and `ANTHROPIC_API_KEY=...` |

It only reads.

- Tools are limited to Read, Glob, Grep and `ls` `ps` `readlink` `stat` `wc`. Anything else is denied without asking, even if the server's Claude Code settings skip permission checks.
- Paths likely to hold keys or tokens (`~/.ssh`, `~/.aws`, `~/.claude`, `.env`, `*.pem`, …) can't be read.
- No session history is saved, and the user's hooks and MCP servers stay off.
- If Claude Code is too old to enforce this, identification stays off and the dashboard asks for an update.

## Install options

| Option | |
|---|---|
| `--port 9000` | Hub port. Default 8787; if it's taken on first install, the next free port is used |
| `--lang ko` | Screen language (`en` default). Set on the hub, it carries over to every agent installed with its line. Viewers can switch with the button on the dashboard |
| `--label NAME` | Server name on the dashboard (default: hostname). With the one-line install: `\| bash -s -- --label NAME` |
| `--no-agent` | Hub only, even if the hub server has a GPU |
| `--no-ai` | Turn off AI job identification |

## Managing

| Task | How |
|---|---|
| Logs | `journalctl --user -u gpu-monitor-agent -f` (hub: `gpu-monitor-hub`) |
| Change settings | Edit `monitor.env`, then `systemctl --user restart gpu-monitor-agent`. Options: [`monitor.env.example`](../monitor.env.example) |
| Update | Hub: `git pull && ./install.sh hub`. GPU servers: run the install line again |
| Uninstall | `./install.sh uninstall`. One-line installs: `~/gpu-monitor/install.sh uninstall` |

Without systemd (some WSL2 setups), cron checks every minute and restarts the agent.
To keep it running after you log out, you may need `sudo loginctl enable-linger $USER` once (the installer tells you).

## Security

- Run the hub inside a trusted LAN or VPN. The dashboard and `/api/all` have no authentication.
- The token only keeps strangers from pushing data. It is part of the install line, so share that line within your team only.
- Process command lines and working directories never leave the GPU server (keys are sometimes passed as arguments).
- The install line fetches a script over plain HTTP. Use it only on networks you trust.

## Files

| File | Role |
|---|---|
| `agent.py` | Runs on each GPU server. Finds GPU processes via `nvidia-smi` and `/proc`, identifies them with AI, pushes to the hub every 2 s |
| `hub.py` | Collects reports and streams them to the dashboard (SSE). Also serves the installer and agent files |
| `dashboard.html` | The dashboard |
| `install.sh` | Install, update, uninstall |
| `run-agent.sh`, `run-hub.sh`, `watchdog.sh` | Launch scripts used by the services |
| `monitor.env.example` | All settings |
