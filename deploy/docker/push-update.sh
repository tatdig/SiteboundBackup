#!/bin/bash
# Update the docker agent from the manager, over the update key (docs/CONTAINERS.md,
# "Updates"). Builds the bundle from a commit and sends it as the key's only input;
# the agent checks every file against SHA256SUMS and runs the bundle's installer
# with the settings it was installed with. Its output comes back here.
#
#     deploy/docker/push-update.sh [commit] [host] [account]
set -euo pipefail
COMMIT=${1:-HEAD}
HOST=${2:-docker.example.lan}
ACCOUNT=${3:-omnibackup}
KEY=/etc/vmbackup/docker-update.key
[ -r "$KEY" ] || { echo "no update key at $KEY (see docs/CONTAINERS.md, Updates)" >&2; exit 1; }
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
"$(dirname "$0")/build-bundle.sh" "$COMMIT" "$WORK/docker-agent.tgz"
ssh -i "$KEY" -o BatchMode=yes -o IdentitiesOnly=yes "$ACCOUNT@$HOST" < "$WORK/docker-agent.tgz"
