"""The Docker projects a host holds: what the Docker agent can be asked to capture.

Extracted from Sitebound Backup's inventory module, which lists the machines every kind
of agent can capture (vSphere, Hyper-V, Proxmox, Windows computers, Docker); only
the Docker part is published here.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

_PROJECT = "com.docker.compose.project"
_WORKING_DIR = "com.docker.compose.project.working_dir"
_CONFIG_FILES = "com.docker.compose.project.config_files"

#: Bind mounts that are plumbing rather than data: never sized, never archived.
_NOT_DATA = ("/var/run/docker.sock", "/run/docker.sock", "/etc/localtime",
             "/etc/timezone", "/proc", "/sys", "/dev")

#: How long sizing may take in total, and per path. Sizing is advice for the
#: phase-2 decisions, so a slow path is reported as unmeasured, never waited out.
_DU_BUDGET_SECONDS = 300
_DU_PATH_SECONDS = 60


def _docker_json(run, *args: str):
    import subprocess  # noqa: PLC0415

    completed = run(["docker", *args], capture_output=True, text=True, timeout=120)
    if completed.returncode != 0:
        lines = (completed.stderr or completed.stdout or "").strip().splitlines()
        raise RuntimeError(lines[-1] if lines else f"docker {args[0]} failed")
    return json.loads(completed.stdout or "null")


def _du_bytes(run, path: str, deadline: float) -> int:
    """Bytes under ``path``, or -1 when it was not measured."""
    import subprocess  # noqa: PLC0415
    import time  # noqa: PLC0415

    remaining = deadline - time.monotonic()
    if remaining <= 1 or not path:
        return -1
    try:
        completed = run(["du", "-sb", "--", path], capture_output=True, text=True,
                        timeout=min(_DU_PATH_SECONDS, remaining))
        return int((completed.stdout or "").split()[0]) if completed.returncode == 0 else -1
    except (subprocess.SubprocessError, OSError, ValueError, IndexError):
        return -1


def docker_vms(site: Dict[str, Any], *, run=None, measure: bool = True) -> List[Dict[str, Any]]:
    """The host's Compose projects and standalone containers, with their data.

    One entry per project — the unit a capture will take (docs/CONTAINERS.md) —
    carrying the standard ``name``/``id``/``power_state`` every inventory has,
    and the detail the phase-2 decisions are made from: each container's image
    and image digest, state and health, and each mount with what kind it is and
    how big. Read through the ``docker`` CLI, so the agent needs the socket (it
    runs as root) and nothing else.
    """
    import subprocess  # noqa: PLC0415
    import time  # noqa: PLC0415

    run = run or subprocess.run
    listed = run(["docker", "ps", "-aq", "--no-trunc"], capture_output=True, text=True,
                 timeout=120)
    if listed.returncode != 0:
        # A daemon that cannot be asked is not a host without containers.
        lines = (listed.stderr or listed.stdout or "").strip().splitlines()
        raise RuntimeError(lines[-1] if lines else "docker ps failed")
    ids = [line for line in (listed.stdout or "").split() if line]
    containers = _docker_json(run, "inspect", *ids) if ids else []
    # Where each named volume lives on the host, for sizing it.
    volume_names = sorted({
        str(m.get("Name")) for c in containers for m in (c.get("Mounts") or [])
        if m.get("Type") == "volume" and m.get("Name")
    })
    volumes = {
        v.get("Name"): v for v in (_docker_json(run, "volume", "inspect", *volume_names)
                                   if volume_names else [])
    }

    deadline = time.monotonic() + (_DU_BUDGET_SECONDS if measure else 0)
    sized: Dict[str, int] = {}

    def size_of(path: str) -> int:
        if path not in sized:
            sized[path] = _du_bytes(run, path, deadline) if measure else -1
        return sized[path]

    projects: Dict[str, Dict[str, Any]] = {}
    for container in containers:
        config = container.get("Config") or {}
        labels = config.get("Labels") or {}
        name = str(container.get("Name") or "").lstrip("/")
        project = labels.get(_PROJECT) or name
        entry = projects.setdefault(project, {
            "name": project, "id": project,
            "kind": "compose" if labels.get(_PROJECT) else "container",
            "working_dir": labels.get(_WORKING_DIR, ""),
            "config_files": [f for f in str(labels.get(_CONFIG_FILES, "")).split(",") if f],
            "containers": [], "mounts": [],
        })
        state = container.get("State") or {}
        entry["containers"].append({
            "name": name,
            "service": labels.get("com.docker.compose.service", ""),
            "image": str(config.get("Image") or ""),
            "image_id": str(container.get("Image") or ""),
            "state": str(state.get("Status") or ""),
            "health": str((state.get("Health") or {}).get("Status") or ""),
            "restart": str(((container.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name") or ""),
            "labels": {k: v for k, v in labels.items() if k.startswith("vmbackup.")},
        })
        seen = {(m["type"], m["source"]) for m in entry["mounts"]}
        for mount in container.get("Mounts") or []:
            kind = str(mount.get("Type") or "")
            source = str(mount.get("Name") if kind == "volume" else mount.get("Source") or "")
            if (kind, source) in seen or kind not in ("volume", "bind"):
                continue
            seen.add((kind, source))
            path = (volumes.get(source) or {}).get("Mountpoint", "") if kind == "volume" \
                else str(mount.get("Source") or "")
            plumbing = kind == "bind" and any(path == p or path.startswith(p + "/") for p in _NOT_DATA)
            entry["mounts"].append({
                "type": kind, "source": source, "path": path,
                "destination": str(mount.get("Destination") or ""),
                "read_only": not bool(mount.get("RW", True)),
                "plumbing": plumbing,
                "bytes": -1 if plumbing else size_of(path),
            })

    for entry in projects.values():
        states = {c["state"] for c in entry["containers"]}
        entry["power_state"] = ("running" if states == {"running"}
                                else "stopped" if "running" not in states else "partial")
    listed = list(projects.values())
    # The host's own files a rebuild needs (docs/CONTAINERS.md): one more item,
    # so the manager schedules and reports it like a project.
    from .dockercapture import HOST_FILES, host_paths  # noqa: PLC0415

    if host_paths(site) and HOST_FILES not in projects:
        listed.append({"name": HOST_FILES, "id": HOST_FILES, "kind": "host",
                       "power_state": "running", "containers": [], "mounts": [],
                       "paths": host_paths(site)})
    return listed


# -- windows (physical computers) ---------------------------------------------


