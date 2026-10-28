#!/usr/bin/env bash
# 에이전트가 살아 있는지 보고, 없으면 띄운다. 여러 번 불러도 안전하다.
#
#   ./watchdog.sh            # 에이전트
#   ./watchdog.sh hub        # hub
#   ./watchdog.sh tunnel     # SSH 터널 (hub 에 SSH 로만 닿는 서버)
#
# systemd 가 없는 머신(예: systemd 를 안 켠 WSL2)에서 감독 역할을 대신한다.
# 밖에서 주기적으로 부르는 것을 전제로 한다 — Windows 작업 스케줄러, cron 등.
#
# 감시는 PID 파일 + kill -0 로 한다. pgrep -f 는 감시 명령 자신을 매칭해
# '살아 있다'고 오판하므로 쓰지 않는다.
set -uo pipefail
cd "$(dirname "$0")" || exit 1

WHAT="${1:-agent}"
case "$WHAT" in
  agent) RUNNER="./run-agent.sh"; SCRIPT="agent.py"; LOG="agent.log" ;;
  hub)   RUNNER="./run-hub.sh";   SCRIPT="hub.py";   LOG="hub.log" ;;
  tunnel) RUNNER="./run-tunnel.sh"; SCRIPT="ExitOnForwardFailure"; LOG="tunnel.log" ;;
  *) echo "사용법: watchdog.sh [agent|hub|tunnel]" >&2; exit 2 ;;
esac
PIDFILE="$PWD/$WHAT.pid"

if [ -f "$PIDFILE" ]; then
  PID="$(cat "$PIDFILE" 2>/dev/null || true)"
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    # pid 는 재사용된다. 그 자리에 정말 우리 프로세스가 있는지 확인한다.
    if tr '\0' ' ' < "/proc/$PID/cmdline" 2>/dev/null | grep -q "$SCRIPT"; then
      exit 0
    fi
  fi
fi

# setsid 로 세션을 끊어 부모(작업 스케줄러가 부른 wsl.exe 등)가 끝나도 살아남게 한다.
setsid nohup "$RUNNER" >> "$PWD/$LOG" 2>&1 &
echo $! > "$PIDFILE"
sleep 2
if kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "$(date '+%F %T') watchdog: $WHAT 시작 (pid $(cat "$PIDFILE"))"
else
  echo "$(date '+%F %T') watchdog: $WHAT 시작 실패 — $LOG 확인" >&2
  exit 1
fi
