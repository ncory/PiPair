#!/usr/bin/env bash
# Copy this checkout to a Pi and install (or update) PiPair there.
#
#   ./scripts/push.sh nate@pipair-head.local head
#   ./scripts/push.sh nate@pipair.local controller
#
# The role is only needed the first time; later runs keep it. Asks for the
# Pi's sudo password. For development: it installs what is in this folder,
# committed or not.
set -euo pipefail

HOST="${1:?usage: push.sh user@host [head|controller]}"
ROLE="${2:-}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"

rsync -az --delete --exclude .git --exclude .venv --exclude dev-data --exclude data \
  --exclude '__pycache__' --exclude .pytest_cache --exclude .claude --exclude .DS_Store \
  "$SRC/" "$HOST:pipair/"
# shellcheck disable=SC2029  # $ROLE is meant to expand here; \$USER on the Pi
ssh -t "$HOST" "sudo ~/pipair/scripts/install.sh --deploy-user \"\$USER\" ${ROLE:+--role $ROLE}"
