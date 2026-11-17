#!/usr/bin/env bash
# Start all four services as separate processes (stdlib only).
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD"
mkdir -p data logs

start() {
  local name=$1 mod=$2 port=$3
  if curl -s "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
    echo "$name already running on $port"
    return
  fi
  python3 -m "$mod" > "logs/${name}.log" 2>&1 &
  echo "$name pid $! -> http://127.0.0.1:${port}"
  sleep 0.5
}

start receiver  services.receiver.app  8011
start extractor services.extractor.app 8012
start review    services.review.app    8013
start warehouse services.warehouse.app 8014

sleep 1
echo
echo "Review UI:  http://127.0.0.1:8013/ui/"
for p in 8011 8012 8013 8014; do
  curl -s "http://127.0.0.1:${p}/health" && echo " <- :${p}"
done
