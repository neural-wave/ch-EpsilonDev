#!/usr/bin/env bash
# Start (or resume) the dev Virtuoso on :8890. Pass "load" to (re)load the patients folder.
#   RELEASE_DIR=../hackathon-virtuoso-release ./infra/virtuoso/dev.sh load
set -euo pipefail
: "${RELEASE_DIR:?Set RELEASE_DIR to the organizer-provided hackathon-virtuoso-release folder}"
export VIRTUOSO_RAM_GB="${VIRTUOSO_RAM_GB:-8}"
"$RELEASE_DIR/start.sh"
if [ "${1:-}" = "load" ]; then
  "$RELEASE_DIR/load-patients.sh"
fi
