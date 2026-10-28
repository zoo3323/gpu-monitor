#!/usr/bin/env bash
# hub 를 전경(foreground)에서 돌린다. 감독은 밖에서 한다 — systemd, 작업
# 스케줄러, 아니면 사람이 직접. tmux 로 붙들고 있지 않는 이유가 이것이다:
# tmux 는 터미널 다중화 도구고, 프로세스 감독 도구가 아니다. tmux 서버가
# 죽으면 안에 있던 것이 조용히 같이 사라진다.
set -uo pipefail
cd "$(dirname "$0")" || exit 1

[ -f monitor.env ] && set -a && . ./monitor.env && set +a

exec python3 "$PWD/hub.py" --port "${MONITOR_PORT:-8787}" --host "${MONITOR_HOST:-0.0.0.0}"
