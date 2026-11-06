#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
umask 077
if [ ! -f .env.enterprise ]; then
  docker run --rm python:3.13-slim python -c 'import base64,secrets; print("PLIQRA_ENCRYPTION_KEY="+base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()); print("PLIQRA_DB_PASSWORD="+secrets.token_urlsafe(32))' > .env.enterprise.tmp
  mv .env.enterprise.tmp .env.enterprise
fi
docker compose --env-file .env.enterprise -f compose.enterprise.yaml up --build -d
