#!/usr/bin/env bash
# Start (or resume) the perf Virtuoso on :8891 with all 100,000 patients. Pass "load" to load them
# (about 80 minutes the first time).
#   RELEASE_DIR=../hackathon-virtuoso-release PERF_PATIENTS_DIR=../data/patients-100k.noindex ./infra/virtuoso/perf.sh load
set -euo pipefail
: "${RELEASE_DIR:?Set RELEASE_DIR to the organizer-provided hackathon-virtuoso-release folder}"
: "${PERF_PATIENTS_DIR:?Set PERF_PATIENTS_DIR to the folder with the 100k patient .ttl files}"
export CONTAINER_NAME=virtuoso-perf HTTP_HOST_PORT=8891 ISQL_HOST_PORT=1112
export PATIENTS_DIR="$PERF_PATIENTS_DIR" VIRTUOSO_RAM_GB="${VIRTUOSO_RAM_GB:-18}"
"$RELEASE_DIR/start.sh"
if [ "${1:-}" = "load" ]; then
  "$RELEASE_DIR/load-patients.sh"
fi
