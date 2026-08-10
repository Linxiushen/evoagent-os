# Local Docker Compose deployment

This directory runs a **single-host v0.1 development preview**. It is suitable for local evaluation, not public or multi-tenant production.

## Core profile

The default profile starts:

- `control-plane`: integrated local Runtime + Fleet + Forge data path on port 8800
- `harnesslab`: standalone trace-regression workbench on port 4318

```bash
cp deploy/.env.example deploy/.env
# Replace each CHANGE_ME value with an independent random value.
docker compose --env-file deploy/.env -f deploy/compose.demo.yml up --build
```

Generate a value with Python:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Open `http://127.0.0.1:8800` and supply `EVOAGENT_OS_TOKEN` when the client requests it. The API is under `/api/v1`, health is `/health`, and OpenAPI is `/docs`.

## Optional profiles

Start the standalone component consoles as well:

```bash
docker compose --env-file deploy/.env -f deploy/compose.demo.yml \
  --profile components up --build
```

Add the fictional, offline realtime demo:

```bash
docker compose --env-file deploy/.env -f deploy/compose.demo.yml \
  --profile realtime up --build
```

Profiles can be combined. The standalone Runtime/Fleet/Forge services use their own volumes and do **not** share state with the integrated control plane.

## Verify

```bash
curl --fail http://127.0.0.1:8800/health
curl --fail http://127.0.0.1:4318/healthz
```

For authenticated control-plane calls:

```bash
curl --fail-with-body http://127.0.0.1:8800/api/v1/overview \
  -H "Authorization: Bearer $EVOAGENT_OS_TOKEN"
```

The services publish to `127.0.0.1` only. `ECHOWEAVE_ALLOW_INSECURE_PRIVATE_TRANSPORT=true` is present solely because the host publication is loopback-bound. Do not change the published address without TLS/WSS, an authenticated edge, exact origin policy and the controls in [`docs/SECURITY.md`](../docs/SECURITY.md).

## State and cleanup

Named volumes preserve state across container replacement. List them before any cleanup:

```bash
docker compose --env-file deploy/.env -f deploy/compose.demo.yml ps
docker volume ls --filter label=com.docker.compose.project=evoagent-os
```

`docker compose down` stops containers but retains named volumes. `docker compose down --volumes` permanently deletes local databases, workspaces, artifacts, registry and consent state; use it only when that exact data is disposable.

Follow [`docs/OPERATIONS.md`](../docs/OPERATIONS.md) for consistent backup and restore. Never commit `deploy/.env`.

## Production gaps

This Compose file does not provide enterprise SSO/RBAC, tenant isolation, worker identity, distributed rate limiting, shared transactional storage, object storage, external audit retention, secret management, signed images or a production reverse proxy. Those are required deployment work, not hidden optional settings.
