# Details

**English** · [한국어](DETAILS.ko.md) · [← README](../README.md)

## Jobs and GPUs

- One row is one job. A training run spread over several GPUs (DDP, torchrun, mp.spawn, DataParallel) is one row such as `GPU0–3`, identified once. Two different jobs on one GPU are two rows. Idle GPUs are grouped (`GPU5–7 available`).
- Open a machine to pick a GPU. The detail (specs, job description, loss curve) and the charts at the top follow the picked GPU.
- **Re-identify** next to a job description asks the AI again — when it named the job wrong, or after you logged in to Claude. A job can be re-identified once a minute.

## AI job identification

| | |
|---|---|
| When | Once, after a process has held a GPU for 45 s. Answers are cached in `~/.cache/gpu-monitor/ai` |
| What is sent | The process's command line, working directory, open files and parent processes, plus the config and log excerpts Claude reads in that folder — through that server's Claude account |
| Cost | Usually 15–30 s, about $0.10–0.20 per job at API prices. With a subscription login it counts toward that account's usage |
| Turn off | `MONITOR_AI=off` in `monitor.env` (or `--no-ai` at install) |
| Use an API key | `MONITOR_AI_AUTH=api_key` and `ANTHROPIC_API_KEY=...` |
| After logging in | While Claude isn't ready (not installed, not logged in) the agent checks every 30 s, so identification starts within a minute of `claude auth login` |

It only reads.

- Tools are limited to Read, Glob, Grep and `ls` `ps` `readlink` `stat` `wc`. Anything else is denied without asking, even if the server's Claude Code settings skip permission checks.
- Paths likely to hold keys or tokens (`~/.ssh`, `~/.aws`, `~/.claude`, `.env`, `*.pem`, …) can't be read.
- No session history is saved, and the user's hooks and MCP servers stay off.
- If Claude Code is too old to enforce this, identification stays off and the dashboard asks for an update.

## Connections

The agent pushes to the hub every 2 s over plain HTTP, so a server behind NAT only needs to reach the hub, not the other way round.

| Network | How |
|---|---|
| Same LAN | The install line from `./install.sh hub` |
| Tailscale / VPN | Same line with the hub's VPN address. Your ACL must allow the hub port (8787) |
| SSH only | `./install.sh agent --ssh you@hub-host --token TOKEN` — needs SSH key login. A tunnel service forwards the hub port and reconnects by itself |
| Both LAN and VPN | The install line already carries every hub address; the agent sends to whichever answers and switches back and forth on its own |

`http_proxy` settings are ignored for the hub, and the install line uses `curl --noproxy '*'`.

## Platforms

| | |
|---|---|
| Linux | systemd user services; without systemd, cron restarts it every minute |
| WSL2 | Windows Task Scheduler starts the agent in a hidden window and checks it every 5 min while you're logged in (WSL stops when its windows close). A hub inside WSL2 is reachable from other machines only with mirrored networking (`networkingMode=mirrored`) |
| Windows | `iex (irm 'http://HUB:PORT/install.ps1?t=TOKEN')` in PowerShell installs the agent for the current user (no admin). Task Scheduler starts it without a window at logon and checks it every 5 min. Needs Python 3.7+ (`winget install -e --id Python.Python.3.12`). Name it first with `$env:GPU_MONITOR_LABEL = 'NAME'`. The hub itself runs on Linux or WSL |

Windows and WSL2 installs are tested on GitHub's Windows Server runners with a stand-in `nvidia-smi` (no real GPU there). AI identification on Windows hasn't been tested yet.

## Install options

| Option | |
|---|---|
| `--port 9000` | Hub port. Default 8787; if it's taken on first install, the next free port is used |
| `--lang ko` | Screen language (`en` default). Set on the hub, it carries over to every agent installed with its line. Viewers can switch with the button on the dashboard |
| `--ssh USER@HOST` | Reach the hub over an SSH tunnel (see Connections) |
| `--label NAME` | Server name on the dashboard (default: hostname). With the one-line install: `\| bash -s -- --label NAME` |
| `--no-agent` | Hub only, even if the hub server has a GPU |
| `--no-ai` | Turn off AI job identification |

## Managing

| Task | How |
|---|---|
| Logs | `journalctl --user -u gpu-monitor-agent -f` (hub: `gpu-monitor-hub`). Windows: `%LOCALAPPDATA%\gpu-monitor\agent.log` |
| Change settings | Edit `monitor.env`, then `systemctl --user restart gpu-monitor-agent`. Options: [`monitor.env.example`](../monitor.env.example) |
| Update | Hub: `git pull && ./install.sh hub`. GPU servers: run the install line again |
| Uninstall | `./install.sh uninstall`. One-line installs: `~/gpu-monitor/install.sh uninstall`. Windows: `powershell -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\gpu-monitor\install.ps1" -Uninstall` |

Without systemd, cron checks every minute and restarts the agent (WSL2 uses Windows Task Scheduler instead, see Platforms).
To keep it running after you log out, you may need `sudo loginctl enable-linger $USER` once (the installer tells you).

## Security

- Run the hub inside a trusted LAN or VPN. The dashboard and `/api/all` have no authentication.
- The token only keeps strangers from pushing data. It is part of the install line, so share that line within your team only.
- Process command lines and working directories never leave the GPU server (keys are sometimes passed as arguments).
- The re-identify button has no login either. The hub accepts only same-origin JSON requests and one per job per minute.
- The install line fetches a script over plain HTTP. Use it only on networks you trust.

## Files

| File | Role |
|---|---|
| `agent.py` | Runs on each GPU server. Finds GPU processes via `nvidia-smi` and `/proc`, identifies them with AI, pushes to the hub every 2 s |
| `hub.py` | Collects reports and streams them to the dashboard (SSE). Also serves the installer and agent files |
| `dashboard.html` | The dashboard |
| `install.sh`, `install.ps1` | Install, update, uninstall (Linux·WSL / Windows) |
| `run-agent.sh`, `run-hub.sh`, `run-tunnel.sh`, `watchdog.sh` | Launch scripts used by the services (only one copy of each runs) |
| `monitor.env.example` | All settings |
