# GPU 모니터

여러 GPU 서버가 **지금 GPU 를 쓰고 있는지, 무엇에 쓰고 있는지**를 한 화면에서 본다.

각 서버의 에이전트가 GPU 를 잡은 프로세스를 찾고, Claude Code 에게 그게 무슨 작업인지 묻는다.
그래서 화면에는 `python train.py` 가 아니라 `ResNet50 사전학습 · 42% · 15:30 종료` 처럼 보인다.

- **에이전트**: GPU 서버마다 하나. 파이썬 표준 라이브러리만 써서 서버의 파이썬 환경을 건드리지 않는다.
- **hub**: 에이전트 보고를 모아 대시보드로 띄운다. 데이터베이스 없이 최근 10분만 메모리에 둔다.
- **AI 식별**: GPU 점유자가 바뀔 때 한 번만 묻는다. 진행률·남은 시간은 그 답이 가리킨 로그를
  에이전트가 2초마다 직접 읽어 계산한다.

## 요구 사항

- Linux(WSL2 포함)와 NVIDIA 드라이버(`nvidia-smi`)
- Python 3.7 이상
- AI 식별을 쓰려면 GPU 서버마다 [Claude Code](https://claude.com/claude-code) 설치와 로그인

## 설치

### 1. hub 서버 (팀에 하나)

```bash
git clone https://github.com/zoo3323/gpu-monitor.git
cd gpu-monitor
./install.sh hub
```

대시보드 주소와, 다른 서버에 붙여 넣을 한 줄이 나온다. 이 서버에 GPU 가 있으면 에이전트도 같이 깔린다.

### 2. GPU 서버마다

hub 가 알려 준 한 줄을 그대로 실행한다.

```bash
curl -fsSL "http://HUB:8787/install.sh?t=TOKEN" | bash
```

화면에 보일 이름을 정하려면 끝을 `| bash -s -- --label 이름` 으로 바꾼다(기본은 hostname).
같은 줄을 다시 실행하면 업데이트된다.

### 3. AI 식별 켜기 (서버마다 한 번)

```bash
curl -fsSL https://claude.ai/install.sh | bash   # Claude Code 가 없으면
claude auth login
```

로그인하면 몇 분 안에 저절로 켜진다. 로그인이 만료되는 등 문제가 생기면 대시보드 위쪽 알림 칸에
이유가 뜬다.

## AI 식별

| | |
|---|---|
| 언제 | GPU 를 잡은 프로세스가 45초 넘게 살아 있으면 한 번. 답은 `~/.cache/gpu-monitor/ai` 에 남는다 |
| 무엇이 전송되나 | 그 프로세스의 명령줄·작업 디렉터리·열린 파일·부모 프로세스, 그리고 Claude 가 읽은 그 폴더의 설정·로그 일부. 그 서버의 Claude 계정으로 간다 |
| 비용 | 한 번에 보통 15~30초, API 요금으로 치면 $0.1~0.2. 구독 로그인이면 그 계정 사용량에서 빠진다 |
| 끄기 | `monitor.env` 에 `MONITOR_AI=off` (설치 때는 `--no-ai`) |
| API 키로 쓰기 | `MONITOR_AI_AUTH=api_key` 와 `ANTHROPIC_API_KEY=...` |

AI 는 읽기만 한다.

- 쓸 수 있는 도구는 Read·Glob·Grep 과 `ls`·`ps`·`readlink`·`stat`·`wc` 뿐이다. 그 밖의 것은 묻지 않고
  거부한다. 서버의 Claude Code 설정이 권한 확인을 끄도록 되어 있어도 마찬가지다.
- 열쇠·토큰이 있을 만한 경로(`~/.ssh`, `~/.aws`, `~/.claude`, `.env`, `*.pem` 등)는 읽지 못한다.
- 대화 기록을 남기지 않고, 그 서버 사용자의 훅·MCP 서버를 끈 채로 돈다.
- Claude Code 가 오래돼 이 제한을 걸 수 없으면 식별하지 않고 화면에 업데이트하라고 띄운다.

## 관리

| 하고 싶은 것 | 방법 |
|---|---|
| 로그 보기 | `journalctl --user -u gpu-monitor-agent -f` (hub 는 `gpu-monitor-hub`) |
| 설정 바꾸기 | `monitor.env` 를 고치고 `systemctl --user restart gpu-monitor-agent`. 항목은 `monitor.env.example` 참고 |
| 업데이트 | hub: `git pull && ./install.sh hub`. GPU 서버: 설치 한 줄을 다시 실행 |
| 제거 | `./install.sh uninstall`. 한 줄로 깐 서버는 `~/gpu-monitor/install.sh uninstall` |

systemd 가 없는 서버(일부 WSL2)는 cron 으로 1분마다 살아 있는지 보고 띄운다.
로그아웃한 뒤에도 계속 돌게 하려면 `sudo loginctl enable-linger $USER` 가 필요할 수 있다(설치 때 알려 준다).

## 보안 참고

- hub 는 같은 사내망이나 VPN 안에서만 연다. 대시보드와 `/api/all` 에는 인증이 없다.
- 토큰은 아무나 데이터를 밀어 넣지 못하게 막는 용도다. 설치 한 줄에 들어 있으니 팀 안에서만 나눈다.
- 프로세스 명령줄과 작업 디렉터리는 hub 로 보내지 않는다(키를 인자로 넘기는 경우가 있다).
- 설치 한 줄은 평문 HTTP 로 스크립트를 받아 실행한다. 믿을 수 있는 망에서만 쓴다.

## 파일

| 파일 | 하는 일 |
|---|---|
| `agent.py` | GPU 서버마다 돈다. `nvidia-smi`·`/proc` 로 점유자를 찾고, AI 로 식별하고, 2초마다 hub 로 보낸다 |
| `hub.py` | 보고를 받아 대시보드로 흘려보낸다(SSE). 설치 스크립트와 에이전트 파일도 내준다 |
| `dashboard.html` | 대시보드 |
| `install.sh` | 설치·업데이트·제거 |
| `run-agent.sh`, `run-hub.sh`, `watchdog.sh` | 서비스가 부르는 실행 스크립트 |
| `monitor.env.example` | 설정 항목 설명 |

## 라이선스

MIT
