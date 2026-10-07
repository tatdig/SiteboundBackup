#!/bin/bash
# Build the bundle the docker agent is installed with (docs/CONTAINERS.md):
# the vmbackup package, the intake and job-runner scripts with the forced
# command, this directory's units, installer and projects.yaml, and the
# manager's two public keys (report fetch, dispatch).
#
#   deploy/docker/build-bundle.sh [commit] [output.tgz]
set -euo pipefail
COMMIT=${1:-HEAD}
OUT=${2:-docker-agent.tgz}
REPO=$(cd "$(dirname "$0")/../.." && pwd)
FETCH_KEY=/etc/vmbackup/site-fetch.key.pub
DISPATCH_KEY=/etc/vmbackup/dispatch.key.pub
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
B=$WORK/docker-agent
mkdir -p "$B"
git -C "$REPO" archive "$COMMIT" backend/vmbackup deploy/hyperv/site-intake.py \
    deploy/hyperv/site-jobs.py deploy/hyperv/vmbackup-agent-jobs-forced deploy/docker \
    | tar x -C "$B"
mv "$B/backend/vmbackup" "$B/vmbackup"
mv "$B/deploy/hyperv/site-intake.py" "$B/deploy/hyperv/site-jobs.py" \
    "$B/deploy/hyperv/vmbackup-agent-jobs-forced" "$B/"
mv "$B/deploy/docker/"* "$B/"
rm -rf "$B/backend" "$B/deploy" "$B/build-bundle.sh"
cp "$FETCH_KEY" "$B/site-fetch.key.pub"
cp "$DISPATCH_KEY" "$B/dispatch.key.pub"
# The update key (deploy/docker/README, "Updates"): carried when the manager has
# one, so the installer can authorise it; without it the channel is not set up.
UPDATE_KEY=/etc/vmbackup/docker-update.key.pub
[ -f "$UPDATE_KEY" ] && cp "$UPDATE_KEY" "$B/docker-update.key.pub"
chmod 755 "$B/install-agent.sh"
# Every file, the installer included: it is the one that runs as root.
(cd "$B" && find . -type f ! -name SHA256SUMS | sort | xargs sha256sum > SHA256SUMS)
tar czf "$OUT" -C "$WORK" docker-agent
echo "built $OUT from $(git -C "$REPO" rev-parse --short "$COMMIT")"
