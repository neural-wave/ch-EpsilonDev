#!/usr/bin/env bash
# Control panel for the run machine: the computer with Virtuoso and the SSH tunnel to the GPU.
# Teammates code elsewhere and push branches; this machine runs the heavy checks.
#
#   scripts/runbox.sh up            start Virtuoso dev, the GPU tunnel and the API/demo (skips what runs)
#   scripts/runbox.sh down          stop the API/demo and the tunnel (Virtuoso keeps running)
#   scripts/runbox.sh restart       down + up, e.g. after git pull
#   scripts/runbox.sh status        what is running
#   scripts/runbox.sh test [--branch NAME]
#   scripts/runbox.sh eval [--execute] [--branch NAME]
#
# --branch runs a teammate's branch in a separate git worktree, so the running demo is untouched.
# Needs GPU_SSH_HOST and GPU_SSH_PORT in .env (see .env.example).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$REPO/.venv/bin/python"
LOGS="$REPO/logs"
WORKTREES="$(dirname "$REPO")/nl2sparql-worktrees"
TUNNEL_PORT=18000
mkdir -p "$LOGS"

set -a
[ -f "$REPO/.env" ] && . "$REPO/.env"
set +a
API_PORT="${API_PORT:-8080}"

listening() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }

start_virtuoso() {
  colima status >/dev/null 2>&1 || colima start
  docker start hackathon-virtuoso >/dev/null && echo "virtuoso dev: running on :8890"
}

start_tunnel() {
  if listening "$TUNNEL_PORT"; then echo "gpu tunnel: already on :$TUNNEL_PORT"; return; fi
  : "${GPU_SSH_HOST:?set GPU_SSH_HOST (e.g. root@1.2.3.4) in .env}" "${GPU_SSH_PORT:?set GPU_SSH_PORT in .env}"
  nohup bash -c "while true; do ssh -N -p '$GPU_SSH_PORT' -o BatchMode=yes -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -L $TUNNEL_PORT:127.0.0.1:18000 '$GPU_SSH_HOST'; sleep 5; done" \
    >"$LOGS/tunnel.log" 2>&1 &
  echo $! >"$LOGS/tunnel.pid"
  echo "gpu tunnel: started (reconnects by itself), log in logs/tunnel.log"
}

start_api() {
  if listening "$API_PORT"; then echo "api/demo: already on :$API_PORT"; return; fi
  (cd "$REPO" && nohup "$PY" -m nl2sparql --port "$API_PORT" >"$LOGS/api.log" 2>&1 & echo $! >"$LOGS/api.pid")
  echo "api/demo: started on http://localhost:$API_PORT (backend: ${NL2SPARQL_BACKEND:-stub}), log in logs/api.log"
}

stop_pidfile() {
  local file="$LOGS/$1.pid"
  if [ -f "$file" ]; then
    pkill -P "$(cat "$file")" 2>/dev/null || true
    kill "$(cat "$file")" 2>/dev/null || true
    rm -f "$file"
  fi
}

status() {
  colima status >/dev/null 2>&1 && echo "colima: running" || echo "colima: stopped"
  docker ps --format '  container {{.Names}}: {{.Status}}' 2>/dev/null || true
  curl -sf "http://localhost:$TUNNEL_PORT/v1/models" >/dev/null && echo "gpu tunnel: up" || echo "gpu tunnel: down"
  curl -sf "http://localhost:$API_PORT/health" || echo "api/demo: down"
  echo
}

# Prints the directory to run in: the repo itself, or a worktree checked out at the branch.
checkout() {
  local branch="${1:-}"
  if [ -z "$branch" ]; then echo "$REPO"; return; fi
  git -C "$REPO" fetch --quiet origin "$branch" 2>/dev/null || true
  local ref="origin/$branch"
  git -C "$REPO" rev-parse --verify --quiet "$ref" >/dev/null || ref="$branch"
  local dir="$WORKTREES/${branch//\//-}"
  if [ -d "$dir" ]; then
    git -C "$dir" checkout --quiet --detach "$ref"
  else
    mkdir -p "$WORKTREES"
    git -C "$REPO" worktree add --quiet --detach "$dir" "$ref"
  fi
  echo "$dir"
}

run_in() {  # run_in <dir> <command...>: the dir's own src/ wins over the editable install
  local dir="$1"; shift
  (cd "$dir" && PYTHONPATH="$dir/src" "$@")
}

command="${1:-status}"; shift || true
branch=""; execute=""
while [ $# -gt 0 ]; do
  case "$1" in
    --branch) branch="$2"; shift 2 ;;
    --execute) execute="--execute"; shift ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done

case "$command" in
  up) start_virtuoso; start_tunnel; start_api ;;
  down) stop_pidfile api; stop_pidfile tunnel; echo "api/demo and gpu tunnel stopped" ;;
  restart) stop_pidfile api; stop_pidfile tunnel; sleep 1; start_virtuoso; start_tunnel; start_api ;;
  status) status ;;
  test) dir="$(checkout "$branch")"; run_in "$dir" "$PY" -m pytest -q ;;
  eval) dir="$(checkout "$branch")"; run_in "$dir" "$PY" "$dir/eval/run_eval.py" $execute ;;
  *) sed -n '2,13p' "$0"; exit 2 ;;
esac
