#!/usr/bin/env bash
set -euo pipefail

compose_file="infra/docker/docker-compose.yml"
year="${YEAR:-2026}"
page_size="${PAGE_SIZE:-50}"

for segment in 81 43; do
  docker compose -f "$compose_file" run --rm worker-io \
    buenapro-worker historical-backfill \
    --segment "$segment" \
    --year "$year" \
    --page-size "$page_size"
done

