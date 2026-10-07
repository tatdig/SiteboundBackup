# Docker agent: installation

`install-agent.sh` installs the agent on an existing Docker host: the package, the
job runner and report scripts (`vmbackup-site-jobs.py`, `vmbackup-site-intake.py`
— part of the full product, not of this excerpt), the NFS mount of its
repository, the systemd units (report every 15 minutes, jobs every minute, a daily
sweep), and an account holding three narrow keys accepted only from the manager:
read the report, queue a job, deliver an update.

`projects.yaml` is an example of the per-project settings (consistency mode,
dumps, excluded mounts) and of the host files a rebuild needs.

Addresses and names here are examples (`192.0.2.0/24` is reserved for
documentation).
