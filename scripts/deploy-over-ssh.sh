#!/usr/bin/env bash
set -Eeuo pipefail

die() { echo "ERROR: $*" >&2; exit 1; }
mode=${1:?Usage: bash scripts/deploy-over-ssh.sh deploy IMAGE | rollback}
image=${2:-}
config=${DEPLOY_CONFIG:?Set DEPLOY_CONFIG to the absolute server configuration path}
port=${DEPLOY_PORT:-22}
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

: "${DEPLOY_HOST:?Set DEPLOY_HOST}"
: "${DEPLOY_USER:?Set DEPLOY_USER}"
: "${DEPLOY_SSH_KEY:?Set DEPLOY_SSH_KEY}"
: "${DEPLOY_KNOWN_HOSTS:?Set DEPLOY_KNOWN_HOSTS}"
[[ "$mode" == deploy || "$mode" == rollback ]] || die 'Use deploy or rollback'
[[ "$DEPLOY_HOST" =~ ^[a-zA-Z0-9][a-zA-Z0-9.:-]*$ ]] || die 'Invalid DEPLOY_HOST'
[[ "$port" =~ ^[0-9]{1,5}$ ]] || die 'Invalid DEPLOY_PORT'
port=$((10#$port))
((port > 0 && port <= 65535)) || die 'DEPLOY_PORT must be between 1 and 65535'
[[ "$config" =~ ^/[a-zA-Z0-9_./-]+$ ]] || die 'DEPLOY_CONFIG must be an absolute path without shell characters'
if [ "$mode" = deploy ]; then
  [[ "$image" =~ ^ghcr\.io/[a-z0-9._/-]+@sha256:[a-f0-9]{64}$ ]] || die 'Use a GHCR image with a full sha256 digest'
else
  image=''
fi

umask 077
work_dir=$(mktemp -d)
trap 'rm -rf -- "$work_dir"' EXIT
printf '%s\n' "$DEPLOY_SSH_KEY" > "$work_dir/key"
printf '%s\n' "$DEPLOY_KNOWN_HOSTS" > "$work_dir/known_hosts"

# Arguments are restricted above so they can be passed through the remote shell.
ssh -T -i "$work_dir/key" -l "$DEPLOY_USER" -p "$port" \
  -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
  -o "UserKnownHostsFile=$work_dir/known_hosts" -o ConnectTimeout=10 \
  -o ServerAliveInterval=15 -o ServerAliveCountMax=4 \
  "$DEPLOY_HOST" "bash -s -- '$mode' '$image' '$config'" < "$script_dir/deploy-agent.sh"
