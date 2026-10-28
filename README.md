<h1 align="center">GPU Monitor</h1>

<p align="center">
  <b>Which GPU servers are busy, and with what — on one screen.</b><br>
  Claude Code names each GPU job. Progress and ETA come straight from its logs.
</p>

<p align="center"><b>English</b> · <a href="README.ko.md">한국어</a></p>

<p align="center"><img src="docs/en/demo.gif" alt="Dashboard: live jobs, progress, ETA and charts across GPU servers" width="100%"></p>

## Install

**1. Hub** — one per team

```bash
git clone https://github.com/zoo3323/gpu-monitor.git && cd gpu-monitor
./install.sh hub
```

<sub>Port 8787 by default (a free one is picked if it's taken) · <code>--port 9000</code> · <code>--lang ko</code> for a Korean screen</sub>

**2. Each GPU server** — paste the line the hub prints

```bash
curl -fsSL "http://HUB:PORT/install.sh?t=TOKEN" | bash
```

**3. Job names by AI** — once per server

```bash
claude auth login
```

<p align="center"><img src="docs/en/install.gif" alt="./install.sh hub on the hub, then the one-line install on a GPU server" width="100%"></p>

## Screens

<table>
  <tr>
    <td width="70%"><img src="docs/en/light.png" alt="Light theme"></td>
    <td><img src="docs/en/mobile.png" alt="Phone, dark theme"></td>
  </tr>
  <tr>
    <td colspan="2"><img src="docs/en/detail.png" alt="Machine detail: what the job is, what runs next, loss curve"></td>
  </tr>
</table>

<sub>Linux + NVIDIA · Python 3.7+ · <a href="https://claude.com/claude-code">Claude Code</a> for job names (optional) · English / 한국어 · <a href="docs/DETAILS.md">Details</a> · MIT</sub>
