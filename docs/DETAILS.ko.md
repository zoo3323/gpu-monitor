# 자세히

[English](DETAILS.md) · **한국어** · [← README](../README.ko.md)

## AI 작업 식별

| | |
|---|---|
| 언제 | GPU를 잡은 프로세스가 45초 넘게 살아 있으면 한 번 식별합니다. 답은 `~/.cache/gpu-monitor/ai`에 남습니다 |
| 무엇이 전송되나 | 그 프로세스의 명령줄·작업 디렉터리·열린 파일·부모 프로세스, 그리고 Claude가 읽은 그 폴더의 설정·로그 일부입니다. 그 서버의 Claude 계정으로 전송됩니다 |
| 비용 | 한 번에 보통 15~30초, API 요금으로 치면 $0.1~0.2입니다. 구독 로그인이면 그 계정 사용량에서 빠집니다 |
| 끄기 | `monitor.env`에 `MONITOR_AI=off` (설치할 때는 `--no-ai`) |
| API 키로 쓰기 | `MONITOR_AI_AUTH=api_key`와 `ANTHROPIC_API_KEY=...` |

AI는 읽기만 합니다.

- 쓸 수 있는 도구는 Read·Glob·Grep과 `ls` `ps` `readlink` `stat` `wc`뿐입니다. 그 밖의 것은 묻지 않고 거부합니다. 서버의 Claude Code 설정이 권한 확인을 끄도록 되어 있어도 마찬가지입니다.
- 열쇠·토큰이 있을 만한 경로(`~/.ssh`, `~/.aws`, `~/.claude`, `.env`, `*.pem` 등)는 읽지 못합니다.
- 대화 기록을 남기지 않고, 그 서버 사용자의 훅·MCP 서버를 끈 채로 돕니다.
- Claude Code가 오래돼 이 제한을 걸 수 없으면 식별하지 않고, 대시보드에 업데이트하라고 띄웁니다.

## 설치 옵션

| 옵션 | |
|---|---|
| `--port 9000` | hub 포트입니다. 기본은 8787이고, 처음 설치할 때 이미 쓰이고 있으면 다음 빈 포트를 씁니다 |
| `--lang ko` | 화면 언어입니다(기본 `en`). hub에 정하면 그 hub의 설치 줄로 붙는 에이전트에도 그대로 전해집니다. 보는 사람은 대시보드의 버튼으로 바꿀 수 있습니다 |
| `--label 이름` | 대시보드에 보일 서버 이름입니다(기본은 hostname). 한 줄 설치에서는 `\| bash -s -- --label 이름` |
| `--no-agent` | hub 서버에 GPU가 있어도 hub만 설치합니다 |
| `--no-ai` | AI 작업 식별을 끕니다 |

## 관리

| 하고 싶은 것 | 방법 |
|---|---|
| 로그 보기 | `journalctl --user -u gpu-monitor-agent -f` (hub는 `gpu-monitor-hub`) |
| 설정 바꾸기 | `monitor.env`를 고치고 `systemctl --user restart gpu-monitor-agent`. 항목: [`monitor.env.example`](../monitor.env.example) |
| 업데이트 | hub: `git pull && ./install.sh hub`. GPU 서버: 설치 줄을 다시 실행합니다 |
| 제거 | `./install.sh uninstall`. 한 줄로 설치한 서버는 `~/gpu-monitor/install.sh uninstall` |

systemd가 없는 서버(일부 WSL2)는 cron으로 1분마다 살아 있는지 확인하고 다시 띄웁니다.
로그아웃한 뒤에도 계속 돌게 하려면 `sudo loginctl enable-linger $USER`를 한 번 실행해야 할 수 있습니다(설치할 때 알려 드립니다).

## 보안

- hub는 믿을 수 있는 사내망이나 VPN 안에서만 여세요. 대시보드와 `/api/all`에는 인증이 없습니다.
- 토큰은 아무나 데이터를 밀어 넣지 못하게 막는 용도입니다. 설치 줄에 들어 있으니 팀 안에서만 나누세요.
- 프로세스 명령줄과 작업 디렉터리는 GPU 서버 밖으로 보내지 않습니다(키를 인자로 넘기는 경우가 있습니다).
- 설치 줄은 평문 HTTP로 스크립트를 받아 실행합니다. 믿을 수 있는 망에서만 쓰세요.

## 파일

| 파일 | 하는 일 |
|---|---|
| `agent.py` | GPU 서버마다 돕니다. `nvidia-smi`·`/proc`로 점유자를 찾고, AI로 식별하고, 2초마다 hub로 보냅니다 |
| `hub.py` | 보고를 받아 대시보드로 흘려보냅니다(SSE). 설치 스크립트와 에이전트 파일도 내줍니다 |
| `dashboard.html` | 대시보드 |
| `install.sh` | 설치·업데이트·제거 |
| `run-agent.sh`, `run-hub.sh`, `watchdog.sh` | 서비스가 부르는 실행 스크립트 |
| `monitor.env.example` | 설정 항목 전체 |
