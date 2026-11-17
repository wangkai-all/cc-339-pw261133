#!/usr/bin/env bash
# Stop the four services by matching their module command lines.
set -euo pipefail
for mod in services.receiver.app services.extractor.app \
           services.review.app services.warehouse.app; do
  pids=$(pgrep -f "python3 -m ${mod}" || true)
  if [ -n "${pids}" ]; then
    kill ${pids} && echo "stopped ${mod} (pid ${pids})"
  else
    echo "${mod} not running"
  fi
done
