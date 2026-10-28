<h1 align="center">GPU Monitor</h1>

<p align="center">
  <b>Which GPU servers are busy, and with what — on one screen.</b><br>
  Claude Code names each GPU job. Progress and ETA come straight from its logs.
</p>

<p align="center"><b>English</b> · <a href="README.ko.md">한국어</a></p>

<p align="center"><img src="docs/demo.gif" alt="Dashboard: live jobs, progress, ETA and charts across GPU servers" width="100%"></p>

## Install

**1. Hub** — one per team

```bash
git clone https://github.com/zoo3323/gpu-monitor.git && cd gpu-monitor
./install.sh hub
```

**2. Each GPU server** — paste the line the hub prints

```bash
curl -fsSL "http://HUB:8787/install.sh?t=TOKEN" | bash
```

**3. Job names by AI** — once per server

```bash
claude auth login
```

<p align="center"><img src="docs/install.gif" alt="./install.sh hub on the hub, then the one-line install on a GPU server" width="100%"></p>

## Screens

<table>
  <tr>
    <td width="70%"><img src="docs/dark.png" alt="Dark theme"></td>
    <td><img src="docs/mobile.png" alt="Phone"></td>
  </tr>
  <tr>
    <td colspan="2"><img src="docs/detail.png" alt="Machine detail: what the job is, what runs next, loss curve"></td>
  </tr>
</table>

<sub>Linux + NVIDIA · Python 3.7+ · <a href="https://claude.com/claude-code">Claude Code</a> for job names (optional) · UI in Korean · <a href="docs/DETAILS.md">Details</a> · MIT</sub>
