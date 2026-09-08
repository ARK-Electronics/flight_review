#!/bin/bash
set -euo pipefail
mkdir -p "$RUNNER_TEMP/flight-data"
sudo chown 10001:10001 "$RUNNER_TEMP/flight-data"
trap 'docker logs flight-smoke; docker rm -f flight-smoke >/dev/null' EXIT
docker run -d --name flight-smoke -p 8080:8080 \
  -v "$RUNNER_TEMP/flight-data:/data" \
  -e FLIGHT_REVIEW_ENV=production -e COOKIE_SECRET="$(openssl rand -hex 32)" flight-review:test
for attempt in $(seq 1 60); do
  if curl --fail --silent http://localhost:8080/readyz > "$RUNNER_TEMP/ready.json"; then break; fi
  sleep 2
done
python -c 'import json,os; assert json.load(open(os.environ["RUNNER_TEMP"]+"/ready.json"))["revision"] == os.environ["GITHUB_SHA"]'
curl --fail --silent http://localhost:8080/browse >/dev/null
test "$(docker exec flight-smoke id -u)" = 10001
docker restart flight-smoke
sleep 5
curl --fail --silent --retry 10 --retry-all-errors --retry-delay 2 http://localhost:8080/readyz
