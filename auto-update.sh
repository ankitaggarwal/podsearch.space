#!/usr/bin/env bash
# Unattended pull-based deploy. A cron on the server runs this every minute or two.
# It only rebuilds when origin/main actually moved, so "push to main" IS the deploy:
# the cron notices the new commit on its next tick and rolls the containers.
#
# Example crontab line (every 2 minutes):
#   */2 * * * * /root/podsearch.space/auto-update.sh >> /var/log/podsearch-deploy.log 2>&1
set -euo pipefail

cd "$(dirname "$0")"

git fetch origin main --quiet

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

# Nothing new — exit quietly so the log doesn't fill up.
if [ "$LOCAL" = "$REMOTE" ]; then
    exit 0
fi

echo "==> $(date -u +%FT%TZ) new commit $REMOTE — deploying"
git reset --hard origin/main
docker compose up -d --build
docker image prune -f
echo "==> deploy complete: $(git rev-parse --short HEAD)"
