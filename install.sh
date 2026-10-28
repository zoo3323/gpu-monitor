#!/usr/bin/env bash
# GPU 모니터 설치.
#
#   ./install.sh hub
#       이 서버에 hub(대시보드)를 띄운다. GPU 가 있으면 이 서버의 에이전트도 같이.
#       끝나면 다른 GPU 서버에 붙여 넣을 한 줄을 알려 준다.
#
#   curl -fsSL "http://HUB:8787/install.sh?t=TOKEN" | bash
#       hub 가 알려 준 그 한 줄. GPU 서버에서 실행하면 에이전트가 깔리고 hub 에 붙는다.
#       (이름을 바꾸려면 끝에 -s -- --label 이름)
#
#   ./install.sh agent --hub http://HUB:8787 --token TOKEN
#       저장소를 받아 둔 서버에서 에이전트만 깐다.
#
#   ./install.sh uninstall
#       서비스를 내린다. 파일은 남긴다.
#
# 옵션: --label 이름(화면에 보일 이름, 기본 hostname)  --port 8787(hub)
#       --no-agent(hub 만)  --no-ai(AI 식별을 끈다)
set -euo pipefail

# hub 가 이 스크립트를 내줄 때 아래 두 줄을 채운다.
SERVED_HUB=""
SERVED_TOKEN=""

# 서비스·폴더 이름. 한 서버에 둘을 띄울 때(시험 등)만 바꾼다.
NAME="${GPU_MONITOR_NAME:-gpu-monitor}"

say()  { printf '%s\n' "$*"; }
warn() { printf '! %s\n' "$*" >&2; }
die()  { printf '✗ %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- 인자
MODE=""; HUB="$SERVED_HUB"; TOKEN="$SERVED_TOKEN"; LABEL=""; PORT=""; WITH_AGENT=1; AI=""
[ -n "$SERVED_HUB" ] && MODE=agent
need() { [ -n "${2:-}" ] || die "$1 뒤에 값이 필요하다"; }
while [ $# -gt 0 ]; do
  case "$1" in
    hub|agent|uninstall) MODE="$1" ;;
    --hub)   need "$1" "${2:-}"; HUB="$2"; shift ;;
    --token) need "$1" "${2:-}"; TOKEN="$2"; shift ;;
    --label) need "$1" "${2:-}"; LABEL="$2"; shift ;;
    --port)  need "$1" "${2:-}"; PORT="$2"; shift ;;
    --no-agent) WITH_AGENT=0 ;;
    --no-ai) AI=off ;;
    -h|--help) sed -n '2,22p' "${BASH_SOURCE[0]:-$0}" 2>/dev/null | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "모르는 인자: $1  (./install.sh --help)" ;;
  esac
  shift
done
[ -n "$MODE" ] || die "무엇을 할지 정한다: ./install.sh hub | agent | uninstall  (--help)"

# 저장소 안에서 돌면 그 자리에 깐다. 파이프(curl | bash)로 돌면 ~/gpu-monitor 에 받는다.
SELF="${BASH_SOURCE[0]:-}"
if [ -n "$SELF" ] && [ -f "$SELF" ] && [ -f "$(dirname "$SELF")/agent.py" ]; then
  DIR="$(cd "$(dirname "$SELF")" && pwd)"; FROM_HUB=0
else
  DIR="${GPU_MONITOR_DIR:-$HOME/$NAME}"; FROM_HUB=1
fi
ENV="$DIR/monitor.env"
ME="${USER:-$(id -un)}"

command -v python3 >/dev/null || die "python3 가 필요하다 (3.7 이상)"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 7))' || die "python3 가 너무 오래됐다 (3.7 이상)"

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
  cat > "$ENV" <<EOF
# GPU 모니터 설정. install.sh 가 만들었다. 고친 뒤에는 서비스를 다시 띄운다:
#   systemctl --user restart $NAME-agent   (hub 는 $NAME-hub)
# 항목 설명은 저장소의 monitor.env.example 에 있다.
EOF
}

# ---------------------------------------------------------------- 점검
http_get() {  # URL → 본문 (실패하면 비정상 종료)
  python3 -c 'import sys, urllib.request; sys.stdout.write(urllib.request.urlopen(sys.argv[1], timeout=8).read().decode("utf-8", "replace"))' "$1"
}
fetch() {  # URL DEST — 받는 중에 도는 서비스가 반쯤 쓴 파일을 읽지 않게 옮겨 놓는다
  python3 -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' "$1" "$2.new" \
    || die "받지 못했다: $1"
  mv -f "$2.new" "$2"
}
check_gpu() {
  if command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1; then
    say "  GPU: $(nvidia-smi -L | sed 's/ (UUID.*//' | awk 'NR > 1 { printf ", " } { printf "%s", $0 }')"
  else
    warn "nvidia-smi 가 안 된다 — 이 서버는 CPU·메모리·디스크만 보인다"
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
  if [ "$(get_env MONITOR_AI)" = off ]; then say "  AI 식별: 꺼 둠 (MONITOR_AI=off)"; return 0; fi
  if [ "$(get_env MONITOR_AI_AUTH)" = api_key ]; then say "  AI 식별: API 키로 쓴다 (MONITOR_AI_AUTH=api_key)"; return 0; fi
  local c; c="$(find_claude)"
  if [ -z "$c" ]; then
    say "  AI 식별: 꺼짐 — Claude Code 가 없다. 설치하고 로그인하면 몇 분 안에 저절로 켜진다:"
    say "      curl -fsSL https://claude.ai/install.sh | bash"
    say "      ~/.local/bin/claude auth login"
  elif env -u ANTHROPIC_API_KEY "$c" auth status --json 2>/dev/null | grep -q '"loggedIn": *true'; then
    say "  AI 식별: 켜짐 ($c)"
    say "      GPU 를 쓰는 작업의 명령줄과 그 폴더의 파일 일부를 이 서버의 Claude 계정이 읽는다(읽기 전용)."
  else
    say "  AI 식별: 로그인 필요 — 이 서버에서 한 번:  $c auth login"
    say "      (로그인하면 몇 분 안에 저절로 켜진다)"
  fi
}

# ---------------------------------------------------------------- 서비스
has_systemd() { command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1; }
unit_path() { printf '%s' "$HOME/.config/systemd/user/$NAME-$1.service"; }
cron_line() { printf '* * * * * %s/watchdog.sh %s >/dev/null 2>&1' "$DIR" "$1"; }

start_service() {  # hub|agent
  local what="$1"
  chmod +x "$DIR/run-$what.sh" "$DIR/watchdog.sh"
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
        || warn "로그아웃해도 계속 돌게 하려면 한 번:  sudo loginctl enable-linger $ME"
    fi
    sleep 2
    systemctl --user is-active --quiet "$NAME-$what.service" \
      || die "$what 가 뜨지 않았다:  journalctl --user -u $NAME-$what -n 30"
    say "  $what: systemd 서비스 $NAME-$what (로그: journalctl --user -u $NAME-$what -f)"
  else
    # systemd 가 없는 곳(일부 WSL2·컨테이너): 1분마다 watchdog 이 살아 있는지 보고 띄운다.
    if [ -f "$DIR/$what.pid" ]; then kill "$(cat "$DIR/$what.pid")" 2>/dev/null || true; fi
    if command -v crontab >/dev/null; then
      # 사용자의 다른 cron 줄은 한 글자도 건드리지 않는다 — 우리 줄이 없을 때만 덧붙인다.
      if ! crontab -l 2>/dev/null | grep -q -F "$DIR/watchdog.sh $what"; then
        { crontab -l 2>/dev/null || true; cron_line "$what"; echo; } | crontab -
      fi
      say "  $what: cron 으로 1분마다 감시 (로그: $DIR/$what.log)"
    else
      warn "systemd·cron 이 없다. 재부팅 뒤에는 직접 다시 띄운다:  $DIR/watchdog.sh $what"
    fi
    "$DIR/watchdog.sh" "$what" >/dev/null || die "$what 가 뜨지 않았다:  tail $DIR/$what.log"
  fi
}

stop_service() {  # hub|agent
  local what="$1"
  if has_systemd && [ -f "$(unit_path "$what")" ]; then
    systemctl --user disable --now "$NAME-$what.service" >/dev/null 2>&1 || true
    rm -f "$(unit_path "$what")"
    systemctl --user daemon-reload
    say "  $what: systemd 서비스를 내렸다"
  fi
  if command -v crontab >/dev/null && crontab -l 2>/dev/null | grep -q -F "$DIR/watchdog.sh $what"; then
    { crontab -l 2>/dev/null | grep -v -F "$DIR/watchdog.sh $what" || true; } | crontab -
    say "  $what: cron 감시를 뺐다"
  fi
  if [ -f "$DIR/$what.pid" ]; then
    kill "$(cat "$DIR/$what.pid")" 2>/dev/null && say "  $what: 프로세스를 멈췄다" || true
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

# ---------------------------------------------------------------- 에이전트
install_agent() {
  [ -n "$HUB" ] || HUB="$(get_env MONITOR_HUB)"
  [ -n "$HUB" ] || die "hub 주소가 필요하다:  ./install.sh agent --hub http://HUB:8787 --token TOKEN"
  [ -n "$TOKEN" ] || TOKEN="$(get_env MONITOR_TOKEN)"
  HUB="${HUB%/}"

  say "에이전트 설치 → $DIR"
  mkdir -p "$DIR"
  if [ "$FROM_HUB" = 1 ]; then
    # install.sh 도 받아 둔다 — 나중에 $DIR/install.sh uninstall 로 내릴 수 있게.
    for f in agent.py run-agent.sh watchdog.sh install.sh; do fetch "$HUB/dist/$f" "$DIR/$f"; done
    chmod +x "$DIR/run-agent.sh" "$DIR/watchdog.sh" "$DIR/install.sh"
  fi
  local label="$LABEL"
  [ -n "$label" ] || label="$(get_env MONITOR_LABEL)"
  [ -n "$label" ] || label="$(hostname -s 2>/dev/null || hostname)"

  # hub 에 닿는지, 같은 이름을 다른 서버가 이미 쓰는지 본다. 이름이 겹치면 화면에서
  # 두 서버가 한 줄로 뒤섞이므로 설정을 바꾸기 전에 멈춘다.
  local all other
  if all="$(http_get "$HUB/api/all" 2>/dev/null)"; then
    other="$(printf '%s' "$all" | python3 -c '
import json, sys
label, me = sys.argv[1], sys.argv[2]
for m in json.load(sys.stdin).get("machines", []):
    if m.get("machine_id") == label and m.get("hostname") not in (None, me):
        print(m.get("hostname"))' "$label" "$(hostname)" 2>/dev/null || true)"
    [ -z "$other" ] || die "'$label' 이라는 이름을 다른 서버($other)가 이미 쓴다 — 끝에 -s -- --label 다른이름 을 붙여 다시 실행한다"
  else
    warn "hub($HUB)에 닿지 않는다 — 주소·방화벽을 확인한다. 에이전트는 깔고, 닿는 대로 붙는다"
  fi

  new_env
  set_env MONITOR_HUB "$HUB"
  set_env MONITOR_TOKEN "$TOKEN"
  set_env MONITOR_LABEL "$label"
  [ -n "$AI" ] && set_env MONITOR_AI "$AI"

  check_gpu
  check_claude
  start_service agent
  say "  이름: $label  →  대시보드 $HUB"
}

# ---------------------------------------------------------------- hub
install_hub() {
  [ "$FROM_HUB" = 0 ] || die "hub 는 저장소를 받은 폴더 안에서 ./install.sh hub 로 깐다"
  [ -f "$DIR/hub.py" ] && [ -f "$DIR/dashboard.html" ] || die "$DIR 에 hub.py·dashboard.html 이 없다"
  say "hub 설치 → $DIR"
  new_env
  local token port
  token="$(get_env MONITOR_TOKEN)"
  [ -n "$token" ] || token="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
  set_env MONITOR_TOKEN "$token"
  port="${PORT:-$(get_env MONITOR_PORT)}"; port="${port:-8787}"
  set_env MONITOR_PORT "$port"
  [ -n "$(get_env MONITOR_HOST)" ] || set_env MONITOR_HOST 0.0.0.0
  start_service hub

  local ok=0
  for _ in 1 2 3 4 5; do
    if http_get "http://127.0.0.1:$port/api/health" >/dev/null 2>&1; then ok=1; break; fi
    sleep 1
  done
  [ "$ok" = 1 ] || die "hub 가 $port 에서 답하지 않는다 (포트가 이미 쓰이는지 확인)"

  if [ "$WITH_AGENT" = 1 ] && command -v nvidia-smi >/dev/null; then
    say
    HUB="http://127.0.0.1:$port"  # 같은 서버의 에이전트는 루프백으로 붙는다
    TOKEN="$token"
    install_agent
  fi

  local addrs; addrs="$(my_addrs)"
  say
  say "────────────────────────────────────────────────────────────"
  say "대시보드:"
  if [ -n "$addrs" ]; then
    printf '%s\n' "$addrs" | while read -r a; do say "  http://$a:$port"; done
  else
    say "  http://<이 서버 주소>:$port"
  fi
  say
  say "다른 GPU 서버에서 아래 한 줄을 실행하면 이 hub 에 붙는다 (닿는 주소로 고른다):"
  if [ -n "$addrs" ]; then
    printf '%s\n' "$addrs" | while read -r a; do say "  curl -fsSL \"http://$a:$port/install.sh?t=$token\" | bash"; done
  else
    say "  curl -fsSL \"http://<이 서버 주소>:$port/install.sh?t=$token\" | bash"
  fi
  say "────────────────────────────────────────────────────────────"
  say "이 줄에는 토큰이 들어 있다. 같은 팀 안에서만 나눈다."
}

# ---------------------------------------------------------------- 실행
# curl | bash 로 돌 때 표준 입력은 이 스크립트 자신이다. 자식 명령이 그걸 읽어 가지
# 못하게 끊는다(읽어 가면 스크립트 뒷부분이 사라진다).
case "$MODE" in
  hub) install_hub ;;
  agent) install_agent ;;
  uninstall)
    say "제거 ($DIR)"
    stop_service agent
    stop_service hub
    say "파일은 남겼다. 지우려면:  rm -rf \"$DIR\""
    ;;
esac < /dev/null
