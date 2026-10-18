#!/usr/bin/env bash
# Start a real FalkorDB server WITHOUT Docker, for machines where Docker is unavailable.
# Uses the redis-server binary and falkordb.so module bundled in the falkordblite wheel.
# This is a development convenience; the documented primary path is compose.yaml.
#
# Usage:  FALKORDB_PORT=<port> FALKORDB_PASSWORD=<password> scripts/dev_server_no_docker.sh start|stop|status
set -euo pipefail

: "${FALKORDB_PORT:?set FALKORDB_PORT}"
: "${FALKORDB_PASSWORD:?set FALKORDB_PASSWORD}"
ROOT="${MONEY_TRAIL_DEV_SERVER_DIR:-$(pwd)/.dev-server}"
VENV="$ROOT/venv"
DATA="$ROOT/data-$FALKORDB_PORT"
PIDFILE="$DATA/redis.pid"

bin_dir() { "$VENV/bin/python" -c 'import os, redislite; print(os.path.join(os.path.dirname(redislite.__file__), "bin"))'; }

case "${1:-}" in
  start)
    if [ ! -x "$VENV/bin/python" ]; then
      python3 -m venv "$VENV"
      "$VENV/bin/pip" install --quiet "falkordblite==0.10.0"
    fi
    mkdir -p "$DATA"
    B="$(bin_dir)"
    "$B/redis-server" --port "$FALKORDB_PORT" --bind 127.0.0.1 \
      --requirepass "$FALKORDB_PASSWORD" --loadmodule "$B/falkordb.so" \
      --dir "$DATA" --pidfile "$PIDFILE" --daemonize yes --logfile "$DATA/server.log"
    for _ in $(seq 1 30); do
      if "$VENV/bin/python" - <<PY 2>/dev/null
import redis, os
redis.Redis(host="127.0.0.1", port=int(os.environ["FALKORDB_PORT"]), password=os.environ["FALKORDB_PASSWORD"]).ping()
PY
      then echo "FalkorDB server accepting connections (loopback only)."; exit 0; fi
      sleep 0.5
    done
    echo "Server did not become ready; see $DATA/server.log" >&2; exit 1 ;;
  stop)
    [ -f "$PIDFILE" ] && kill "$(cat "$PIDFILE")" && echo "stopped" || echo "not running" ;;
  status)
    [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null && echo "running" || { echo "not running"; exit 1; } ;;
  *) echo "usage: $0 start|stop|status" >&2; exit 2 ;;
esac
