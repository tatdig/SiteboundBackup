# Operator guide

For whoever uses Sitebound Backup day to day: checking last night's backups,
starting one by hand, proving backups work, and getting things back. Setting the
system up is in the [administrator guide](ADMIN-GUIDE.md).

> **This guide describes the whole system;** this repository is an excerpt (see
> the [README](../README.md)). Every page of the web interface, with example
> data: [SCREENSHOTS.md](SCREENSHOTS.md).

Two places to work from:

* **The web interface** (the manager): schedules, jobs, agents, boot checks, the
  repository, alarms. Everything that *starts* or *checks* a backup.
* **The restore desk** of each site (an RDP desktop): opening a backup to take
  files out, preparing a whole-computer recovery, malware scans. Everything that
  *reads* a backup. It sees only its own site's backups, and only read-only.

## 1. Your role

| You can... | admin | operator | viewer |
|---|:--:|:--:|:--:|
| See every page, backup, job and the audit log | ✓ | ✓ | ✓ |
| Start and cancel backups, checks and restores; edit schedules | ✓ | ✓ | |
| Manage accounts | ✓ | | |

Everything you start is recorded in the audit log under your name. Times are
shown in your own time zone, set on first sign-in from your browser and
changeable on your profile.

## 2. The web interface, page by page

| Page | What it is for |
|---|---|
| **Dashboard** | Every machine and its last backup: overdue, never backed up, failed. Start a backup from here. |
| **Schedules** | When each machine is backed up (for example *every day at 02:00, full on Sunday*). |
| **Agents** | Each site's agent: when it last reported, what it holds, errors, its integrity sweep. |
| **Jobs** | Jobs sent to agents (capture, verify, boot check, restore): queued, running, done, failed. |
| **Repository** | What is stored, per machine and per site: runs, sizes, chains, boot verdicts. |
| **Restore** | Put a whole VM back, always previewed first (section 6). |
| **Boot checks** | Proof that a backup *boots*: the newest verdict per machine; start a check. |
| **Storage** | Space on each repository, and how fast it fills. |
| **Audit log** | Who did what, when. |

## 3. Every morning (five minutes)

1. **Dashboard:** nothing red, nothing *overdue*. A computer that was switched
   off is retried every 30 minutes within the backup window.
2. **Jobs:** no *failed* row since yesterday. A failed job's detail says why.
3. **Agents:** every agent reported recently, with no error, and its daily sweep
   found no bad backup.
4. **Alarms:** monitoring or the morning digest — and check that the digest
   itself arrived.
5. **Restore desks**, now and then: *Scan results* — every newest backup *clean*.

## 4. Backing up

* **Scheduled:** on *Schedules* — a time, the days, and when a full is taken (the
  rest are incrementals where the machine supports them). A run missed while
  something was down still happens within a grace window, not days later.
* **Now, by hand:** *Dashboard → Back up*. One backup per machine at a time: a
  second one waits for the first.

## 5. Proving backups work

| Check | What it proves | How |
|---|---|---|
| **Integrity sweep** | the stored bytes are still the bytes written | automatic, daily, on every site |
| **Verify** | the same, for one backup, now | a *verify* job; it reads the whole backup |
| **Boot check** | the backup starts a working machine | *Boot checks*: restored as a throwaway with no network, started, asked whether it is up, removed |

A backup is **known good** when it has passed a boot check *and* a malware scan.

## 6. Getting things back

**Choose the backup first.** Newest is not always right: if a machine was
infected, encrypted or broken *before* last night, last night's backup carries
that too. Pick the newest known-good backup from before the problem began.

| You need | Where | How |
|---|---|---|
| **A file or folder** from any VM, a Windows computer (any kept version) or a Linux computer | the site's **restore desk** | *Open a backup* (desktop icon) → machine → date (→ version): the file manager opens it read-only → right-click → **Copy to scratch**. Close it from the menu when done. |
| **A whole vSphere or Proxmox VE VM** at a site | web interface: **Agents → Restore…** | Pick the VM, optionally an older backup, a new name and where it goes. **Check plan** writes nothing; then type the VM's name to confirm. Always a **new** VM, its network **disconnected** and not started unless asked (never both at once). Check it, then connect it deliberately. |
| **A whole VM backed up by the manager** | web interface: **Restore** | Preview first; then a **new VM** beside the original, or replacing the disks, which needs `REPLACE` typed. |
| **A whole Windows computer** (a dead disk, a new laptop) | the **restore desk**, then the computer | *Prepare computer recovery* → the computer → backup → version. Boot the computer from Windows installation media → *Repair your computer → Troubleshoot → Command Prompt*, and type the steps the desk shows (`net use R: ...`, then `R:\recover.cmd 0`). Type **ERASE** when asked. |
| **A whole Linux computer** (a hypervisor host too) | the **restore desk**, then the computer | *Prepare computer recovery* → the computer → backup. Boot **SystemRescue**, then type the steps shown (`mount -t cifs ...`, then `bash /mnt/backup/restore.sh /dev/sda`). Type **ERASE** when asked. |

After a whole-machine restore:

* **The network is your call.** The restored machine is a copy: the same name
  and, if it had one, the same fixed IP. With the original still on the network,
  expect an IP or name conflict.
* **Stop the recovery share** on the desk afterwards, or it stops by itself after
  8 hours.
* A Linux machine with SELinux relabels itself on its first start and restarts once.

## 7. Malware scans

Each restore desk scans the newest backup of every machine nightly with ClamAV,
and any backup on request (*Scan a backup for malware*; it runs in the
background). The backup lists show **clean**, **INFECTED** or **not scanned**.

**A backup is INFECTED:** do not restore from it. Tell an administrator — the
machine itself is probably infected now. Restore from the newest *clean* backup
from before, and scan the restored machine before it goes back on the network.

## 8. When something is red

| You see | What to do |
|---|---|
| A backup **failed** | Open the job: the detail says why. A machine that was off is retried; credentials, certificates or a full share are for an administrator. |
| A machine **overdue** or **never backed up** | Is the machine there, is its agent reporting? Back it up by hand; tell an administrator if that fails. |
| An agent **stale** | The agent or its network is down. Stored backups are safe; nothing new is taken there. An administrator. |
| A **boot check failed** | That backup does not start a machine. Keep it, take a fresh backup, check again, tell an administrator. |
| The **integrity sweep** found a bad backup | Do not restore from it; an administrator. |
| A backup **INFECTED** | Section 7. |

## 9. Never

* **Never delete or "tidy" anything in a repository** by hand. Retention removes
  old backups and keeps chains whole.
* **Never restore over a running production machine** — restore as a new
  machine, check it, then switch.
* **Never connect a restored copy while the original is on the network** without
  deciding about the conflict first.
