# The Docker agent

## Why, when the VM is already backed up

A Docker host that is itself a VM can be captured whole, and that is disaster
recovery — but it is **crash-consistent** (a database inside a container is
copied mid-write) and **all or nothing** (the whole VM comes back, never "this
application's data as of Tuesday"). Container-level backup adds per-application,
consistent, granular restore. The two complement each other — or, for a host
that is cheap to rebuild, the container backups *are* the backup and the host is
a documented rebuild.

## Where it fits

An agent like the others:

* **On the Docker host itself.** It needs the Docker socket, which is root on that
  host, so it is never reached over the network (no Docker API over TCP).
* **Its inventory** is the host's Compose projects and standalone containers with
  their volumes (`docker_inventory.py`), reported to the manager, which offers
  them on its Schedules page like VMs.
* **The unit of backup** is a Compose project, or a single container outside one.
* **The repository** is its own NAS export, admitted to this host alone.
* **The run format** is every agent's: `repository/<project>/<run>/manifest.json`
  plus one archive per data mount, so verify, the report, retention and the
  manager's protection checks work unchanged.

## What a capture holds

| Part | How |
|---|---|
| Volumes and bind mounts — the data | one `tar` + zstd archive each (numeric owners, ACLs, xattrs, holes), digested |
| The project's definition | every container's `docker inspect`, the volumes' and images' inspects, the Compose files and `.env` |
| Images | by **registry digest**, so a restore pulls exactly that image; `docker save` only for images built locally, hard-linked when an older run already holds the same image id |
| Database dumps | one file per dump, streamed through zstd, digested like the archives |
| `host-files` | one more item: the host's own files a rebuild needs (build contexts, registry login, network connections, `/etc/docker`) |

A mount the site excludes, or one nested inside another mount of the same
project, is listed under `excluded` with the reason — never silently missing.
`tar` reporting "file changed as we read it" is a **warning** on the run, not a
failure.

## Consistency

Set per project (`deploy/docker/projects.yaml` → `site.yaml`):

| Mode | What a capture does | Recorded as |
|---|---|---|
| `dump` | runs each dump inside its container (`docker exec [-u USER] … sh -c COMMAND`, the container named by Compose service), stores its output, then archives the data | `dump`, when every dump worked |
| `pause` | `docker pause` the project's containers while their data is read, `docker unpause` always (in a `finally`) | `pause`, with how long they were held |
| `stop` | the same with `docker stop` / `docker start` | `stop` |
| (none) | as if the power had been pulled mid-read | `crash` |

* A **failed dump** keeps the run (its data is as good as crash-consistent) and
  fails the job, so the schedule and monitoring show the missing dump.
* Containers held by a capture that died leave a **marker**; the next capture of
  that project resumes them first and says so.
* The definition is archived before a pause and local images saved after it, so a
  project is frozen only while its data is read.

## Integrity

Each file of a run has its own SHA-256; the run's digest is the digest of those
(`combined_digest`), so editing a manifest's file list cannot pass on files that
each still match. The agent re-reads every stored run daily (the sweep) and the
manager raises an alert on any mismatch.

## Restore, and proving it (designed)

* **Restore:** recreate the volumes from their archives, pull the images by digest,
  `docker compose up` from the saved project — in place, or under a new project
  name beside the original.
* **Restore check** (the boot check's equivalent): restore under a throwaway name
  on an **isolated Docker network with no outside access**, wait for each
  container's `HEALTHCHECK`, remove it. Passed / failed / inconclusive — the last
  when a container has no health check.

## Updates

The agent account holds three narrow keys, each accepted only from the manager:
one reads the report, one queues a job, one delivers an **update bundle**. The
bundle is checked file by file against its `SHA256SUMS` (the installer included,
nothing unlisted allowed) by a root-owned script that is the account's only sudo
rule, and installed with the agent's original settings
(`deploy/docker/vmbackup-agent-update-forced`, `vmbackup-agent-update-apply`,
`push-update.sh`).
