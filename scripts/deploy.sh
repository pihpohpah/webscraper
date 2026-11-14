#!/usr/bin/env bash
# shellcheck disable=SC2029 # The validated DEPLOY_PATH intentionally expands on the client.
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

ENV_FILE="${DEPLOY_ENV_FILE:-deploy.env}"
if [[ ! -f "$ENV_FILE" ]]; then
    printf 'Missing %s. Copy deploy.env.example and fill in the deployment settings.\n' "$ENV_FILE" >&2
    exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

: "${EVENT_AGENT_IMAGE:?Set EVENT_AGENT_IMAGE in deploy.env}"
: "${DEPLOY_HOST:?Set DEPLOY_HOST in deploy.env}"
: "${DEPLOY_USER:?Set DEPLOY_USER in deploy.env}"
: "${DEPLOY_PATH:?Set DEPLOY_PATH in deploy.env}"

EVENT_AGENT_TAG="${EVENT_AGENT_TAG:-$(git rev-parse --short HEAD)}"
BUILD_PLATFORM="${BUILD_PLATFORM:-linux/amd64}"
DEPLOY_SSH_PORT="${DEPLOY_SSH_PORT:-22}"
DEPLOY_HEALTH_TIMEOUT="${DEPLOY_HEALTH_TIMEOUT:-60}"
SKIP_BUILD="${SKIP_BUILD:-0}"

if [[ ! "$EVENT_AGENT_IMAGE" =~ ^[a-z0-9][a-z0-9._:/-]*$ ]]; then
    printf 'Invalid EVENT_AGENT_IMAGE: %s\n' "$EVENT_AGENT_IMAGE" >&2
    exit 1
fi
if [[ ! "$EVENT_AGENT_TAG" =~ ^[[:alnum:]_.-]+$ ]]; then
    printf 'Invalid EVENT_AGENT_TAG: %s\n' "$EVENT_AGENT_TAG" >&2
    exit 1
fi
if [[ ! "$DEPLOY_HOST" =~ ^[[:alnum:]_.-]+$ ]]; then
    printf 'DEPLOY_HOST must be a hostname, IPv4 address, or SSH config alias.\n' >&2
    exit 1
fi
if [[ ! "$DEPLOY_USER" =~ ^[a-z_][a-z0-9_.-]*$ ]]; then
    printf 'Invalid DEPLOY_USER: %s\n' "$DEPLOY_USER" >&2
    exit 1
fi
if [[ ! "$DEPLOY_PATH" =~ ^/[[:alnum:]_./-]+$ ]]; then
    printf 'DEPLOY_PATH must be an absolute path without spaces or shell characters.\n' >&2
    exit 1
fi
if [[ ! "$DEPLOY_HEALTH_TIMEOUT" =~ ^[1-9][0-9]*$ ]]; then
    printf 'DEPLOY_HEALTH_TIMEOUT must be a positive integer.\n' >&2
    exit 1
fi
if [[ ! "$DEPLOY_SSH_PORT" =~ ^[1-9][0-9]*$ ]]; then
    printf 'DEPLOY_SSH_PORT must be a positive integer.\n' >&2
    exit 1
fi

for command_name in awk docker git ssh scp; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        printf 'Required command is not installed: %s\n' "$command_name" >&2
        exit 1
    fi
done

SSH_TARGET="${DEPLOY_USER}@${DEPLOY_HOST}"
SSH_OPTIONS=(-p "$DEPLOY_SSH_PORT")
SCP_OPTIONS=(-P "$DEPLOY_SSH_PORT")

ssh "${SSH_OPTIONS[@]}" "$SSH_TARGET" "install -d -m 700 -- '$DEPLOY_PATH'"
if ! ssh "${SSH_OPTIONS[@]}" "$SSH_TARGET" \
    "cd '$DEPLOY_PATH' && test -f .env && chmod 600 .env"; then
    printf 'Place the application secrets in %s:%s/.env with mode 0600, then retry.\n' \
        "$SSH_TARGET" "$DEPLOY_PATH" >&2
    exit 1
fi

if [[ "$SKIP_BUILD" != "1" ]]; then
    docker buildx build \
        --platform "$BUILD_PLATFORM" \
        --tag "${EVENT_AGENT_IMAGE}:${EVENT_AGENT_TAG}" \
        --push \
        "$ROOT_DIR"
fi

TEMP_ENV="$(mktemp)"
trap 'rm -f "$TEMP_ENV"' EXIT
awk '!/^EVENT_AGENT_TAG=/' "$ENV_FILE" > "$TEMP_ENV"
printf 'EVENT_AGENT_TAG=%s\n' "$EVENT_AGENT_TAG" >> "$TEMP_ENV"

scp "${SCP_OPTIONS[@]}" compose.yaml config.yaml "$SSH_TARGET:$DEPLOY_PATH/"
scp "${SCP_OPTIONS[@]}" "$TEMP_ENV" "$SSH_TARGET:$DEPLOY_PATH/deploy.env"

ssh "${SSH_OPTIONS[@]}" "$SSH_TARGET" \
    "cd '$DEPLOY_PATH' && docker compose --env-file deploy.env pull event-agent && docker compose --env-file deploy.env up -d --remove-orphans"

printf 'Waiting up to %s seconds for the service to become healthy...\n' "$DEPLOY_HEALTH_TIMEOUT"
deadline=$((SECONDS + DEPLOY_HEALTH_TIMEOUT))
while (( SECONDS < deadline )); do
    status="$(ssh "${SSH_OPTIONS[@]}" "$SSH_TARGET" \
        "cd '$DEPLOY_PATH' && container_id=\$(docker compose --env-file deploy.env ps -q event-agent) && test -n \"\$container_id\" && docker inspect --format '{{.State.Health.Status}}' \"\$container_id\"" \
        2>/dev/null || true)"
    if [[ "$status" == "healthy" ]]; then
        printf 'Deployed %s:%s to %s.\n' "$EVENT_AGENT_IMAGE" "$EVENT_AGENT_TAG" "$SSH_TARGET"
        exit 0
    fi
    sleep 3
done

printf 'Service did not become healthy. Recent status and logs:\n' >&2
ssh "${SSH_OPTIONS[@]}" "$SSH_TARGET" \
    "cd '$DEPLOY_PATH' && docker compose --env-file deploy.env ps && docker compose --env-file deploy.env logs --tail=100 event-agent" >&2 || true
exit 1
