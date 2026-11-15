# Production deployment on the shared Caddy host

Hedgehoog runs as the Docker Compose project `hedgehoog-production`. Docker runs
PostgreSQL and the four Rust processes; the frontend is a Vite static build served
directly by the host's shared Caddy. There is no frontend nginx container.

## Runtime layout

- `/opt/hedgehoog/compose.yaml`: installed production Compose file.
- `/opt/hedgehoog/.env.production`: root-owned secrets, mode `0600`.
- `/opt/hedgehoog/.release.env`: currently selected immutable release SHA.
- `/opt/hedgehoog/static/releases/<sha>/`: immutable frontend builds.
- `/opt/hedgehoog/static/current`: atomic symlink used by Caddy.
- `/opt/hedgehoog/deployed-sha`: current successful release.
- `/opt/hedgehoog/rollback-sha`: previous successful distinct release.

The API is published only on `127.0.0.1:8180`. PostgreSQL has no published
production port. Existing MongoDB data and volumes are not referenced, migrated,
or deleted.

## One-time Caddy setup

Mount the static release root into the shared Caddy container:

```yaml
services:
  caddy:
    volumes:
      - /opt/hedgehoog/static:/srv/hedgehoog:ro
```

Use the site block in `deploy/Caddyfile.hedgehoog`. It serves the SPA from
`/srv/hedgehoog/current`, gives hashed assets immutable caching, and strips the
`/api` prefix before proxying requests to `127.0.0.1:8180`.

Validate the Caddy configuration before recreating the container. A new bind mount
requires container recreation rather than only a reload:

```sh
cd /home/singleton/services/caddy
docker compose config --quiet
docker compose run --rm caddy caddy validate --config /etc/caddy/Caddyfile
docker compose up -d --force-recreate caddy
```

## Production environment

Create `/opt/hedgehoog/.env.production` with mode `0600`. Required values are:

- `POSTGRES_PASSWORD`
- `JWT_SECRET`
- `ADMIN_PASSWORD`
- `ALLOWED_ORIGINS=https://hedgehoog.antonprokhorov.synology.me`

Keep `COOKIE_SECURE=true` and `ALLOW_INSECURE_HTTP=false` for the HTTPS site. The
admin account is seeded only when absent; changing `ADMIN_PASSWORD` later does not
rotate an existing password.

## Build a release bundle

From a clean checkout of the desired commit:

```sh
set -euo pipefail
sha=$(git rev-parse HEAD)
npm --prefix frontend ci
npm --prefix frontend run build
docker build -f backend-rust/Dockerfile -t "hedgehoog-backend:$sha" .
rm -rf bundle
mkdir bundle
docker save "hedgehoog-backend:$sha" -o bundle/images.tar
tar -C frontend/dist -czf bundle/frontend.tar.gz .
cp deploy/compose.yaml bundle/
printf '%s\n' "$sha" > bundle/release-sha
tar -C bundle -cf "hedgehoog-release-$sha.tar" \
  images.tar frontend.tar.gz compose.yaml release-sha
```

The GitHub `Build release bundle` workflow produces the same archive as a workflow
artifact; it does not connect to the host.

## Install

Install `deploy/install-release.sh` as a privileged local command, then stream the
bundle to it:

```sh
sudo install -d -m 0755 /opt/hedgehoog
sudo install -m 0755 deploy/install-release.sh /usr/local/sbin/hedgehoog-deploy
sudo /usr/local/sbin/hedgehoog-deploy < "hedgehoog-release-$sha.tar"
```

The installer serializes rollouts, prepares the static release before changing
traffic, loads the backend image, waits for migrations and all Compose health
checks, verifies the loopback API, atomically switches the static symlink, and only
then records success. It keeps the current and previous backend/static releases and
removes unreferenced legacy `hedgehoog-web:<sha>` images.

## Verification

```sh
cd /opt/hedgehoog
docker compose --env-file .env.production --env-file .release.env ps
docker compose --env-file .env.production --env-file .release.env logs --tail 100
curl --fail http://127.0.0.1:8180/health
curl --fail https://hedgehoog.antonprokhorov.synology.me/api/health
curl --fail https://hedgehoog.antonprokhorov.synology.me/
cat deployed-sha
```

Do not use `docker compose down -v`; that deletes the PostgreSQL volume. Roll back
by redeploying the previous release bundle identified by `rollback-sha`. Static
files are switched only after the old API has been replaced successfully, so keep
both bundles available until post-deployment verification is complete.
