"""Capture one Docker project from the docker agent (docs/CONTAINERS.md, phase 2).

The unit is what the inventory lists: a Compose project, or a standalone
container. A run is the shape every agent's run has —
``<root>/repository/<project>/<run>/manifest.json`` beside the files it names —
so the report, retention, the share probe and ``verify`` treat it like any
other. What is different is what the files are:

* ``definition.tar.zst`` — every container's ``docker inspect``, the named
  volumes' ``docker volume inspect``, every image's ``docker image inspect``,
  and the Compose project's own files with its ``.env``. Always present, so a
  project with no data still has a run that says how to recreate it.
* ``volume-<name>.tar.zst`` / ``bind-<path>.tar.zst`` — one archive per data
  mount, ``tar`` with numeric owners, ACLs and xattrs, through ``zstd``.
  Plumbing (the Docker socket, ``/proc``, …) is never archived, and a mount the
  site configuration excludes is recorded as excluded rather than silently
  missing.
* ``image-<ref>.tar.zst`` — ``docker save`` of an image that is in **no
  registry** (no ``RepoDigests``): it could not be pulled back. An image with a
  registry digest is kept by that digest only. A saved image whose id an older
  run of the project already holds is hard-linked rather than saved again.

Every file is in the manifest's ``disks`` list with its own SHA-256 (the whole
file, ``hash_kind: file``: an archive is dense, there are no holes to skip), so
``verify`` re-reads each one. The top-level ``sha256`` is a digest of those
digests — the run's identity, which is what the report and a later verdict key
on.

**Consistency, in this phase, is crash-consistent** and the manifest says so:
the containers keep running while their files are read, as if the power had
been pulled at some instant during the read. ``tar`` exiting 1 ("file changed
as we read it") is that, said out loud, and is a warning on the run rather than
a failure. Dump, pause and stop are phase 3.

Nothing here decides *when* to capture. A capture is a job the manager sends.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import chains
from .digest import DIGEST_KIND_FILE, file_digest
from .util import safe_slug

#: Runs kept per project unless the site configuration says otherwise.
DEFAULT_KEEP = 2

#: What the run is: its consistency, recorded in every manifest.
CONSISTENCY = "crash"

ARCHIVE_SUFFIX = ".tar.zst"
DEFINITION = "definition" + ARCHIVE_SUFFIX

#: ``tar`` that keeps what a restore needs: numeric owners (the uid inside a
#: container is not a name on this host), ACLs, xattrs, holes, one filesystem.
TAR = ["tar", "--create", "--file=-", "--numeric-owner", "--acls", "--xattrs",
       "--sparse", "--one-file-system"]
#: Fast, multi-threaded, and a level that leaves the network the bottleneck.
ZSTD = ["zstd", "-T0", "-3", "-q", "-f"]

#: GNU tar's "some files differ": a file changed while it was read. On a running
#: container that is the definition of crash-consistent, not an error.
TAR_FILES_CHANGED = 1

Archiver = Callable[[List[str], Path], Tuple[int, str]]
Runner = Callable[..., Any]


#: The item a docker agent lists for its host's own files (the rebuild kit).
HOST_FILES = "host-files"


def host_paths(site: Optional[Dict[str, Any]]) -> List[str]:
    """``docker.host.paths`` in site.yaml: absolute paths, globs allowed."""
    block = (site or {}).get("docker")
    block = block if isinstance(block, dict) else {}
    host = block.get("host")
    paths = host.get("paths") if isinstance(host, dict) else None
    return [str(p) for p in paths or [] if str(p).startswith("/")]


def project_settings(site: Optional[Dict[str, Any]], project: str) -> Dict[str, Any]:
    """This project's block under ``docker: projects:`` in site.yaml, or ``{}``."""
    block = (site or {}).get("docker")
    block = block if isinstance(block, dict) else {}
    projects = block.get("projects")
    projects = projects if isinstance(projects, dict) else {}
    entry = projects.get(project)
    return dict(entry) if isinstance(entry, dict) else {}


def keep_for(site: Optional[Dict[str, Any]], project: str) -> int:
    """Runs to keep: the project's ``keep``, else ``docker.keep``, else 2."""
    block = (site or {}).get("docker")
    block = block if isinstance(block, dict) else {}
    for value in (project_settings(site, project).get("keep"), block.get("keep")):
        try:
            if value not in (None, "") and int(value) > 0:
                return int(value)
        except (TypeError, ValueError):
            continue
    return DEFAULT_KEEP


#: The consistency modes a project can be given in site.yaml (docs/CONTAINERS.md).
MODES = ("crash", "dump", "pause", "stop")


def mode_for(settings: Dict[str, Any]) -> str:
    mode = str(settings.get("mode") or "crash").strip().lower()
    return mode if mode in MODES else "crash"


def held_marker(root: Path, project: str) -> Path:
    """Written before containers are paused or stopped, removed once they run
    again: a capture that died in between leaves it, and the next one resumes them."""
    return Path(root) / "staging" / f".held-{safe_slug(project, fallback='project')}.json"


def _docker(run: Runner, *args: str, timeout: int = 180):
    return run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def _resume(run: Runner, how: str, names: Sequence[str]) -> str:
    """Unpause or start the containers again. Returns a problem, or ""."""
    if not names:
        return ""
    verb = "unpause" if how == "paused" else "start"
    done = _docker(run, verb, *names)
    if done.returncode != 0:
        return f"could not {verb} {', '.join(names)}: {(done.stderr or '').strip()[-200:]}"
    return ""


def _excluded(mount: Dict[str, Any], exclude: Sequence[str]) -> bool:
    """Whether the site configuration names this mount — by volume name, host path or destination."""
    names = {str(mount.get(k) or "") for k in ("source", "path", "destination")}
    return any(str(item) in names for item in exclude if str(item))


def _covered_by(mount: Dict[str, Any], mounts: Sequence[Dict[str, Any]]) -> str:
    """The path of another archived mount this one lies inside, or ``""``."""
    path = str(mount.get("path") or "").rstrip("/")
    for other in mounts:
        outer = str(other.get("path") or "").rstrip("/")
        if outer and path != outer and path.startswith(outer + "/"):
            return outer
    return ""


def pipe_to_zstd(producer: List[str], out: Path) -> Tuple[int, str]:
    """``producer | zstd > out``. Returns the producer's exit code and its stderr.

    A failing ``zstd`` (a full share) is raised, because nothing the producer
    said can make that archive whole.
    """
    with open(os.devnull, "wb") as devnull:
        source = subprocess.Popen(producer, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  stdin=devnull)
        sink = subprocess.Popen([*ZSTD, "-o", str(out)], stdin=source.stdout,
                                stderr=subprocess.PIPE)
        assert source.stdout is not None
        source.stdout.close()  # so the producer sees SIGPIPE if zstd dies
        _, sink_err = sink.communicate()
        source_err = source.stderr.read() if source.stderr is not None else b""
        source.wait()
    if sink.returncode != 0:
        raise RuntimeError(f"zstd failed writing {out.name}: "
                           f"{sink_err.decode(errors='replace').strip()[-300:]}")
    return source.returncode, source_err.decode(errors="replace").strip()


def _tar_argv(path: Path) -> List[str]:
    """``tar`` for a directory (its contents) or a single bind-mounted file."""
    if path.is_dir():
        return [*TAR, "--directory", str(path), "."]
    return [*TAR, "--directory", str(path.parent), path.name]


def _docker_json(run: Runner, *args: str) -> Any:
    completed = run(["docker", *args], capture_output=True, text=True, timeout=300)
    if completed.returncode != 0:
        lines = (completed.stderr or completed.stdout or "").strip().splitlines()
        raise RuntimeError(lines[-1] if lines else f"docker {args[0]} failed")
    return json.loads(completed.stdout or "null")


def _found_images(run: Runner, image_ids: Sequence[str]) -> List[Dict[str, Any]]:
    """``docker image inspect`` of each id, keeping the ones the daemon still has.

    One missing id makes the command exit non-zero while it still prints the
    others, and an image that cannot be described is a warning on the run, not a
    reason to lose the project's data.
    """
    if not image_ids:
        return []
    completed = run(["docker", "image", "inspect", *image_ids], capture_output=True,
                    text=True, timeout=300)
    try:
        found = json.loads(completed.stdout or "[]")
    except ValueError:
        found = []
    return [item for item in found if isinstance(item, dict)] if isinstance(found, list) else []


def combined_digest(disks: Sequence[Dict[str, Any]]) -> str:
    """The run's identity: SHA-256 over each file's key and digest, in order."""
    h = hashlib.sha256(b"vmbackup-docker-run-v1\0")
    for disk in disks:
        h.update(f"{disk['key']}\0{disk['sha256']}\n".encode())
    return h.hexdigest()


def _saved_images(vm_dir: Path) -> Dict[str, Path]:
    """Image id → a saved archive an older run of this project still holds."""
    held: Dict[str, Path] = {}
    for entry in chains.load(vm_dir) if vm_dir.is_dir() else []:
        for image in entry.manifest.get("images") or []:
            archive = str(image.get("archive") or "")
            path = Path(entry.run) / archive
            if archive and image.get("id") and path.is_file():
                held.setdefault(str(image["id"]), path)
    return held


def capture_docker(
    project: str,
    *,
    root: Path,
    site_name: str,
    site: Optional[Dict[str, Any]] = None,
    keep: Optional[int] = None,
    run: Optional[Runner] = None,
    archiver: Optional[Archiver] = None,
    log: Optional[Callable[[str], None]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Back one project up now. Returns a job result; never raises for a failed capture."""
    from .docker_inventory import docker_vms  # noqa: PLC0415 - one listing, the report's own

    run = run or subprocess.run
    archiver = archiver or pipe_to_zstd
    say = log or (lambda message: None)
    started = time.time()

    def failed(detail: str, **metrics) -> Dict[str, Any]:
        return {"state": "failed", "evidence": "", "detail": detail, "metrics": metrics}

    project = str(project or "").strip()
    if not project:
        return failed("no 'vm' (project) was given")
    try:
        listed = docker_vms(site or {}, run=run, measure=False)
    except Exception as exc:  # noqa: BLE001 - a daemon that cannot be asked is a result
        return failed(f"the Docker daemon could not be listed: {exc}")
    entry = next((item for item in listed if item.get("name") == project), None)
    if entry is None:
        known = ", ".join(sorted(str(item.get("name")) for item in listed)) or "none"
        return failed(f"no project or container named '{project}' on this host (it has: {known})")
    if entry.get("kind") == "host":
        return _capture_host(site, Path(root), site_name, archiver, say, now, started, failed)

    settings = project_settings(site, project)
    exclude = [str(x) for x in settings.get("exclude") or [] if str(x)]
    mode = mode_for(settings)
    marker = held_marker(Path(root), project)
    leftover_warning = ""
    if marker.is_file():
        # A capture died with this project's containers paused or stopped.
        try:
            held = json.loads(marker.read_text())
            problem = _resume(run, str(held.get("how")), list(held.get("containers") or []))
        except (OSError, ValueError) as exc:
            problem = str(exc)
        leftover_warning = ("an earlier capture left containers held; "
                            + (f"resuming them failed: {problem}" if problem else "they were resumed"))
        if not problem:
            marker.unlink(missing_ok=True)
    keep = keep if keep and keep > 0 else keep_for(site, project)

    slug = safe_slug(project, fallback="project")
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    run_name = f"{stamp}-{os.urandom(4).hex()}"
    repository = Path(root) / "repository"
    vm_dir = repository / slug
    staging = Path(root) / "staging" / slug / run_name
    run_dir = vm_dir / run_name
    try:
        staging.mkdir(parents=True)
        vm_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return failed(f"cannot write under {root}: {exc}")

    disks: List[Dict[str, Any]] = []
    warnings: List[str] = [leftover_warning] if leftover_warning else []
    consistency = CONSISTENCY
    dumps_done: List[Dict[str, Any]] = []
    dump_problems: List[str] = []
    held_record: Dict[str, Any] = {}
    excluded: List[Dict[str, str]] = []
    images_out: List[Dict[str, Any]] = []

    def add(key: str, name: str, **extra) -> None:
        path = staging / name
        sha = file_digest(path)
        disks.append({"key": key, "format": "tar.zst", "image": name, "sha256": sha,
                      "bytes": path.stat().st_size, **extra})

    try:
        # 1. The definition: what it takes to recreate the project around its data.
        containers = _docker_json(run, "inspect", *[c["name"] for c in entry["containers"]])
        volume_names = sorted({m["source"] for m in entry["mounts"] if m["type"] == "volume"})
        volumes = _docker_json(run, "volume", "inspect", *volume_names) if volume_names else []
        image_ids = sorted({c["image_id"] for c in entry["containers"] if c.get("image_id")})
        images = _found_images(run, image_ids)

        tree = staging / "definition"
        for sub in ("containers", "volumes", "images", "compose"):
            (tree / sub).mkdir(parents=True)
        for item in containers or []:
            name = safe_slug(str(item.get("Name") or "").lstrip("/"), fallback="container")
            (tree / "containers" / f"{name}.json").write_text(json.dumps(item, indent=2))
        for item in volumes or []:
            name = safe_slug(str(item.get("Name") or ""), fallback="volume")
            (tree / "volumes" / f"{name}.json").write_text(json.dumps(item, indent=2))
        for item in images or []:
            name = safe_slug(str(item.get("Id") or "").replace("sha256:", "")[:24], fallback="image")
            (tree / "images" / f"{name}.json").write_text(json.dumps(item, indent=2))
        compose_files: List[str] = []
        wanted = list(entry.get("config_files") or [])
        if entry.get("working_dir"):
            wanted.append(str(Path(entry["working_dir"]) / ".env"))
        for index, original in enumerate(wanted):
            source = Path(original)
            if not source.is_absolute() and entry.get("working_dir"):
                # docker-compose v1 labels the files by bare name, relative to
                # the project's directory.
                source = Path(entry["working_dir"]) / source
                original = str(source)
            if not source.is_file():
                if not original.endswith("/.env"):
                    warnings.append(f"compose file {original} is not readable on the host")
                continue
            target = tree / "compose" / source.name
            if target.exists():  # two files of one name from different directories
                target = target.with_name(f"{index}-{source.name}")
            shutil.copy2(source, target)
            compose_files.append(original)
        (tree / "project.json").write_text(json.dumps(entry, indent=2))
        code, err = archiver(_tar_argv(tree), staging / DEFINITION)
        if code not in (0, TAR_FILES_CHANGED):
            raise RuntimeError(f"the definition could not be archived: {err[-300:]}")
        shutil.rmtree(tree)
        add("definition", DEFINITION)
        say(f"{project}: definition of {len(containers or [])} container(s) archived")

        # 2. Consistency, then the data: one archive per mount.
        running = [c for c in entry["containers"] if c.get("state") == "running"]
        if mode == "dump":
            for spec in settings.get("dumps") or []:
                if not isinstance(spec, dict):
                    continue
                want = str(spec.get("service") or spec.get("container") or "")
                container = next((c for c in running if want in (c.get("service"), c.get("name"))), None)
                if container is None:
                    dump_problems.append(f"dump of '{want}': no running container")
                    continue
                if not spec.get("command"):
                    dump_problems.append(f"dump of '{want}': no command is configured")
                    continue
                file = safe_slug(str(spec.get("file") or f"{want}.dump"), fallback="dump")
                name = f"dump-{file}.zst"
                argv = ["docker", "exec", *(["-u", str(spec["user"])] if spec.get("user") else []),
                        container["name"], "sh", "-c", str(spec["command"])]
                began = time.time()
                code, err = archiver(argv, staging / name)
                if code != 0:
                    dump_problems.append(f"dump of '{want}' failed (exit {code}): {err[-200:]}")
                    (staging / name).unlink(missing_ok=True)
                    continue
                add(f"dump:{want}", name, container=container["name"],
                    service=container.get("service"))
                dumps_done.append({"service": container.get("service"),
                                   "container": container["name"], "file": name})
                say(f"{project}: dumped {want}, {disks[-1]['bytes'] / 2**20:.1f} MiB "
                    f"compressed, in {time.time() - began:.0f}s")
            if not settings.get("dumps"):
                dump_problems.append("mode is dump, but no dumps are configured")
            if dumps_done and not dump_problems:
                consistency = "dump"
            warnings.extend(dump_problems)
        held_names: List[str] = []
        how = ""
        if mode in ("pause", "stop") and running:
            how = "paused" if mode == "pause" else "stopped"
            held_names = [c["name"] for c in running]
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps({"how": how, "containers": held_names}))
            done = (_docker(run, "pause", *held_names) if mode == "pause"
                    else _docker(run, "stop", "-t", "60", *held_names, timeout=600))
            if done.returncode != 0:
                if not _resume(run, how, held_names):
                    marker.unlink(missing_ok=True)
                raise RuntimeError(f"could not {mode} {', '.join(held_names)}: "
                                   f"{(done.stderr or '').strip()[-200:]}")
            say(f"{project}: {how} {', '.join(held_names)}")
        held_since = time.time()

        def archive_mounts() -> None:
            wanted_mounts = [m for m in entry["mounts"]
                             if not m.get("plumbing") and not _excluded(m, exclude)]
            for mount in entry["mounts"]:
                if mount.get("plumbing"):
                    continue
                label = mount["source"] or mount["path"]
                if _excluded(mount, exclude):
                    excluded.append({"type": mount["type"], "source": mount["source"],
                                     "reason": "excluded by site.yaml"})
                    say(f"{project}: {label} excluded by the site configuration")
                    continue
                outer = _covered_by(mount, wanted_mounts)
                if outer:
                    # Already inside another mount's archive: storing it twice would
                    # double the bytes and give a restore two copies to disagree.
                    excluded.append({"type": mount["type"], "source": mount["source"],
                                     "reason": f"inside {outer}, archived there"})
                    continue
                path = Path(mount["path"])
                if not mount["path"] or not path.exists():
                    warnings.append(f"{mount['type']} {label}: {mount['path'] or 'no path'} is not on the host")
                    continue
                prefix = "volume" if mount["type"] == "volume" else "bind"
                name = f"{prefix}-{safe_slug(label.strip('/').replace('/', '_'), fallback='mount')}{ARCHIVE_SUFFIX}"
                taken = {d["image"] for d in disks}
                if name in taken:
                    name = name.replace(ARCHIVE_SUFFIX, f"-{len(disks)}{ARCHIVE_SUFFIX}")
                began = time.time()
                code, err = archiver(_tar_argv(path), staging / name)
                if code == TAR_FILES_CHANGED:
                    warnings.append(f"{label}: files changed while they were read (crash-consistent)")
                elif code != 0:
                    raise RuntimeError(f"{label} could not be archived (tar exit {code}): {err[-300:]}")
                add(f"{prefix}:{label}", name, type=mount["type"], source=mount["source"],
                    path=mount["path"], destination=mount["destination"],
                    read_only=bool(mount.get("read_only")))
                say(f"{project}: {label} archived, {disks[-1]['bytes'] / 2**20:.1f} MiB "
                    f"in {time.time() - began:.0f}s")


        try:
            archive_mounts()
        finally:
            if held_names:
                problem = _resume(run, how, held_names)
                held_record = {"how": how, "containers": held_names,
                               "seconds": round(time.time() - held_since, 1)}
                if problem:
                    warnings.append(problem)  # the marker stays: the next capture resumes them
                else:
                    marker.unlink(missing_ok=True)
                    consistency = mode
                    say(f"{project}: resumed after {held_record['seconds']:.0f}s")

        # 3. Images: by registry digest, or saved when no registry has them.
        held = _saved_images(vm_dir)
        by_id = {str(item.get("Id")): item for item in images or []}
        for image_id in image_ids:
            info = by_id.get(image_id) or {}
            refs = [c["image"] for c in entry["containers"] if c.get("image_id") == image_id]
            tags = list(info.get("RepoTags") or [])
            digests = list(info.get("RepoDigests") or [])
            record = {"id": image_id, "refs": sorted(set(refs)), "tags": tags,
                      "repo_digests": digests}
            if not info:
                record["kept"] = "not kept: the daemon could not describe it"
                warnings.append(f"image {', '.join(refs) or image_id} could not be inspected")
                images_out.append(record)
                continue
            if digests:
                record["kept"] = "digest"
                images_out.append(record)
                continue
            ref = next((r for r in refs if r in tags), tags[0] if tags else image_id)
            name = f"image-{safe_slug(ref.replace('/', '_').replace(':', '_'), fallback='image')}{ARCHIVE_SUFFIX}"
            if image_id in held:
                os.link(held[image_id], staging / name)
                record["kept"] = "saved, unchanged since an earlier run (hard link)"
            else:
                code, err = archiver(["docker", "save", ref], staging / name)
                if code != 0:
                    raise RuntimeError(f"docker save {ref} failed: {err[-300:]}")
                record["kept"] = "saved"
            record["archive"] = name
            add(f"image:{ref}", name, image_id=image_id)
            images_out.append(record)
            say(f"{project}: image {ref} is in no registry; {record['kept']}")

        manifest = {
            "version": "0.1.1",
            "site": site_name,
            "hypervisor": "docker",
            "vm": project,
            "vm_id": project,
            "kind": entry.get("kind"),
            "created_utc": (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "backup_type": "full",
            "source": "docker",
            "consistency": consistency,
            "mode": mode,
            "dumps": dumps_done,
            "held": held_record,
            "working_dir": entry.get("working_dir", ""),
            "compose_files": compose_files,
            "containers": [{k: c.get(k) for k in ("name", "service", "image", "image_id",
                                                     "state", "health")}
                           for c in entry["containers"]],
            "images": images_out,
            "excluded": excluded,
            "warnings": warnings,
            "sha256": combined_digest(disks),
            "hash_kind": DIGEST_KIND_FILE,
            "disks": disks,
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        # The whole run appears at once: a reader sees no run, or a finished one.
        os.replace(staging, run_dir)
    except Exception as exc:  # noqa: BLE001 - a failed capture is a job result
        shutil.rmtree(staging, ignore_errors=True)
        return failed(f"{type(exc).__name__}: {exc}", project=project)
    finally:
        try:
            staging.parent.rmdir()
        except OSError:
            pass

    pruned = []
    for doomed in chains.prune_plan(chains.load(vm_dir), keep):
        shutil.rmtree(doomed.run, ignore_errors=True)
        pruned.append(Path(doomed.run).name)
    if pruned:
        say(f"{project}: kept the newest {keep} run(s), removed {', '.join(pruned)}")

    stored = sum(d["bytes"] for d in disks)
    seconds = round(time.time() - started, 1)
    data_archives = sum(1 for d in disks if d["key"].startswith(("volume:", "bind:")))
    described = {"crash": "crash-consistent", "dump": "databases dumped",
                 "pause": "paused while read", "stop": "stopped while read"}[consistency]
    if dump_problems:
        # The run is kept — its data is as good as a crash-consistent copy — but the
        # job fails, so the schedule and monitoring say a dump is missing.
        return {
            "state": "failed",
            "evidence": f"captured {project} into {run_name}, but a dump failed: kept crash-consistent",
            "detail": "; ".join(dump_problems),
            "metrics": {"run": run_name, "bytes": sum(d["bytes"] for d in disks),
                        "consistency": consistency, "status": "dump-failed"},
        }
    return {
        "state": "success",
        "evidence": (f"captured {project} into {run_name}: {data_archives} data archive(s), "
                     f"{stored / 2**30:.2f} GiB, {described}"
                     + (f"; {len(warnings)} warning(s)" if warnings else "")),
        "detail": "; ".join(warnings) if warnings else f"{len(disks)} file(s) digested in {seconds}s",
        "metrics": {"run": run_name, "bytes": stored, "files": len(disks),
                    "seconds": seconds, "sha256": manifest["sha256"],
                    "consistency": consistency, "excluded": len(excluded),
                    "warnings": warnings, "pruned": pruned},
    }


def _capture_host(site, root: Path, site_name: str, archiver: Archiver,
                  say: Callable[[str], None], now: Optional[datetime], started: float,
                  failed: Callable[..., Dict[str, Any]]) -> Dict[str, Any]:
    """The host's own files a rebuild needs, as one archive in a run of its own.

    The paths come from ``docker.host.paths`` (globs expanded here); one that
    matches nothing is a warning, not a failure — a rebuild kit with one file
    missing is still worth having, and the warning says which.
    """
    import glob  # noqa: PLC0415

    wanted = host_paths(site)
    found: List[str] = []
    warnings: List[str] = []
    for pattern in wanted:
        matches = sorted(glob.glob(pattern))
        if not matches:
            warnings.append(f"{pattern}: nothing there")
        found.extend(m for m in matches if m not in found)
    if not found:
        return failed("none of docker.host.paths exists on this host")
    stamp = (now or datetime.now(timezone.utc))
    run_name = f"{stamp.strftime('%Y%m%dT%H%M%SZ')}-{os.urandom(4).hex()}"
    vm_dir = root / "repository" / HOST_FILES
    staging = root / "staging" / HOST_FILES / run_name
    run_dir = vm_dir / run_name
    name = "host-files" + ARCHIVE_SUFFIX
    try:
        staging.mkdir(parents=True)
        vm_dir.mkdir(parents=True, exist_ok=True)
        relative = [path.lstrip("/") for path in found]
        code, err = archiver([*TAR, "--directory", "/", *relative], staging / name)
        if code == TAR_FILES_CHANGED:
            warnings.append("some files changed while they were read")
        elif code != 0:
            raise RuntimeError(f"the host files could not be archived (tar exit {code}): {err[-300:]}")
        disks = [{"key": "host", "format": "tar.zst", "image": name,
                  "sha256": file_digest(staging / name), "bytes": (staging / name).stat().st_size}]
        manifest = {
            "version": "0.1.1", "site": site_name, "hypervisor": "docker",
            "vm": HOST_FILES, "vm_id": HOST_FILES, "kind": "host",
            "created_utc": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "backup_type": "full", "source": "docker", "consistency": "crash",
            "paths": wanted, "archived": found, "warnings": warnings,
            "sha256": combined_digest(disks), "hash_kind": DIGEST_KIND_FILE, "disks": disks,
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        os.replace(staging, run_dir)
    except Exception as exc:  # noqa: BLE001 - a failed capture is a job result
        shutil.rmtree(staging, ignore_errors=True)
        return failed(f"{type(exc).__name__}: {exc}", project=HOST_FILES)
    finally:
        try:
            staging.parent.rmdir()
        except OSError:
            pass
    keep = keep_for(site, HOST_FILES)
    pruned = []
    for doomed in chains.prune_plan(chains.load(vm_dir), keep):
        shutil.rmtree(doomed.run, ignore_errors=True)
        pruned.append(Path(doomed.run).name)
    size = disks[0]["bytes"]
    say(f"{HOST_FILES}: {len(found)} path(s) archived, {size / 2**20:.1f} MiB")
    return {
        "state": "success",
        "evidence": (f"captured {HOST_FILES} into {run_name}: {len(found)} path(s), "
                     f"{size / 2**20:.1f} MiB" + (f"; {len(warnings)} warning(s)" if warnings else "")),
        "detail": "; ".join(warnings) if warnings else f"{round(time.time() - started, 1)}s",
        "metrics": {"run": run_name, "bytes": size, "files": 1, "paths": len(found),
                    "consistency": "crash", "warnings": warnings, "pruned": pruned},
    }


__all__ = ["CONSISTENCY", "HOST_FILES", "host_paths", "DEFAULT_KEEP", "MODES", "capture_docker", "combined_digest", "held_marker",
           "mode_for",
           "keep_for", "pipe_to_zstd", "project_settings"]
