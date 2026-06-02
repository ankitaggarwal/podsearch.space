#!/usr/bin/env bash
# Pull the latest main and restart the containers. Run on the server.
set -euo pipefail
cd "$(dirname "$0")"

git fetch origin main
git reset --hard origin/main
docker compose up -d --build
docker image prune -f
echo "Deployed $(git rev-parse --short HEAD)"
