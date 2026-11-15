#!/bin/bash
set -euo pipefail

HEDGEHOOG_ROOT=${HEDGEHOOG_ROOT:-/opt/hedgehoog}
HEDGEHOOG_STATIC_ROOT=${HEDGEHOOG_STATIC_ROOT:-$HEDGEHOOG_ROOT/static}

cleanup_release_images() {
    local current=$1 rollback=$2 containers container used_ids='' images ref image_id repository sha
    containers=$(docker container ls --all --quiet)
    for container in $containers; do
        used_ids+="$(docker container inspect --format '{{.Image}}' "$container")"$'\n'
    done
    images=$(docker image ls --no-trunc --format '{{.Repository}}:{{.Tag}} {{.ID}}')
    while read -r ref image_id; do
        [[ $ref =~ ^hedgehoog-(backend|web):([a-f0-9]{40})$ ]] || continue
        repository=${BASH_REMATCH[1]}
        sha=${BASH_REMATCH[2]}
        if [[ $repository == backend && ($sha == "$current" || $sha == "$rollback") ]]; then
            continue
        fi
        # Protect every container's image, including stopped containers and alias tags.
        [[ $'\n'$used_ids != *$'\n'"$image_id"$'\n'* ]] || continue
        docker image rm "$ref"
    done <<< "$images"
}

prepare_static_release() {
    local sha=$1 archive=$2 release_dir stage
    [[ $sha =~ ^[a-f0-9]{40}$ ]]
    release_dir="$HEDGEHOOG_STATIC_ROOT/releases/$sha"
    if [[ -d $release_dir ]]; then
        [[ -f $release_dir/index.html ]]
        return
    fi

    mkdir -p "$HEDGEHOOG_STATIC_ROOT/releases"
    stage=$(mktemp -d "$HEDGEHOOG_STATIC_ROOT/releases/.${sha}.XXXXXX")
    tar -xzf "$archive" -C "$stage" --no-same-owner
    [[ -f $stage/index.html ]]
    find "$stage" -type d -exec chmod 755 {} +
    find "$stage" -type f -exec chmod 644 {} +
    mv "$stage" "$release_dir"
}

activate_static_release() {
    local sha=$1 link
    [[ -f $HEDGEHOOG_STATIC_ROOT/releases/$sha/index.html ]]
    link="$HEDGEHOOG_STATIC_ROOT/.current.$$.tmp"
    rm -f "$link"
    ln -s "releases/$sha" "$link"
    mv -Tf "$link" "$HEDGEHOOG_STATIC_ROOT/current"
}

cleanup_static_releases() {
    local current=$1 rollback=$2 directory sha
    [[ -d $HEDGEHOOG_STATIC_ROOT/releases ]] || return 0
    while IFS= read -r -d '' directory; do
        sha=${directory##*/}
        [[ $sha == "$current" || $sha == "$rollback" ]] || rm -rf "$directory"
    done < <(find "$HEDGEHOOG_STATIC_ROOT/releases" -mindepth 1 -maxdepth 1 -type d \
        -regextype posix-extended -regex '.*/[a-f0-9]{40}' -print0)
}

record_successful_release() {
    local sha=$1 previous
    previous=$(cat "$HEDGEHOOG_ROOT/deployed-sha" 2>/dev/null || true)
    if [[ $previous =~ ^[a-f0-9]{40}$ && $previous != "$sha" ]]; then
        printf '%s\n' "$previous" > "$HEDGEHOOG_ROOT/rollback-sha"
    fi
    printf '%s\n' "$sha" > "$HEDGEHOOG_ROOT/deployed-sha"
}

install_release() {
    local stage sha rollback
    mkdir -p "$HEDGEHOOG_ROOT"
    exec 9>"$HEDGEHOOG_ROOT/deploy.lock"
    flock -w 900 9
    stage=$(mktemp -d "$HEDGEHOOG_ROOT/release.XXXXXX")
    trap "rm -rf -- $(printf '%q' "$stage")" EXIT
    tar -xf - -C "$stage" --no-same-owner
    cd "$stage"
    [[ $(cat release-sha) =~ ^[a-f0-9]{40}$ ]]
    sha=$(cat release-sha)
    prepare_static_release "$sha" frontend.tar.gz
    docker load -i images.tar
    install -m 644 compose.yaml "$HEDGEHOOG_ROOT/compose.yaml"
    printf 'RELEASE_SHA=%s\n' "$sha" > "$HEDGEHOOG_ROOT/.release.env"
    cd "$HEDGEHOOG_ROOT"
    docker compose --env-file .env.production --env-file .release.env config --quiet
    docker compose --env-file .env.production --env-file .release.env up -d --wait --wait-timeout 300
    curl --fail --retry 10 --retry-delay 3 http://127.0.0.1:8180/health
    activate_static_release "$sha"
    record_successful_release "$sha"
    rollback=$(cat rollback-sha 2>/dev/null || true)
    cleanup_release_images "$sha" "$rollback"
    cleanup_static_releases "$sha" "$rollback"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    install_release
fi
