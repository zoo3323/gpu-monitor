#!/usr/bin/env bash
# 에이전트를 전경(foreground)에서 돌린다. 감독은 밖에서 한다.
#
#   ./run-agent.sh [label] [watch-glob ...]
#
# 인자를 생략하면 monitor.env 의 MONITOR_LABEL / MONITOR_WATCH 를 쓴다. 그래서
# systemd 유닛이나 작업 스케줄러가 인자 없이 이 스크립트만 부르면 된다.
set -uo pipefail
cd "$(dirname "$0")" || exit 1

[ -f monitor.env ] && set -a && . ./monitor.env && set +a

LABEL="${1:-${MONITOR_LABEL:-$(hostname)}}"
[ "$#" -gt 0 ] && shift

HUB="${MONITOR_HUB:-}"
if [ -z "$HUB" ]; then
  echo "MONITOR_HUB 가 없다. monitor.env 에 적어라" >&2
  exit 1
fi

ARGS=(--hub "$HUB" --label "$LABEL")

# 감시 글롭: 인자가 이기고, 없으면 MONITOR_WATCH(콜론 구분). 둘 다 없으면
# 순수 GPU 모니터로 돈다 — 점유자는 이것 없이도 그대로 보인다.
if [ "$#" -gt 0 ]; then
  for pattern in "$@"; do ARGS+=(--watch "$pattern"); done
elif [ -n "${MONITOR_WATCH:-}" ]; then
  IFS=':' read -r -a patterns <<< "$MONITOR_WATCH"
  for pattern in "${patterns[@]}"; do
    [ -n "$pattern" ] && ARGS+=(--watch "$pattern")
  done
fi

if [ -n "${MONITOR_DISK:-}" ]; then
  IFS=':' read -r -a mounts <<< "$MONITOR_DISK"
  for mount in "${mounts[@]}"; do
    [ -n "$mount" ] && ARGS+=(--disk "$mount")
  done
fi

[ -n "${MONITOR_NOTE:-}" ] && ARGS+=(--note "$MONITOR_NOTE")

# 토큰은 일부러 넘기지 않는다 — agent.py 가 monitor.env 를 직접 읽는다.
# 명령줄에 실으면 같은 머신의 누구나 ps 로 본다.
exec python3 "$PWD/agent.py" "${ARGS[@]}"
