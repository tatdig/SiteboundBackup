# Administrator guide

For whoever builds Sitebound Backup and keeps it running: the manager, the site
agents, the storage, the restore desks, monitoring and security. Day-to-day use
is in the [operator guide](OPERATOR-GUIDE.md).

> **This guide describes the whole system.** This repository is an excerpt — the
> Docker agent's capture core and its install files (see the
> [README](../README.md)) — so most commands below belong to parts that are not
> published here. They are shown so the design and its operation can be read
> end to end. Names such as `SITE-A` and addresses such as `192.0.2.10` are
> examples.

## 1. The shape of the system

```
                MANAGER  (schedules, web interface, catalogue, alarms, e-mail)
                   |   jobs out / reports in - never backup data
     +-------------+-------------+--------------+
     |             |             |              |
  SITE-A        SITE-B        SITE-C           ...
  agent  ->  the site's own repository (NAS)  ->  restore desk (read-only)
     ^
  the site's machines: vSphere, Hyper-V, Proxmox VE, Docker, Windows and Linux computers
```

* **One rule above all: a site's backups stay on that site.** An agent writes only
  to its own site's NAS; a site's restore desk reads only that NAS; a restore puts
  a machine back on the same site. Nothing copies backups between sites.
* The **manager** holds no backup data of the sites. It sends typed jobs
  (capture, verify, boot check, restore, prune) to agents over narrow SSH keys,
  collects their reports, schedules, shows everything in the web interface and
  raises alarms. It can also back up VMs itself, into its own repository.
* An **agent** is a small Linux VM next to what it backs up, one kind per agent:

  | Kind | Backs up |
  |---|---|
  | `vmware` | vSphere VMs: VDDK over NBD, changed-block tracking for incrementals |
  | `hyperv` | Hyper-V VMs: the host pushes a frozen VHDX over SFTP, in a chroot |
  | `kvm` | Proxmox VE guests: `vzdump`, or true incrementals through Proxmox's backup-provider API |
  | `docker` | Docker projects: volumes, database dumps, the definition, the host's own files ([CONTAINERS.md](CONTAINERS.md)) |
  | `windows` | physical computers: Windows (its own `wbadmin` onto an iSCSI LUN) and Linux (pulled over SSH, any distribution) |

* A **restore desk** per site is an RDP desktop that opens that site's backups
  read-only, prepares bare-metal recoveries and scans backups for malware.

How the pieces talk, and why: [ARCHITECTURE.md](ARCHITECTURE.md).

## 2. The manager

Installed on Rocky Linux, as root, from a checkout of the full system:

```bash
sudo deploy/install.sh        # Python package, configuration, systemd service, API token
sudo deploy/install-web.sh    # httpd + php-fpm + MariaDB, the web application, TLS, vhost
```

`install-web.sh` prints the first administrator's password **once**. Both
scripts are safe to re-run.

| What | Where |
|---|---|
| Backend configuration (repository, sites, retention, e-mail, vSphere endpoint) | `/etc/vmbackup/config.yaml` |
| Web configuration (application name, database, sign-in providers) | `/etc/vmbackup/web-config.php` |
| The API token the web interface uses | `/etc/vmbackup/api.token` |
| Installed backend | a Python virtual environment (an update is `pip install`, not a file copy) |
| Services | `vmbackup-api`, `httpd`, `php-fpm`, `mariadb` |

**Health:** `vmbackup doctor` (configuration, connectivity, the last integrity
sweep) and `vmbackup storage` (repository space).

**Updating:** keep a copy of what runs, `pip install` the new backend, run the
database step when a release has one (`deploy/setup-database.sh`, safe to
re-run), copy the web files with root ownership and modes 0755/0644, reload
`php-fpm`, restart `vmbackup-api`. Never restart the API while it is taking a
backup.

**The manager's own backup:** `vmbackup appliance --backup` writes a bundle of
its configuration, database and keys, the secrets sealed to a certificate whose
private key is kept elsewhere. Keep the bundle off the manager.

## 3. Accounts and sign-in

Three roles: **admin** (everything, including accounts), **operator** (start and
cancel backups, restores and checks), **viewer** (read only). Accounts are local
(a password hash in MariaDB) or come from **LDAP / Active Directory**, where
directory groups map onto roles and the highest matching role wins. Keep one
local administrator for when the directory is unreachable.

```bash
sudo php deploy/create-admin.php --list
sudo php deploy/create-admin.php --username admin --reset-password
sudo php deploy/check-ldap.php --username alice        # test LDAP without the web interface
```

Every sign-in, backup, restore and account change is in the audit log.

## 4. Storage: one NAS share per site

For every site's repository dataset on the NAS:

1. **The agent's export**, NFS 4.2, writable by that agent's address only.
2. **The restore desk's export, read-only** — a separate share (a nested folder,
   where the NAS allows one share per path). Deploying a desk checks that the NAS
   really refuses a write.
3. **Periodic snapshots taken by the NAS itself**, kept two weeks or more: an
   NFS client cannot delete them, so a compromised agent cannot erase the
   history. Schedule them outside the capture window.

## 5. Adding a site

1. **Build the agent VM** from the agent template on the site's hypervisor, then
   on it:
   ```bash
   sudo vmbackup-agent-init --site SITE-A --hypervisor <kind> \
        --nas nas.example:/mnt/pool/site-a --nas-mount /mnt/site-a --root /mnt/site-a/vmbackup \
        --appliance-key /root/site-fetch.key.pub --dispatch-key /root/dispatch.key.pub
   ```
   The two public keys are the manager's: one may only fetch the report, one may
   only hand over a job (it is fenced to the job runner).
2. **Kind-specific set-up**: a vSphere role with backup privileges and VDDK; a
   Proxmox account and storage (section 9); Docker projects and their consistency
   modes in `site.yaml`; computers enrolled one by one (section 6).
3. **Tell the manager**: add the site to `sites:` in `/etc/vmbackup/config.yaml`
   (`host`, `user`, `id_file`, `dispatch_id_file`), check the agent's SSH host key
   against the one the agent prints itself, restart the API. The site appears on
   the *Agents* page once it reports (every 15 minutes).
4. **A restore desk** for the site (section 7), **schedules** for its machines,
   and the site in monitoring.

**Updating agents:** a signed-off update bundle, copied to the agent and
installed by its own script, which checks every file against its checksum.
Never while the agent runs a job.

## 6. Physical computers

**Windows** (Home editions included): on the computer, as administrator, a
script sets up WinRM over HTTPS for the agent only and prints the exact
enrolment command for the agent, with the certificate's fingerprint. The
computer then images itself with Windows' own `wbadmin` onto **its own iSCSI
LUN** on the agent, which is enabled only while the image is taken; Windows keeps
older versions on the LUN as shadow copies.

**Linux** (any distribution with GNU tar — a minimal install may need
`dnf/apt install tar`): on the agent,

```bash
vmbackup-linux-add NAME 192.0.2.20 --fingerprint SHA256:...
```

with the fingerprint read **on the computer** (`ssh-keygen -lf
/etc/ssh/ssh_host_ed25519_key.pub`). It prints one block to run there as root:
a read-only capture script, and a key that may run only that script and only
from the agent. Run the same command again to finish. The agent then pulls the
disk layout and one archive per filesystem; nothing on the computer can write to
the backups. A hypervisor host (a Proxmox VE node, for instance) is backed up the
same way; its VM disks are left to their own agent.

## 7. Restore desks

One per site: an Ubuntu RDP desktop that mounts only that site's repositories,
read-only. Built by one script (on vSphere; by the same steps by hand
elsewhere), which installs the browse tools, the recovery share (Samba, its own
services disabled), ClamAV with a nightly scan after the capture window, a
service that closes open backups before shutdown, and the desk's wallpaper.

## 8. Monitoring and notifications

* **Zabbix:** a template with one JSON snapshot item from the manager and
  dependent items, triggers and a per-site discovery: backups overdue or failed,
  sites gone quiet, sweeps that found damage or stopped, shares misbehaving,
  Proxmox nodes whose provider storage or fleecing space is at risk.
* **E-mail or webhook** on failures, plus a daily digest.
* **Proof that backups work:** a daily integrity sweep on every site, verify on
  request, and boot checks that start a restored copy with no network.

## 9. Security essentials

* Credentials are files readable by root only, typed by people — never
  generated and displayed by a tool.
* Every hypervisor API is reached with a **pinned** certificate or host key; a
  mismatch stops before a password is sent.
* Agents' accounts are narrow: a vSphere role with backup privileges; on Proxmox
  an account that may change only guests in one dedicated pool, where boot checks
  and restores put their guests; a Linux computer's key runs one read-only script.
* Restores always create a **new** machine with its network **disconnected**.
* **The repository holds the machines' secrets**: a file-level backup contains
  password hashes, keys and configuration databases. Protect the NAS like the
  machines themselves.

## 10. When something is wrong

First rule: **do not touch the repository.** Deleting or "tidying" a run is the
one mistake that cannot be undone. Then find out what still works
(`vmbackup doctor`, `vmbackup storage`, the *Agents* page), write down the time,
and let a running backup finish before starting a restore.
