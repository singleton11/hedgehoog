#!/bin/bash
set -euo pipefail

cleanup_release_images() {
    local current=$1 rollback=$2 containers container used_ids='' images ref image_id
    containers=$(docker container ls --all --quiet)
    for container in $containers; do
        used_ids+="$(docker container inspect --format '{{.Image}}' "$container")"$'\n'
    done
    images=$(docker image ls --no-trunc --format '{{.Repository}}:{{.Tag}} {{.ID}}')
    while read -r ref image_id; do
        [[ $ref =~ ^hedgehoog-(backend|web):([a-f0-9]{40})$ ]] || continue
        [[ ${BASH_REMATCH[2]} != "$current" && ${BASH_REMATCH[2]} != "$rollback" ]] || continue
        # Protect every container's image, including stopped containers and alias tags.
        [[ $'\n'$used_ids != *$'\n'"$image_id"$'\n'* ]] || continue
        docker image rm "$ref"
    done <<< "$images"
}

record_successful_release() {
    local sha=$1 previous
    previous=$(cat deployed-sha 2>/dev/null || true)
    if [[ $previous =~ ^[a-f0-9]{40}$ && $previous != "$sha" ]]; then
        printf '%s\n' "$previous" > rollback-sha
    fi
    printf '%s\n' "$sha" > deployed-sha
}

install_release() {
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
    record_successful_release "$sha"
    rollback=$(cat rollback-sha 2>/dev/null || true)
    cleanup_release_images "$sha" "$rollback"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    install_release
fi
