# Architecture

## One manager, many agents

```
                       ┌──────────────────────────────┐
                       │ manager                      │
                       │  schedules · catalogue · UI  │
                       │  alerts · audit · restore UI │
                       └──────┬─────────┬─────────────┘
          reports (read-only) │         │ jobs (typed intents)
        ┌─────────────┬───────┴─────┬───┴─────────┬──────────────┐
        ▼             ▼             ▼             ▼              ▼
   vSphere agent  Hyper-V agent  Proxmox agent  Docker agent  Windows agent
        │             │             │             │              │
   its own NAS    its own NAS    its own NAS    its own NAS   its own NAS
     export         export         export         export        export
```

* **The manager does no agent work.** It decides *when*; an agent decides *how*,
  with its own credentials, against its own hypervisor, into its own storage.
* **An agent works when the manager cannot reach it.** Schedules are dispatched,
  but a report, a sweep and a capture already queued all run locally.
* **Site-bound**: a site's backups, its restore workstation and its restores all
  use that site's storage and hypervisor — never another site's.

## The job channel

A job is a JSON document with a kind (`capture`, `verify`, `bootcheck`,
`storage`, `cleanup`, `restore`) and a **closed set of parameters per kind**.
Keys that would turn an intent into a command (`command`, `shell`, `args`, …)
are refused by name. Delivery is over SSH to a **forced command** that can only
queue a job or print results; the agent writes a ledger entry *before* work
starts, so a re-delivered job is never run twice. Progress and results come back
the same way, and the manager records them.

An agent **declares** which kinds it accepts, from what it can actually do on
that machine (tools installed, credentials present, readiness checks); the
manager offers only those, and the agent refuses anything else with a sentence.

## A stored run

```
repository/<machine>/<run>/
  manifest.json          what the run is: machine, time, source, files, digests
  <image or archive>     the data — raw sparse disk images, or tar.zst archives
  <file>.extents         where each image's data is (reads skip the holes)
  vm-config/ …           the machine's definition, when it has one
  bootcheck.json         the newest boot-check verdict on these bytes
```

Each file carries its own digest; a run of several files also carries the digest
of their digests, so a manifest whose file list was edited cannot pass on files
that each still match. A digest's domain prefix is part of every stored checksum
and never changes.

## Updates without a login

An agent is updated by a bundle (the package and its scripts, with `SHA256SUMS`)
sent over a key that can do nothing else: the bundle is copied out of the
delivering account's reach, every file checked, nothing unlisted allowed, and
the bundle's own installer run with the agent's original settings. See
`deploy/docker/` for the Docker agent's side of it.
