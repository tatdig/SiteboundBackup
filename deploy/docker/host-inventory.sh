#!/bin/bash
# Read-only inventory of a Docker host's own setup — what no container backup
# carries — for its rebuild checklist (docs/CONTAINERS.md, decision 4).
#
#     sudo bash host-inventory.sh > ~/host-inventory.txt
#
# Changes nothing. Leaves secrets out: .env files and registry logins are
# listed by name only, authorized_keys are not read, and sudoers files only by
# name. Read the output before sharing it.
set -u
section() { printf '\n===== %s\n' "$*"; }
run() { printf -- '--- %s\n' "$*"; "$@" 2>&1 | sed 's/^/  /'; }

section "host"
run hostnamectl
run uname -r
run cat /etc/os-release
run uptime

section "network"
run ip -br addr
run ip route
run ip rule
run cat /etc/resolv.conf
run cat /etc/hosts
for f in /etc/network/interfaces /etc/network/interfaces.d/* /etc/netplan/*.yaml; do
    [ -f "$f" ] && run cat "$f"
done
command -v nmcli >/dev/null && run nmcli -t -f NAME,DEVICE,TYPE connection show

section "packages installed by hand"
command -v apt-mark >/dev/null && run apt-mark showmanual
run ls /etc/apt/sources.list.d/

section "docker"
run docker version --format '{{.Server.Version}} (API {{.Server.APIVersion}})'
run docker info --format 'root={{.DockerRootDir}} driver={{.Driver}} cgroup={{.CgroupDriver}} logging={{.LoggingDriver}}'
[ -f /etc/docker/daemon.json ] && run cat /etc/docker/daemon.json
run ls -la /etc/docker/
[ -d /etc/docker/certs.d ] && run find /etc/docker/certs.d -type f
run ls -la /etc/systemd/system/docker.service.d/ 2>/dev/null
for f in /etc/systemd/system/docker.service.d/*.conf; do [ -f "$f" ] && run cat "$f"; done
command -v docker-compose >/dev/null && run docker-compose version --short
run docker compose version --short
run docker network ls --format '{{.Name}} {{.Driver}} {{.Scope}}'
for n in $(docker network ls --format '{{.Name}}' | grep -vx 'bridge\|host\|none'); do
    run docker network inspect "$n" --format '{{.Name}}: subnet {{range .IPAM.Config}}{{.Subnet}} {{end}}internal={{.Internal}}'
done
run docker volume ls --format '{{.Name}} {{.Driver}}'
for home in /root /home/*; do
    cfg="$home/.docker/config.json"
    [ -f "$cfg" ] && printf -- '--- registry logins in %s (names only)\n' "$cfg" && \
        python3 -c "import json,sys; c=json.load(open(sys.argv[1])); print('  ', sorted((c.get('auths') or {}).keys()), 'credsStore=' + str(c.get('credsStore','')))" "$cfg"
done

section "compose projects and their directories (file names only)"
for d in $(docker ps -aq | xargs -r docker inspect --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' | sort -u); do
    [ -n "$d" ] && run ls -la "$d"
done
run ls -la /root

section "data directories the containers use"
run du -sh /var/data/* /srv/* /var/lib/electrumx-db /var/lib/tdcoin-data 2>/dev/null

section "mounts"
run cat /etc/fstab
run findmnt -t nfs,nfs4,cifs,ext4,xfs,btrfs -o TARGET,SOURCE,FSTYPE,OPTIONS
run df -h -x tmpfs -x devtmpfs -x overlay

section "scheduled jobs"
run cat /etc/crontab
run ls -la /etc/cron.d /etc/cron.daily /etc/cron.weekly
for f in /etc/cron.d/*; do [ -f "$f" ] && run cat "$f"; done
for u in $(cut -d: -f1 /etc/passwd); do
    c=$(crontab -l -u "$u" 2>/dev/null) && [ -n "$c" ] && printf -- '--- crontab of %s\n%s\n' "$u" "$(echo "$c" | sed 's/^/  /')"
done
run systemctl list-timers --all --no-pager

section "services"
run systemctl list-unit-files --state=enabled --no-pager --no-legend
run ls -la /etc/systemd/system/
for f in /etc/systemd/system/*.service; do [ -f "$f" ] && ! [ -L "$f" ] && run cat "$f"; done

section "firewall"
command -v ufw >/dev/null && run ufw status verbose
command -v nft >/dev/null && run sh -c "nft list ruleset | grep -v -i docker | head -80"
run sh -c "iptables -S INPUT 2>/dev/null | head -40"
run sh -c "iptables -S DOCKER-USER 2>/dev/null"

section "accounts and access"
run sh -c "awk -F: '\$7 !~ /(nologin|false)\$/ {print \$1, \$3, \$6, \$7}' /etc/passwd"
run ls /etc/sudoers.d/
run getent group docker sudo
run sh -c "grep -E '^[^#]' /etc/ssh/sshd_config /etc/ssh/sshd_config.d/* 2>/dev/null"

section "other agents and tools on the host"
run sh -c "ls /opt /usr/local/bin /usr/local/sbin"
run systemctl is-active zabbix-agent zabbix-agent2 node_exporter vmtoolsd open-vm-tools qemu-guest-agent
