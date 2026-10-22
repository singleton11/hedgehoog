#!/bin/bash
set -euo pipefail
exec 9>/opt/hedgehoog/deploy.lock
flock -w 900 9
stage=$(mktemp -d /opt/hedgehoog/release.XXXXXX)
trap 'rm -rf "$stage"' EXIT
tar -xf - -C "$stage" --no-same-owner
cd "$stage"
[[ $(cat release-sha) =~ ^[a-f0-9]{40}$ ]]
sha=$(cat release-sha)
docker load -i images.tar
install -m 644 compose.yaml /opt/hedgehoog/compose.yaml
install -m 644 mongo-init.js /opt/hedgehoog/mongo-init.js
printf 'RELEASE_SHA=%s\n' "$sha" > /opt/hedgehoog/.release.env
cd /opt/hedgehoog
docker compose --env-file .env.production --env-file .release.env config --quiet
docker compose --env-file .env.production --env-file .release.env up -d --wait --wait-timeout 300
curl --fail --retry 10 --retry-delay 3 http://127.0.0.1/api/health
printf '%s\n' "$sha" > deployed-sha
# Keep running images and the previous release available for rollback.
docker image prune -f --filter until=168h
