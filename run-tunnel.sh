#!/usr/bin/env bash
# hub 에 직접 닿지 않는 서버에서, SSH 로 hub 포트를 이 서버의 루프백으로 끌어온다.
# 에이전트는 http://127.0.0.1:<MONITOR_SSH_LOCAL_PORT> 로 보낸다. 감독(systemd·cron·
# 윈도우 작업)이 끊기면 다시 띄운다. 키로 접속돼야 한다(비밀번호를 물으면 실패한다).
set -uo pipefail
cd "$(dirname "$0")" || exit 1

[ -f monitor.env ] && set -a && . ./monitor.env && set +a

if [ -z "${MONITOR_SSH:-}" ]; then
  echo "MONITOR_SSH 가 없다. monitor.env 에 user@host 를 적어라" >&2
  exit 1
fi
# user@host 또는 user@host:포트
TARGET="$MONITOR_SSH"; PORT_ARGS=()
case "$MONITOR_SSH" in
  *:*) TARGET="${MONITOR_SSH%:*}"; PORT_ARGS=(-p "${MONITOR_SSH##*:}") ;;
esac

exec ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -o ConnectTimeout=10 \
  -o StrictHostKeyChecking=accept-new "${PORT_ARGS[@]}" \
  -L "127.0.0.1:${MONITOR_SSH_LOCAL_PORT:-18787}:127.0.0.1:${MONITOR_SSH_REMOTE_PORT:-8787}" "$TARGET"
