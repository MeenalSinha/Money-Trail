#!/usr/bin/env bash
# Money Trail -- Reproducible one-shot setup script (Linux / macOS / WSL).
#
# Prerequisites:
#   - Python 3.10-3.13 on PATH
#   - Docker (with compose plugin or docker-compose v2)
#   - A .env file (or .env.example configured with real credentials)
#
# Usage:
#   bash scripts/setup.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOTAL=7

step() { printf "\n[%s/%s] %s\n" "$1" "$TOTAL" "$2"; }
abort() { printf "\nERROR: %s\n" "$1" >&2; exit 1; }

# 1. Python
step 1 "Checking Python"
PYTHON=""
for candidate in python3 python; do
  if command -v "$candidate" &>/dev/null; then
    ver=$("$candidate" --version 2>&1 | grep -Eo 'Python [0-9]+\.[0-9]+')
    major=$(echo "$ver" | awk -F'[ .]' '{print $2}')
    minor=$(echo "$ver" | awk -F'[ .]' '{print $3}')
    if [ "$major" -eq 3 ] && [ "$minor" -ge 10 ] && [ "$minor" -le 13 ]; then
      PYTHON="$candidate"
      echo "  Found: $ver"
      break
    fi
  fi
done
[ -z "$PYTHON" ] && abort "Python 3.10-3.13 not found on PATH."

# 2. Virtual environment
step 2 "Creating virtual environment (.venv)"
VENV="$ROOT/.venv"
if [ ! -d "$VENV" ]; then
  "$PYTHON" -m venv "$VENV"
  echo "  Created .venv"
else
  echo "  .venv already exists; skipping creation"
fi
PIP="$VENV/bin/pip"
echo "  Installing money-trail[dev] ..."
"$PIP" install --quiet -e "$ROOT/.[dev]"
echo "  Packages installed."

# 3. Environment file
step 3 "Checking environment file"
if [ ! -f "$ROOT/.env" ]; then
  if [ -f "$ROOT/.env.example" ]; then
    cp "$ROOT/.env.example" "$ROOT/.env"
    echo "  Copied .env.example -> .env"
    echo "  !! Edit .env: set FALKORDB_HOST, FALKORDB_PORT, FALKORDB_PASSWORD,"
    echo "     FALKORDB_GRAPH_NAME, and MONEY_TRAIL_TEST_GRAPH_PREFIX."
  else
    echo "  WARNING: no .env or .env.example found. Create .env manually."
  fi
else
  echo "  .env already exists; skipping copy"
fi

# 4. Start FalkorDB
step 4 "Starting FalkorDB (docker compose)"
cd "$ROOT"
if docker compose version &>/dev/null 2>&1; then
  docker compose up -d
elif docker-compose version &>/dev/null 2>&1; then
  docker-compose up -d
else
  abort "Neither 'docker compose' nor 'docker-compose' found. Install Docker."
fi

# 5. Wait for readiness
step 5 "Waiting for FalkorDB to be ready (up to 60s)"
CHECK="$VENV/bin/money-trail-check"
ready=0
for i in $(seq 1 12); do
  sleep 5
  if "$CHECK" &>/dev/null 2>&1; then
    ready=1
    echo "  FalkorDB ready after ~$((i*5))s"
    break
  fi
  echo "  Attempt $i/12 -- not ready yet ..."
done
[ $ready -eq 0 ] && abort "FalkorDB did not become ready within 60s. Check docker compose logs."

# 6. Initialize schema
step 6 "Initializing / migrating graph schema"
"$VENV/bin/money-trail-init"

# 7. Health check
step 7 "Final health check"
"$CHECK"

printf "\n%s\n" "============================================================"
printf "  Money Trail setup complete!\n"
printf "%s\n" "============================================================"
printf "\n  Next steps:\n"
printf "    Activate env : source .venv/bin/activate\n"
printf "    Run demo     : money-trail demo\n"
printf "    Run QA suite : money-trail qa --output-dir evaluation_reports\n"
printf "    Launch UI    : money-trail dashboard\n"
printf "    Run tests    : pytest\n\n"
