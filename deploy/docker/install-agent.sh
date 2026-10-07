#!/bin/bash
# Install the vmbackup docker agent on an EXISTING Docker host (docs/CONTAINERS.md).
# Phase 2: inventory and report, and captures the manager dispatches, into a
# repository on an NFS share.
#
# Run from the unpacked bundle, as root, on the Docker host:
#     sudo ./install-agent.sh [--site DOCKER-01] [--user omnibackup] [--manager-from 192.0.2.1]
#                             [--nas 192.0.2.10:/mnt/tank/omnibackup-docker]
#                             [--mount /mnt/vmbackup-docker] [--reseed-projects]
#
# What it installs, and nothing else (the host's own packages, network and
# Docker setup are not touched):
#   /opt/vmbackup/vmbackup            the shared package
#   /opt/vmbackup/venv                a venv using the system's python3-yaml
#   /usr/local/bin/vmbackup-site-intake.py, vmbackup-site-jobs.py,
#   /usr/local/bin/vmbackup-agent-jobs-forced
#   /etc/vmbackup/site.yaml           site, hypervisor docker, storage; a
#                                     `docker:` block it already has is kept
#   an fstab line for the share, mounted at --mount; the repository is its root
#   vmbackup-intake.{service,timer}   the report pass, every 15 minutes, as root
#   vmbackup-jobs.{service,timer}     runs dispatched jobs, every minute, as root
#   an account (default `omnibackup`) with two keys, both accepted only from the
#   manager: the fetch key, forced to `cat /var/lib/vmbackup/report.json`, and
#   the dispatch key, forced to queue a job document or print results.
#
# Re-runnable: running it again updates the files and keeps what is there.
set -euo pipefail

SITE=DOCKER-01
ACCOUNT=omnibackup
MANAGER_FROM=192.0.2.1
NAS=192.0.2.10:/mnt/tank/omnibackup-docker
MOUNT=/mnt/vmbackup-docker
RESEED=0
while [ $# -gt 0 ]; do
    case "$1" in
        --site) SITE=$2; shift 2 ;;
        --user) ACCOUNT=$2; shift 2 ;;
        --manager-from) MANAGER_FROM=$2; shift 2 ;;
        --nas) NAS=$2; shift 2 ;;
        --mount) MOUNT=${2%/}; shift 2 ;;
        --reseed-projects) RESEED=1; shift ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

HERE=$(cd "$(dirname "$0")" && pwd)
cd "$HERE"
[ "$(id -u)" = 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
sha256sum --quiet -c SHA256SUMS

# The repository is the share's root: the dataset is this agent's alone.
ROOT=$MOUNT
MOUNT_OPTIONS=vers=4,hard,timeo=600,retrans=2,_netdev,nofail

echo "== checks"
command -v docker >/dev/null || { echo "docker is not installed here" >&2; exit 1; }
docker version --format 'docker {{.Server.Version}}' \
    || { echo "the docker daemon cannot be reached as root" >&2; exit 1; }
python3 -c 'import sys; assert sys.version_info >= (3, 9), sys.version' \
    || { echo "python 3.9 or newer is required" >&2; exit 1; }
missing=""
python3 -c 'import venv, ensurepip' 2>/dev/null || missing="$missing python3-venv"
python3 -c 'import yaml' 2>/dev/null || missing="$missing python3-yaml"
command -v zstd >/dev/null || missing="$missing zstd"
command -v mount.nfs >/dev/null || missing="$missing nfs-common"
if [ -n "$missing" ]; then
    echo "missing packages:$missing — install them first: apt-get install$missing" >&2
    exit 1
fi

echo "== the repository's share: $NAS at $MOUNT"
install -d -m 0755 "$MOUNT"
if ! grep -qE "^[^#]*[[:space:]]$MOUNT[[:space:]]" /etc/fstab; then
    cp -a /etc/fstab "/etc/fstab.bak-$(date -u +%Y%m%dT%H%M%SZ)"
    printf '%s %s nfs %s 0 0\n' "$NAS" "$MOUNT" "$MOUNT_OPTIONS" >> /etc/fstab
    systemctl daemon-reload
fi
mountpoint -q "$MOUNT" || mount "$MOUNT" || {
    echo "could not mount $NAS — is this host's address the one the export allows?" >&2
    echo "  the TrueNAS sees: $(ip route get "${NAS%%:*}" 2>/dev/null | grep -o 'src [0-9.]*')" >&2
    exit 1
}
findmnt -no SOURCE,FSTYPE,OPTIONS "$MOUNT" | cut -c1-120
probe=$MOUNT/.vmbackup-install-probe
echo probe > "$probe" && rm -f "$probe" || { echo "$MOUNT is not writable as root" >&2; exit 1; }
install -d -m 0755 "$ROOT/repository"

echo "== package and scripts"
install -d -m 0755 /opt/vmbackup /etc/vmbackup /var/log/vmbackup \
    /var/lib/vmbackup /var/lib/vmbackup/incoming
rm -rf /opt/vmbackup/vmbackup.new
cp -a vmbackup /opt/vmbackup/vmbackup.new
chmod -R a+rX /opt/vmbackup/vmbackup.new
rm -rf /opt/vmbackup/vmbackup.old
[ -d /opt/vmbackup/vmbackup ] && mv /opt/vmbackup/vmbackup /opt/vmbackup/vmbackup.old
mv /opt/vmbackup/vmbackup.new /opt/vmbackup/vmbackup
[ -x /opt/vmbackup/venv/bin/python ] || python3 -m venv --system-site-packages /opt/vmbackup/venv
install -m 0755 site-intake.py /usr/local/bin/vmbackup-site-intake.py
install -m 0755 site-jobs.py /usr/local/bin/vmbackup-site-jobs.py
install -m 0755 vmbackup-agent-jobs-forced /usr/local/bin/vmbackup-agent-jobs-forced
/opt/vmbackup/venv/bin/python -c \
    "import sys; sys.path.insert(0, '/opt/vmbackup'); import vmbackup.inventory, vmbackup.sitejobs, vmbackup.dockercapture, yaml" \
    && echo "modules ok"

echo "== site.yaml"
if [ -f /etc/vmbackup/site.yaml ]; then
    cp -a /etc/vmbackup/site.yaml "/etc/vmbackup/site.yaml.bak-$(date -u +%Y%m%dT%H%M%SZ)"
fi
# Rewritten from what is known, keeping a `docker:` block the operator already
# has (the per-project settings); projects.yaml seeds it the first time.
/opt/vmbackup/venv/bin/python - "$SITE" "$NAS" "$MOUNT" "$ROOT" "$RESEED" <<'PY'
import sys
from pathlib import Path

import yaml

site, nas, mount, root, reseed = sys.argv[1:6]
path = Path("/etc/vmbackup/site.yaml")
old = {}
if path.is_file():
    loaded = yaml.safe_load(path.read_text()) or {}
    old = loaded if isinstance(loaded, dict) else {}
docker = old.get("docker")
seeded = not isinstance(docker, dict) or reseed == "1"
if seeded:
    docker = yaml.safe_load(Path("projects.yaml").read_text()) or {}
body = yaml.safe_dump({"site": site, "hypervisor": "docker",
                       "storage": {"nas": nas, "mount": mount, "root": root},
                       "docker": docker}, sort_keys=False)
path.write_text("# vmbackup docker agent (docs/CONTAINERS.md). Written by install-agent.sh;\n"
                "# the docker: block is yours to edit and a re-install keeps it.\n" + body)
print("docker settings:", "seeded from projects.yaml" if seeded else "kept from the previous site.yaml")
for name, entry in sorted((docker.get("projects") or {}).items()):
    print(f"  {name:<28} {entry.get('mode') or 'crash'}"
          + (f", {len(entry.get('dumps') or [])} dump(s)" if entry.get("dumps") else "")
          + (f", excludes {', '.join(entry.get('exclude'))}" if entry.get("exclude") else ""))
PY
chmod 0644 /etc/vmbackup/site.yaml

echo "== account $ACCOUNT (no sudo needed), with the report and dispatch keys"
id "$ACCOUNT" >/dev/null 2>&1 || useradd --system --user-group --create-home --shell /bin/bash "$ACCOUNT"
HOME_DIR=$(getent passwd "$ACCOUNT" | cut -d: -f6)
install -d -m 0700 -o "$ACCOUNT" -g "$ACCOUNT" "$HOME_DIR/.ssh"
keys=$HOME_DIR/.ssh/authorized_keys
touch "$keys"
OPTIONS=no-agent-forwarding,no-port-forwarding,no-pty,no-user-rc,no-X11-forwarding
grep -v -e 'vmbackup-report-key' -e 'vmbackup-dispatch-key' "$keys" > "$keys.new" || true
printf 'from="%s",command="cat /var/lib/vmbackup/report.json",%s %s vmbackup-report-key\n' \
    "$MANAGER_FROM" "$OPTIONS" "$(cut -d' ' -f1-2 site-fetch.key.pub)" >> "$keys.new"
printf 'from="%s",command="/usr/local/bin/vmbackup-agent-jobs-forced",%s %s vmbackup-dispatch-key\n' \
    "$MANAGER_FROM" "$OPTIONS" "$(cut -d' ' -f1-2 dispatch.key.pub)" >> "$keys.new"
# The update key: one bundle on stdin, applied by a root-owned script that is
# the only thing the account may run with sudo (docs/CONTAINERS.md, "Updates").
grep -v -e 'vmbackup-update-key' "$keys.new" > "$keys.tmp" || true
mv "$keys.tmp" "$keys.new"
if [ -f docker-update.key.pub ]; then
    printf 'from="%s",command="/usr/local/bin/vmbackup-agent-update-forced",%s %s vmbackup-update-key\n' \
        "$MANAGER_FROM" "$OPTIONS" "$(cut -d' ' -f1-2 docker-update.key.pub)" >> "$keys.new"
    UPDATE_CHANNEL="the update key"
else
    UPDATE_CHANNEL="none (the manager has no /etc/vmbackup/docker-update.key yet)"
fi
install -m 0600 -o "$ACCOUNT" -g "$ACCOUNT" "$keys.new" "$keys"
rm -f "$keys.new"
install -m 0755 vmbackup-agent-update-forced /usr/local/bin/vmbackup-agent-update-forced
install -m 0755 -o root -g root vmbackup-agent-update-apply /usr/local/sbin/vmbackup-agent-update-apply
install -d -m 0750 -o "$ACCOUNT" -g "$ACCOUNT" /var/lib/vmbackup/updates
SUDOERS=/etc/sudoers.d/vmbackup-agent-update
printf '# Written by install-agent.sh: the update key may apply a delivered bundle, nothing else.\n%s ALL=(root) NOPASSWD: /usr/local/sbin/vmbackup-agent-update-apply /var/lib/vmbackup/updates/bundle-*.tgz\n' \
    "$ACCOUNT" > "$SUDOERS.new"
visudo -cqf "$SUDOERS.new" && install -m 0440 "$SUDOERS.new" "$SUDOERS"
rm -f "$SUDOERS.new"
# The settings an update re-applies: what this run was given, never a reseed.
printf '# install-agent.sh settings, re-used by vmbackup-agent-update-apply\n--site %s --user %s --manager-from %s --nas %s --mount %s\n' \
    "$SITE" "$ACCOUNT" "$MANAGER_FROM" "$NAS" "$MOUNT" > /etc/vmbackup/docker-agent.args
echo "update channel: $UPDATE_CHANNEL"
if sudo -l -U "$ACCOUNT" 2>/dev/null | grep -q '(ALL'; then
    echo "WARNING: $ACCOUNT has sudo rights on this host; the agent does not need them (it needs none: its jobs run as root under systemd, and updates have their own one-line rule)" >&2
fi
# The dispatch queue: the manager's key runs as the account, so it owns the
# directory it writes into; results are the root runner's, only read back.
install -d -m 0770 -o "$ACCOUNT" -g "$ACCOUNT" /var/lib/vmbackup/jobs
install -d -m 0755 -o root -g root /var/lib/vmbackup/results

echo "== units"
for unit in vmbackup-intake.service vmbackup-jobs.service vmbackup-scrub.service; do
    sed -e "s#@SITE@#$SITE#g" -e "s#@ROOT@#$ROOT#g" -e "s#@MOUNT@#$MOUNT#g" "$unit" \
        > "/etc/systemd/system/$unit"
done
install -m 0644 vmbackup-intake.timer vmbackup-jobs.timer vmbackup-scrub.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now vmbackup-intake.timer vmbackup-jobs.timer vmbackup-scrub.timer
systemctl start vmbackup-intake.service
chmod 0644 /var/lib/vmbackup/report.json
# Phase 1's local repository folder, if it is still there and empty.
rmdir /var/lib/vmbackup/docker/repository /var/lib/vmbackup/docker 2>/dev/null || true

echo "== what the report says"
/opt/vmbackup/venv/bin/python - <<'PY'
import json
r = json.load(open("/var/lib/vmbackup/report.json"))
inv = r.get("inventory") or {}
caps = r.get("capabilities") or {}
print("site:", r.get("site"), "| hypervisor:", r.get("hypervisor"),
      "| projects:", inv.get("count"), "| error:", inv.get("error") or "none")
print("accepts:", ", ".join(caps.get("job_kinds") or []) or "nothing",
      "| capture blocked by:", caps.get("capture_blocked_by") or "nothing")
print("repository:", (r.get("storage") or {}), "| runs:", r.get("runs"))
PY

echo "== for the manager: account $ACCOUNT, and this host's SSH key fingerprints"
for key in /etc/ssh/ssh_host_*_key.pub; do ssh-keygen -lf "$key"; done
