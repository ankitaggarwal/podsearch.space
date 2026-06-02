#!/usr/bin/env bash
# Pull the latest code and (re)start everything. Run this on the server, from the
# repo directory, whenever you want to deploy the newest commit by hand.
# (For unattended deploys, a cron runs auto-update.sh — see DEPLOY.md.)
set -euo pipefail

cd "$(dirname "$0")"

echo "==> Pulling latest code"
git pull

echo "==> Rebuilding and restarting containers"
docker compose up -d --build

echo "==> Cleaning up old images"
docker image prune -f

echo "==> Done. Current status:"
docker compose ps
