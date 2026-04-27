# Deploying OpenAdServer

OpenAdServer prod runs on the Azure VM `20.185.219.8` (port `2222`, user
`gregcohen`) at `/opt/openadserver/`. Deploy is a direct git-pull on the VM
followed by a container restart — there is no GHCR image registry.

## TL;DR

For a code-only change (Python under `liteads/`):

```bash
ssh -i ~/.ssh/id_ed25519_workgh -p 2222 gregcohen@20.185.219.8 \
  "cd /opt/openadserver && git pull && docker compose -f docker-compose.custom.yml restart adserver"
```

That is the whole deploy. No image build, no `up -d --build`.

## Why no build

`docker-compose.custom.yml` bind-mounts the source code into the container:

```yaml
volumes:
  - /opt/openadserver/liteads:/app/liteads:ro
```

So `git pull` puts the new files in front of the running container
instantly. The container only needs to be restarted so the Python process
re-imports the modules.

Rebuild only if one of these changes:

- `pyproject.toml` (Python dependencies)
- `deployment/docker/Dockerfile.light` (the prod Dockerfile)
- Anything outside `liteads/` that the image embeds at build time

For those cases use `docker compose -f docker-compose.custom.yml up -d --build adserver`.

## Critical: use the right compose file

The repo contains two compose files at the root. They are NOT
interchangeable:

| File                          | Purpose                                                        |
| ----------------------------- | -------------------------------------------------------------- |
| `docker-compose.yml`          | Local dev stack. Spins up its own postgres, exposes host ports.|
| `docker-compose.custom.yml`   | Prod stack on the VM. Connects to `n8n-postgres`, no host ports beyond the Caddy upstream, container names without `-1` suffix. |

**Never run `docker compose up` against `docker-compose.yml` from
`/opt/openadserver/`.** Both files share the project name `openadserver`,
so the dev stack will hijack the `openadserver-redis` container name and
break the prod adserver's cache connection (`Temporary failure in name
resolution for openadserver-redis`). If this happens, recover with:

```bash
docker stop openadserver-ad-server-1 openadserver-redis-1 openadserver-postgres-1
docker rm   openadserver-ad-server-1 openadserver-redis-1 openadserver-postgres-1
docker network rm openadserver_liteads-network
docker compose -f docker-compose.custom.yml up -d redis
docker compose -f docker-compose.custom.yml restart adserver
```

Always pass `-f docker-compose.custom.yml` when running compose on the VM.

## Prod container layout

| Container                   | Service        | Networks                                  |
| --------------------------- | -------------- | ----------------------------------------- |
| `openadserver-adserver`     | ad server      | `portainer-proxy_proxy`, `internal`       |
| `openadserver-admin-api`    | admin FastAPI  | `portainer-proxy_proxy`, `openadserver_internal` |
| `openadserver-redis`        | redis cache    | `internal`                                |

Caddy routes:

- `media.aslalabs.org` → `openadserver-adserver` (port 8000 inside)
- `ads.aslalabs.org` → admin dashboard + `openadserver-admin-api` (`/api/*`)

The admin dashboard and admin-api have their own compose files at
`/opt/openadserver/admin-api/docker-compose.yml` and
`/opt/openadserver/admin-dashboard/docker-compose.yml`. They are deployed
separately from the ad server.

## Common gotchas

**`git pull` aborts on local changes.** Files have historically been edited
directly on the VM. If pull fails with "Your local changes to the following
files would be overwritten", first inspect:

```bash
git status --short
git diff origin/main -- <file>
```

If the local changes are already represented upstream (someone committed
them from elsewhere), stash and pull:

```bash
git stash push -m "pre-deploy YYYY-MM-DD: <reason>"
git pull
```

If the changes are genuinely unique work, stop and figure out what they
are before throwing them away — don't reset --hard without understanding
what you're discarding.

**Cache stays hot through a restart.** Restarting the adserver does not
clear the redis cache. Active campaigns are cached for 5 minutes. If the
deploy changes campaign-eligibility logic, expect up to 5 minutes of
mixed-version behavior before all entries expire, or flush manually:

```bash
docker exec openadserver-redis redis-cli FLUSHDB
```

**Verifying the new code is loaded.** Bind mount + restart can be checked
by grepping the running container directly:

```bash
docker exec openadserver-adserver grep -c "<some_new_symbol>" /app/liteads/<path>
docker logs --tail 20 openadserver-adserver
curl -sS https://media.aslalabs.org/health
```
