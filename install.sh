#!/usr/bin/env bash
# GPU Monitor installer.
#
#   ./install.sh hub
#       Start the hub (dashboard) on this server, plus this server's agent if it has a GPU.
#       At the end it prints the one line to paste on every other GPU server.
#
#   curl -fsSL --noproxy '*' "http://HUB:PORT/install.sh?t=TOKEN" | bash
#       That line. Run it on a GPU server to install the agent and join the hub.
#       (To pick the server's name, end it with  | bash -s -- --label NAME)
#
#   ./install.sh agent --ssh USER@HUB_HOST --token TOKEN
#       For a server that can reach the hub only over SSH (key login): keeps an SSH tunnel up.
#
#   ./install.sh agent --hub http://HUB:PORT[,http://OTHER:PORT] --token TOKEN
#       Install only the agent from a cloned repository. Several hub addresses fail over.
#
#   ./install.sh uninstall
#       Stop and remove the services. Files are kept.
#
# Options: --label NAME  --port PORT (hub port, default 8787)  --lang en|ko
#          --no-agent (hub only)  --no-ai
set -euo pipefail

# hub 가 이 스크립트를 내줄 때 아래 세 줄을 채운다.
SERVED_HUB=""
SERVED_TOKEN=""
SERVED_LANG=""

# 서비스·폴더 이름. 한 서버에 둘을 띄울 때(시험 등)만 바꾼다.
NAME="${GPU_MONITOR_NAME:-gpu-monitor}"
DEFAULT_PORT=8787
REPO_URL="https://github.com/zoo3323/gpu-monitor.git"

# 화면 언어: --lang > hub 가 채운 값 > 기존 monitor.env > $MONITOR_LANG > en
UILANG="${SERVED_LANG:-${MONITOR_LANG:-en}}"
t()    { if [ "$UILANG" = ko ]; then printf '%s' "$2"; else printf '%s' "$1"; fi; }
say()  { printf '%s\n' "$*"; }
warn() { printf '! %s\n' "$*" >&2; }
die()  { printf '✗ %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- 인자
MODE=""; HUB="$SERVED_HUB"; TOKEN="$SERVED_TOKEN"; LABEL=""; PORT=""; WITH_AGENT=1; AI=""; LANG_ARG=""; SSH=""
[ -n "$SERVED_HUB" ] && MODE=agent
need() { [ -n "${2:-}" ] || die "$(t "$1 needs a value" "$1 뒤에 값이 필요합니다")"; }
while [ $# -gt 0 ]; do
  case "$1" in
    hub|agent|uninstall) MODE="$1" ;;
    --hub)   need "$1" "${2:-}"; HUB="$2"; shift ;;
    --token) need "$1" "${2:-}"; TOKEN="$2"; shift ;;
    --label) need "$1" "${2:-}"; LABEL="$2"; shift ;;
    --port)  need "$1" "${2:-}"; PORT="$2"; shift ;;
    --ssh)   need "$1" "${2:-}"; SSH="$2"; shift ;;
    --lang)  need "$1" "${2:-}"; LANG_ARG="$2"; UILANG="$2"; shift ;;
    --no-agent) WITH_AGENT=0 ;;
    --no-ai) AI=off ;;
    -h|--help) sed -n '2,24p' "${BASH_SOURCE[0]:-$0}" 2>/dev/null | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "$(t "Unknown argument: $1  (./install.sh --help)" "모르는 인자입니다: $1  (./install.sh --help)")" ;;
  esac
  shift
done
case "$UILANG" in en|ko) ;; *) UILANG=en; die "--lang: en | ko" ;; esac
[ -n "$MODE" ] || die "$(t "Choose what to do: ./install.sh hub | agent | uninstall  (--help)" \
                          "무엇을 할지 정해 주세요: ./install.sh hub | agent | uninstall  (--help)")"
case "$PORT" in ''|*[!0-9]*) [ -z "$PORT" ] || die "--port: $PORT" ;; esac

# 저장소 안에서 돌면 그 자리에 깐다. 파이프(curl | bash)로 돌면 ~/gpu-monitor 에 받는다.
SELF="${BASH_SOURCE[0]:-}"
if [ -n "$SELF" ] && [ -f "$SELF" ] && [ -f "$(dirname "$SELF")/agent.py" ]; then
  DIR="$(cd "$(dirname "$SELF")" && pwd)"; FROM_HUB=0
else
  DIR="${GPU_MONITOR_DIR:-$HOME/$NAME}"; FROM_HUB=1
fi
ENV="$DIR/monitor.env"
ME="${USER:-$(id -un)}"

command -v python3 >/dev/null || die "$(t "python3 is required (3.7+)" "python3가 필요합니다 (3.7 이상)")"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 7))' \
  || die "$(t "python3 is too old (3.7+ needed)" "python3가 너무 오래됐습니다 (3.7 이상 필요)")"

# ---------------------------------------------------------------- 설정 파일
get_env() {  # KEY → 값(따옴표 벗김). 없으면 빈 문자열
  [ -f "$ENV" ] || return 0
  sed -n "s/^$1=//p" "$ENV" | tail -n 1 | sed "s/^['\"]//; s/['\"]\$//"
}
set_env() {  # KEY VALUE — 있으면 바꾸고 없으면 붙인다. 주석·다른 줄은 그대로 둔다
  mkdir -p "$DIR"; touch "$ENV"; chmod 600 "$ENV"
  python3 - "$ENV" "$1" "$2" <<'PY'
import shlex, sys
path, key, val = sys.argv[1:]
line = f"{key}={shlex.quote(val)}" if val else f"{key}="
with open(path, encoding="utf-8") as fh:
    lines = fh.read().splitlines()
hits = [i for i, l in enumerate(lines) if l.startswith(key + "=")]
if hits:
    lines[hits[-1]] = line
else:
    lines.append(line)
with open(path, "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines) + "\n")
PY
}
new_env() {  # 처음 만들 때 설명 머리말을 단다
  [ -s "$ENV" ] && return 0
  mkdir -p "$DIR"; touch "$ENV"; chmod 600 "$ENV"
  if [ "$UILANG" = ko ]; then
    printf '%s\n' "# GPU 모니터 설정입니다. install.sh가 만들었습니다. 고친 뒤에는 서비스를 다시 띄우세요:" \
      "#   systemctl --user restart $NAME-agent   (hub는 $NAME-hub)" \
      "# 항목 설명은 저장소의 monitor.env.example에 있습니다." > "$ENV"
  else
    printf '%s\n' "# GPU Monitor settings, written by install.sh. Restart the service after editing:" \
      "#   systemctl --user restart $NAME-agent   (hub: $NAME-hub)" \
      "# Every option is described in monitor.env.example in the repository." > "$ENV"
  fi
}
# --lang 을 안 줬으면 기존 설정의 언어를 따른다(다시 설치해도 언어가 바뀌지 않게).
if [ -z "$LANG_ARG" ] && [ -z "$SERVED_LANG" ]; then
  case "$(get_env MONITOR_LANG)" in en|ko) UILANG="$(get_env MONITOR_LANG)" ;; esac
fi

# ---------------------------------------------------------------- 점검
# hub 는 사내망·VPN·SSH 터널 안에 있다. http_proxy 가 걸려 있어도 hub 로는 프록시를 쓰지 않는다.
http_get() {  # URL → 본문 (실패하면 비정상 종료)
  python3 -c 'import sys, urllib.request as u
o = u.build_opener(u.ProxyHandler({}))
sys.stdout.write(o.open(sys.argv[1], timeout=8).read().decode("utf-8", "replace"))' "$1"
}
fetch() {  # URL DEST — 받는 중에 도는 서비스가 반쯤 쓴 파일을 읽지 않게 옮겨 놓는다
  python3 -c 'import sys, urllib.request as u
o = u.build_opener(u.ProxyHandler({}))
data = o.open(sys.argv[1], timeout=30).read()
open(sys.argv[2], "wb").write(data)' "$1" "$2.new" || die "$(t "Could not download: $1" "받지 못했습니다: $1")"
  mv -f "$2.new" "$2"
}
port_free() {  # PORT → 비어 있으면 0
  python3 -c 'import socket, sys
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try: s.bind(("0.0.0.0", int(sys.argv[1])))
except OSError: sys.exit(1)' "$1"
}
is_wsl() { [ "${GPU_MONITOR_WSL:-}" = 1 ] || grep -qi microsoft /proc/version 2>/dev/null; }  # 앞쪽은 시험용
check_gpu() {
  if command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1; then
    say "  GPU: $(nvidia-smi -L | sed 's/ (UUID.*//' | awk 'NR > 1 { printf ", " } { printf "%s", $0 }')"
  else
    warn "$(t "nvidia-smi doesn't work — this server will show CPU, memory and disk only" \
              "nvidia-smi가 동작하지 않습니다 — 이 서버는 CPU·메모리·디스크만 보입니다")"
  fi
}
find_claude() {
  local c; c="$(get_env MONITOR_AI_CMD)"
  if [ -n "$c" ]; then [ -x "$c" ] && printf '%s' "$c"; return 0; fi
  for c in "$(command -v claude 2>/dev/null || true)" "$HOME/.local/bin/claude" "$HOME/.claude/local/claude" \
           /usr/local/bin/claude /opt/homebrew/bin/claude "$HOME/.npm-global/bin/claude" /usr/bin/claude; do
    if [ -n "$c" ] && [ -x "$c" ]; then printf '%s' "$c"; return 0; fi
  done
}
check_claude() {
  local ai; ai="$(t "AI identification" "AI 식별")"
  if [ "$(get_env MONITOR_AI)" = off ]; then say "  $ai: $(t "off" "꺼 둠") (MONITOR_AI=off)"; return 0; fi
  if [ "$(get_env MONITOR_AI_AUTH)" = api_key ]; then
    say "  $ai: $(t "using an API key" "API 키로 씁니다") (MONITOR_AI_AUTH=api_key)"; return 0
  fi
  local c; c="$(find_claude)"
  if [ -z "$c" ]; then
    say "  $ai: $(t "off — Claude Code is not installed. Install it and log in; it turns on within a minute:" \
                    "꺼짐 — Claude Code가 없습니다. 설치하고 로그인하면 1분 안에 자동으로 켜집니다:")"
    say "      curl -fsSL https://claude.ai/install.sh | bash"
    say "      ~/.local/bin/claude auth login"
  elif env -u ANTHROPIC_API_KEY "$c" auth status --json 2>/dev/null | grep -q '"loggedIn": *true'; then
    say "  $ai: $(t "on" "켜짐") ($c)"
    say "      $(t "Claude on this server's account reads the command line of GPU jobs and some files in their folders (read-only)." \
                   "GPU를 쓰는 작업의 명령줄과 그 폴더의 파일 일부를 이 서버의 Claude 계정이 읽습니다(읽기 전용).")"
  else
    say "  $ai: $(t "login needed — once on this server:" "로그인이 필요합니다 — 이 서버에서 한 번:")  $c auth login"
    say "      $(t "(turns on within a minute after you log in)" "(로그인하면 1분 안에 자동으로 켜집니다)")"
  fi
}

# ---------------------------------------------------------------- 서비스
has_systemd() { command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1; }
unit_path() { printf '%s' "$HOME/.config/systemd/user/$NAME-$1.service"; }
cron_line() { printf '* * * * * %s/watchdog.sh %s >/dev/null 2>&1' "$DIR" "$1"; }

# WSL: 창을 다 닫으면 WSL 이 멈추고, systemd·cron 도 꺼져 있기 쉽다. 윈도우 작업 스케줄러가
# 로그인해 있는 동안 5분마다 확인해, 꺼져 있으면 숨은 창으로 다시 띄운다. 작업은 명령이
# 끝날 때까지 '실행 중'이라 이미 돌고 있으면 새로 띄우지 않는다.
win_appdata() {
  command -v schtasks.exe >/dev/null && command -v cmd.exe >/dev/null && command -v wslpath >/dev/null || return 1
  cmd.exe /c "echo %LOCALAPPDATA%" 2>/dev/null | tr -d '\r' | grep -v '^$' | tail -n 1
}
win_task() {  # 작업이름 WSL안의명령
  local task="$1" cmd="$2" appdata dir
  appdata="$(win_appdata)" || return 1
  [ -n "$appdata" ] || return 1
  dir="$(wslpath -u "$appdata")/$NAME"
  mkdir -p "$dir" || return 1
  printf 'CreateObject("Wscript.Shell").Run "wsl.exe -d %s -u %s --exec %s", 0, True\r\n' \
    "${WSL_DISTRO_NAME:-Ubuntu}" "$ME" "$cmd" > "$dir/$task.vbs" || return 1
  schtasks.exe /Create /F /SC MINUTE /MO 5 /TN "$task" \
    /TR "wscript.exe //B \"$appdata\\$NAME\\$task.vbs\"" >/dev/null 2>&1 || return 1
  schtasks.exe /Run /TN "$task" >/dev/null 2>&1 || true
}
win_task_remove() {  # 작업이름
  command -v schtasks.exe >/dev/null || return 0
  schtasks.exe /Query /TN "$1" >/dev/null 2>&1 || return 0
  schtasks.exe /Delete /F /TN "$1" >/dev/null 2>&1 && say "  $1: $(t "Windows task removed" "윈도우 작업을 지웠습니다")"
  local appdata; appdata="$(win_appdata)" && rm -f "$(wslpath -u "$appdata")/$NAME/$1.vbs"
  return 0
}
wait_lock() {  # agent|hub — 실행 잠금이 잡힐 때까지(= 떴다) 최대 15초
  command -v flock >/dev/null || { sleep 3; return 0; }
  for _ in $(seq 1 15); do
    if ! flock -n "$DIR/.$1.lock" true 2>/dev/null; then return 0; fi
    sleep 1
  done
  return 1
}

start_service() {  # hub|agent|tunnel
  local what="$1"
  chmod +x "$DIR/run-$what.sh" "$DIR/watchdog.sh"
  if is_wsl && ! has_systemd; then
    if win_task "$NAME-$what" "$DIR/run-$what.sh"; then
      [ "$what" = tunnel ] || wait_lock "$what" \
        || die "$(t "$what did not start:" "$what가 뜨지 않았습니다:")  $DIR/run-$what.sh"
      say "  $what: $(t "Windows Task Scheduler" "윈도우 작업 스케줄러") $NAME-$what ($(t "checked every 5 min while you're logged in" "로그인해 있는 동안 5분마다 확인"))"
      return 0
    fi
    warn "$(t "Couldn't register a Windows task (WSL interop off?) — falling back to cron" \
              "윈도우 작업을 등록하지 못했습니다(WSL interop이 꺼져 있나요?) — cron으로 대신합니다")"
  fi
  if has_systemd; then
    mkdir -p "$(dirname "$(unit_path "$what")")"
    cat > "$(unit_path "$what")" <<UNIT
[Unit]
Description=GPU monitor $what ($DIR)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$DIR
ExecStart=$DIR/run-$what.sh
Restart=always
RestartSec=5
# 모니터는 학습·작업보다 뒤에 선다. 머신이 바쁠 때 CPU 를 양보한다.
Nice=10

[Install]
WantedBy=default.target
UNIT
    systemctl --user daemon-reload
    systemctl --user enable "$NAME-$what.service" >/dev/null 2>&1
    systemctl --user restart "$NAME-$what.service"
    # 로그인 세션이 없어도 계속 돌게 한다(부팅 직후·SSH 로그아웃 뒤).
    if [ "$(loginctl show-user "$ME" -p Linger --value 2>/dev/null || true)" != "yes" ]; then
      loginctl enable-linger "$ME" 2>/dev/null \
        || warn "$(t "To keep it running after you log out, run once:" "로그아웃해도 계속 돌게 하려면 한 번 실행하세요:")  sudo loginctl enable-linger $ME"
    fi
    # systemd 를 켠 WSL 도 창을 다 닫으면 멈춘다 — 윈도우 작업이 WSL 을 깨워 둔다.
    if is_wsl; then win_task "$NAME-keepalive" "/bin/sleep infinity" || true; fi
    sleep 2
    systemctl --user is-active --quiet "$NAME-$what.service" \
      || die "$(t "$what did not start:" "$what가 뜨지 않았습니다:")  journalctl --user -u $NAME-$what -n 30"
    say "  $what: $(t "systemd service" "systemd 서비스") $NAME-$what ($(t "logs" "로그"): journalctl --user -u $NAME-$what -f)"
  else
    # systemd 가 없는 곳(컨테이너 등): 1분마다 watchdog 이 살아 있는지 보고 띄운다.
    if [ -f "$DIR/$what.pid" ]; then kill "$(cat "$DIR/$what.pid")" 2>/dev/null || true; fi
    if command -v crontab >/dev/null; then
      # 사용자의 다른 cron 줄은 한 글자도 건드리지 않는다 — 우리 줄이 없을 때만 덧붙인다.
      if ! crontab -l 2>/dev/null | grep -q -F "$DIR/watchdog.sh $what"; then
        { crontab -l 2>/dev/null || true; cron_line "$what"; echo; } | crontab -
      fi
      say "  $what: $(t "cron checks it every minute" "cron으로 1분마다 확인합니다") ($(t "log" "로그"): $DIR/$what.log)"
      if is_wsl; then
        warn "$(t "In WSL, cron runs only if the cron service is started:  sudo service cron start" \
                  "WSL에서는 cron 서비스를 켜야 cron이 돕니다:  sudo service cron start")"
      fi
    else
      warn "$(t "No systemd or cron. After a reboot, start it again:" "systemd·cron이 없습니다. 재부팅한 뒤에는 직접 다시 띄우세요:")  $DIR/watchdog.sh $what"
    fi
    "$DIR/watchdog.sh" "$what" >/dev/null || die "$(t "$what did not start:" "$what가 뜨지 않았습니다:")  tail $DIR/$what.log"
  fi
}

stop_service() {  # hub|agent|tunnel
  local what="$1"
  if has_systemd && [ -f "$(unit_path "$what")" ]; then
    systemctl --user disable --now "$NAME-$what.service" >/dev/null 2>&1 || true
    rm -f "$(unit_path "$what")"
    systemctl --user daemon-reload
    say "  $what: $(t "systemd service removed" "systemd 서비스를 내렸습니다")"
  fi
  if command -v crontab >/dev/null && crontab -l 2>/dev/null | grep -q -F "$DIR/watchdog.sh $what"; then
    { crontab -l 2>/dev/null | grep -v -F "$DIR/watchdog.sh $what" || true; } | crontab -
    say "  $what: $(t "cron check removed" "cron 감시를 뺐습니다")"
  fi
  if is_wsl; then
    win_task_remove "$NAME-$what"
    # 실행 스크립트는 flock·python·ssh 로 바뀌어 있다 — 실제 프로그램으로 찾는다.
    case "$what" in
      agent) pkill -f "$DIR/agent.py" 2>/dev/null || true ;;
      hub) pkill -f "$DIR/hub.py" 2>/dev/null || true ;;
      tunnel) pkill -f "ExitOnForwardFailure.*127.0.0.1:$(get_env MONITOR_SSH_LOCAL_PORT):" 2>/dev/null || true ;;
    esac
  fi
  if [ -f "$DIR/$what.pid" ]; then
    kill "$(cat "$DIR/$what.pid")" 2>/dev/null && say "  $what: $(t "process stopped" "프로세스를 멈췄습니다")" || true
    rm -f "$DIR/$what.pid"
  fi
}

# ---------------------------------------------------------------- 주소
my_addrs() {  # 다른 서버가 이 서버에 닿을 만한 IPv4 (도커·가상 브리지 제외)
  { ip -4 -o addr show scope global 2>/dev/null \
      | awk '$2 !~ /^(docker|br-|veth|virbr|cni|flannel|lxc|podman)/ {split($4, a, "/"); print a[1]}'
    command -v ip >/dev/null || hostname -I 2>/dev/null | tr ' ' '\n'
  } | grep -E '^[0-9]+(\.[0-9]+){3}$' | awk '!seen[$0]++'
}
join_by_comma() { local IFS=,; printf '%s' "$*"; }

# ---------------------------------------------------------------- SSH 터널
# hub 에 HTTP 로 직접 닿지 않고 SSH(키 로그인)로만 닿는 서버. hub 포트를 이 서버의
# 루프백으로 끌어오는 터널을 서비스로 띄우고, 에이전트는 그 터널로 보낸다.
setup_tunnel() {
  local target="$SSH" port_args=() rport lport
  case "$SSH" in *:*) target="${SSH%:*}"; port_args=(-p "${SSH##*:}") ;; esac
  command -v ssh >/dev/null || die "$(t "ssh is not installed" "ssh가 없습니다")"
  [ -n "$TOKEN" ] || [ -n "$(get_env MONITOR_TOKEN)" ] \
    || die "$(t "--ssh needs --token TOKEN (the hub's token)" "--ssh에는 --token TOKEN(hub의 토큰)이 필요합니다")"
  ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new "${port_args[@]}" "$target" true \
    </dev/null >/dev/null 2>&1 \
    || die "$(t "Can't log in to $target with an SSH key. Set it up first, then run again:" \
                "$target에 SSH 키로 접속되지 않습니다. 먼저 키를 등록하고 다시 실행하세요:")  ssh-copy-id $target"
  rport="${PORT:-$(get_env MONITOR_SSH_REMOTE_PORT)}"; rport="${rport:-$DEFAULT_PORT}"
  if [ "$(get_env MONITOR_SSH)" = "$SSH" ] && [ -n "$(get_env MONITOR_SSH_LOCAL_PORT)" ]; then
    lport="$(get_env MONITOR_SSH_LOCAL_PORT)"   # 다시 설치 — 이미 떠 있는 터널의 포트를 그대로 쓴다
  else
    lport=18787
    while ! port_free "$lport"; do lport=$((lport + 1)); done
  fi
  new_env
  set_env MONITOR_SSH "$SSH"
  set_env MONITOR_SSH_LOCAL_PORT "$lport"
  set_env MONITOR_SSH_REMOTE_PORT "$rport"
  start_service tunnel
  local ok=0
  for _ in $(seq 1 15); do
    if http_get "http://127.0.0.1:$lport/api/health" >/dev/null 2>&1; then ok=1; break; fi
    sleep 1
  done
  [ "$ok" = 1 ] || die "$(t "The SSH tunnel is up but the hub isn't answering on $target port $rport (use --port)" \
                            "SSH 터널은 떴지만 $target의 $rport 포트에서 hub가 답하지 않습니다(--port로 지정하세요)")"
  say "  tunnel: 127.0.0.1:$lport → $target:$rport"
  HUB="http://127.0.0.1:$lport${HUB:+,$HUB}"
}

# ---------------------------------------------------------------- 에이전트
install_agent() {
  [ -n "$SSH" ] || SSH="$(get_env MONITOR_SSH)"   # 터널로 깔았던 서버를 다시 설치할 때
  [ -n "$HUB" ] || [ -n "$SSH" ] || HUB="$(get_env MONITOR_HUB)"
  [ -n "$TOKEN" ] || TOKEN="$(get_env MONITOR_TOKEN)"
  say "$(t "Installing agent" "에이전트 설치") → $DIR"
  mkdir -p "$DIR"
  if [ -n "$SSH" ]; then setup_tunnel; fi
  [ -n "$HUB" ] || die "$(t "Hub address needed:" "hub 주소가 필요합니다:")  ./install.sh agent --hub http://HUB:PORT --token TOKEN"

  # hub 주소는 쉼표로 여러 개일 수 있다(사내망·Tailscale·터널). 닿는 주소를 앞에 둔다.
  local urls=() reach=() far=() u
  IFS=',' read -r -a urls <<< "$HUB"
  for u in "${urls[@]}"; do
    u="${u%/}"; [ -n "$u" ] || continue
    if http_get "$u/api/health" >/dev/null 2>&1; then reach+=("$u"); else far+=("$u"); fi
  done
  HUB="$(join_by_comma "${reach[@]}" "${far[@]}")"
  local first="${reach[0]:-${far[0]:-}}"

  if [ "$FROM_HUB" = 1 ]; then
    [ "${#reach[@]}" -gt 0 ] || die "$(t "Can't reach the hub:" "hub에 닿지 않습니다:") $HUB"
    # install.sh 도 받아 둔다 — 나중에 $DIR/install.sh uninstall 로 내릴 수 있게.
    for f in agent.py run-agent.sh run-tunnel.sh watchdog.sh install.sh; do fetch "$first/dist/$f" "$DIR/$f"; done
    chmod +x "$DIR/run-agent.sh" "$DIR/run-tunnel.sh" "$DIR/watchdog.sh" "$DIR/install.sh"
  fi
  local label="$LABEL"
  [ -n "$label" ] || label="$(get_env MONITOR_LABEL)"
  [ -n "$label" ] || label="$(hostname -s 2>/dev/null || hostname)"

  # 같은 이름을 다른 서버가 이미 쓰는지 본다. 이름이 겹치면 화면에서 두 서버가 한 줄로
  # 뒤섞이므로 설정을 바꾸기 전에 멈춘다.
  local all other
  if [ "${#reach[@]}" -gt 0 ] && all="$(http_get "$first/api/all" 2>/dev/null)"; then
    other="$(printf '%s' "$all" | python3 -c '
import json, sys
label, me = sys.argv[1], sys.argv[2]
for m in json.load(sys.stdin).get("machines", []):
    if m.get("machine_id") == label and m.get("hostname") not in (None, me):
        print(m.get("hostname"))' "$label" "$(hostname)" 2>/dev/null || true)"
    [ -z "$other" ] || die "$(t "'$label' is already used by another server ($other) — run again with  -s -- --label OTHER_NAME  at the end" \
                                "'$label' 이름을 다른 서버($other)가 이미 씁니다 — 끝에  -s -- --label 다른이름  을 붙여 다시 실행하세요")"
  else
    warn "$(t "Can't reach the hub ($HUB) — check the address and firewall. The agent is installed and will connect once it can." \
              "hub($HUB)에 닿지 않습니다 — 주소와 방화벽을 확인하세요. 에이전트는 설치했고, 닿는 대로 붙습니다")"
  fi

  new_env
  set_env MONITOR_HUB "$HUB"
  set_env MONITOR_TOKEN "$TOKEN"
  set_env MONITOR_LABEL "$label"
  set_env MONITOR_LANG "$UILANG"
  if [ -n "$AI" ]; then set_env MONITOR_AI "$AI"; fi

  check_gpu
  check_claude
  start_service agent
  if [ -n "$SSH" ]; then say "  $(t "Name" "이름"): $label  →  hub $SSH (SSH)"
  else say "  $(t "Name" "이름"): $label  →  $(t "dashboard" "대시보드") $first"; fi
}

# ---------------------------------------------------------------- hub
install_hub() {
  [ "$FROM_HUB" = 0 ] || die "$(t "Install the hub from the cloned repository:  ./install.sh hub" \
                                  "hub는 저장소를 받은 폴더 안에서 설치합니다:  ./install.sh hub")"
  [ -f "$DIR/hub.py" ] && [ -f "$DIR/dashboard.html" ] \
    || die "$(t "$DIR has no hub.py or dashboard.html" "$DIR에 hub.py·dashboard.html이 없습니다")"
  say "$(t "Installing hub" "hub 설치") → $DIR"
  if is_wsl; then
    warn "$(t "A hub inside WSL2 is reachable from other machines only with mirrored networking (networkingMode=mirrored in .wslconfig) or Windows port forwarding." \
              "WSL2 안의 hub는 미러 네트워킹(.wslconfig의 networkingMode=mirrored)이나 윈도우 포트 포워딩이 있어야 다른 서버에서 닿습니다.")"
  fi

  # 포트: --port > 기존 설정 > 8787. 처음 깔 때 8787 이 쓰이고 있으면 빈 포트를 고른다.
  # 다시 깔 때는 이미 떠 있는 우리 hub 가 그 포트를 쓰고 있으므로 보지 않는다.
  local token port
  port="$(get_env MONITOR_PORT)"
  if [ -n "$PORT" ]; then
    if [ "$PORT" != "$port" ] && ! port_free "$PORT"; then
      die "$(t "Port $PORT is already in use — choose another with --port" "$PORT 포트를 이미 쓰고 있습니다 — --port로 다른 포트를 고르세요")"
    fi
    port="$PORT"
  elif [ -z "$port" ]; then
    port="$DEFAULT_PORT"
    while ! port_free "$port"; do
      port=$((port + 1))
      [ "$port" -le $((DEFAULT_PORT + 50)) ] || die "$(t "No free port near $DEFAULT_PORT — choose one with --port" "$DEFAULT_PORT 근처에 빈 포트가 없습니다 — --port로 고르세요")"
    done
    [ "$port" = "$DEFAULT_PORT" ] || say "  $(t "Port $DEFAULT_PORT is in use — using $port" "$DEFAULT_PORT 포트를 이미 쓰고 있어 $port 포트를 씁니다")"
  fi

  local addrs; addrs="$(my_addrs)"
  new_env
  token="$(get_env MONITOR_TOKEN)"
  [ -n "$token" ] || token="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
  set_env MONITOR_TOKEN "$token"
  set_env MONITOR_PORT "$port"
  set_env MONITOR_LANG "$UILANG"
  # 설치 한 줄로 붙는 에이전트는 이 주소들을 모두 받아, 닿는 쪽으로 보낸다(사내망↔Tailscale).
  set_env MONITOR_PUBLIC_URLS "$(printf '%s\n' "$addrs" | grep . | sed "s|.*|http://&:$port|" | paste -sd, -)"
  [ -n "$(get_env MONITOR_HOST)" ] || set_env MONITOR_HOST 0.0.0.0
  start_service hub

  local ok=0
  for _ in 1 2 3 4 5; do
    if http_get "http://127.0.0.1:$port/api/health" >/dev/null 2>&1; then ok=1; break; fi
    sleep 1
  done
  [ "$ok" = 1 ] || die "$(t "The hub isn't answering on port $port" "hub가 $port 포트에서 답하지 않습니다")"

  if [ "$WITH_AGENT" = 1 ] && command -v nvidia-smi >/dev/null; then
    say
    HUB="http://127.0.0.1:$port"  # 같은 서버의 에이전트는 루프백으로 붙는다
    TOKEN="$token"
    SSH=""
    install_agent
  fi

  local here; here="$(t "<this server's address>" "<이 서버 주소>")"
  say
  say "────────────────────────────────────────────────────────────"
  say "$(t "Dashboard:" "대시보드:")"
  if [ -n "$addrs" ]; then
    printf '%s\n' "$addrs" | while read -r a; do say "  http://$a:$port"; done
  else
    say "  http://$here:$port"
  fi
  say
  say "$(t "On each GPU server, run this line to join this hub (use an address it can reach):" \
           "다른 GPU 서버에서 아래 한 줄을 실행하면 이 hub에 붙습니다 (닿는 주소로 고르세요):")"
  if [ -n "$addrs" ]; then
    printf '%s\n' "$addrs" | while read -r a; do say "  curl -fsSL --noproxy '*' \"http://$a:$port/install.sh?t=$token\" | bash"; done
  else
    say "  curl -fsSL --noproxy '*' \"http://$here:$port/install.sh?t=$token\" | bash"
  fi
  say
  say "$(t "A server that reaches this hub only over SSH (key login):" "SSH(키 로그인)로만 이 hub에 닿는 서버:")"
  say "  git clone $REPO_URL && gpu-monitor/install.sh agent --ssh $ME@<$(t "this server" "이 서버")> --port $port --token $token"
  say "────────────────────────────────────────────────────────────"
  say "$(t "These lines contain the token. Share them only within your team." \
           "이 줄들에는 토큰이 들어 있습니다. 같은 팀 안에서만 나누세요.")"
}

# ---------------------------------------------------------------- 실행
# curl | bash 로 돌 때 표준 입력은 이 스크립트 자신이다. 자식 명령이 그걸 읽어 가지
# 못하게 끊는다(읽어 가면 스크립트 뒷부분이 사라진다).
case "$MODE" in
  hub) install_hub ;;
  agent) install_agent ;;
  uninstall)
    say "$(t "Uninstalling" "제거") ($DIR)"
    stop_service agent
    stop_service tunnel
    stop_service hub
    if is_wsl; then win_task_remove "$NAME-keepalive"; fi
    say "$(t "Files are kept. To delete them:" "파일은 남겨 두었습니다. 지우려면:")  rm -rf \"$DIR\""
    ;;
esac < /dev/null
