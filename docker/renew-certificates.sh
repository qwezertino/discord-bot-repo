#!/bin/sh
set -eu
# Invoke from the repository directory; no Docker socket is mounted in Certbot.
COMPOSE_SETTINGS=${1:-.deploy/compose.env}
docker compose --env-file "$COMPOSE_SETTINGS" --profile tls run --rm certbot renew --non-interactive --quiet
docker compose --env-file "$COMPOSE_SETTINGS" exec -T nginx nginx -t
docker compose --env-file "$COMPOSE_SETTINGS" exec -T nginx nginx -s reload
