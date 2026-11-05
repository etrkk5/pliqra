# Operate and recover Pliqra

[← Pliqra](../README.md)

Configuration, persistent data, recovery and release boundaries for a self-hosted workspace.

## HTTPS and storage

Persist the whole `/data` volume. It contains SQLite/WAL data and `encryption.key`; neither alone is a complete recovery copy. Provider secrets, SAML private keys, optional captured content and cached responses use authenticated encryption. Keys and sessions are stored as hashes; passwords use salted scrypt. Startup refuses a missing/mismatched encryption key and a second process sharing the same volume.

| Variable | Default | Purpose |
|---|---|---|
| `PLIQRA_DATA_DIR` | `data` locally; `/data` in Docker | Persistent directory |
| `PLIQRA_HOST` / `PLIQRA_PORT` | `0.0.0.0` / `8080` | Listener |
| `PLIQRA_PUBLIC_URL` | Request base URL | Exact public origin for redirects, CSRF and federation |
| `PLIQRA_SECURE_COOKIES` | `0` | Set `1` for HTTPS |
| `PLIQRA_ENCRYPTION_KEY` | Generated persistent key | Optional externally managed Fernet key; preserve it |
| `PLIQRA_TLS_CERTFILE` / `PLIQRA_TLS_KEYFILE` | Unset | Native HTTPS certificate and private-key file paths |

HTTPS can use the native TLS settings with mounted certificate files, or your existing reverse proxy. Configure the exact public URL and secure cookies. Forwarded headers are not trusted automatically. The default loopback port mapping is intended for local setup; create the administrator before exposing the instance to a wider network.

## Backup and recovery

In **Operations**, download a password-encrypted backup and keep its password separately. It includes a consistent database snapshot and its encryption key. Restore requires typing `RESTORE`, no active inference/evaluation, and signs everyone out. A local pre-restore snapshot and crash marker recover an interrupted restore. When `PLIQRA_ENCRYPTION_KEY` is externally supplied, it must match the archive's key.

The following offline commands work for the Compose installation. Stop the service first; the temporary recovery container automatically uses the same volume and non-root user. Passwords are entered interactively:

```sh
docker compose stop pliqra
docker compose run --rm --no-deps pliqra python -m pliqra check
docker compose run --rm --no-deps pliqra python -m pliqra backup --file /data/workspace.mpb
docker compose run --rm --no-deps pliqra python -m pliqra restore --file /data/workspace.mpb
docker compose run --rm --no-deps pliqra python -m pliqra reset-password --email owner@example.com
docker compose up -d pliqra
```

Change an account's own password in **Settings & API**. Administrators can revoke member sessions and rotate application, provisioning and monitoring tokens. At least one active local administrator must remain.

## Usage and operating limits

Costs are configured-price **estimates**, not invoices. Each attempted upstream call is accounted separately. Missing provider usage or pricing remains unknown, including partially known fallback totals. Budgeted calls require pricing and atomically reserve a conservative allowance for possible attempts. Unknown charges keep the remaining reservation until an administrator reconciles them, with an audit note. Budgets use UTC calendar months and do not guarantee final provider billing. Cached calls use zero upstream billable tokens; the returned cached response retains its original provider usage and includes `pliqra.cache_hit`.

Runtime retention defaults to 90 days and cannot be shorter than 32 days, preserving current-month budgets. Pending requests and unresolved reservations are retained. Captured content follows its request's retention; test sets and explicitly saved evaluation outputs remain until deleted. Prometheus `/metrics` uses a separate monitoring bearer token created in **Operations**.

The supported deployment is **one workspace, one process, one persistent volume**. Project isolation is authorization inside that workspace; it is not separate physical tenants. Horizontal scaling, distributed quotas and PostgreSQL are not in this release. Chat completions are a supported subset: no multi-choice `n>1`, Responses API, embeddings, audio, model training or model deployment management. OpenAI-compatible function calling/structured output is supported; native Ollama tool-loop/stream shapes require adapter-specific conformance before use. Gateway policies compose local, NeMo check-API and custom HTTP guardrails; local patterns are not a complete DLP or semantic safety engine.

## Upgrade an earlier Model Port installation

Pliqra is the new name across the application, Python package, SDK, environment variables, response headers and protocol discovery. The canonical entry points are `python -m pliqra`, `sdk/pliqra_client.py`, `PLIQRA_*`, `X-Pliqra-*` and `/.well-known/pliqra`.

Before upgrading, take an encrypted backup, stop the old container, and attach its existing data volume to Pliqra. The database is checkpointed and atomically renamed from `modelport.sqlite3` to `pliqra.sqlite3`; accounts, services, keys, history and encrypted credentials are retained. An ambiguous volume containing both database names is rejected. Keep the encryption key with its volume.

Existing encrypted backups and portable service exports remain importable. Existing hashed application keys remain valid. Browser sessions should sign in again after the rename. Legacy `MODELPORT_*` environment variables are accepted as a fallback where the corresponding canonical variable is not set; update deployment settings to `PLIQRA_*` explicitly, including Docker/Compose environment values.

For an existing standalone installation using the old volume name:

```sh
docker stop modelport
docker run -d --name pliqra \
  -p 127.0.0.1:8088:8080 \
  -v modelport-data:/data \
  --add-host host.docker.internal:host-gateway \
  --restart unless-stopped pliqra:1.1.0
```

The volume name is an existing storage reference, not the product name. Do not run the old and new processes on the same volume simultaneously. Restore the pre-upgrade backup before returning to the old build.
